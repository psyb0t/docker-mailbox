"""Read-only insights: threads, per-folder stats and deep health checks.

These functions are called by `services.Service` after the scope check and
the mailbox resolution, so they only deal with IMAP/SMTP.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses
from typing import Any

from . import imap_client, mime, smtp_client
from .config import ImapConfig, MailboxConfig
from .errors import Conflict
from .imap_client import ImapError, Session
from .principal import Principal

log = logging.getLogger(__name__)

MAX_THREAD_IDS = 30
MAX_FOLDER_HITS = 200
MAX_SCAN = 500
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|tr|aw|wg)\s*:\s*)+", re.I)
_KINDS = ("sent", "drafts", "trash", "junk")


def _imap(mb: MailboxConfig) -> ImapConfig:
    if mb.imap is None:
        raise Conflict(f"mailbox {mb.name!r} has no IMAP configured")
    return mb.imap


def _sent_folder(s: Session, mb: MailboxConfig) -> str | None:
    try:
        return s.special_folder("sent", mb.folders.sent)
    except ImapError:
        return None


def _or_chain(criteria: list[list[str | bytes]]) -> list[str | bytes]:
    """IMAP search keys are ANDed; OR is binary and prefix: OR a OR b c."""
    out: list[str | bytes] = []
    for crit in criteria[:-1]:
        out.append("OR")
        out.extend(crit)
    out.extend(criteria[-1])
    return out


def _header_key(name: str, value: str) -> list[str | bytes]:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return ["HEADER", name, f'"{escaped}"']


def _id_set(msg: Any) -> tuple[str, list[str]]:
    """(root id, all ids) of a parsed message header."""
    own = mime.message_ids(mime.decode(msg.get("Message-ID")))
    refs = mime.message_ids(mime.decode(msg.get("References")))
    irt = mime.message_ids(mime.decode(msg.get("In-Reply-To")))
    root = (refs or irt or own or [""])[0]
    ids: list[str] = []
    for i in [*refs, *irt, *own]:
        if i not in ids:
            ids.append(i)
    return root, ids[-MAX_THREAD_IDS:] if len(ids) > MAX_THREAD_IDS else ids


def _usable(value: str) -> bool:
    return bool(value) and value.isascii() and "\r" not in value and "\n" not in value


def _find_related(s: Session, root: str, ids: list[str]) -> list[int]:
    keys = [_header_key("Message-ID", i) for i in ids if _usable(i)]
    if _usable(root):
        keys.append(_header_key("References", root))
    if not keys:
        return []
    uids = s.search(_or_chain(keys))
    return uids[-MAX_FOLDER_HITS:]


def _date_key(m: dict[str, Any]) -> float:
    return imap_client.message_timestamp(m)


def thread(
    svc: Any, p: Principal, mb: MailboxConfig, uid: str, folder: str | None
) -> dict[str, Any]:
    """The conversation a message belongs to, across its folder and Sent."""
    imap = _imap(mb)
    folder = folder or imap.default_folder
    found: dict[str, dict[str, Any]] = {}
    with imap_client.session(imap) as s:
        s.select(folder, readonly=True)
        head = s.fetch_message(uid, header_only=True)
        root, ids = _id_set(head)
        folders = [folder]
        sent = _sent_folder(s, mb)
        if sent and sent != folder:
            folders.append(sent)
        for name in folders:
            if name != folder:
                s.select(name, readonly=True)
            for m in s.fetch_headers(_find_related(s, root, ids)):
                m["folder"] = name
                key = m.get("message_id") or f"{name}:{m.get('uid')}"
                found.setdefault(key, m)
    messages = sorted(found.values(), key=_date_key)
    return {"root": root, "count": len(messages), "messages": messages}


# ── threads ──────────────────────────────────────────────────────────────


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def normalize_subject(subject: str) -> str:
    return " ".join(_SUBJECT_PREFIX.sub("", subject or "").lower().split())


def _msg_ids(m: dict[str, Any]) -> list[str]:
    ids = mime.message_ids(m.get("message_id") or "")
    ids += list(m.get("references") or [])
    ids += mime.message_ids(m.get("in_reply_to") or "")
    return ids


def group_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group header dicts into conversations (union-find on Message-IDs)."""
    uf = _UnionFind()
    keyed: list[tuple[str, dict[str, Any]]] = []
    for m in messages:
        ids = _msg_ids(m)
        if ids:
            for other in ids[1:]:
                uf.union(ids[0], other)
            key = ids[0]
        else:
            key = "subject:" + normalize_subject(str(m.get("subject") or ""))
        keyed.append((key, m))
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, m in keyed:
        groups.setdefault(uf.find(key), []).append(m)
    return list(groups.values())


def _scan(s: Session, folder: str, count: int) -> list[dict[str, Any]]:
    s.select(folder, readonly=True)
    uids = s.search("ALL")[-count:]
    out = s.fetch_headers(uids)
    for m in out:
        m["folder"] = folder
    return out


