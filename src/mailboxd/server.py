"""FastAPI HTTP layer.

URL shape:
  GET    /health
  GET    /mailboxes
  GET    /inbox                                       unified across all IMAP mailboxes
         ?mailbox=name1,addr@x  ← optional filter (name or email address)
         &from=&to=&subject=&body=&text=
         &since=&before=&unseen=&seen=&flagged=&answered=
         &larger_than=&smaller_than=&folder=&limit=
  GET    /mailboxes/{name}/folders                    (IMAP)
  GET    /mailboxes/{name}/messages?folder=&limit=&search=
  GET    /mailboxes/{name}/search?from=&subject=&...  structured single-mailbox
  GET    /mailboxes/{name}/messages/{uid}?folder=
  DELETE /mailboxes/{name}/messages/{uid}?folder=
  POST   /mailboxes/{name}/messages/{uid}/seen        body: {"seen": bool}
  POST   /mailboxes/{name}/send                       (SMTP)
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Config, ConfigError, MailboxConfig, load_config
from .imap_client import ImapError
from .imap_client import delete_message as imap_delete
from .imap_client import fetch_message as imap_fetch
from .imap_client import list_folders as imap_list_folders
from .imap_client import list_messages as imap_list_messages
from .imap_client import mark_seen as imap_mark_seen
from .imap_client import search_messages as imap_search_messages
from .imap_client import unified_search as imap_unified_search
from .mcp_server import build_session_manager, mcp_lifespan
from .schemas import (
    FoldersResponse,
    GenericOK,
    HealthResponse,
    MailboxList,
    MailboxSummary,
    SendRequest,
)
from .smtp_client import SmtpError
from .smtp_client import send as smtp_send

log = logging.getLogger("mailboxd")


def _load_or_die() -> Config:
    try:
        return load_config()
    except ConfigError as e:
        log.error("config load failed: %s", e)
        raise


def _resolve(cfg: Config, name: str) -> MailboxConfig:
    m = cfg.get(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown mailbox: {name!r}")
    return m


def _need(mb: MailboxConfig, proto: str) -> None:
    if getattr(mb, proto) is None:
        raise HTTPException(
            status_code=409,
            detail=f"mailbox {mb.name!r} has no {proto} configured",
        )


_AUTH_EXEMPT_PATHS = frozenset({"/health"})


def _bearer_ok(cfg: Config, scope: Scope) -> bool:
    """Constant-time bearer check against scope headers. True if disabled or valid."""
    if not cfg.auth.tokens:
        return True
    import hmac

    for k, v in scope.get("headers", []):
        if k == b"authorization":
            auth = v.decode("latin-1")
            if not auth.startswith("Bearer "):
                return False
            presented = auth[7:].strip()
            if not presented:
                return False
            return any(hmac.compare_digest(t, presented) for t in cfg.auth.tokens)
    return False


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
        if not _bearer_ok(self._cfg, scope):
            await _send_401(send)
            return
        await self._app(scope, receive, send)


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or _load_or_die()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    mcp_manager = build_session_manager(cfg)

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

    app.router.routes.append(Mount("/mcp", app=mcp_manager.handle_request))

    app.add_middleware(_BearerASGI, cfg=cfg)

    @app.get("/health", response_model=HealthResponse)
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/mailboxes", response_model=MailboxList)
    def list_mailboxes() -> MailboxList:
        return MailboxList(
            mailboxes=[
                MailboxSummary(
                    name=m.name,
                    description=m.description,
                    imap=m.imap is not None,
                    smtp=m.smtp is not None,
                )
                for m in cfg.mailboxes
            ]
        )

    # ── Unified inbox (default view) ────────────────────────────────────────

    def _select_mailboxes(filter_csv: str | None) -> list[MailboxConfig]:
        if not filter_csv:
            return [m for m in cfg.mailboxes if m.imap is not None]
        wanted = {s.strip() for s in filter_csv.split(",") if s.strip()}
        out: list[MailboxConfig] = []
        for m in cfg.mailboxes:
            if m.imap is None:
                continue
            if m.name in wanted or (m.imap.username in wanted):
                out.append(m)
        if not out:
            raise HTTPException(
                status_code=404,
                detail=f"no IMAP-enabled mailboxes matched filter: {filter_csv!r}",
            )
        return out

    @app.get("/inbox")
    def inbox(
        mailbox: str | None = Query(None, description="CSV of mailbox names or addresses"),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        subject: str | None = Query(None),
        body: str | None = Query(None),
        text: str | None = Query(None),
        since: str | None = Query(None, description="IMAP date e.g. 1-Jan-2026"),
        before: str | None = Query(None),
        unseen: bool | None = Query(None),
        seen: bool | None = Query(None),
        flagged: bool | None = Query(None),
        answered: bool | None = Query(None),
        larger_than: int | None = Query(None, ge=0),
        smaller_than: int | None = Query(None, ge=0),
    ) -> dict[str, Any]:
        targets = _select_mailboxes(mailbox)
        return imap_unified_search(
            targets,
            folder=folder,
            limit=limit,
            from_=from_,
            to=to,
            subject=subject,
            body=body,
            text=text,
            since=since,
            before=before,
            unseen=unseen,
            seen=seen,
            flagged=flagged,
            answered=answered,
            larger_than=larger_than,
            smaller_than=smaller_than,
        )

    # ── IMAP ────────────────────────────────────────────────────────────────

    @app.get("/mailboxes/{name}/folders", response_model=FoldersResponse)
    def folders(name: str = Path(...)) -> FoldersResponse:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            return FoldersResponse(folders=imap_list_folders(mb.imap))  # type: ignore[arg-type]
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/mailboxes/{name}/messages")
    def list_msgs(
        name: str = Path(...),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        search: str = Query("ALL"),
    ) -> dict[str, Any]:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            msgs = imap_list_messages(
                mb.imap,  # type: ignore[arg-type]
                folder=folder,
                limit=limit,
                search=search,
            )
            return {"messages": msgs}
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/mailboxes/{name}/search")
    def search_msgs(
        name: str = Path(...),
        folder: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        subject: str | None = Query(None),
        body: str | None = Query(None),
        text: str | None = Query(None),
        since: str | None = Query(None),
        before: str | None = Query(None),
        unseen: bool | None = Query(None),
        seen: bool | None = Query(None),
        flagged: bool | None = Query(None),
        answered: bool | None = Query(None),
        larger_than: int | None = Query(None, ge=0),
        smaller_than: int | None = Query(None, ge=0),
    ) -> dict[str, Any]:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            msgs = imap_search_messages(
                mb.imap,  # type: ignore[arg-type]
                folder=folder,
                limit=limit,
                from_=from_,
                to=to,
                subject=subject,
                body=body,
                text=text,
                since=since,
                before=before,
                unseen=unseen,
                seen=seen,
                flagged=flagged,
                answered=answered,
                larger_than=larger_than,
                smaller_than=smaller_than,
            )
            return {"messages": msgs}
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/mailboxes/{name}/messages/{uid}")
    def get_msg(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
    ) -> dict[str, Any]:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            return imap_fetch(mb.imap, uid, folder=folder)  # type: ignore[arg-type]
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.delete("/mailboxes/{name}/messages/{uid}", response_model=GenericOK)
    def del_msg(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
    ) -> GenericOK:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            imap_delete(mb.imap, uid, folder=folder)  # type: ignore[arg-type]
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return GenericOK()

    class _SeenBody(BaseModel):
        seen: bool = True

    @app.post("/mailboxes/{name}/messages/{uid}/seen", response_model=GenericOK)
    def set_seen(
        name: str = Path(...),
        uid: str = Path(...),
        folder: str | None = Query(None),
        body: _SeenBody = Body(default_factory=_SeenBody),
    ) -> GenericOK:
        mb = _resolve(cfg, name)
        _need(mb, "imap")
        try:
            imap_mark_seen(mb.imap, uid, folder=folder, seen=body.seen)  # type: ignore[arg-type]
        except ImapError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return GenericOK()

    # ── SMTP ────────────────────────────────────────────────────────────────

    @app.post("/mailboxes/{name}/send")
    def send(name: str = Path(...), req: SendRequest = Body(...)) -> dict[str, Any]:
        mb = _resolve(cfg, name)
        _need(mb, "smtp")
        try:
            return smtp_send(
                mb.smtp,  # type: ignore[arg-type]
                to=[str(x) for x in req.to],
                subject=req.subject,
                body_text=req.body_text,
                body_html=req.body_html,
                cc=[str(x) for x in (req.cc or [])],
                bcc=[str(x) for x in (req.bcc or [])],
                from_address=req.from_address,
                reply_to=str(req.reply_to) if req.reply_to else None,
            )
        except SmtpError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    return app
