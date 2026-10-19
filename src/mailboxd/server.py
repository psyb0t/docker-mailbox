"""FastAPI HTTP layer.

All routes except GET /health require `Authorization: Bearer <token>`; the token resolves to a
`Principal` (scopes + mailbox allow-list) and every route delegates to `services.Service`.

URL shape (scope in brackets):
  GET    /health                                      (no auth)
  GET    /health/deep                                 [admin]
  GET    /mailboxes                                   visible mailboxes + identities
  GET    /inbox                                       [read] unified across IMAP mailboxes
         ?mailbox=name1,addr@x &folder= &limit= &label= &checkpoint= &since_cursor=
         &from=&to=&recipient=&subject=&body=&text=&since=&before=
         &unseen=&seen=&flagged=&answered=&larger_than=&smaller_than=
  GET    /mailboxes/{name}/folders                    [read]
  POST   /mailboxes/{name}/folders                    [organize]  {"name"}
  GET    /mailboxes/{name}/messages?folder&limit&search&cursor        [read]
  GET    /mailboxes/{name}/search?...&has_attachments&label&cursor&since_cursor&checkpoint
  GET    /mailboxes/{name}/messages/{uid}?folder&reader&headers&safe_html     [read]
  GET    /mailboxes/{name}/messages/{uid}/raw         [read] message/rfc822 download
  GET    /mailboxes/{name}/messages/{uid}/attachments/{part_id}  [read] octet-stream download
  GET    /mailboxes/{name}/messages/{uid}/thread      [read]
  GET    /mailboxes/{name}/threads?folder&limit       [read]
  DELETE /mailboxes/{name}/messages/{uid}?folder&permanent=false  [organize / delete]
  POST   /mailboxes/{name}/messages/delete            [organize / delete] {uids, permanent}
  POST   /mailboxes/{name}/messages/{uid}/seen        [organize]  {"seen": bool}
  POST   /mailboxes/{name}/messages/seen              [organize]  {uids, seen}
  POST   /mailboxes/{name}/messages/labels            [organize]  LabelsRequest (?folder)
  POST   /mailboxes/{name}/messages/move              [organize]  MoveRequest (?folder)
  POST   /mailboxes/{name}/messages/{uid}/reply       [send, or draft] ReplyRequest (?folder)
  POST   /mailboxes/{name}/send                       [send]  ComposeRequest
  GET    /mailboxes/{name}/drafts                     [read]
  POST   /mailboxes/{name}/drafts                     [draft]
  PUT    /mailboxes/{name}/drafts/{uid}               [draft]
  DELETE /mailboxes/{name}/drafts/{uid}               [draft]
  POST   /mailboxes/{name}/drafts/{uid}/send          [send]  {"dry_run"}
  GET    /mailboxes/{name}/identities                 [read]
  GET    /mailboxes/{name}/stats                      [read]
  GET    /audit?since&action&token&mailbox&limit      [admin]
  /mcp                                                MCP streamable HTTP (same bearer auth)

Errors: {"error": {"code", "message", "details"?}, "detail": message}; 429 adds Retry-After.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Config, ConfigError, load_config
from .errors import MailboxdError, RateLimited
from .imap_client import ImapError
from .mcp_server import build_session_manager, mcp_lifespan
from .mime import safe_filename
from .principal import SCOPE_KEY, Principal, authenticate, from_scope
from .schemas import (
    ComposeRequest,
    DraftUpdate,
    FolderCreate,
    HealthResponse,
    LabelsRequest,
    MoveRequest,
    ReplyRequest,
    SeenRequest,
    SendDraftRequest,
)
from .services import Service
from .smtp_client import SmtpError

log = logging.getLogger("mailboxd")


def _load_or_die() -> Config:
    try:
        return load_config()
    except ConfigError as e:
        log.error("config load failed: %s", e)
        raise


_AUTH_EXEMPT_PATHS = frozenset({"/health"})
_MCP_PATH = "/mcp"
_MCP_PATH_WITH_TRAILING_SLASH = f"{_MCP_PATH}/"
_MCP_RAW_PATH = _MCP_PATH.encode("ascii")
_MCP_RAW_PATH_WITH_TRAILING_SLASH = _MCP_PATH_WITH_TRAILING_SLASH.encode("ascii")


def _bearer_token(scope: Scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            auth = v.decode("latin-1")
            if not auth.startswith("Bearer "):
                return None
            return auth[7:].strip() or None
    return None


async def _send_401(send: Send) -> None:
    body = b'{"detail":"missing or invalid Bearer token"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _MCPSlashNormalizer:
    """Route the documented MCP path to the mounted Streamable HTTP handler."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _MCP_PATH:
            await self._app(scope, receive, send)
            return

        normalized_scope = dict(scope)
        normalized_scope["path"] = _MCP_PATH_WITH_TRAILING_SLASH
        if scope.get("raw_path") == _MCP_RAW_PATH:
            normalized_scope["raw_path"] = _MCP_RAW_PATH_WITH_TRAILING_SLASH
        await self._app(normalized_scope, receive, send)