def _participants(msgs: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, str] = {}
    for m in msgs:
        for _, addr in getaddresses([str(m.get("from") or ""), str(m.get("to") or "")]):
            if addr and addr.lower() not in seen:
                seen[addr.lower()] = addr
    return list(seen.values())[:10]


def _thread_entry(group: list[dict[str, Any]], sent: str | None) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for m in group:
        by_id.setdefault(m.get("message_id") or f"{m['folder']}:{m.get('uid')}", m)
    msgs = sorted(by_id.values(), key=_date_key)
    first, last = msgs[0], msgs[-1]
    ids = _msg_ids(first)
    root = (list(first.get("references") or []) or ids or [""])[0]
    unread = sum(1 for m in msgs if m["folder"] != sent and "\\Seen" not in m.get("flags", []))
    return {
        "thread_id": root or f"{first['folder']}:{first.get('uid')}",
        "subject": first.get("subject") or "",
        "count": len(msgs),
        "unread": unread,
        "participants": _participants(msgs),
        "last_date": last.get("date") or "",
        "messages": [
            {
                "uid": m.get("uid"),
                "folder": m["folder"],
                "from": m.get("from"),
                "date": m.get("date"),
                "subject": m.get("subject"),
                "flags": m.get("flags", []),
            }
            for m in msgs
        ],
        "_ts": _date_key(last),
    }


def threads(
    svc: Any, p: Principal, mb: MailboxConfig, folder: str | None, limit: int
) -> dict[str, Any]:
    """Recent conversations, grouped locally from the newest headers."""
    imap = _imap(mb)
    folder = folder or imap.default_folder
    limit = max(0, limit)
    count = min(MAX_SCAN, max(limit * 4, 1))
    with imap_client.session(imap) as s:
        messages = _scan(s, folder, count)
        sent = _sent_folder(s, mb)
        if sent and sent != folder:
            messages += _scan(s, sent, count)
    entries = [_thread_entry(g, sent) for g in group_messages(messages)]
    entries.sort(key=lambda e: e["_ts"], reverse=True)
    for e in entries:
        del e["_ts"]
    return {"threads": entries[:limit]}


# ── stats ────────────────────────────────────────────────────────────────


def _special_map(s: Session, mb: MailboxConfig) -> dict[str, str]:
    out: dict[str, str] = {}
    for kind in _KINDS:
        try:
            out[s.special_folder(kind, getattr(mb.folders, kind))] = kind
        except ImapError:
            continue
    return out


def stats(svc: Any, p: Principal, mb: MailboxConfig) -> dict[str, Any]:
    """Message/unseen/recent counts per selectable folder, plus quota."""
    imap = _imap(mb)
    with imap_client.session(imap) as s:
        special = _special_map(s, mb)
        folders: list[dict[str, Any]] = []
        for f in s.folders():
            flags = {x.lower() for x in f.get("flags", [])}
            if flags & {"\\noselect", "\\nonexistent"}:
                continue
            entry: dict[str, Any] = {"name": f["name"], "special": special.get(f["name"])}
            try:
                st = s.status(f["name"])
            except ImapError as e:
                entry["error"] = str(e)
                folders.append(entry)
                continue
            entry.update(
                messages=st.get("messages", 0),
                unseen=st.get("unseen", 0),
                recent=st.get("recent", 0),
            )
            folders.append(entry)
        quota = s.quota()
    return {"folders": folders, "quota": quota}


# ── deep health ──────────────────────────────────────────────────────────


def _timed(fn: Any) -> dict[str, Any]:
    start = time.monotonic()
    try:
        fn()
    except Exception as e:  # health checks report, never raise
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - start) * 1000),
            "error": str(e) or type(e).__name__,
        }
    return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000), "error": None}


def _check_imap(cfg: ImapConfig) -> None:
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=True)
        typ, _ = s.conn.noop()
        if typ != "OK":
            raise ImapError("NOOP failed")


def _check_mailbox(mb: MailboxConfig) -> dict[str, Any]:
    imap = mb.imap
    smtp = mb.smtp
    return {
        "name": mb.name,
        "imap": _timed(lambda: _check_imap(imap)) if imap is not None else None,
        "smtp": _timed(lambda: smtp_client.check(smtp)) if smtp is not None else None,
    }


def health_deep(svc: Any, p: Principal, mailboxes: list[MailboxConfig]) -> dict[str, Any]:
    if not mailboxes:
        return {"ok": True, "mailboxes": []}
    with ThreadPoolExecutor(max_workers=min(8, len(mailboxes))) as ex:
        results = list(ex.map(_check_mailbox, mailboxes))
    ok = all(all(r[k] is None or r[k]["ok"] for k in ("imap", "smtp")) for r in results)
    return {"ok": ok, "mailboxes": results}
