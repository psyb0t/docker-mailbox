"""Shared fakes for the Service tests: an in-memory IMAP server and builders.

`install_fake_imap` patches `imap_client.session`, so the real `search_page`,
`serialize_message`, ... run against `FakeServer` without any socket.
"""

from __future__ import annotations

import email
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator

import pytest

from mailboxd import imap_client, mime
from mailboxd.config import Config
from mailboxd.errors import NotFound
from mailboxd.imap_client import ImapError
from mailboxd.principal import Principal
from mailboxd.services import Service
from mailboxd.state import State

ALL = ("read", "draft", "organize", "send", "delete", "admin")


class FakeMsg:
    def __init__(self, uid: int, raw: bytes, flags: list[str]) -> None:
        self.uid, self.raw, self.flags = uid, raw, flags


def make_raw(
    subject: str = "hello",
    frm: str = "Alice <alice@example.org>",
    to: str = "contact@example.com",
    body: str = "body text",
    msgid: str | None = None,
    **headers: str,
) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Date"] = "Mon, 01 Jan 2026 10:00:00 +0000"
    m["Message-ID"] = msgid or f"<{abs(hash(subject)) % 10**8}@example.org>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


class FakeServer:
    def __init__(self) -> None:
        self.folders: dict[str, list[FakeMsg]] = {
            n: [] for n in ("INBOX", "Sent", "Drafts", "Trash", "Junk")
        }
        self.caps = {"IMAP4REV1", "UIDPLUS", "MOVE"}
        self.uidvalidity = "100"
        self.next_uid = 1
        self.plain_expunges: list[str] = []
        self.fail_append: set[str] = set()

    def add(self, folder: str, raw: bytes, flags: list[str] | None = None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder].append(FakeMsg(uid, raw, list(flags or [])))
        return uid

    def get(self, folder: str, uid: int) -> FakeMsg:
        return next(m for m in self.folders[folder] if m.uid == uid)

    def uids(self, folder: str) -> list[int]:
        return [m.uid for m in self.folders[folder]]


class _Conn:
    def __init__(self, s: "FakeSession") -> None:
        self.s = s

    def expunge(self) -> tuple[str, list[Any]]:
        sel = self.s.selected or ""
        self.s.srv.plain_expunges.append(sel)
        keep = [m for m in self.s.srv.folders[sel] if r"\Deleted" not in m.flags]
        self.s.srv.folders[sel] = keep
        return "OK", []