class _BearerASGI:
    """Pure-ASGI bearer-auth wrapper around the whole app.

    Pure ASGI (not BaseHTTPMiddleware) so it doesn't buffer streaming
    responses — required for the MCP streamable-HTTP transport mounted
    under /mcp to keep working.
    """

    def __init__(self, app: ASGIApp, cfg: Config) -> None:
        self._app = app
        self._cfg = cfg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _AUTH_EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return
        presented = _bearer_token(scope)
        principal = authenticate(self._cfg, presented) if presented else None
        if principal is None:
            await _send_401(send)
            return
        scope[SCOPE_KEY] = principal
        await self._app(scope, receive, send)


# ── request models local to the HTTP layer ──────────────────────────────────


class DeleteRequest(BaseModel):
    uids: list[str] = Field(..., min_length=1)
    permanent: bool = False


def _error_body(err: MailboxdError) -> dict[str, Any]:
    return {"error": err.to_dict(), "detail": err.message}


def _upstream_error(code: str, exc: Exception) -> JSONResponse:
    message = str(exc)
    return JSONResponse(
        status_code=502,
        content={"error": {"code": code, "message": message}, "detail": message},
    )


def principal_dependency(request: Request) -> Principal:
    return from_scope(request.scope)


P = Depends(principal_dependency)


def _criteria(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    recipient: str | None = Query(None, description="To or Cc contains"),
    subject: str | None = Query(None),
    body: str | None = Query(None),
    text: str | None = Query(None),
    since: str | None = Query(None, description="ISO date or IMAP date e.g. 1-Jan-2026"),
    before: str | None = Query(None),
    unseen: bool | None = Query(None),
    seen: bool | None = Query(None),
    flagged: bool | None = Query(None),
    answered: bool | None = Query(None),
    larger_than: int | None = Query(None, ge=0),
    smaller_than: int | None = Query(None, ge=0),
    keyword: str | None = Query(None),
    unkeyword: str | None = Query(None),
    message_id: str | None = Query(None),
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "from_": from_,
        "to": to,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "text": text,
        "since": since,
        "before": before,
        "unseen": unseen,
        "seen": seen,
        "flagged": flagged,
        "answered": answered,
        "larger_than": larger_than,
        "smaller_than": smaller_than,
        "keyword": keyword,
        "unkeyword": unkeyword,
        "message_id": message_id,
    }
    return {k: v for k, v in raw.items() if v is not None}


Criteria = Depends(_criteria)


