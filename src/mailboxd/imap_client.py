"""IMAP operations.

Connections are short-lived: opened per request and closed in a finally
block. Long-lived pooling would be nice but isn't worth the lifecycle
complexity for a control-plane API. UIDs (not sequence numbers) are used
everywhere so subsequent operations stay stable across mailbox mutations.

`session()` yields a `Session` so one request can run several commands on
one connection (read the original, file a copy, flag it answered, ...).
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, cast

import html2text

from . import mime
from .config import FoldersConfig, ImapConfig, MailboxConfig
from .errors import TooLarge

log = logging.getLogger(__name__)

_decode = mime.decode


def _html_to_reader(html: str) -> str:
    """Strip HTML chrome and return readable plain-text/markdown.

    Designed for email bodies: drops style/script, flattens tables, keeps
    links inline, ignores images. Output is wrapped at a sane width so
    long marketing-email lines don't blow out terminals.
    """
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = False
    h.single_line_break = False
    h.skip_internal_links = True
    h.unicode_snob = True
    return h.handle(html).strip()


class ImapError(Exception):
    """Raised when an IMAP operation fails."""


# IMAP command lines are CRLF-delimited and its atoms/quoted-strings cannot
# contain CR, LF or NUL. Python's imaplib passes caller-supplied arguments to
# the server verbatim (no sanitization), so a CR/LF smuggled into a
# folder/uid/search value would terminate the current command and inject an
# additional, attacker-chosen IMAP command onto the same authenticated
# connection (issue #1: command injection, incl. STORE+EXPUNGE through a
# read-only GET route). Reject those bytes at the boundary, before any value
# reaches imaplib.
_FORBIDDEN_CHARS = ("\r", "\n", "\x00")

# A UID (or sequence-set) is only ever digits and the range punctuation
# `,` `:` `*`. Anything else cannot be a valid UID and is refused outright,
# which also makes UID values injection-proof by construction.
_UID_RE = re.compile(r"^[0-9][0-9,:*]*$")
_KEYWORD_RE = re.compile(r"^\\?[A-Za-z0-9$_.\-]+$")

_HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"

# Header fetches are capped with a partial range and truncated rather than
# refused, so one message with absurd headers cannot break a listing or stall
# the watcher. Real header blocks are a few KiB.
_HEADER_FIELDS_MAX_BYTES = 64 * 1024
_HEADER_MAX_BYTES = 256 * 1024

DEFAULT_MESSAGE_MAX_BYTES = 32 * 1024 * 1024

# Fallback names when the server does not advertise SPECIAL-USE.
_SPECIAL_NAMES: dict[str, list[str]] = {
    "sent": ["Sent", "Sent Items", "Sent Messages", "INBOX.Sent", "Envoyés", "Sent Mail"],
    "drafts": ["Drafts", "INBOX.Drafts", "Brouillons", "Draft"],
    "trash": ["Trash", "Deleted Items", "Deleted Messages", "INBOX.Trash", "Corbeille", "Bin"],
    "junk": ["Junk", "Spam", "INBOX.Junk", "INBOX.Spam", "Junk E-mail", "Indésirables"],
}
_SPECIAL_FLAGS = {"sent": "\\sent", "drafts": "\\drafts", "trash": "\\trash", "junk": "\\junk"}
_special_cache: dict[tuple[str, int, str, str], tuple[float, str]] = {}
_SPECIAL_TTL = 600.0
_SOCKET_TIMEOUT_SECONDS = 30


def _reject_control_chars(value: str, field: str) -> None:
    if any(ch in value for ch in _FORBIDDEN_CHARS):
        raise ImapError(f"invalid {field}: control characters are not allowed")


def _validate_uid(uid: str) -> None:
    if not _UID_RE.match(uid):
        raise ImapError(f"invalid uid: {uid!r}")


def uid_set(uids: str | list[str] | list[int]) -> str:
    """Normalise a UID or list of UIDs to a validated IMAP UID set."""
    if isinstance(uids, str):
        value = uids
    else:
        value = ",".join(str(u) for u in uids)
    if not value:
        raise ImapError("no uid given")
    _validate_uid(value)
    return value


def quote_mailbox(folder: str) -> str:
    _reject_control_chars(folder, "folder")
    # Send the name as a properly-escaped IMAP quoted string so an embedded
    # `"` or `\` cannot break out of the quoting either.
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def imap_date(value: str) -> str:
    """Accept IMAP dates (1-Jan-2026) or ISO dates (2026-01-01)."""
    _reject_control_chars(value, "date")
    value = value.strip()
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        if not re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{4}", value):
            raise ImapError(f"invalid date {value!r}: use 2026-01-31 or 31-Jan-2026")
        return value
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{d.day}-{months[d.month - 1]}-{d.year}"


class _CappedIMAP4(imaplib.IMAP4):
    """Refuses any server literal over `max_literal` before allocating it.

    imaplib reads a `{N}` literal with one `read(N)`, sized by the server. A
    server that ignores a partial-fetch range could otherwise make the client
    allocate whatever it announces, whatever limit the request asked for.
    """

    max_literal = DEFAULT_MESSAGE_MAX_BYTES + 1

    def read(self, size: int) -> bytes:
        if size > self.max_literal:
            # The literal is still in flight, so the connection cannot be reused.
            self.shutdown()
            raise TooLarge("IMAP server sent more data than requested", limit=self.max_literal)
        return super().read(size)


class _CappedIMAP4_SSL(_CappedIMAP4, imaplib.IMAP4_SSL):
    pass


@contextmanager
def _connect(
    cfg: ImapConfig, message_max_bytes: int = DEFAULT_MESSAGE_MAX_BYTES
) -> Iterator[imaplib.IMAP4]:
    conn: _CappedIMAP4
    if cfg.tls == "ssl":
        conn = _CappedIMAP4_SSL(
            cfg.host,
            cfg.port,
            ssl_context=ssl.create_default_context(),
            timeout=_SOCKET_TIMEOUT_SECONDS,
        )
    else:
        conn = _CappedIMAP4(cfg.host, cfg.port, timeout=_SOCKET_TIMEOUT_SECONDS)
    # Header fetches may legitimately exceed a very small message limit.
    conn.max_literal = max(message_max_bytes + 1, _HEADER_MAX_BYTES)
    try:
        if cfg.tls == "starttls":
            conn.starttls(ssl_context=ssl.create_default_context())
        try:
            conn.login(cfg.username, cfg.password)
        except imaplib.IMAP4.error as e:
            raise ImapError(f"login failed: {e}") from e
        yield conn
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError) as error:
            log.warning("IMAP logout failed", extra={"error_type": type(error).__name__})
            try:
                conn.shutdown()
            except OSError as close_error:
                log.warning(
                    "IMAP socket close failed", extra={"error_type": type(close_error).__name__}
                )


_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.I
)


def _parse_list_line(raw: Any) -> dict[str, Any] | None:
    literal_name: str | None = None
    if isinstance(raw, tuple):
        head, lit = raw[0], raw[1]
        raw = head
        literal_name = lit.decode("utf-8", "replace") if isinstance(lit, bytes) else str(lit)
    if not raw:
        return None
    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    name = literal_name if literal_name is not None else m.group("name").strip()
    if literal_name is None and name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    delim = m.group("delim")
    return {
        "name": name,
        "delimiter": None if delim.upper() == "NIL" else re.sub(r"\\(.)", r"\1", delim[1:-1]),
        "flags": m.group("flags").split(),
    }


class Session:
    """One authenticated IMAP connection."""

    def __init__(
        self,
        conn: imaplib.IMAP4,
        cfg: ImapConfig,
        message_max_bytes: int = DEFAULT_MESSAGE_MAX_BYTES,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.message_max_bytes = message_max_bytes
        self._caps: set[str] | None = None
        self._folders: list[dict[str, Any]] | None = None
        self.selected: str | None = None
        self.readonly = True

    # ── capabilities / folders ───────────────────────────────────────────

    def caps(self) -> set[str]:
        if self._caps is None:
            caps: set[str] = set()
            try:
                typ, data = self.conn.capability()
                if typ == "OK" and data and isinstance(data[0], (bytes, str)):
                    raw = data[0].decode() if isinstance(data[0], bytes) else data[0]
                    caps = {c.upper() for c in raw.split()}
            except (imaplib.IMAP4.error, OSError, TypeError, ValueError):
                pass
            if not caps:
                try:
                    caps = {str(c).upper() for c in (self.conn.capabilities or ())}
                except TypeError:
                    caps = set()
            self._caps = caps
        return self._caps

    def has(self, cap: str) -> bool:
        return cap.upper() in self.caps()

    def folders(self) -> list[dict[str, Any]]:
        if self._folders is None:
            typ, data = self.conn.list()
            if typ != "OK":
                raise ImapError("LIST failed")
            out = []
            for raw in data or []:
                parsed = _parse_list_line(raw)
                if parsed and parsed["name"]:
                    out.append(parsed)
            self._folders = out
        return self._folders

    def special_folder(self, kind: str, override: str | None = None) -> str:
        if override:
            return override
        key = (self.cfg.host, self.cfg.port, self.cfg.username, kind)
        hit = _special_cache.get(key)
        if hit and time.monotonic() - hit[0] < _SPECIAL_TTL:
            return hit[1]
        folders = self.folders()
        flag = _SPECIAL_FLAGS[kind]
        found = next((f["name"] for f in folders if flag in [x.lower() for x in f["flags"]]), None)
        if found is None:
            by_lower = {f["name"].lower(): f["name"] for f in folders}
            for candidate in _SPECIAL_NAMES[kind]:
                found = by_lower.get(candidate.lower())
                if found:
                    break
            if found is None:
                for f in folders:  # e.g. "[Gmail]/Trash", "INBOX/Sent"
                    leaf = re.split(r"[./]", f["name"])[-1].lower()
                    if leaf in {c.lower() for c in _SPECIAL_NAMES[kind]}:
                        found = f["name"]
                        break
        if found is None:
            raise ImapError(f"no {kind} folder found; set folders.{kind} in the config")
        _special_cache[key] = (time.monotonic(), found)
        return found

    # ── selection / search / fetch ───────────────────────────────────────

    def select(self, folder: str, readonly: bool = True) -> str:
        """Select a folder; returns its UIDVALIDITY ('' when unknown)."""
        typ, _ = self.conn.select(quote_mailbox(folder), readonly=readonly)
        if typ != "OK":
            raise ImapError(f"could not select folder {folder!r}")
        self.selected, self.readonly = folder, readonly
        try:
            _, vals = self.conn.response("UIDVALIDITY")
            if vals and isinstance(vals[-1], (bytes, str)):
                v = vals[-1].decode() if isinstance(vals[-1], bytes) else vals[-1]
                return v.strip()
        except (imaplib.IMAP4.error, TypeError, ValueError, AttributeError):
            pass
        return ""

    def search(self, criteria: list[str | bytes] | str) -> list[int]:
        if isinstance(criteria, str):
            _reject_control_chars(criteria, "search")
            typ, data = self.conn.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        else:
            typ, data = self.conn.uid("SEARCH", *criteria)  # type: ignore[arg-type]
        if typ != "OK":
            raise ImapError(f"SEARCH failed: {data!r}")
        out = []
        for tok in (data[0] or b"").split() if data else []:
            if tok.isdigit():
                out.append(int(tok))
        return sorted(out)

    def fetch_headers(self, uids: list[int]) -> list[dict[str, Any]]:
        if not uids:
            return []
        typ, fdata = self.conn.uid(
            "FETCH",
            uid_set(uids),
            f"(BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})]<0.{_HEADER_FIELDS_MAX_BYTES}>"
            " FLAGS RFC822.SIZE)",
        )
        if typ != "OK":
            raise ImapError("FETCH headers failed")
        parsed = _parse_header_fetch(fdata)
        order = {str(u): i for i, u in enumerate(uids)}
        parsed.sort(key=lambda m: order.get(str(m.get("uid")), 0))
        return parsed

    def bodystructures(self, uids: list[int]) -> dict[int, str]:
        if not uids:
            return {}
        typ, data = self.conn.uid("FETCH", uid_set(uids), "(BODYSTRUCTURE)")
        if typ != "OK":
            raise ImapError("FETCH BODYSTRUCTURE failed")
        out: dict[int, str] = {}
        buf = ""
        for item in data or []:
            if isinstance(item, tuple):
                buf += "".join(x.decode("utf-8", "replace") for x in item if isinstance(x, bytes))
                continue
            if isinstance(item, bytes):
                buf += item.decode("utf-8", "replace")
            m = re.search(r"UID (\d+)", buf)
            if m:
                out[int(m.group(1))] = buf
            buf = ""
        return out

    def message_size(self, uid: str) -> int:
        _validate_uid(uid)
        typ, data = self.conn.uid("FETCH", uid, "(RFC822.SIZE)")
        if typ == "OK":
            for item in data or []:
                meta = item[0] if isinstance(item, tuple) else item
                if not isinstance(meta, bytes):
                    continue
                text = meta.decode("ascii", "replace")
                size = _extract(text, "RFC822.SIZE")
                if _extract(text, "UID") == uid and size.isdigit():
                    return int(size)
        raise ImapError(f"message UID {uid} not found in {self.selected!r}")

    def fetch_raw(self, uid: str, header_only: bool = False) -> bytes:
        _validate_uid(uid)
        if header_only:
            section = f"BODY.PEEK[HEADER]<0.{_HEADER_MAX_BYTES}>"
        else:
            limit = self.message_max_bytes
            size = self.message_size(uid)
            if size > limit:
                raise TooLarge("message exceeds the incoming size limit", limit=limit, size=size)
            # The extra byte catches a server that under-reported RFC822.SIZE;
            # the range keeps memory bounded either way.
            section = f"BODY.PEEK[]<0.{limit + 1}>"
        typ, data = self.conn.uid("FETCH", uid, f"({section})")
        if typ != "OK" or not data:
            raise ImapError(f"could not fetch UID {uid}")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                if not header_only and len(raw) > self.message_max_bytes:
                    raise TooLarge(
                        "message exceeds the incoming size limit", limit=self.message_max_bytes
                    )
                return raw
        raise ImapError(f"message UID {uid} not found in {self.selected!r}")

    def fetch_message(self, uid: str, header_only: bool = False) -> Message:
        return email.message_from_bytes(self.fetch_raw(uid, header_only=header_only))

    # ── mutations ────────────────────────────────────────────────────────

    def _writable(self) -> None:
        if self.selected is None or self.readonly:
            raise ImapError("internal: folder not selected read-write")

    def store(self, uids: str, op: str, flags: list[str]) -> None:
        self._writable()
        for f in flags:
            if not _KEYWORD_RE.match(f):
                raise ImapError(f"invalid flag {f!r}")
        typ, data = self.conn.uid("STORE", uid_set(uids), op, "(" + " ".join(flags) + ")")
        if typ != "OK":
            raise ImapError(f"STORE {op} failed: {data!r}")

    def expunge_uids(self, uids: str) -> bool:
        """Expunge exactly these UIDs (needs UIDPLUS). Never a folder-wide EXPUNGE."""
        self._writable()
        if not self.has("UIDPLUS"):
            return False
        typ, data = self.conn.uid("EXPUNGE", uid_set(uids))
        if typ != "OK":
            raise ImapError(f"UID EXPUNGE failed: {data!r}")
        return True

    def move(self, uids: str, dest: str) -> str:
        """Move UIDs from the selected folder to `dest`. Returns the method used."""
        self._writable()
        uids = uid_set(uids)
        if self.has("MOVE"):
            typ, data = self.conn.uid("MOVE", uids, quote_mailbox(dest))
            if typ != "OK":
                raise ImapError(f"UID MOVE failed: {data!r}")
            return "move"
        typ, data = self.conn.uid("COPY", uids, quote_mailbox(dest))
        if typ != "OK":
            raise ImapError(f"UID COPY failed: {data!r}")
        self.store(uids, "+FLAGS.SILENT", [r"\Deleted"])
        if self.expunge_uids(uids):
            return "copy+uid-expunge"
        # Without UIDPLUS a plain EXPUNGE would also purge unrelated messages
        # someone else flagged \Deleted, so the original stays flagged instead.
        return "copy+flagged-deleted"

    def append(self, folder: str, raw: bytes, flags: list[str]) -> str | None:
        """APPEND a message; returns its new UID when the server reports APPENDUID."""
        for f in flags:
            if not _KEYWORD_RE.match(f):
                raise ImapError(f"invalid flag {f!r}")
        # mypy 1.13's imaplib stub declares APPEND as str instead of the protocol tuple.
        typ, data = cast(
            tuple[str, list[Any]],
            self.conn.append(
                quote_mailbox(folder),
                "(" + " ".join(flags) + ")",
                imaplib.Time2Internaldate(time.time()),
                raw,
            ),
        )
        if typ != "OK":
            raise ImapError(f"APPEND to {folder!r} failed: {data!r}")
        for d in data or []:
            text = d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
            m = re.search(r"APPENDUID \d+ (\d+)", text, re.I)
            if m:
                return m.group(1)
        return None

    def create(self, folder: str) -> None:
        typ, data = self.conn.create(quote_mailbox(folder))
        if typ != "OK":
            raise ImapError(f"CREATE {folder!r} failed: {data!r}")
        self._folders = None
        try:
            self.conn.subscribe(quote_mailbox(folder))
        except imaplib.IMAP4.error:
            pass

    def status(self, folder: str) -> dict[str, int]:
        typ, data = self.conn.status(quote_mailbox(folder), "(MESSAGES UNSEEN RECENT)")
        if typ != "OK" or not data:
            raise ImapError(f"STATUS {folder!r} failed")
        raw = data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
        return {k.lower(): int(v) for k, v in re.findall(r"(MESSAGES|UNSEEN|RECENT) (\d+)", raw)}

    def quota(self) -> list[dict[str, Any]]:
        if not self.has("QUOTA"):
            return []
        try:
            typ, data = self.conn.getquotaroot(quote_mailbox("INBOX"))
        except imaplib.IMAP4.error:
            return []
        if typ != "OK":
            return []
        out = []
        for block in data or []:
            for item in block if isinstance(block, list) else [block]:
                text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                for res, used, limit in re.findall(r"(STORAGE|MESSAGE) (\d+) (\d+)", text):
                    out.append({"resource": res.lower(), "used": int(used), "limit": int(limit)})
        return out


@contextmanager
def session(
    cfg: ImapConfig, message_max_bytes: int = DEFAULT_MESSAGE_MAX_BYTES
) -> Iterator[Session]:
    with _connect(cfg, message_max_bytes) as conn:
        yield Session(conn, cfg, message_max_bytes)


def special_folder(cfg: ImapConfig, folders: FoldersConfig, kind: str) -> str:
    override = getattr(folders, kind)
    if override:
        return str(override)
    with session(cfg) as s:
        return s.special_folder(kind)


def list_folders(cfg: ImapConfig) -> list[str]:
    with session(cfg) as s:
        return [f["name"] for f in s.folders()]


def list_folders_detailed(cfg: ImapConfig) -> list[dict[str, Any]]:
    with session(cfg) as s:
        return s.folders()


def build_search_criteria(
    text: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unseen: bool | None = None,
    seen: bool | None = None,
    flagged: bool | None = None,
    answered: bool | None = None,
    larger_than: int | None = None,
    smaller_than: int | None = None,
    recipient: str | None = None,
    keyword: str | None = None,
    unkeyword: str | None = None,
    message_id: str | None = None,
) -> list[str | bytes]:
    """Translate a structured search request into an IMAP SEARCH criteria list.

    Returns a list of tokens ready to splat into `conn.uid("SEARCH", *tokens)`.
    Strings with non-ASCII content are encoded as IMAP `CHARSET UTF-8 ...`
    literal byte tokens — most modern servers accept this.
    """
    crit: list[str | bytes] = []

    def _q(value: str) -> str | bytes:
        _reject_control_chars(value, "search term")
        # imaplib sends `str` arguments as ASCII and `bytes` verbatim, so a
        # non-ASCII term has to be bytes -- and it must stay quoted, otherwise
        # a term containing a space would split into several SEARCH keys.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        if any(ord(c) > 127 for c in value):
            return b'"' + escaped.encode("utf-8") + b'"'
        return f'"{escaped}"'

    has_nonascii = False

    def _add_text(key: str, value: str) -> None:
        nonlocal has_nonascii
        if any(ord(c) > 127 for c in value):
            has_nonascii = True
        crit.append(key)
        crit.append(_q(value))

    if from_:
        _add_text("FROM", from_)
    if to:
        _add_text("TO", to)
    if recipient:
        # Matches To, Cc and the delivery headers, so contact@ vs support@
        # can be told apart even for Bcc / alias deliveries.
        _reject_control_chars(recipient, "recipient")
        q = _q(recipient)
        if isinstance(q, bytes):
            has_nonascii = True
        crit.extend(["OR", "OR", "OR", "TO", q, "CC", q])
        crit.extend(["HEADER", "Delivered-To", q, "HEADER", "X-Original-To", q])
    if subject:
        _add_text("SUBJECT", subject)
    if body:
        _add_text("BODY", body)
    if text:
        _add_text("TEXT", text)
    if message_id:
        _reject_control_chars(message_id, "message_id")
        crit.extend(["HEADER", "Message-ID", _q(message_id)])
    if since:
        crit.extend(["SINCE", imap_date(since)])
    if before:
        crit.extend(["BEFORE", imap_date(before)])
    if unseen:
        crit.append("UNSEEN")
    if seen:
        crit.append("SEEN")
    if flagged:
        crit.append("FLAGGED")
    if answered:
        crit.append("ANSWERED")
    if keyword:
        if not _KEYWORD_RE.match(keyword):
            raise ImapError(f"invalid keyword {keyword!r}")
        crit.extend(["KEYWORD", keyword])
    if unkeyword:
        if not _KEYWORD_RE.match(unkeyword):
            raise ImapError(f"invalid keyword {unkeyword!r}")
        crit.extend(["UNKEYWORD", unkeyword])
    if larger_than is not None:
        crit.extend(["LARGER", str(larger_than)])
    if smaller_than is not None:
        crit.extend(["SMALLER", str(smaller_than)])

    if not crit:
        crit.append("ALL")

    if has_nonascii:
        return ["CHARSET", "UTF-8", *crit]
    return crit


def _has_attachment(bodystructure: str) -> bool:
    s = bodystructure.upper()
    return '"ATTACHMENT"' in s or '"FILENAME"' in s


def search_page(
    cfg: ImapConfig,
    folder: str | None = None,
    limit: int = 50,
    criteria: list[str | bytes] | str = "ALL",
    before_uid: int | None = None,
    after_uid: int | None = None,
    has_attachments: bool | None = None,
) -> dict[str, Any]:
    """One page of matches, newest first.

    `before_uid` pages backwards (older mail). With `after_uid` the page holds
    the OLDEST `limit` matches newer than that UID, so a checkpoint moved to
    `next_after_uid` never skips mail (still listed newest first). `max_uid` is
    the highest UID of the folder search; `next_before_uid` is set when older
    matches remain.
    """
    folder = folder or cfg.default_folder
    _reject_control_chars(folder, "folder")
    if isinstance(criteria, str):
        _reject_control_chars(criteria, "search")
    with session(cfg) as s:
        uidvalidity = s.select(folder, readonly=True)
        uids = s.search(criteria)
        max_uid = uids[-1] if uids else (after_uid or 0)
        if after_uid is not None:
            uids = [u for u in uids if u > after_uid]
        if before_uid is not None:
            uids = [u for u in uids if u < before_uid]
        total = len(uids)
        limit = max(0, limit)
        # Scan order: oldest first when following a checkpoint, else newest first.
        order = uids if after_uid is not None else list(reversed(uids))
        if has_attachments is None:
            page = order[:limit]
            scanned_to = len(page)
        else:
            page, scanned = [], 0
            while len(page) < limit and scanned < len(order):
                chunk = order[scanned : scanned + 200]
                bs = s.bodystructures(chunk)
                for u in chunk:
                    scanned += 1
                    if _has_attachment(bs.get(u, "")) == has_attachments:
                        page.append(u)
                        if len(page) >= limit:
                            break
            scanned_to = scanned
            total = -1  # unknown without scanning everything
        more = scanned_to < len(order)
        messages = s.fetch_headers(sorted(page, reverse=True))
    if after_uid is not None:
        if more:
            next_after = order[scanned_to - 1] if scanned_to else after_uid
        else:
            next_after = max(max_uid, after_uid)
    else:
        next_after = max_uid
    return {
        "messages": messages,
        "total": total,
        "next_before_uid": (page[-1] if page and more else None) if after_uid is None else None,
        "next_after_uid": next_after,
        "more_new": bool(more and after_uid is not None),
        "uidvalidity": uidvalidity,
        "max_uid": max_uid,
        "folder": folder,
    }


def list_messages(
    cfg: ImapConfig,
    folder: str | None = None,
    limit: int = 50,
    search: str = "ALL",
) -> list[dict[str, Any]]:
    return search_page(cfg, folder=folder, limit=limit, criteria=search)["messages"]


def search_messages(
    cfg: ImapConfig,
    folder: str | None = None,
    limit: int = 50,
    **criteria: Any,
) -> list[dict[str, Any]]:
    """Structured search inside one IMAP folder.

    Accepts the same keyword arguments as `build_search_criteria`.
    """
    spec = build_search_criteria(**criteria)
    return search_page(cfg, folder=folder, limit=limit, criteria=spec)["messages"]


def _parse_header_fetch(fdata: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # imaplib returns attributes after a literal as a separate bytes item.
    records: list[tuple[Any, Any]] = []
    pending: int | None = None
    for item in fdata:
        if isinstance(item, tuple) and len(item) >= 2:
            records.append((item[0], item[1]))
            pending = len(records) - 1
        elif isinstance(item, bytes) and pending is not None:
            # Never attach a separate numbered unsolicited FETCH response.
            if re.match(rb"^\s*\d+\s+\(", item):
                pending = None
                continue
            meta_raw, body_raw = records[pending]
            if isinstance(meta_raw, bytes):
                records[pending] = (meta_raw + b" " + item, body_raw)
            pending = None
    for meta_raw, body_raw in records:
        meta = (
            meta_raw.decode("utf-8", errors="replace")
            if isinstance(meta_raw, bytes)
            else str(meta_raw)
        )
        uid = _extract(meta, "UID")
        size = _extract(meta, "RFC822.SIZE")
        flags = _extract_flags(meta)
        msg = email.message_from_bytes(
            body_raw if isinstance(body_raw, bytes) else body_raw.encode()
        )
        out.append(
            {
                "uid": uid,
                "size": int(size) if size and size.isdigit() else None,
                "flags": flags,
                "from": _decode(msg.get("From")),
                "to": _decode(msg.get("To")),
                "cc": _decode(msg.get("Cc")),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
                "message_id": _decode(msg.get("Message-ID")),
                "in_reply_to": _decode(msg.get("In-Reply-To")),
                "references": mime.message_ids(_decode(msg.get("References"))),
            }
        )
    return out


def _extract(meta: str, key: str) -> str:
    # Crude but reliable: find `KEY VALUE` token in the metadata string.
    parts = meta.replace("(", " ").replace(")", " ").split()
    for i, p in enumerate(parts):
        if p == key and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_flags(meta: str) -> list[str]:
    i = meta.find("FLAGS")
    if i < 0:
        return []
    j = meta.find("(", i)
    k = meta.find(")", j)
    if j < 0 or k < 0:
        return []
    return meta[j + 1 : k].split()


def fetch_raw(cfg: ImapConfig, uid: str, folder: str | None = None) -> bytes:
    folder = folder or cfg.default_folder
    _validate_uid(uid)
    _reject_control_chars(folder, "folder")
    with session(cfg) as s:
        s.select(folder, readonly=True)
        return s.fetch_raw(uid)


def fetch_message(
    cfg: ImapConfig,
    uid: str,
    folder: str | None = None,
    reader: bool = False,
) -> dict[str, Any]:
    raw = fetch_raw(cfg, uid, folder=folder)
    return serialize_message(email.message_from_bytes(raw), uid=uid, reader=reader)


def serialize_message(
    msg: Message,
    uid: str | None = None,
    reader: bool = False,
) -> dict[str, Any]:
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[dict[str, Any]] = []

    for part_id, part in mime.iter_parts(msg):
        if mime.is_attachment(part):
            attachments.append(mime.attachment_info(part_id, part))
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and body_text is None:
            body_text = _payload_str(part)
        elif ctype == "text/html" and body_html is None:
            body_html = _payload_str(part)

    body_reader: str | None = None
    if reader:
        if body_html:
            body_reader = _html_to_reader(body_html)
        elif body_text:
            body_reader = body_text.strip() or None

    return {
        "uid": uid,
        "from": _decode(msg.get("From")),
        "to": _decode(msg.get("To")),
        "cc": _decode(msg.get("Cc")),
        "reply_to": _decode(msg.get("Reply-To")),
        "subject": _decode(msg.get("Subject")),
        "date": _decode(msg.get("Date")),
        "message_id": _decode(msg.get("Message-ID")),
        "in_reply_to": _decode(msg.get("In-Reply-To")),
        "references": mime.message_ids(_decode(msg.get("References"))),
        "body_text": body_text,
        "body_html": body_html,
        "body_reader": body_reader,
        "attachments": attachments,
    }


def _payload_str(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def unified_search(
    mailboxes: list[MailboxConfig],
    folder: str | None = None,
    limit: int = 50,
    after_uids: dict[str, int] | None = None,
    **criteria: Any,
) -> dict[str, Any]:
    """Fan-out search across multiple IMAP mailboxes.

    Each result message is tagged with `mailbox` (config name) and
    `mailbox_address` (IMAP login / email) so the caller can tell where
    it came from. Per-mailbox errors are collected in `errors` rather
    than aborting the whole call. `after_uids` maps mailbox name to the
    last UID already seen; per-mailbox `uidvalidity`/`max_uid` come back
    in `positions` so the caller can move a checkpoint forward.
    """
    targets = [mb for mb in mailboxes if mb.imap is not None]
    if not targets:
        return {"messages": [], "errors": [], "positions": {}}
    spec = build_search_criteria(**criteria)

    def _one(mb: MailboxConfig) -> tuple[str, dict[str, Any] | None, str | None]:
        imap = mb.imap
        if imap is None:
            return mb.name, None, "IMAP not configured"
        try:
            page = search_page(
                imap,
                folder=folder,
                limit=limit,
                criteria=spec,
                after_uid=(after_uids or {}).get(mb.name),
            )
        except (ImapError, OSError, imaplib.IMAP4.error) as e:
            return mb.name, None, str(e)
        for m in page["messages"]:
            m["mailbox"] = mb.name
            m["mailbox_address"] = imap.username
        return mb.name, page, None

    merged: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    positions: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
        for name, page, err in ex.map(_one, targets):
            if err is not None or page is None:
                errors.append({"mailbox": name, "error": err or "unknown error"})
                continue
            merged.extend(page["messages"])
            positions[name] = {
                "uidvalidity": page["uidvalidity"],
                "max_uid": page["max_uid"],
                "next_after_uid": page["next_after_uid"],
                "folder": page["folder"],
            }

    merged.sort(key=message_timestamp, reverse=True)
    return {"messages": merged[: max(0, limit)], "errors": errors, "positions": positions}


def message_timestamp(m: dict[str, Any]) -> float:
    try:
        return parsedate_to_datetime(m.get("date") or "").timestamp()
    except (TypeError, ValueError):
        return 0.0


def store_flags(
    cfg: ImapConfig,
    uids: str | list[str],
    folder: str | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    folder = folder or cfg.default_folder
    uids = uid_set(uids)
    _reject_control_chars(folder, "folder")
    with session(cfg) as s:
        s.select(folder, readonly=False)
        if add:
            s.store(uids, "+FLAGS", add)
        if remove:
            s.store(uids, "-FLAGS", remove)


def mark_seen(
    cfg: ImapConfig, uid: str | list[str], folder: str | None = None, seen: bool = True
) -> None:
    if seen:
        store_flags(cfg, uid, folder=folder, add=[r"\Seen"])
    else:
        store_flags(cfg, uid, folder=folder, remove=[r"\Seen"])
