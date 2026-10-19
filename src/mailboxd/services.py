"""The service layer: every mailbox operation, once, for HTTP and MCP.

Each `Service` method takes the calling `Principal` first, resolves the
mailbox (hidden mailboxes look unknown), checks the scope, runs the IMAP /
SMTP work and writes an audit row for every mutation (denials included).
Callers translate `MailboxdError` / `ImapError` / `SmtpError` into responses.
"""

from __future__ import annotations

import base64
import binascii
import email
import html as html_lib
import imaplib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, parseaddr
from typing import Any, Iterator

from . import compose, imap_client, mime, smtp_client
from .compose import OutgoingAttachment
from .config import Config, MailboxConfig, SmtpConfig
from .errors import (
    Conflict,
    InvalidRequest,
    MailboxdError,
    NotFound,
    PermissionDenied,
    TooLarge,
)
from .imap_client import ImapError, Session
from .principal import Principal, resolve_mailbox, visible_mailboxes
from .schemas import AttachmentIn, ComposeRequest, DraftUpdate, ReplyRequest
from .state import State

_IMAP_ERRORS = (ImapError, OSError, imaplib.IMAP4.error)
_KINDS = ("sent", "drafts", "trash", "junk")
_BUILTIN_LABELS = {"flagged": r"\Flagged"}
_DEFAULT_LABELS = {"suspect": "$Suspect", "a_traiter": "$ATraiter", "fait": "$Fait"}
_QUOTE_MAX_CHARS = 50_000
_UID_TOKEN = re.compile(r"^[0-9]+$")


# ── Cursors ──────────────────────────────────────────────────────────────