def _content_disposition(filename: str) -> str:
    name = safe_filename(filename) or "attachment"
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or _load_or_die()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    service = Service(cfg)
    mcp_manager = build_session_manager(cfg, service)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_lifespan(mcp_manager):
            yield

    app = FastAPI(
        title="mailboxd",
        version=__version__,
        description="Multi-mailbox IMAP/SMTP control plane.",
        lifespan=_lifespan,
    )
    app.state.service = service

    app.router.routes.append(Mount(_MCP_PATH, app=mcp_manager.handle_request))

    app.add_middleware(_MCPSlashNormalizer)
    app.add_middleware(_BearerASGI, cfg=cfg)

    # ── errors ──────────────────────────────────────────────────────────────

    @app.exception_handler(MailboxdError)
    async def _on_mailboxd_error(_request: Request, exc: MailboxdError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after)}
        return JSONResponse(status_code=exc.status, content=_error_body(exc), headers=headers)

    @app.exception_handler(ImapError)
    async def _on_imap_error(_request: Request, exc: ImapError) -> JSONResponse:
        return _upstream_error("imap_error", exc)

    @app.exception_handler(SmtpError)
    async def _on_smtp_error(_request: Request, exc: SmtpError) -> JSONResponse:
        return _upstream_error("smtp_error", exc)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "invalid request",
                    "details": {"errors": errors},
                },
                "detail": errors,
            },
        )

    # ── health / discovery ──────────────────────────────────────────────────

    @app.get("/health", response_model=HealthResponse)
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/health/deep")
    def health_deep(p: Principal = P) -> dict[str, Any]:
        return service.health_deep(p)

    @app.get("/mailboxes")
    def list_mailboxes(p: Principal = P) -> dict[str, Any]:
        return service.mailboxes(p)

    @app.get("/audit")
    def audit(
        since: str | None = Query(None),
        action: str | None = Query(None),
        token: str | None = Query(None),
        mailbox: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.audit_log(
            p, since=since, action=action, token=token, mailbox=mailbox, limit=limit
        )

    # ── reading ─────────────────────────────────────────────────────────────

    @app.get("/inbox")
    def inbox(
        mailbox: str | None = Query(None, description="CSV of mailbox names or addresses"),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        label: str | None = Query(None),
        checkpoint: str | None = Query(None),
        since_cursor: str | None = Query(None),
        criteria: dict[str, Any] = Criteria,
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.inbox(
            p,
            mailbox=mailbox,
            folder=folder,
            limit=limit,
            checkpoint=checkpoint,
            since_cursor=since_cursor,
            label=label,
            **criteria,
        )

    @app.get("/mailboxes/{name}/folders")
    def folders(name: str = Path(...), p: Principal = P) -> dict[str, Any]:
        return service.folders(p, name)

    @app.post("/mailboxes/{name}/folders")
    def create_folder(
        name: str = Path(...), req: FolderCreate = Body(...), p: Principal = P
    ) -> dict[str, Any]:
        return service.create_folder(p, name, req.name)

    @app.get("/mailboxes/{name}/messages")
    def list_msgs(
        name: str = Path(...),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        search: str = Query("ALL"),
        cursor: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.list_messages(
            p, name, folder=folder, limit=limit, search=search, cursor=cursor
        )

    @app.get("/mailboxes/{name}/search")
    def search_msgs(
        name: str = Path(...),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        cursor: str | None = Query(None),
        since_cursor: str | None = Query(None),
        checkpoint: str | None = Query(None),
        has_attachments: bool | None = Query(None),
        label: str | None = Query(None),
        criteria: dict[str, Any] = Criteria,
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.search(
            p,
            name,
            folder=folder,
            limit=limit,
            cursor=cursor,
            since_cursor=since_cursor,
            checkpoint=checkpoint,
            has_attachments=has_attachments,
            label=label,
            **criteria,
        )

    @app.get("/mailboxes/{name}/messages/{uid}")
    def get_msg(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        reader: bool = Query(
            False, description="Add `body_reader`: readable text/markdown from HTML"
        ),
        headers: bool = Query(False, description="Include all headers"),
        safe_html: bool = Query(False, description="Include sanitized `body_html_safe`"),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.get_message(
            p, name, uid, folder=folder, reader=reader, headers=headers, safe_html=safe_html
        )

    @app.get("/mailboxes/{name}/messages/{uid}/raw")
    def get_raw(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> Response:
        data = service.get_raw(p, name, uid, folder=folder)
        return Response(
            content=data,
            media_type="message/rfc822",
            headers={
                "Content-Disposition": _content_disposition(f"message-{uid}.eml"),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/mailboxes/{name}/messages/{uid}/attachments/{part_id}")
    def get_attachment(
        name: str = Path(...),
        uid: str = Path(...),
        part_id: str = Path(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> Response:
        info, data = service.get_attachment(p, name, uid, part_id, folder=folder)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _content_disposition(str(info.get("filename") or "")),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/mailboxes/{name}/messages/{uid}/thread")
    def get_thread(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.thread(p, name, uid, folder=folder)

    @app.get("/mailboxes/{name}/threads")
    def list_threads(
        name: str = Path(...),
        folder: str | None = Query(None),
        limit: int = Query(20, ge=1, le=200),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.threads(p, name, folder=folder, limit=limit)

    @app.get("/mailboxes/{name}/identities")
    def identities(name: str = Path(...), p: Principal = P) -> dict[str, Any]:
        return service.identities(p, name)

    @app.get("/mailboxes/{name}/stats")
    def stats(name: str = Path(...), p: Principal = P) -> dict[str, Any]:
        return service.stats(p, name)

    # ── organizing ──────────────────────────────────────────────────────────

    @app.delete("/mailboxes/{name}/messages/{uid}")
    def del_msg(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        permanent: bool = Query(False),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.delete(p, name, [uid], folder=folder, permanent=permanent)

    @app.post("/mailboxes/{name}/messages/delete")
    def del_msgs(
        name: str = Path(...),
        req: DeleteRequest = Body(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.delete(p, name, req.uids, folder=folder, permanent=req.permanent)

    @app.post("/mailboxes/{name}/messages/seen")
    def set_seen_batch(
        name: str = Path(...),
        req: SeenRequest = Body(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.mark_seen(p, name, req.uids or [], folder=folder, seen=req.seen)

    @app.post("/mailboxes/{name}/messages/labels")
    def set_labels(
        name: str = Path(...),
        req: LabelsRequest = Body(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.set_labels(p, name, req.uids, folder=folder, add=req.add, remove=req.remove)

    @app.post("/mailboxes/{name}/messages/move")
    def move_msgs(
        name: str = Path(...),
        req: MoveRequest = Body(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.move(p, name, req.uids, req.to_folder, folder=folder)

    @app.post("/mailboxes/{name}/messages/{uid}/seen")
    def set_seen(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        body: SeenRequest = Body(default_factory=SeenRequest),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.mark_seen(p, name, [uid], folder=folder, seen=body.seen)

    # ── sending / drafts ────────────────────────────────────────────────────

    @app.post("/mailboxes/{name}/send")
    def send(
        name: str = Path(...), req: ComposeRequest = Body(...), p: Principal = P
    ) -> dict[str, Any]:
        return service.send(p, name, req)

    @app.post("/mailboxes/{name}/messages/{uid}/reply")
    def reply(
        name: str = Path(...),
        uid: str = Path(...),
        req: ReplyRequest = Body(...),
        folder: str | None = Query(None),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.reply(p, name, uid, req, folder=folder)

    @app.get("/mailboxes/{name}/drafts")
    def list_drafts(
        name: str = Path(...),
        limit: int = Query(50, ge=1, le=500),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.list_drafts(p, name, limit=limit)

    @app.post("/mailboxes/{name}/drafts")
    def create_draft(
        name: str = Path(...), req: ComposeRequest = Body(...), p: Principal = P
    ) -> dict[str, Any]:
        return service.create_draft(p, name, req)

    @app.put("/mailboxes/{name}/drafts/{uid}")
    def update_draft(
        name: str = Path(...),
        uid: str = Path(...),
        req: DraftUpdate = Body(...),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.update_draft(p, name, uid, req)

    @app.delete("/mailboxes/{name}/drafts/{uid}")
    def delete_draft(
        name: str = Path(...), uid: str = Path(...), p: Principal = P
    ) -> dict[str, Any]:
        return service.delete_draft(p, name, uid)

    @app.post("/mailboxes/{name}/drafts/{uid}/send")
    def send_draft(
        name: str = Path(...),
        uid: str = Path(...),
        body: SendDraftRequest = Body(default_factory=SendDraftRequest),
        p: Principal = P,
    ) -> dict[str, Any]:
        return service.send_draft(p, name, uid, dry_run=body.dry_run)

    return app
