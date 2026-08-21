"""A tiny in-process IMAP4 server for tests.

Hand-rolled on `socketserver` (same approach as the SMTP capture sink in
`test_smtp_integration.py`) rather than a container, so the suite has no
external moving parts and starts in milliseconds. It speaks just enough of
IMAP4rev1 for `mailboxd.imap_client` to drive it: CAPABILITY, LOGIN,
SELECT/EXAMINE, UID SEARCH/FETCH/STORE and EXPUNGE, including the `{N}`
literal framing imaplib needs to parse FETCH bodies.

Every command line the client sends is recorded verbatim on `Sink.commands`,
which is what lets a test assert that a crafted folder/uid/search value did
NOT smuggle an extra IMAP command onto the connection.
"""

from __future__ import annotations

import email
import socket
import socketserver
import threading
from typing import Any, Iterator


class Sink:
    """Shared state between the test and the mock server."""

    def __init__(self, messages: dict[int, bytes]) -> None:
        # uid -> raw RFC822 bytes the server will hand back on FETCH.
        self.messages = messages
        # Every command line received, tag included, CRLF stripped.
        self.commands: list[str] = []

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


def _make_handler(sink: Sink) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def _send(self, line: str) -> None:
            self.wfile.write((line + "\r\n").encode())

        def _ok(self, tag: str, text: str) -> None:
            self._send(f"{tag} OK {text}")

        def handle(self) -> None:
            self._send("* OK [CAPABILITY IMAP4rev1] mock ready")
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                sink.commands.append(line)
                parts = line.split(" ", 2)
                if len(parts) < 2:
                    continue
                tag, cmd = parts[0], parts[1].upper()
                rest = parts[2] if len(parts) > 2 else ""
                if self._dispatch(tag, cmd, rest):
                    return

        def _dispatch(self, tag: str, cmd: str, rest: str) -> bool:
            """Handle one command. Returns True when the connection should close."""
            if cmd == "CAPABILITY":
                self._send("* CAPABILITY IMAP4rev1")
                self._ok(tag, "CAPABILITY completed")
            elif cmd == "LOGIN":
                self._ok(tag, "LOGIN completed")
            elif cmd in ("EXAMINE", "SELECT"):
                mode = "READ-ONLY" if cmd == "EXAMINE" else "READ-WRITE"
                self._send(f"* {len(sink.messages)} EXISTS")
                self._ok(tag, f"[{mode}] {cmd} completed")
            elif cmd == "UID":
                self._handle_uid(tag, rest)
            elif cmd == "EXPUNGE":
                self._send("* 1 EXPUNGE")
                self._ok(tag, "EXPUNGE completed")
            elif cmd == "LOGOUT":
                self._send("* BYE mock logging out")
                self._ok(tag, "LOGOUT completed")
                return True
            else:
                self._ok(tag, f"{cmd} completed")
            return False

        def _handle_uid(self, tag: str, rest: str) -> None:
            sub, _, args = rest.partition(" ")
            sub = sub.upper()
            if sub == "SEARCH":
                uids = " ".join(str(u) for u in sorted(sink.messages))
                self._send(f"* SEARCH {uids}".rstrip())
                self._ok(tag, "UID SEARCH completed")
            elif sub == "FETCH":
                self._handle_fetch(tag, args)
            elif sub == "STORE":
                uid = args.split(" ", 1)[0]
                self._send(f"* 1 FETCH (UID {uid} FLAGS (\\Deleted))")
                self._ok(tag, "UID STORE completed")
            else:
                self._ok(tag, f"UID {sub} completed")

        def _handle_fetch(self, tag: str, args: str) -> None:
            uid_set, _, items = args.partition(" ")
            want_full = "RFC822" in items and "HEADER" not in items
            for uid in _parse_uid_set(uid_set):
                raw = sink.messages.get(uid)
                if raw is None:
                    continue
                if want_full:
                    self._fetch_full(uid, raw)
                else:
                    self._fetch_headers(uid, raw)
            self._ok(tag, "UID FETCH completed")

        def _fetch_full(self, uid: int, raw: bytes) -> None:
            head = b"* %d FETCH (UID %d RFC822 {%d}\r\n" % (uid, uid, len(raw))
            self.wfile.write(head + raw + b")\r\n")

        def _fetch_headers(self, uid: int, raw: bytes) -> None:
            msg = email.message_from_bytes(raw)
            fields = ["From", "To", "Subject", "Date", "Message-ID"]
            lines = [f"{f}: {msg[f]}" for f in fields if msg[f] is not None]
            hdr = ("\r\n".join(lines) + "\r\n\r\n").encode()
            head = (
                b"* %d FETCH (UID %d RFC822.SIZE %d FLAGS (\\Seen) "
                b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {%d}\r\n"
                % (uid, uid, len(raw), len(hdr))
            )
            self.wfile.write(head + hdr + b")\r\n")

    return Handler


def _parse_uid_set(uid_set: str) -> list[int]:
    out: list[int] = []
    for tok in uid_set.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Injection tests deliberately feed malformed sequences; a handler
        # thread blowing up on them is expected, so don't spew tracebacks.
        pass


def start(messages: dict[int, bytes]) -> tuple[Sink, _Server, str, int]:
    """Start a mock IMAP server on an ephemeral port and wait for it to listen."""
    sink = Sink(messages)
    srv = _Server(("127.0.0.1", 0), _make_handler(sink))
    host, port = srv.server_address[:2]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            continue
    return sink, srv, str(host), int(port)


def serve(messages: dict[int, bytes]) -> Iterator[tuple[Sink, str, int]]:
    """Context-manager style helper: yields (sink, host, port), cleans up after."""
    sink, srv, host, port = start(messages)
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
