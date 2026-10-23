"""Incoming messages over `limits.incoming_message_max_bytes` are refused
before their body is downloaded, while listings and header reads keep working.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import tracemalloc
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator

import imap_mock
import pytest
from fastapi.testclient import TestClient

from mailboxd import imap_client
from mailboxd.config import ImapConfig, load_config
from mailboxd.errors import TooLarge
from mailboxd.imap_client import Session

LIMIT = 4096


def _big_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Big"
    msg["Message-ID"] = "<big@example.com>"
    msg.set_content("big body\n")
    msg.add_attachment(b"x" * (LIMIT * 4), maintype="application", subtype="octet-stream")
    return msg.as_bytes()


@pytest.fixture()
def server() -> Iterator[tuple[imap_mock.Sink, str, int]]:
    msgs = {1: _big_message(), 2: imap_mock.sample_message("Small")}
    sink, srv, host, port = imap_mock.start(msgs)
    yield sink, host, port
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def client(
    server: tuple[imap_mock.Sink, str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))
    _, host, port = server
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
auth:
  tokens: [EXAMPLE-DO-NOT-USE]
limits:
  incoming_message_max_bytes: {LIMIT}
mailboxes:
  - name: m
    imap:
      host: {host}
      port: {port}
      tls: none
      username: "u"
      password: "p"
      default_folder: INBOX
""")
    from mailboxd.server import create_app

    return TestClient(
        create_app(load_config(str(cfg))),
        raise_server_exceptions=False,
        headers={"Authorization": "Bearer EXAMPLE-DO-NOT-USE"},
    )


def _body_fetches(sink: imap_mock.Sink, uid: int) -> list[str]:
    return [c for c in sink.commands if f"UID FETCH {uid} (BODY" in c]


@pytest.mark.parametrize("suffix", ["", "/raw", "/attachments/2"])
def test_oversized_message_is_refused_before_download(
    client: TestClient, server: tuple[imap_mock.Sink, str, int], suffix: str
) -> None:
    sink, _, _ = server
    resp = client.get(f"/mailboxes/m/messages/1{suffix}")
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "too_large"
    assert _body_fetches(sink, 1) == []


def test_message_under_the_limit_reads_normally(
    client: TestClient, server: tuple[imap_mock.Sink, str, int]
) -> None:
    sink, _, _ = server
    resp = client.get("/mailboxes/m/messages/2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Small"
    assert any(f"BODY.PEEK[]<0.{LIMIT + 1}>" in c for c in _body_fetches(sink, 2))


def test_listing_still_shows_oversized_message(client: TestClient) -> None:
    resp = client.get("/mailboxes/m/messages")
    assert resp.status_code == 200, resp.text
    assert {m["subject"] for m in resp.json()["messages"]} == {"Big", "Small"}


def test_header_only_read_of_oversized_message_works(
    server: tuple[imap_mock.Sink, str, int],
) -> None:
    import imaplib

    sink, host, port = server
    conn = imaplib.IMAP4(host, port)
    conn.login("u", "p")
    s = Session(conn, ImapConfig(host=host, port=port, tls="none", username="u", password="p"))
    s.message_max_bytes = LIMIT
    s.select("INBOX", readonly=True)
    assert s.fetch_message("1", header_only=True)["Subject"] == "Big"
    conn.logout()
    assert all("<0." in c for c in _body_fetches(sink, 1))


class _LyingConn:
    """Reports a tiny RFC822.SIZE, then answers the body fetch with more data."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[str] = []

    def uid(self, command: str, uid: str, items: str) -> tuple[str, list[Any]]:
        self.calls.append(items)
        if items == "(RFC822.SIZE)":
            # An unsolicited FETCH for another message must not be mistaken for ours.
            return "OK", [b"9 (UID 9 RFC822.SIZE 99999999)", b"1 (UID 1 RFC822.SIZE 10)"]
        return "OK", [(b"1 (UID 1 BODY[]<0> {%d}" % len(self.body), self.body), b")"]


def _session(conn: Any) -> Session:
    s = Session(conn, ImapConfig(host="x", username="u", password="p"), message_max_bytes=LIMIT)
    s.selected = "INBOX"
    return s


def test_under_reported_size_is_still_capped() -> None:
    conn = _LyingConn(b"x" * (LIMIT + 1))
    with pytest.raises(TooLarge):
        _session(conn).fetch_raw("1")
    assert conn.calls[-1] == f"(BODY.PEEK[]<0.{LIMIT + 1}>)"


def test_exact_limit_is_accepted() -> None:
    conn = _LyingConn(b"x" * LIMIT)
    assert len(_session(conn).fetch_raw("1")) == LIMIT


HOSTILE_LITERAL = 64 * 1024 * 1024


class _HostileHandler(socketserver.StreamRequestHandler):
    """Claims a tiny RFC822.SIZE, then ignores the partial range and streams a
    literal far past the limit, the way a broken or malicious server could."""

    def handle(self) -> None:
        self.wfile.write(b"* OK [CAPABILITY IMAP4rev1] hostile\r\n")
        try:
            for line in self.rfile:
                tag, _, rest = line.decode().strip().partition(" ")
                cmd = rest.upper()
                if cmd.startswith(("SELECT", "EXAMINE")):
                    self.wfile.write(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 1] ok\r\n")
                elif "RFC822.SIZE" in cmd:
                    self.wfile.write(b"* 1 FETCH (UID 1 RFC822.SIZE 10)\r\n")
                elif "BODY.PEEK[]" in cmd:
                    self._stream_literal()
                elif cmd.startswith("LOGOUT"):
                    self.wfile.write(b"* BYE\r\n" + tag.encode() + b" OK done\r\n")
                    return
                self.wfile.write(tag.encode() + b" OK done\r\n")
        except OSError:
            pass  # the client hung up mid-literal

    def _stream_literal(self) -> None:
        self.wfile.write(b"* 1 FETCH (UID 1 BODY[] {%d}\r\n" % HOSTILE_LITERAL)
        chunk = b"x" * (1024 * 1024)
        for _ in range(HOSTILE_LITERAL // len(chunk)):
            self.wfile.write(chunk)
        self.wfile.write(b")\r\n")


@pytest.fixture()
def hostile() -> Iterator[tuple[str, int]]:
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HostileHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    socket.create_connection((host, port), timeout=2).close()
    yield str(host), int(port)
    srv.shutdown()
    srv.server_close()


def test_server_ignoring_the_range_cannot_blow_up_memory(hostile: tuple[str, int]) -> None:
    host, port = hostile
    cfg = ImapConfig(host=host, port=port, tls="none", username="u", password="p")
    tracemalloc.start()
    try:
        with pytest.raises(TooLarge):
            with imap_client.session(cfg, LIMIT) as s:
                s.select("INBOX", readonly=True)
                s.fetch_raw("1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak allocation {peak} bytes for a {LIMIT} byte limit"