def encode_cursor(obj: Any) -> str:
    """Opaque URL-safe cursor (base64url JSON)."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> Any:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return json.loads(raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as e:
        raise InvalidRequest("invalid cursor") from e


def _page_cursor(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    obj = decode_cursor(value)
    if not isinstance(obj, dict) or not isinstance(obj.get("b"), int):
        raise InvalidRequest("invalid cursor")
    return str(obj.get("v", "")), int(obj["b"])


def _since_cursor(value: str | None) -> dict[str, tuple[str, int]]:
    if not value:
        return {}
    obj = decode_cursor(value)
    out: dict[str, tuple[str, int]] = {}
    if not isinstance(obj, dict):
        raise InvalidRequest("invalid since_cursor")
    for name, pos in obj.items():
        if not (isinstance(pos, list) and len(pos) == 2 and str(pos[1]).isdigit()):
            raise InvalidRequest("invalid since_cursor")
        out[str(name)] = (str(pos[0]), int(pos[1]))
    return out


# ── Labels ───────────────────────────────────────────────────────────────


def labels_for(flags: list[str], labels: dict[str, str] | None = None) -> list[str]:
    """Friendly label names for IMAP flags / keywords (e.g. `$Suspect` -> `suspect`)."""
    reverse = {kw.lower(): name for name, kw in (labels or _DEFAULT_LABELS).items()}
    reverse.update({kw.lower(): name for name, kw in _BUILTIN_LABELS.items()})
    out = [reverse[f.lower()] for f in flags if f.lower() in reverse]
    return sorted(set(out))


def _parse_when(value: float | int | str | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError as e:
        raise InvalidRequest(f"invalid `since`: {value!r}") from e


class Service:
    def __init__(self, cfg: Config, state: State | None = None) -> None:
        self.cfg = cfg
        self.state = state or State(cfg.state.path)

    # ── plumbing ─────────────────────────────────────────────────────────

    def _mb(self, p: Principal, ident: str) -> MailboxConfig:
        return resolve_mailbox(self.cfg, p, ident)

    @staticmethod
    def _imap(mb: MailboxConfig) -> Any:
        if mb.imap is None:
            raise Conflict(f"mailbox {mb.name!r} has no IMAP configured")
        return mb.imap

    @staticmethod
    def _smtp(mb: MailboxConfig) -> SmtpConfig:
        if mb.smtp is None:
            raise Conflict(f"mailbox {mb.name!r} has no SMTP configured")
        return mb.smtp

    def _session(self, mb: MailboxConfig) -> Any:
        return imap_client.session(self._imap(mb))

    def _log(
        self,
        p: Principal,
        action: str,
        mb: MailboxConfig | None,
        folder: str | None,
        uids: list[str] | str | None,
        outcome: str,
        **details: Any,
    ) -> None:
        self.state.audit(
            p.name, action, mb.name if mb else None, folder, uids, outcome=outcome, **details
        )

    @contextmanager
    def _mutation(
        self,
        p: Principal,
        action: str,
        mb: MailboxConfig,
        scope: str,
        folder: str | None = None,
        uids: list[str] | str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Scope check + audit row (ok / denied / error) around a mutation."""
        try:
            p.require(scope)
        except PermissionDenied:
            self._log(p, action, mb, folder, uids, "denied", scope=scope)
            raise
        details: dict[str, Any] = {}
        try:
            yield details
        except Exception as e:
            folder = details.pop("folder", folder)
            code = e.code if isinstance(e, MailboxdError) else type(e).__name__
            outcome = "denied" if isinstance(e, PermissionDenied) else "error"
            err = str(e)[:300]
            self._log(p, action, mb, folder, uids, outcome, error=err, code=code, **details)
            raise
        folder = details.pop("folder", folder)
        outcome = details.pop("outcome", "ok")
        self._log(p, action, mb, folder, uids, outcome, **details)

    @staticmethod
    def _uids(uids: list[str]) -> list[str]:
        out = [str(u).strip() for u in uids or []]
        if not out or not all(_UID_TOKEN.match(u) for u in out):
            raise InvalidRequest("`uids` must be a non-empty list of numeric UIDs")
        return out

    @staticmethod
    def _fetch_raw(s: Session, uid: str, folder: str) -> bytes:
        try:
            return s.fetch_raw(uid)
        except ImapError as e:
            raise NotFound(f"message {uid} not found in {folder!r}") from e

    def _labels_for(self, messages: list[dict[str, Any]]) -> None:
        for m in messages:
            m["labels"] = labels_for(m.get("flags") or [], self.cfg.labels)

    def _label_keywords(self, names: list[str]) -> list[str]:
        out = []
        for n in names:
            kw = _BUILTIN_LABELS.get(n) or self.cfg.labels.get(n)
            if kw is None:
                known = sorted(set(self.cfg.labels) | set(_BUILTIN_LABELS))
                raise InvalidRequest(f"unknown label {n!r}", known=known)
            out.append(kw)
        return out

    def _criteria(self, label: str | None, criteria: dict[str, Any]) -> list[str | bytes]:
        crit = {k: v for k, v in criteria.items() if v is not None}
        if label:
            if label == "flagged":
                crit["flagged"] = True
            else:
                crit.setdefault("keyword", self._label_keywords([label])[0])
        try:
            return imap_client.build_search_criteria(**crit)
        except TypeError as e:
            raise InvalidRequest(f"unsupported search criterion: {e}") from e
        except ImapError as e:
            raise InvalidRequest(str(e)) from e

    def _page(
        self,
        mb: MailboxConfig,
        folder: str,
        limit: int,
        spec: list[str | bytes] | str,
        before: tuple[str, int] | None = None,
        after: tuple[str, int] | None = None,
        has_attachments: bool | None = None,
        follow: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """search_page with UIDVALIDITY handling; returns (page, reset).

        `follow` starts from UID 0 in ascending mode when there is no position,
        so a reset never skips mail.
        """
        imap = self._imap(mb)
        kw: dict[str, Any] = {"has_attachments": has_attachments}
        pos = after or before
        page = imap_client.search_page(
            imap,
            folder=folder,
            limit=limit,
            criteria=spec,
            before_uid=before[1] if before else None,
            after_uid=after[1] if after else None,
            **kw,
        )
        if pos and pos[0] and page["uidvalidity"] and pos[0] != str(page["uidvalidity"]):
            page = imap_client.search_page(
                imap,
                folder=folder,
                limit=limit,
                criteria=spec,
                after_uid=0 if follow else None,
                has_attachments=has_attachments,
            )
            return page, True
        return page, False

    def _tag(self, mb: MailboxConfig, page: dict[str, Any]) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = page["messages"]
        self._labels_for(msgs)
        for m in msgs:
            m["mailbox"] = mb.name
            m["folder"] = page["folder"]
            if mb.imap is not None:
                m["mailbox_address"] = mb.imap.username
        return msgs

    # ── discovery ────────────────────────────────────────────────────────

    def mailboxes(self, p: Principal) -> dict[str, Any]:
        out = []
        for mb in visible_mailboxes(self.cfg, p):
            idents = mb.smtp.all_identities() if mb.smtp else []
            default = mb.smtp.default_identity().address if mb.smtp else None
            address = default or (mb.imap.username if mb.imap else "")
            out.append(
                {
                    "name": mb.name,
                    "description": mb.description,
                    "address": address,
                    "imap": mb.imap is not None,
                    "smtp": mb.smtp is not None,
                    "identities": [
                        {"address": i.address, "name": i.name, "default": i.address == default}
                        for i in idents
                    ],
                }
            )
        return {"mailboxes": out}

    def identities(self, p: Principal, mailbox: str) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        smtp = self._smtp(mb)
        default = smtp.default_identity().address
        return {
            "identities": [
                {
                    "address": i.address,
                    "name": i.name,
                    "default": i.address == default,
                    "has_signature": bool(i.signature_text or i.signature_html),
                }
                for i in smtp.all_identities()
            ]
        }

    def folders(self, p: Principal, mailbox: str) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        with self._session(mb) as s:
            found = s.folders()
            special: dict[str, str] = {}
            for kind in _KINDS:
                try:
                    special.setdefault(
                        s.special_folder(kind, getattr(mb.folders, kind)), kind
                    )
                except ImapError:
                    continue
        details = [
            {
                "name": f["name"],
                "delimiter": f.get("delimiter"),
                "flags": f.get("flags", []),
                "special": special.get(f["name"]),
            }
            for f in found
        ]
        return {"folders": [f["name"] for f in found], "details": details}

    # ── reading ──────────────────────────────────────────────────────────

    def inbox(
        self,
        p: Principal,
        mailbox: str | None = None,
        folder: str | None = None,
        limit: int = 50,
        checkpoint: str | None = None,
        since_cursor: str | None = None,
        label: str | None = None,
        **criteria: Any,
    ) -> dict[str, Any]:
        """Newest mail across mailboxes; `limit` applies per mailbox."""
        p.require("read")
        targets: list[MailboxConfig] = []
        if mailbox:
            for ident in [x.strip() for x in mailbox.split(",") if x.strip()]:
                mb = self._mb(p, ident)
                if mb.imap is not None and mb not in targets:
                    targets.append(mb)
        else:
            targets = [m for m in visible_mailboxes(self.cfg, p) if m.imap is not None]
        if not targets:
            raise NotFound("no matching mailbox with IMAP")
        spec = self._criteria(label, criteria)
        since = _since_cursor(since_cursor)

        def one(mb: MailboxConfig) -> tuple[MailboxConfig, dict[str, Any] | None, bool, str]:
            fld = folder or self._imap(mb).default_folder
            pos = since.get(mb.name)
            if pos is None and checkpoint:
                pos = self.state.get_cursor(p.name, checkpoint, mb.name, fld)
            try:
                page, reset = self._page(mb, fld, limit, spec, after=pos, follow=pos is not None)
            except _IMAP_ERRORS as e:
                return mb, {"error": str(e) or type(e).__name__}, False, fld
            return mb, page, reset, fld

        messages: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        positions: dict[str, dict[str, Any]] = {}
        next_since: dict[str, list[Any]] = {k: [v[0], v[1]] for k, v in since.items()}
        resets: list[str] = []
        with ThreadPoolExecutor(max_workers=len(targets)) as ex:
            results = list(ex.map(one, targets))
        for mb, page, reset, fld in results:
            if page is None or "error" in page:
                errors.append({"mailbox": mb.name, "error": (page or {}).get("error", "unknown")})
                continue
            messages.extend(self._tag(mb, page))
            positions[mb.name] = {
                "uidvalidity": page["uidvalidity"],
                "max_uid": page["max_uid"],
                "next_after_uid": page["next_after_uid"],
                "more_new": page.get("more_new", False),
                "folder": fld,
            }
            next_since[mb.name] = [page["uidvalidity"], page["next_after_uid"]]
            if reset:
                resets.append(mb.name)
            if checkpoint:
                nxt = page["next_after_uid"]
                uv = str(page["uidvalidity"])
                self.state.set_cursor(p.name, checkpoint, mb.name, fld, uv, nxt)
        messages.sort(key=imap_client.message_timestamp, reverse=True)
        out: dict[str, Any] = {
            "messages": messages,
            "errors": errors,
            "next_since_cursor": encode_cursor(next_since),
            "positions": positions,
        }
        if resets:
            out["reset"] = True
            out["reset_mailboxes"] = resets
        return out

    def list_messages(
        self,
        p: Principal,
        mailbox: str,
        folder: str | None = None,
        limit: int = 50,
        search: str = "ALL",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        fld = folder or self._imap(mb).default_folder
        page, reset = self._page(mb, fld, limit, search or "ALL", before=_page_cursor(cursor))
        return self._result(mb, page, reset)

    def _result(self, mb: MailboxConfig, page: dict[str, Any], reset: bool) -> dict[str, Any]:
        nxt = page["next_before_uid"]
        out: dict[str, Any] = {
            "messages": self._tag(mb, page),
            "total": page["total"],
            "next_cursor": encode_cursor({"v": page["uidvalidity"], "b": nxt}) if nxt else None,
        }
        if reset:
            out["reset"] = True
        return out

    def search(
        self,
        p: Principal,
        mailbox: str,
        folder: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        since_cursor: str | None = None,
        checkpoint: str | None = None,
        has_attachments: bool | None = None,
        label: str | None = None,
        **criteria: Any,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        fld = folder or self._imap(mb).default_folder
        spec = self._criteria(label, criteria)
        pos = _since_cursor(since_cursor).get(mb.name)
        if pos is None and not since_cursor and checkpoint:
            pos = self.state.get_cursor(p.name, checkpoint, mb.name, fld)
        page, reset = self._page(
            mb,
            fld,
            limit,
            spec,
            before=None if pos else _page_cursor(cursor),
            after=pos,
            has_attachments=has_attachments,
            follow=pos is not None,
        )
        out = self._result(mb, page, reset)
        out["next_since_cursor"] = encode_cursor(
            {mb.name: [page["uidvalidity"], page["next_after_uid"]]}
        )
        if page.get("more_new"):
            out["more_new"] = True
        if checkpoint:
            self.state.set_cursor(
                p.name, checkpoint, mb.name, fld, str(page["uidvalidity"]), page["next_after_uid"]
            )
        return out

    def _read_raw(self, mb: MailboxConfig, uid: str, folder: str | None) -> bytes:
        self._uids([uid])
        fld = folder or self._imap(mb).default_folder
        with self._session(mb) as s:
            s.select(fld, readonly=True)
            return self._fetch_raw(s, uid, fld)

    def get_message(
        self,
        p: Principal,
        mailbox: str,
        uid: str,
        folder: str | None = None,
        reader: bool = False,
        headers: bool = False,
        safe_html: bool = False,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        msg = email.message_from_bytes(self._read_raw(mb, uid, folder))
        out = imap_client.serialize_message(msg, uid=uid, reader=reader)
        auth = mime.parse_authentication_results(msg, self.cfg.auth.trusted_authserv_id)
        auth["signals"] = mime.security_signals(msg, auth)
        out["authentication"] = auth
        if headers:
            out["headers"] = mime.all_headers(msg)
        if out.get("body_html"):
            out["html_analysis"] = mime.analyze_html(out["body_html"])
            if safe_html:
                out["body_html_safe"] = mime.sanitize_html(out["body_html"])
        return out

    def get_raw(self, p: Principal, mailbox: str, uid: str, folder: str | None = None) -> bytes:
        mb = self._mb(p, mailbox)
        p.require("read")
        return self._read_raw(mb, uid, folder)

    def get_attachment(
        self, p: Principal, mailbox: str, uid: str, part_id: str, folder: str | None = None
    ) -> tuple[dict[str, Any], bytes]:
        mb = self._mb(p, mailbox)
        p.require("read")
        msg = email.message_from_bytes(self._read_raw(mb, uid, folder))
        return self._extract_part(msg, part_id, self.cfg.limits.attachment_max_bytes)

    @staticmethod
    def _extract_part(msg: Message, part_id: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
        part = mime.get_part(msg, part_id)
        if part is None or not mime.is_attachment(part):
            raise NotFound(f"attachment {part_id!r} not found")
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        if len(data) > max_bytes:
            raise TooLarge(
                f"attachment is {len(data)} bytes; the limit is {max_bytes}",
                size=len(data),
                limit=max_bytes,
            )
        return mime.attachment_info(part_id, part), data

    # Delegated to insights (scope + mailbox resolution happen here).

    def thread(
        self, p: Principal, mailbox: str, uid: str, folder: str | None = None
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        self._imap(mb)
        from . import insights

        out: dict[str, Any] = insights.thread(self, p, mb, uid, folder)
        return out

    def threads(
        self, p: Principal, mailbox: str, folder: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        self._imap(mb)
        from . import insights

        out: dict[str, Any] = insights.threads(self, p, mb, folder, limit)
        return out

    def stats(self, p: Principal, mailbox: str) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        self._imap(mb)
        from . import insights

        out: dict[str, Any] = insights.stats(self, p, mb)
        return out

    def health_deep(self, p: Principal) -> dict[str, Any]:
        p.require("admin")
        from . import insights

        out: dict[str, Any] = insights.health_deep(self, p, visible_mailboxes(self.cfg, p))
        return out

    def audit_log(
        self,
        p: Principal,
        since: float | int | str | None = None,
        action: str | None = None,
        token: str | None = None,
        mailbox: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        p.require("admin")
        rows = self.state.list_audit(
            since=_parse_when(since),
            action=action,
            token=token,
            mailbox=mailbox,
            limit=max(1, min(int(limit), 1000)),
        )
        return {"entries": rows}

    # ── organising ───────────────────────────────────────────────────────

    def delete(
        self,
        p: Principal,
        mailbox: str,
        uids: list[str],
        folder: str | None = None,
        permanent: bool = False,
    ) -> dict[str, Any]:
        """Move to Trash (organize), or expunge from Trash itself (delete scope)."""
        mb = self._mb(p, mailbox)
        imap = self._imap(mb)
        ids = self._uids(uids)
        fld = folder or imap.default_folder
        action = "delete_permanent" if permanent else "delete"
        with self._mutation(p, action, mb, "delete" if permanent else "organize", fld, ids) as d:
            joined = ",".join(ids)
            with self._session(mb) as s:
                trash = s.special_folder("trash", mb.folders.trash)
                d["trash_folder"] = trash
                if not permanent:
                    if fld == trash:
                        raise Conflict(
                            "messages are already in the trash folder; "
                            "use permanent=true (delete scope) to erase them",
                            trash_folder=trash,
                        )
                    s.select(fld, readonly=False)
                    method = s.move(joined, trash)
                    d["method"] = method
                    return {
                        "ok": True,
                        "action": "trashed",
                        "method": method,
                        "trash_folder": trash,
                    }
                if fld != trash:
                    raise Conflict(
                        "permanent deletion is only allowed from the trash folder; "
                        "delete without `permanent` first",
                        trash_folder=trash,
                    )
                s.select(fld, readonly=False)
                s.store(joined, "+FLAGS", [r"\Deleted"])
                if s.expunge_uids(joined):
                    method = "uid-expunge"
                else:
                    # No UIDPLUS: acceptable only because this is the trash folder.
                    typ, data = s.conn.expunge()
                    if typ != "OK":
                        raise ImapError(f"EXPUNGE failed: {data!r}")
                    method = "expunge-trash"
                d["method"] = method
                return {"ok": True, "action": "deleted", "method": method, "trash_folder": trash}

    def _flag_op(
        self,
        p: Principal,
        mailbox: str,
        action: str,
        uids: list[str],
        folder: str | None,
        add: list[str],
        remove: list[str],
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        imap = self._imap(mb)
        ids = self._uids(uids)
        fld = folder or imap.default_folder
        with self._mutation(p, action, mb, "organize", fld, ids) as d:
            d.update(details or {})
            with self._session(mb) as s:
                s.select(fld, readonly=False)
                if add:
                    s.store(",".join(ids), "+FLAGS", add)
                if remove:
                    s.store(",".join(ids), "-FLAGS", remove)
        return {"ok": True}

    def mark_seen(
        self,
        p: Principal,
        mailbox: str,
        uids: list[str],
        folder: str | None = None,
        seen: bool = True,
    ) -> dict[str, Any]:
        flag = [r"\Seen"]
        return self._flag_op(
            p, mailbox, "mark_seen", uids, folder, flag if seen else [], [] if seen else flag,
            {"seen": seen},
        )

    def set_labels(
        self,
        p: Principal,
        mailbox: str,
        uids: list[str],
        folder: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        add, remove = list(add or []), list(remove or [])
        try:
            p.require("organize")
        except PermissionDenied:
            self._log(p, "set_labels", mb, folder, uids, "denied", scope="organize")
            raise
        if not add and not remove:
            raise InvalidRequest("`add` or `remove` is required")
        return self._flag_op(
            p,
            mailbox,
            "set_labels",
            uids,
            folder,
            self._label_keywords(add),
            self._label_keywords(remove),
            {"add": add, "remove": remove},
        )

    def move(
        self,
        p: Principal,
        mailbox: str,
        uids: list[str],
        to_folder: str,
        folder: str | None = None,
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        imap = self._imap(mb)
        ids = self._uids(uids)
        fld = folder or imap.default_folder
        with self._mutation(p, "move", mb, "organize", fld, ids) as d:
            d["to_folder"] = to_folder
            with self._session(mb) as s:
                if to_folder not in {f["name"] for f in s.folders()}:
                    raise NotFound(f"folder {to_folder!r} does not exist")
                s.select(fld, readonly=False)
                method = s.move(",".join(ids), to_folder)
            d["method"] = method
        return {"ok": True, "method": method, "to_folder": to_folder}

    def create_folder(self, p: Principal, mailbox: str, name: str) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        self._imap(mb)
        with self._mutation(p, "create_folder", mb, "organize") as d:
            d["name"] = name
            name = (name or "").strip()
            if not name or len(name) > 200 or any(c in name for c in "\r\n\x00"):
                raise InvalidRequest("invalid folder name")
            with self._session(mb) as s:
                if name in {f["name"] for f in s.folders()}:
                    raise Conflict(f"folder {name!r} already exists")
                s.create(name)
        return {"ok": True, "folder": name}

    # ── composing ────────────────────────────────────────────────────────

    def _attachments(
        self, p: Principal, mb: MailboxConfig, items: list[AttachmentIn]
    ) -> list[OutgoingAttachment]:
        limits = self.cfg.limits
        budget = limits.outgoing_attachments_max_bytes
        out: list[OutgoingAttachment] = []
        total = 0
        for a in items:
            if (a.data_base64 is None) == (a.from_message is None):
                raise InvalidRequest("attachment needs exactly one of data_base64 / from_message")
            if a.data_base64 is not None:
                if len(a.data_base64) > budget * 4 // 3 + 8:
                    raise TooLarge("attachments exceed the outgoing size limit", limit=budget)
                try:
                    data = base64.b64decode(a.data_base64, validate=True)
                except (binascii.Error, ValueError) as e:
                    raise InvalidRequest("attachment data_base64 is not valid base64") from e
                name = mime.safe_filename(a.filename or "") or "attachment"
                ctype = a.content_type or ""
            else:
                ref = a.from_message
                assert ref is not None
                p.require("read")
                msg = email.message_from_bytes(self._read_raw(mb, ref.uid, ref.folder))
                info, data = self._extract_part(msg, ref.part_id, budget)
                name = mime.safe_filename(a.filename or "") or info["filename"]
                ctype = a.content_type or info["content_type"]
            total += len(data)
            if total > budget:
                raise TooLarge("attachments exceed the outgoing size limit", limit=budget)
            out.append(OutgoingAttachment(filename=name, content_type=ctype, data=data))
        return out

    @staticmethod
    def _preview(msg: Message) -> dict[str, Any]:
        text = ""
        if isinstance(msg, EmailMessage):
            body = msg.get_body(("plain",))
            if body is not None:
                text = str(body.get_content())
        return {"headers": [[k, str(v)] for k, v in msg.items()], "body_text": text}

    def _deliver(
        self,
        p: Principal,
        mb: MailboxConfig,
        msg: EmailMessage,
        requested_dry: bool,
        d: dict[str, Any],
        answered: tuple[str, str] | None = None,
        remove_draft: str | None = None,
    ) -> dict[str, Any]:
        smtp = self._smtp(mb)
        compose.strip_internal_headers(msg)
        dry = self.cfg.send_guard.dry_run or p.dry_run or requested_dry
        rcpts = compose.envelope_recipients(msg)
        warnings: list[str] = []
        result: dict[str, Any] = {
            "dry_run": dry,
            "from": str(msg.get("From", "")),
            "to": _addr_list(msg.get("To")),
            "cc": _addr_list(msg.get("Cc")),
            "subject": str(msg.get("Subject", "")),
            "message_id": str(msg.get("Message-ID", "")),
            "sent_copy": None,
            "warnings": warnings,
        }
        d.update(to=result["to"], cc=result["cc"], subject=result["subject"])
        d["message_id"] = result["message_id"]
        if dry:
            result["preview"] = self._preview(msg)
            d.update(dry_run=True, outcome="dry_run")
            return result
        guard = self.cfg.send_guard.per_hour
        mailbox_limit = guard if smtp.per_hour is None else smtp.per_hour
        self.state.check_and_record_send(mb.name, p.name, mailbox_limit, guard)
        smtp_client.send_message(smtp, msg, rcpts)
        if smtp.save_sent and mb.imap is not None:
            try:
                with self._session(mb) as s:
                    sent = s.special_folder("sent", mb.folders.sent)
                    uid = s.append(sent, msg.as_bytes(), [r"\Seen"])
                result["sent_copy"] = {"folder": sent, "uid": uid}
            except _IMAP_ERRORS:
                warnings.append("sent_copy_failed")
        if mb.imap is not None and (answered or remove_draft):
            try:
                with self._session(mb) as s:
                    if answered:
                        s.select(answered[0], readonly=False)
                        s.store(answered[1], "+FLAGS", [r"\Answered"])
                    if remove_draft:
                        s.select(self._drafts_folder(s, mb), readonly=False)
                        self._remove(s, mb, remove_draft)
            except _IMAP_ERRORS:
                warnings.append("answered_flag_failed" if answered else "draft_not_removed")
        return result

    def send(self, p: Principal, mailbox: str, req: ComposeRequest) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        with self._mutation(p, "send", mb, "send") as d:
            smtp = self._smtp(mb)
            msg = self._build(p, mb, smtp, req)
            return self._deliver(p, mb, msg, req.dry_run, d)

    def _build(
        self, p: Principal, mb: MailboxConfig, smtp: SmtpConfig, req: ComposeRequest
    ) -> EmailMessage:
        ident = compose.resolve_identity(smtp, req.identity, req.from_address)
        atts = self._attachments(p, mb, req.attachments)
        return compose.build_message(
            ident,
            req.to,
            req.subject,
            body_text=req.body_text,
            body_html=req.body_html,
            cc=req.cc,
            bcc=req.bcc,
            reply_to=req.reply_to,
            attachments=atts,
            signature=req.signature,
        )

    def reply(
        self,
        p: Principal,
        mailbox: str,
        uid: str,
        req: ReplyRequest,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Reply with server-derived recipients, subject and threading headers."""
        mb = self._mb(p, mailbox)
        imap = self._imap(mb)
        self._uids([uid])
        fld = folder or imap.default_folder
        scope = "draft" if req.draft else "send"
        with self._mutation(p, "reply", mb, scope, fld, [uid]) as d:
            d["draft"] = req.draft
            smtp = self._smtp(mb)
            raw = self._read_raw(mb, uid, fld)
            orig = email.message_from_bytes(raw)
            fields = compose.reply_fields(orig, mb.own_addresses(), req.reply_all)
            ident = self._reply_identity(smtp, orig, req.identity)
            body_text, body_html = req.body_text, req.body_html
            if req.quote:
                body_text, body_html = self._quote(orig, body_text, body_html)
            msg = compose.build_message(
                ident,
                list(fields["to"]),  # type: ignore[call-overload]
                str(fields["subject"]),
                body_text=body_text,
                body_html=body_html,
                cc=list(fields["cc"]) or None,  # type: ignore[call-overload]
                in_reply_to=fields["in_reply_to"],  # type: ignore[arg-type]
                references=list(fields["references"]),  # type: ignore[call-overload]
                attachments=self._attachments(p, mb, req.attachments),
                signature=req.signature,
            )
            if req.draft:
                return self._save_draft(p, mb, msg, d)
            return self._deliver(p, mb, msg, req.dry_run, d, answered=(fld, uid))

    @staticmethod
    def _reply_identity(smtp: SmtpConfig, orig: Message, wanted: str | None) -> Any:
        if wanted:
            return compose.resolve_identity(smtp, wanted)
        addrs = {
            a.lower()
            for _, a in getaddresses(
                [mime.decode(orig.get("To")), mime.decode(orig.get("Cc"))]
            )
        }
        for i in smtp.all_identities():
            if i.address.lower() in addrs:
                return i
        return smtp.default_identity()

    @staticmethod
    def _quote(
        orig: Message, body_text: str | None, body_html: str | None
    ) -> tuple[str | None, str | None]:
        info = imap_client.serialize_message(orig, reader=True)
        original = (info.get("body_text") or info.get("body_reader") or "")[:_QUOTE_MAX_CHARS]
        who, when = info["from"], info["date"]
        if body_text is not None:
            body_text = body_text.rstrip("\n") + compose.quote_text(original, who, when)
        if body_html is not None:
            quoted = html_lib.escape(original).replace("\n", "<br>")
            head = html_lib.escape(f"On {when}, {who} wrote:")
            body_html += f'<blockquote type="cite">{head}<br>{quoted}</blockquote>'
        return body_text, body_html

    # ── drafts ───────────────────────────────────────────────────────────

    def _drafts_folder(self, s: Session, mb: MailboxConfig) -> str:
        return s.special_folder("drafts", mb.folders.drafts)

    @staticmethod
    def _remove(s: Session, mb: MailboxConfig, uid: str) -> str:
        """Delete a message from the selected-writable folder; never a folder-wide EXPUNGE."""
        s.store(uid, "+FLAGS", [r"\Deleted"])
        return "uid-expunge" if s.expunge_uids(uid) else "flagged-deleted"

    def _save_draft(
        self,
        p: Principal,
        mb: MailboxConfig,
        msg: EmailMessage,
        d: dict[str, Any],
        replace_uid: str | None = None,
    ) -> dict[str, Any]:
        del msg["X-Mailboxd-Draft-By"]
        msg["X-Mailboxd-Draft-By"] = p.name
        with self._session(mb) as s:
            folder = self._drafts_folder(s, mb)
            uid = s.append(folder, msg.as_bytes(), [r"\Draft", r"\Seen"])
            result: dict[str, Any] = {
                "uid": uid,
                "folder": folder,
                "message_id": str(msg.get("Message-ID", "")),
            }
            if replace_uid:
                s.select(folder, readonly=False)
                result["replaced_uid"] = replace_uid
                result["replace_method"] = self._remove(s, mb, replace_uid)
        d.update(uid=uid, folder=folder, subject=str(msg.get("Subject", "")))
        return result

    def create_draft(self, p: Principal, mailbox: str, req: ComposeRequest) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        with self._mutation(p, "create_draft", mb, "draft") as d:
            self._imap(mb)
            msg = self._build(p, mb, self._smtp(mb), req)
            return self._save_draft(p, mb, msg, d)

    def list_drafts(self, p: Principal, mailbox: str, limit: int = 50) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        p.require("read")
        imap = self._imap(mb)
        with self._session(mb) as s:
            folder = self._drafts_folder(s, mb)
        page = imap_client.search_page(imap, folder=folder, limit=limit, criteria="ALL")
        return {"messages": self._tag(mb, page), "folder": folder}

    def _read_draft(self, mb: MailboxConfig, uid: str) -> tuple[str, bytes]:
        self._uids([uid])
        with self._session(mb) as s:
            folder = self._drafts_folder(s, mb)
            s.select(folder, readonly=True)
            return folder, self._fetch_raw(s, uid, folder)

    def update_draft(
        self, p: Principal, mailbox: str, uid: str, req: DraftUpdate
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        with self._mutation(p, "update_draft", mb, "draft", uids=[uid]) as d:
            smtp = self._smtp(mb)
            self._imap(mb)
            folder, raw = self._read_draft(mb, uid)
            old = email.message_from_bytes(raw)
            info = imap_client.serialize_message(old)
            signed = bool(old.get(compose.SIGNATURE_MARKER_HEADER))
            ident = compose.resolve_identity(
                smtp, req.identity or req.from_address or parseaddr(mime.decode(old.get("From")))[1]
            )
            old_text, old_html = (
                v.replace("\r\n", "\n") if isinstance(v, str) else v
                for v in (info["body_text"], info["body_html"])
            )
            new_body = req.body_text is not None or req.body_html is not None
            if signed and new_body:
                old_text, old_html = _strip_signature(old_text, old_html, ident)
            text = req.body_text if req.body_text is not None else old_text
            html = req.body_html if req.body_html is not None else old_html
            if req.attachments is not None:
                atts = self._attachments(p, mb, req.attachments)
            else:
                atts = self._old_attachments(old)
            sign = (req.signature if req.signature is not None else signed) if new_body else False

            def addrs(field: str) -> list[str] | None:
                value = mime.decode(old.get(field))
                found = [f"{n} <{a}>" if n else a for n, a in getaddresses([value]) if a]
                return found or None

            msg = compose.build_message(
                ident,
                req.to if req.to is not None else (addrs("To") or []),
                req.subject if req.subject is not None else mime.decode(old.get("Subject")),
                body_text=text,
                body_html=html,
                cc=req.cc if req.cc is not None else addrs("Cc"),
                bcc=req.bcc if req.bcc is not None else addrs("Bcc"),
                reply_to=(
                    req.reply_to
                    if req.reply_to is not None
                    else mime.decode(old.get("Reply-To")) or None
                ),
                in_reply_to=mime.decode(old.get("In-Reply-To")) or None,
                references=mime.message_ids(old.get("References")) or None,
                attachments=atts,
                signature=sign,
            )
            if signed and not sign and not new_body:
                msg[compose.SIGNATURE_MARKER_HEADER] = "1"
            return self._save_draft(p, mb, msg, d, replace_uid=uid)

    @staticmethod
    def _old_attachments(old: Message) -> list[OutgoingAttachment]:
        out = []
        for pid, part in mime.iter_parts(old):
            if not mime.is_attachment(part):
                continue
            payload = part.get_payload(decode=True)
            info = mime.attachment_info(pid, part)
            out.append(
                OutgoingAttachment(
                    info["filename"],
                    info["content_type"],
                    payload if isinstance(payload, bytes) else b"",
                )
            )
        return out

    def delete_draft(self, p: Principal, mailbox: str, uid: str) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        self._imap(mb)
        self._uids([uid])
        with self._mutation(p, "delete_draft", mb, "draft", uids=[uid]) as d:
            with self._session(mb) as s:
                folder = self._drafts_folder(s, mb)
                s.select(folder, readonly=False)
                d["method"] = self._remove(s, mb, uid)
                d["folder"] = folder
        return {"ok": True}

    def send_draft(
        self, p: Principal, mailbox: str, uid: str, dry_run: bool = False
    ) -> dict[str, Any]:
        mb = self._mb(p, mailbox)
        with self._mutation(p, "send_draft", mb, "send", uids=[uid]) as d:
            smtp = self._smtp(mb)
            self._imap(mb)
            folder, raw = self._read_draft(mb, uid)
            d["folder"] = folder
            msg = email.message_from_bytes(raw, policy=policy.SMTP)
            assert isinstance(msg, EmailMessage)
            compose.strip_internal_headers(msg)
            ident = compose.resolve_identity(smtp, parseaddr(str(msg.get("From", "")))[1])
            del msg["From"]
            msg["From"] = ident.formatted()
            del msg["Date"]
            msg["Date"] = formatdate(localtime=True)
            answered = self._find_original(mb, str(msg.get("In-Reply-To", "")))
            return self._deliver(p, mb, msg, dry_run, d, answered=answered, remove_draft=uid)

    def _find_original(self, mb: MailboxConfig, in_reply_to: str) -> tuple[str, str] | None:
        """Locate the message a reply draft answers, so it can be flagged \\Answered."""
        ids = mime.message_ids(in_reply_to)
        if not ids or mb.imap is None:
            return None
        folder = mb.imap.default_folder
        try:
            with self._session(mb) as s:
                s.select(folder, readonly=True)
                uids = s.search(imap_client.build_search_criteria(message_id=ids[0]))
        except _IMAP_ERRORS:
            return None
        return (folder, str(uids[-1])) if uids else None


def _addr_list(value: Any) -> list[str]:
    return [f"{n} <{a}>" if n else a for n, a in getaddresses([str(value or "")]) if a]


def _strip_signature(
    text: str | None, html: str | None, ident: Any
) -> tuple[str | None, str | None]:
    """Remove the signature `apply_signature` appended, so a rebuild does not double it."""
    if text is not None and ident.signature_text:
        tail = "\n\n-- \n" + ident.signature_text
        stripped = text.rstrip("\n")
        if stripped.endswith(tail):
            text = stripped[: -len(tail)] + "\n"
    if html is not None:
        html = re.sub(r'<div class="signature">.*?</div>', "", html, count=1, flags=re.S)
    return text, html
