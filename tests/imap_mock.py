"""A tiny in-process IMAP4 server for tests.

Hand-rolled on `socketserver` (same approach as the SMTP capture sink in
`test_smtp_integration.py`) rather than a container, so the suite has no
external moving parts and starts in milliseconds.

Two modes of use:

* legacy: `start({1: raw, 2: raw})` -> a bare IMAP4rev1 server with one INBOX
  and no extensions (what the injection tests rely on);
* modern: `start(msgs, caps={"MOVE", "UIDPLUS", "SPECIAL-USE"}, folders={...})`
  -> several folders with per-message flags, UIDVALIDITY, LIST special-use
  attributes, UID COPY/MOVE/EXPUNGE, STORE that really updates flags, APPEND
  with APPENDUID, STATUS, CREATE.

`folders` maps a folder name to either a plain `{uid: raw}` dict or a spec
dict `{"messages": {uid: raw}, "flags": {uid: ["\\\\Seen"]}, "attrs": ["\\\\Sent"],
"uidvalidity": 7, "literal": True}`. When SPECIAL-USE is advertised and `attrs`
is omitted, the attribute is derived from the usual names (Sent, Drafts, ...).

Every command line the client sends is recorded verbatim on `Sink.commands`,
which is what lets a test assert that a crafted folder/uid/search value did
NOT smuggle an extra IMAP command onto the connection. A folder-wide
(non-UID) EXPUNGE is additionally recorded on `Sink.plain_expunges` so tests
can assert it never happens.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import re
import socket
import socketserver
import threading
from typing import Any, Iterator

MODERN_CAPS = frozenset({"MOVE", "UIDPLUS", "SPECIAL-USE"})

_SPECIAL_BY_NAME = {
    "sent": "\\Sent",
    "sent items": "\\Sent",
    "drafts": "\\Drafts",
    "trash": "\\Trash",
    "junk": "\\Junk",
    "spam": "\\Junk",
}


class Folder:
    def __init__(
        self,
        name: str,
        messages: dict[int, bytes] | None = None,
        flags: dict[int, list[str]] | None = None,
        attrs: list[str] | None = None,
        uidvalidity: int = 1,
        literal: bool = False,
    ) -> None:
        self.name = name
        self.msgs: dict[int, bytes] = messages if messages is not None else {}
        self.flags: dict[int, set[str]] = {u: set((flags or {}).get(u, [])) for u in self.msgs}
        self.attrs = attrs
        self.uidvalidity = uidvalidity
        self.literal = literal
        self.next_uid = max(self.msgs, default=0) + 1

    def add(self, raw: bytes, flags: list[str] | set[str] | tuple[str, ...] = ()) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.msgs[uid] = raw
        self.flags[uid] = set(flags)
        return uid

    def remove(self, uid: int) -> None:
        self.msgs.pop(uid, None)
        self.flags.pop(uid, None)


class Sink:
    """Shared state between the test and the mock server."""

    def __init__(
        self,
        messages: dict[int, bytes],
        caps: set[str] | frozenset[str] | None = None,
        folders: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> None:
        self.caps: set[str] = {c.upper() for c in (caps or ())}
        # Strict servers answer NO to SELECT of an unknown folder; the legacy
        # mock silently created it, which the injection tests rely on.
        self.strict = strict
        self.lock = threading.RLock()
        # Every command line received, tag included, CRLF stripped.
        self.commands: list[str] = []
        # Folders on which a plain (non-UID) EXPUNGE was issued.
        self.plain_expunges: list[str] = []
        self.uid_expunges: list[tuple[str, str]] = []
        # (folder, uid, flags, raw) for every APPEND.
        self.appended: list[tuple[str, int, set[str], bytes]] = []
        self.folders: dict[str, Folder] = {}
        self.folders["INBOX"] = Folder("INBOX", messages)
        # uid -> raw RFC822 bytes of the INBOX (same dict object, kept for legacy tests).
        self.messages = self.folders["INBOX"].msgs
        for name, spec in (folders or {}).items():
            self.folders[name] = self._build_folder(name, spec)

    def _build_folder(self, name: str, spec: Any) -> Folder:
        if name == "INBOX":
            folder = self.folders["INBOX"]
            if isinstance(spec, dict) and "messages" in spec:
                folder.msgs.update(spec["messages"])
                folder.flags = {u: set(spec.get("flags", {}).get(u, [])) for u in folder.msgs}
                folder.next_uid = max(folder.msgs, default=0) + 1
            elif isinstance(spec, dict):
                folder.msgs.update(spec)
                folder.flags = {u: set() for u in folder.msgs}
                folder.next_uid = max(folder.msgs, default=0) + 1
            return folder
        if isinstance(spec, dict) and not any(isinstance(k, int) for k in spec):
            attrs = spec.get("attrs")
            if attrs is None and "SPECIAL-USE" in self.caps:
                derived = _SPECIAL_BY_NAME.get(name.lower())
                attrs = [derived] if derived else []
            return Folder(
                name,
                dict(spec.get("messages", {})),
                spec.get("flags"),
                attrs,
                spec.get("uidvalidity", 1),
                spec.get("literal", False),
            )
        attrs = None
        if "SPECIAL-USE" in self.caps:
            derived = _SPECIAL_BY_NAME.get(name.lower())
            attrs = [derived] if derived else []
        return Folder(name, dict(spec), None, attrs)

    # Convenience accessors for assertions ---------------------------------

    def flags(self, uid: int, folder: str = "INBOX") -> set[str]:
        return set(self.folders[folder].flags.get(uid, set()))

    def uids(self, folder: str = "INBOX") -> list[int]:
        return sorted(self.folders[folder].msgs)

    def commands_after_login(self) -> list[str]:
        """Command lines with the connection-setup noise removed."""
        skip = ("CAPABILITY", "LOGIN", "LOGOUT")
        out = []
        for line in self.commands:
            parts = line.split(" ", 2)
            verb = parts[1].upper() if len(parts) > 1 else ""
            if verb in skip:
                continue
            out.append(line)
        return out

    def verbs(self) -> list[str]:
        """Upper-cased command verbs (for UID commands: `UID FETCH` etc.)."""
        out = []
        for line in self.commands:
            parts = line.split(" ", 3)
            if len(parts) < 2:
                continue
            verb = parts[1].upper()
            if verb == "UID" and len(parts) > 2:
                verb = "UID " + parts[2].upper()
            out.append(verb)
        return out


# ── tokenizer / helpers ──────────────────────────────────────────────────


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tokenize(text: str) -> list[Any]:
    """Split an IMAP argument string into atoms/strings and nested lists."""
    pos = 0
    stack: list[list[Any]] = [[]]
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == " ":
            pos += 1
        elif ch == "(":
            new: list[Any] = []
            stack[-1].append(new)
            stack.append(new)
            pos += 1
        elif ch == ")":
            if len(stack) > 1:
                stack.pop()
            pos += 1
        elif ch == '"':
            pos += 1
            buf = []
            while pos < n and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < n:
                    pos += 1
                buf.append(text[pos])
                pos += 1
            pos += 1
            stack[-1].append("".join(buf))
        else:
            start = pos
            depth = 0
            while pos < n and (depth > 0 or text[pos] not in " ()"):
                if text[pos] == "[":
                    depth += 1
                elif text[pos] == "]":
                    depth = max(0, depth - 1)
                pos += 1
            stack[-1].append(text[start:pos])
    return stack[0]


def _expand_uid_set(spec: str, uids: list[int]) -> list[int]:
    top = max(uids, default=0)
    wanted: set[int] = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        lo_s, _, hi_s = tok.partition(":")

        def conv(s: str) -> int:
            return top if s == "*" else int(s)

        try:
            lo = conv(lo_s)
            hi = conv(hi_s) if hi_s else lo
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        wanted.update(u for u in uids if lo <= u <= hi)
    return sorted(wanted)


def _has_flag(flags: set[str], name: str) -> bool:
    return name.lower() in {f.lower() for f in flags}


def _split_headers(raw: bytes) -> tuple[bytes, bytes]:
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = raw.find(sep)
        if idx >= 0:
            return raw[:idx], raw[idx + len(sep) :]
    return raw, b""


def _header_entries(raw: bytes) -> list[tuple[str, bytes]]:
    head, _ = _split_headers(raw)
    entries: list[tuple[str, list[bytes]]] = []
    for line in head.splitlines():
        if line[:1] in (b" ", b"\t") and entries:
            entries[-1][1].append(line)
        elif b":" in line:
            entries.append((line.split(b":", 1)[0].decode("latin-1").lower(), [line]))
    return [(name, b"\r\n".join(lines) + b"\r\n") for name, lines in entries]


def _header_block(raw: bytes, fields: list[str] | None) -> bytes:
    entries = _header_entries(raw)
    if fields is not None:
        wanted = {f.lower() for f in fields}
        entries = [e for e in entries if e[0] in wanted]
    return b"".join(e[1] for e in entries) + b"\r\n"


def _bodystructure(part: email.message.Message) -> str:
    if part.is_multipart():
        subs = "".join(_bodystructure(p) for p in part.get_payload())  # type: ignore[union-attr]
        return f"({subs} {_quote(part.get_content_subtype().upper())})"
    maintype = part.get_content_maintype().upper()
    subtype = part.get_content_subtype().upper()
    charset = part.get_content_charset()
    params = f"({_quote('CHARSET')} {_quote(charset)})" if charset else "NIL"
    payload = part.get_payload(decode=True)
    size = len(payload) if isinstance(payload, bytes) else 0
    filename = part.get_filename()
    disp_kind = part.get_content_disposition()
    if disp_kind or filename:
        fn = f" ({_quote('FILENAME')} {_quote(filename)})" if filename else " NIL"
        disp = f"({_quote((disp_kind or 'attachment').upper())}{fn})"
    else:
        disp = "NIL"
    extra = f" {size // 40 + 1}" if maintype == "TEXT" else ""
    return (
        f"({_quote(maintype)} {_quote(subtype)} {params} NIL NIL "
        f'"7BIT" {size}{extra} NIL {disp} NIL)'
    )


# ── SEARCH ───────────────────────────────────────────────────────────────

_ONE_ARG = {
    "FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT", "KEYWORD", "UNKEYWORD",
    "SINCE", "BEFORE", "ON", "SENTSINCE", "SENTBEFORE", "LARGER", "SMALLER",
}  # fmt: skip
_FLAG_KEYS = {
    "SEEN": ("\\Seen", True),
    "UNSEEN": ("\\Seen", False),
    "FLAGGED": ("\\Flagged", True),
    "UNFLAGGED": ("\\Flagged", False),
    "ANSWERED": ("\\Answered", True),
    "UNANSWERED": ("\\Answered", False),
    "DELETED": ("\\Deleted", True),
    "UNDELETED": ("\\Deleted", False),
    "DRAFT": ("\\Draft", True),
    "UNDRAFT": ("\\Draft", False),
}


class _Ctx:
    def __init__(self, folder: Folder, uid: int) -> None:
        self.uid = uid
        self.raw = folder.msgs[uid]
        self.flags = folder.flags.get(uid, set())
        self.uids = sorted(folder.msgs)


def _parse_term(tokens: list[Any], i: int) -> tuple[Any, int]:
    """Parse one search term starting at tokens[i]; unknown terms match everything."""
    tok = tokens[i]
    if isinstance(tok, list):
        preds = _parse_all(tok)
        return (lambda c: all(p(c) for p in preds)), i + 1
    key = str(tok).upper()
    if re.fullmatch(r"[0-9*][0-9,:*]*", str(tok)):
        spec = str(tok)
        return (lambda c: c.uid in _expand_uid_set(spec, c.uids)), i + 1
    if key == "CHARSET":
        return (lambda c: True), min(i + 2, len(tokens))
    if key == "ALL":
        return (lambda c: True), i + 1
    if key == "OR" and i + 1 < len(tokens):
        a, j = _parse_term(tokens, i + 1)
        b, k = _parse_term(tokens, j) if j < len(tokens) else ((lambda c: True), j)
        return (lambda c: a(c) or b(c)), k
    if key == "NOT" and i + 1 < len(tokens):
        a, j = _parse_term(tokens, i + 1)
        return (lambda c: not a(c)), j
    if key in _FLAG_KEYS:
        flag, want = _FLAG_KEYS[key]
        return (lambda c: _has_flag(c.flags, flag) == want), i + 1
    if key == "KEYWORD" and i + 1 < len(tokens):
        kw = str(tokens[i + 1])
        return (lambda c: _has_flag(c.flags, kw)), i + 2
    if key == "UNKEYWORD" and i + 1 < len(tokens):
        kw = str(tokens[i + 1])
        return (lambda c: not _has_flag(c.flags, kw)), i + 2
    if key == "UID" and i + 1 < len(tokens):
        spec = str(tokens[i + 1])
        return (lambda c: c.uid in _expand_uid_set(spec, c.uids)), i + 2
    if key == "HEADER" and i + 2 < len(tokens):
        name, needle = str(tokens[i + 1]).lower(), str(tokens[i + 2]).lower()

        def hdr(c: _Ctx) -> bool:
            for hname, line in _header_entries(c.raw):
                if hname == name:
                    value = line.split(b":", 1)[1].decode("utf-8", "replace").lower()
                    if needle in value:
                        return True
            return False

        return hdr, i + 3
    if key in ("FROM", "TO", "CC", "BCC", "SUBJECT") and i + 1 < len(tokens):
        name, needle = key.lower(), str(tokens[i + 1]).lower()

        def field(c: _Ctx) -> bool:
            return any(
                h == name and needle in ln.decode("utf-8", "replace").lower()
                for h, ln in _header_entries(c.raw)
            )

        return field, i + 2
    if key in ("BODY", "TEXT") and i + 1 < len(tokens):
        needle = str(tokens[i + 1]).lower()
        return (lambda c: needle in c.raw.decode("utf-8", "replace").lower()), i + 2
    if key == "LARGER" and i + 1 < len(tokens):
        n = int(tokens[i + 1])
        return (lambda c: len(c.raw) > n), i + 2
    if key == "SMALLER" and i + 1 < len(tokens):
        n = int(tokens[i + 1])
        return (lambda c: len(c.raw) < n), i + 2
    if key in _ONE_ARG:
        return (lambda c: True), min(i + 2, len(tokens))
    return (lambda c: True), i + 1


def _parse_all(tokens: list[Any]) -> list[Any]:
    preds = []
    i = 0
    while i < len(tokens):
        pred, i = _parse_term(tokens, i)
        preds.append(pred)
    return preds


# ── handler ──────────────────────────────────────────────────────────────

_FETCH_ITEM_RE = re.compile(
    r"BODY(?:\.PEEK)?\[[^\]]*\]|BODYSTRUCTURE|RFC822\.SIZE|RFC822\.HEADER|RFC822|FLAGS|UID"
    r"|INTERNALDATE|ENVELOPE",
    re.I,
)


def _make_handler(sink: Sink) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        selected: Folder | None = None
        readonly = True

        def _send(self, line: str) -> None:
            self.wfile.write((line + "\r\n").encode())

        def _ok(self, tag: str, text: str) -> None:
            self._send(f"{tag} OK {text}")

        def _cap_line(self) -> str:
            return " ".join(["IMAP4rev1", *sorted(sink.caps)])

        def _read_command(self) -> tuple[str, list[bytes]] | None:
            raw = self.rfile.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            sink.commands.append(line)
            full, literals = line, []
            while True:
                m = re.search(r"\{(\d+)(\+?)\}$", full)
                if not m:
                    break
                size = int(m.group(1))
                if not m.group(2):
                    self.wfile.write(b"+ go ahead\r\n")
                data = self.rfile.read(size)
                rest = self.rfile.readline().decode("utf-8", errors="replace").rstrip("\r\n")
                if rest:
                    sink.commands.append(rest)
                literals.append(data)
                full = full[: m.start()] + _quote(data.decode("utf-8", "replace")) + rest
            return full, literals

        def handle(self) -> None:
            self._send(f"* OK [CAPABILITY {self._cap_line()}] mock ready")
            while True:
                got = self._read_command()
                if got is None:
                    return
                line, literals = got
                parts = line.split(" ", 2)
                if len(parts) < 2:
                    continue
                tag, cmd = parts[0], parts[1].upper()
                rest = parts[2] if len(parts) > 2 else ""
                try:
                    with sink.lock:
                        if self._dispatch(tag, cmd, rest, literals):
                            return
                except (ValueError, IndexError, KeyError):
                    self._send(f"{tag} BAD mock could not parse the command")

        # ── dispatch ────────────────────────────────────────────────────

        def _folder(self, name: str) -> Folder | None:
            if name.upper() == "INBOX":
                name = "INBOX"
            return sink.folders.get(name)

        def _dispatch(self, tag: str, cmd: str, rest: str, literals: list[bytes]) -> bool:
            """Handle one command. Returns True when the connection should close."""
            if cmd == "CAPABILITY":
                self._send(f"* CAPABILITY {self._cap_line()}")
                self._ok(tag, "CAPABILITY completed")
            elif cmd == "LOGIN":
                self._ok(tag, "LOGIN completed")
            elif cmd in ("EXAMINE", "SELECT"):
                self._select(tag, cmd, rest)
            elif cmd == "LIST":
                self._list(tag)
            elif cmd == "UID":
                self._handle_uid(tag, rest)
            elif cmd == "EXPUNGE":
                self._plain_expunge(tag)
            elif cmd == "APPEND":
                self._append(tag, rest, literals)
            elif cmd == "CREATE":
                self._create(tag, rest)
            elif cmd == "STATUS":
                self._status(tag, rest)
            elif cmd == "NOOP":
                self._ok(tag, "NOOP completed")
            elif cmd == "LOGOUT":
                self._send("* BYE mock logging out")
                self._ok(tag, "LOGOUT completed")
                return True
            else:
                self._ok(tag, f"{cmd} completed")
            return False

        def _select(self, tag: str, cmd: str, rest: str) -> None:
            name = str(_tokenize(rest)[0])
            folder = self._folder(name)
            if folder is None:
                if sink.strict:
                    self._send(f"{tag} NO [NONEXISTENT] no such mailbox")
                    return
                folder = sink.folders.setdefault(name, Folder(name))
            self.selected, self.readonly = folder, cmd == "EXAMINE"
            mode = "READ-ONLY" if self.readonly else "READ-WRITE"
            self._send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
            self._send(f"* {len(folder.msgs)} EXISTS")
            self._send("* 0 RECENT")
            self._send(f"* OK [UIDVALIDITY {folder.uidvalidity}] UIDs valid")
            self._send(f"* OK [UIDNEXT {folder.next_uid}] predicted next UID")
            self._ok(tag, f"[{mode}] {cmd} completed")

        def _list(self, tag: str) -> None:
            for name, folder in sink.folders.items():
                attrs = ["\\HasNoChildren", *(folder.attrs or [])]
                head = f"* LIST ({' '.join(attrs)}) \"/\" "
                if folder.literal:
                    data = name.encode()
                    self.wfile.write(head.encode() + b"{%d}\r\n" % len(data) + data + b"\r\n")
                else:
                    self._send(head + _quote(name))
            self._ok(tag, "LIST completed")

        def _create(self, tag: str, rest: str) -> None:
            name = str(_tokenize(rest)[0])
            if name in sink.folders:
                self._send(f"{tag} NO [ALREADYEXISTS] mailbox exists")
                return
            attrs = []
            if "SPECIAL-USE" in sink.caps and _SPECIAL_BY_NAME.get(name.lower()):
                attrs = [_SPECIAL_BY_NAME[name.lower()]]
            sink.folders[name] = Folder(name, attrs=attrs)
            self._ok(tag, "CREATE completed")

        def _status(self, tag: str, rest: str) -> None:
            tokens = _tokenize(rest)
            folder = self._folder(str(tokens[0]))
            if folder is None:
                self._send(f"{tag} NO [NONEXISTENT] no such mailbox")
                return
            unseen = sum(1 for u in folder.msgs if not _has_flag(folder.flags[u], "\\Seen"))
            self._send(
                f"* STATUS {_quote(folder.name)} (MESSAGES {len(folder.msgs)} "
                f"UNSEEN {unseen} RECENT 0 UIDNEXT {folder.next_uid} "
                f"UIDVALIDITY {folder.uidvalidity})"
            )
            self._ok(tag, "STATUS completed")

        def _append(self, tag: str, rest: str, literals: list[bytes]) -> None:
            tokens = _tokenize(rest)
            folder = self._folder(str(tokens[0]))
            if folder is None:
                self._send(f"{tag} NO [TRYCREATE] no such mailbox")
                return
            flags: list[str] = []
            if len(tokens) > 1 and isinstance(tokens[1], list):
                flags = [str(f) for f in tokens[1]]
            uid = folder.add(literals[-1], flags)
            sink.appended.append((folder.name, uid, set(flags), literals[-1]))
            if "UIDPLUS" in sink.caps:
                self._ok(tag, f"[APPENDUID {folder.uidvalidity} {uid}] APPEND completed")
            else:
                self._ok(tag, "APPEND completed")

        def _require_writable(self, tag: str) -> Folder | None:
            if self.selected is None:
                self._send(f"{tag} BAD no mailbox selected")
                return None
            if self.readonly:
                self._send(f"{tag} NO [READ-ONLY] mailbox selected read-only")
                return None
            return self.selected

        def _seq(self, folder: Folder, uid: int) -> int:
            return sorted(folder.msgs).index(uid) + 1

        def _plain_expunge(self, tag: str) -> None:
            folder = self._require_writable(tag)
            if folder is None:
                return
            sink.plain_expunges.append(folder.name)
            self._expunge(folder, [u for u in sorted(folder.msgs)])
            self._ok(tag, "EXPUNGE completed")

        def _expunge(self, folder: Folder, candidates: list[int]) -> None:
            for uid in candidates:
                if uid in folder.msgs and _has_flag(folder.flags[uid], "\\Deleted"):
                    seq = self._seq(folder, uid)
                    folder.remove(uid)
                    self._send(f"* {seq} EXPUNGE")

        def _handle_uid(self, tag: str, rest: str) -> None:
            sub, _, args = rest.partition(" ")
            sub = sub.upper()
            if self.selected is None and sub in {"SEARCH", "FETCH", "STORE", "COPY", "MOVE"}:
                # Legacy leniency: behave as if INBOX were selected.
                self.selected = sink.folders["INBOX"]
            if sub == "SEARCH":
                self._search(tag, args)
            elif sub == "FETCH":
                self._handle_fetch(tag, args)
            elif sub == "STORE":
                self._store(tag, args)
            elif sub in ("COPY", "MOVE"):
                self._copy_move(tag, sub, args)
            elif sub == "EXPUNGE":
                self._uid_expunge(tag, args)
            else:
                self._ok(tag, f"UID {sub} completed")

        def _search(self, tag: str, args: str) -> None:
            folder = self.selected or sink.folders["INBOX"]
            preds = _parse_all(_tokenize(args))
            hits = [
                u for u in sorted(folder.msgs) if all(p(_Ctx(folder, u)) for p in preds)
            ]
            self._send(("* SEARCH " + " ".join(str(u) for u in hits)).rstrip())
            self._ok(tag, "UID SEARCH completed")

        def _store(self, tag: str, args: str) -> None:
            folder = self._require_writable(tag)
            if folder is None:
                return
            tokens = _tokenize(args)
            uids = _expand_uid_set(str(tokens[0]), sorted(folder.msgs))
            mode = str(tokens[1]).upper()
            raw_flags = tokens[2] if isinstance(tokens[2], list) else [tokens[2]]
            flags = {str(f) for f in raw_flags}
            silent = mode.endswith(".SILENT")
            op = mode.replace(".SILENT", "")
            for uid in uids:
                cur = folder.flags[uid]
                if op == "+FLAGS":
                    cur |= flags
                elif op == "-FLAGS":
                    low = {f.lower() for f in flags}
                    cur -= {f for f in cur if f.lower() in low}
                elif op == "FLAGS":
                    cur.clear()
                    cur |= flags
                if not silent:
                    seq = self._seq(folder, uid)
                    self._send(f"* {seq} FETCH (UID {uid} FLAGS ({' '.join(sorted(cur))}))")
            self._ok(tag, "UID STORE completed")

        def _copy_move(self, tag: str, sub: str, args: str) -> None:
            if sub == "MOVE" and "MOVE" not in sink.caps:
                self._send(f"{tag} BAD UID MOVE not supported")
                return
            src = self.selected or sink.folders["INBOX"]
            if sub == "MOVE" and self._require_writable(tag) is None:
                return
            tokens = _tokenize(args)
            dest = self._folder(str(tokens[1]))
            if dest is None:
                self._send(f"{tag} NO [TRYCREATE] no such mailbox")
                return
            uids = _expand_uid_set(str(tokens[0]), sorted(src.msgs))
            mapping = [
                (u, dest.add(src.msgs[u], set(src.flags[u]) - {"\\Deleted"})) for u in uids
            ]
            code = ""
            if "UIDPLUS" in sink.caps and mapping:
                a = ",".join(str(s) for s, _ in mapping)
                b = ",".join(str(d) for _, d in mapping)
                code = f"[COPYUID {dest.uidvalidity} {a} {b}] "
            if sub == "COPY":
                self._ok(tag, f"{code}UID COPY completed")
                return
            if code:
                self._send(f"* OK {code.strip()}")
            for uid, _ in mapping:
                seq = self._seq(src, uid)
                src.remove(uid)
                self._send(f"* {seq} EXPUNGE")
            self._ok(tag, "UID MOVE completed")

        def _uid_expunge(self, tag: str, args: str) -> None:
            if "UIDPLUS" not in sink.caps:
                self._send(f"{tag} BAD UID EXPUNGE not supported")
                return
            folder = self._require_writable(tag)
            if folder is None:
                return
            sink.uid_expunges.append((folder.name, args.strip()))
            self._expunge(folder, _expand_uid_set(args.strip(), sorted(folder.msgs)))
            self._ok(tag, "UID EXPUNGE completed")

        # ── FETCH ───────────────────────────────────────────────────────

        def _handle_fetch(self, tag: str, args: str) -> None:
            folder = self.selected or sink.folders["INBOX"]
            uid_spec, _, items = args.partition(" ")
            wanted = _FETCH_ITEM_RE.findall(items)
            for uid in _expand_uid_set(uid_spec, sorted(folder.msgs)):
                self._fetch_one(folder, uid, wanted)
            self._ok(tag, "UID FETCH completed")

        def _fetch_one(self, folder: Folder, uid: int, wanted: list[str]) -> None:
            raw = folder.msgs[uid]
            flags = folder.flags[uid]
            before = set(flags)
            plain: list[str] = []
            literals: list[tuple[str, bytes]] = []
            for item in wanted:
                up = item.upper()
                if up == "FLAGS":
                    continue  # always emitted below
                if up == "RFC822.SIZE":
                    plain.append(f"RFC822.SIZE {len(raw)}")
                elif up == "BODYSTRUCTURE":
                    parsed = email.message_from_bytes(raw, policy=email.policy.compat32)
                    plain.append(f"BODYSTRUCTURE {_bodystructure(parsed)}")
                elif up == "INTERNALDATE":
                    plain.append('INTERNALDATE "18-May-2026 10:00:00 +0000"')
                elif up == "RFC822":
                    flags.add("\\Seen")
                    literals.append(("RFC822", raw))
                elif up == "RFC822.HEADER":
                    literals.append(("RFC822.HEADER", _header_block(raw, None)))
                elif up.startswith("BODY"):
                    peek = up.startswith("BODY.PEEK")
                    section = item[item.index("[") + 1 : -1]
                    label = f"BODY[{section}]"
                    if not peek and not self.readonly:
                        flags.add("\\Seen")
                    sec = section.upper()
                    if sec == "":
                        literals.append((label, raw))
                    elif sec == "HEADER":
                        literals.append((label, _header_block(raw, None)))
                    elif sec.startswith("HEADER.FIELDS"):
                        names = re.search(r"\(([^)]*)\)", section)
                        fields = names.group(1).split() if names else []
                        literals.append((label, _header_block(raw, fields)))
                    elif sec == "TEXT":
                        literals.append((label, _split_headers(raw)[1]))
                    else:
                        literals.append((label, b""))
            if flags != before and self.readonly:
                flags.clear()
                flags.update(before)
            flag_txt = " ".join(sorted(flags))
            seq = self._seq(folder, uid)
            head = f"* {seq} FETCH (UID {uid}"
            if plain:
                head += " " + " ".join(plain)
            head += f" FLAGS ({flag_txt})"
            out = head.encode()
            for label, data in literals:
                out += f" {label} {{{len(data)}}}\r\n".encode() + data
            self.wfile.write(out + b")\r\n")

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Injection tests deliberately feed malformed sequences; a handler
        # thread blowing up on them is expected, so don't spew tracebacks.
        pass


def start(
    messages: dict[int, bytes],
    caps: set[str] | frozenset[str] | None = None,
    folders: dict[str, Any] | None = None,
    strict: bool = False,
) -> tuple[Sink, _Server, str, int]:
    """Start a mock IMAP server on an ephemeral port and wait for it to listen."""
    sink = Sink(messages, caps=caps, folders=folders, strict=strict)
    srv = _Server(("127.0.0.1", 0), _make_handler(sink))
    host, port = srv.server_address[:2]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            continue
    return sink, srv, str(host), int(port)


def serve(
    messages: dict[int, bytes],
    caps: set[str] | frozenset[str] | None = None,
    folders: dict[str, Any] | None = None,
    strict: bool = False,
) -> Iterator[tuple[Sink, str, int]]:
    """Context-manager style helper: yields (sink, host, port), cleans up after."""
    sink, srv, host, port = start(messages, caps=caps, folders=folders, strict=strict)
    try:
        yield sink, host, port
    finally:
        srv.shutdown()
        srv.server_close()


def sample_message(subject: str, sender: str = "alice@example.com") -> bytes:
    """Build a minimal multipart-free RFC822 message for FETCH responses."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "bob@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 18 May 2026 10:00:00 +0000"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    msg.set_content("hello from the mock imap server")
    return msg.as_bytes()