class FakeSession:
    def __init__(self, srv: FakeServer, cfg: Any) -> None:
        self.srv, self.cfg = srv, cfg
        self.selected: str | None = None
        self.readonly = True
        self.conn = _Conn(self)

    def has(self, cap: str) -> bool:
        return cap.upper() in self.srv.caps

    def caps(self) -> set[str]:
        return self.srv.caps

    def folders(self) -> list[dict[str, Any]]:
        flags = {"Sent": [r"\Sent"], "Drafts": [r"\Drafts"], "Trash": [r"\Trash"]}
        return [{"name": n, "delimiter": "/", "flags": flags.get(n, [])} for n in self.srv.folders]

    def special_folder(self, kind: str, override: str | None = None) -> str:
        return override or kind.capitalize()

    def select(self, folder: str, readonly: bool = True) -> str:
        if folder not in self.srv.folders:
            raise ImapError(f"could not select folder {folder!r}")
        self.selected, self.readonly = folder, readonly
        return self.srv.uidvalidity

    def _sel(self) -> list[FakeMsg]:
        assert self.selected is not None
        return self.srv.folders[self.selected]

    def _writable(self) -> None:
        if self.selected is None or self.readonly:
            raise ImapError("internal: folder not selected read-write")

    def search(self, criteria: Any) -> list[int]:
        crit = [criteria] if isinstance(criteria, str) else [str(c) for c in criteria]
        msgs = self._sel()
        if "KEYWORD" in crit:
            kw = crit[crit.index("KEYWORD") + 1]
            msgs = [m for m in msgs if kw in m.flags]
        return sorted(m.uid for m in msgs)

    def fetch_headers(self, uids: list[int]) -> list[dict[str, Any]]:
        out = []
        for u in uids:
            m = next(x for x in self._sel() if x.uid == u)
            msg = email.message_from_bytes(m.raw)
            out.append(
                {
                    "uid": str(u),
                    "size": len(m.raw),
                    "flags": list(m.flags),
                    "from": mime.decode(msg["From"]),
                    "to": mime.decode(msg["To"]),
                    "cc": mime.decode(msg["Cc"]),
                    "subject": mime.decode(msg["Subject"]),
                    "date": mime.decode(msg["Date"]),
                    "message_id": mime.decode(msg["Message-ID"]),
                    "in_reply_to": "",
                    "references": [],
                }
            )
        return out

    def bodystructures(self, uids: list[int]) -> dict[int, str]:
        return {u: "" for u in uids}

    def fetch_raw(self, uid: str, header_only: bool = False) -> bytes:
        for m in self._sel():
            if str(m.uid) == str(uid):
                return m.raw
        raise ImapError(f"message UID {uid} not found")

    def _pick(self, uids: str) -> list[FakeMsg]:
        wanted = {int(x) for x in uids.split(",")}
        return [m for m in self._sel() if m.uid in wanted]

    def store(self, uids: str, op: str, flags: list[str]) -> None:
        self._writable()
        for m in self._pick(uids):
            for f in flags:
                if op.startswith("+") and f not in m.flags:
                    m.flags.append(f)
                if op.startswith("-") and f in m.flags:
                    m.flags.remove(f)

    def expunge_uids(self, uids: str) -> bool:
        self._writable()
        if not self.has("UIDPLUS"):
            return False
        gone = {m.uid for m in self._pick(uids) if r"\Deleted" in m.flags}
        assert self.selected is not None
        self.srv.folders[self.selected] = [m for m in self._sel() if m.uid not in gone]
        return True

    def move(self, uids: str, dest: str) -> str:
        self._writable()
        picked = self._pick(uids)
        for m in picked:
            self.srv.add(dest, m.raw, m.flags)
        if self.has("MOVE"):
            gone = {m.uid for m in picked}
            assert self.selected is not None
            self.srv.folders[self.selected] = [m for m in self._sel() if m.uid not in gone]
            return "move"
        self.store(uids, "+FLAGS.SILENT", [r"\Deleted"])
        return "copy+uid-expunge" if self.expunge_uids(uids) else "copy+flagged-deleted"

    def append(self, folder: str, raw: bytes, flags: list[str]) -> str | None:
        if folder in self.srv.fail_append:
            raise ImapError(f"APPEND to {folder!r} failed")
        return str(self.srv.add(folder, raw, flags))

    def create(self, folder: str) -> None:
        self.srv.folders[folder] = []

    def status(self, folder: str) -> dict[str, int]:
        return {"messages": len(self.srv.folders[folder]), "unseen": 0, "recent": 0}

    def quota(self) -> list[dict[str, Any]]:
        return []


def install_fake_imap(monkeypatch: pytest.MonkeyPatch, srv: FakeServer) -> None:
    @contextmanager
    def fake_session(cfg: Any, message_max_bytes: int = 0) -> Iterator[FakeSession]:
        yield FakeSession(srv, cfg)

    monkeypatch.setattr(imap_client, "session", fake_session)


def make_config(**over: Any) -> Config:
    imap = {"host": "imap.invalid", "username": "contact@example.com", "password": "x"}
    smtp = {
        "host": "smtp.invalid",
        "username": "contact@example.com",
        "password": "x",
        "from_address": "contact@example.com",
        "identities": [
            {"address": "contact@example.com", "name": "Contact", "default": True},
            {"address": "support@example.com", "name": "Support"},
        ],
    }
    data: dict[str, Any] = {
        "auth": {"trusted_authserv_id": "mx.good.example"},
        "mailboxes": [
            {"name": "main", "imap": imap, "smtp": smtp},
            {"name": "ro", "imap": {**imap, "username": "ro@example.com"}},
            {"name": "out", "smtp": {**smtp, "username": "out@example.com"}},
        ],
    }
    data.update(over)
    return Config.model_validate(data)


def principal(
    *scopes: str, name: str = "tok", mailboxes: Any = None, dry_run: bool = False
) -> Principal:
    return Principal(
        name=name,
        scopes=frozenset(scopes or ALL),
        mailboxes=None if mailboxes is None else frozenset(mailboxes),
        dry_run=dry_run,
    )


class Smtp:
    """Records what would have gone on the wire."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, list[str]]] = []


def make_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cfg: Config | None = None
) -> tuple[Service, FakeServer, Smtp]:
    from mailboxd import smtp_client

    srv = FakeServer()
    install_fake_imap(monkeypatch, srv)
    smtp = Smtp()
    monkeypatch.setattr(
        smtp_client, "send_message", lambda c, msg, rcpts: smtp.sent.append((msg, list(rcpts)))
    )
    svc = Service(cfg or make_config(), State(str(tmp_path / "s.db")))
    return svc, srv, smtp


def audit_rows(svc: Service, **kw: Any) -> list[dict[str, Any]]:
    return svc.state.list_audit(**kw)


def not_found_message(svc: Service, p: Principal, name: str) -> str:
    try:
        svc.folders(p, name)
    except NotFound as e:
        return e.message
    raise AssertionError("expected NotFound")
