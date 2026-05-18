"""SMTP integration test against an in-process socketserver capture sink.

Validates the SMTP client formats messages correctly and that the HTTP
/send endpoint actually delivers them end-to-end. Uses a tiny hand-rolled
SMTP server (no aiosmtpd) so the fixture has no external moving parts.
"""

from __future__ import annotations

import email
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app


class _Sink:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []


def _make_handler(sink: _Sink) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            mail_from = ""
            rcpts: list[str] = []
            data_buf: list[bytes] = []

            def send(line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            send("220 test ESMTP")
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                up = line.upper()
                if up.startswith(("HELO", "EHLO")):
                    send("250-test")
                    send("250 OK")
                elif up.startswith("MAIL FROM"):
                    mail_from = _between(line, "<", ">")
                    send("250 OK")
                elif up.startswith("RCPT TO"):
                    rcpts.append(_between(line, "<", ">"))
                    send("250 OK")
                elif up == "DATA":
                    send("354 End data with <CR><LF>.<CR><LF>")
                    while True:
                        chunk = self.rfile.readline()
                        if not chunk:
                            return
                        if chunk in (b".\r\n", b".\n"):
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        data_buf.append(chunk)
                    sink.messages.append(b"".join(data_buf))
                    sink.envelopes.append((mail_from, list(rcpts)))
                    data_buf.clear()
                    rcpts = []
                    mail_from = ""
                    send("250 OK queued")
                elif up == "QUIT":
                    send("221 bye")
                    return
                elif up == "RSET":
                    rcpts = []
                    mail_from = ""
                    data_buf.clear()
                    send("250 OK")
                elif up == "NOOP":
                    send("250 OK")
                else:
                    send("250 OK")

    return Handler


def _between(s: str, a: str, b: str) -> str:
    i = s.find(a)
    j = s.find(b, i + 1)
    if i < 0 or j < 0:
        return ""
    return s[i + 1 : j]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture()
def smtp_sink() -> Any:
    sink = _Sink()
    srv = _Server(("127.0.0.1", 0), _make_handler(sink))
    host, port = srv.server_address[:2]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    # Probe the listener is up before yielding.
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            continue
    try:
        yield sink, host, port
    finally:
        srv.shutdown()
        srv.server_close()


def _client(tmp_path: Path, host: str, port: int) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
mailboxes:
  - name: sink
    smtp:
      host: {host}
      port: {port}
      tls: none
      username: ""
      password: ""
      from_address: "sender@example.com"
""")
    return TestClient(create_app(load_config(str(cfg))))


def test_send_delivers_message(smtp_sink: Any, tmp_path: Path) -> None:
    sink, host, port = smtp_sink
    c = _client(tmp_path, host, port)
    r = c.post(
        "/mailboxes/sink/send",
        json={
            "to": ["dest@example.com"],
            "subject": "hi from mailboxd",
            "body_text": "body line",
        },
    )
    assert r.status_code == 200, r.text
    assert len(sink.messages) == 1
    raw = sink.messages[0]
    msg = email.message_from_bytes(raw)
    assert msg["Subject"] == "hi from mailboxd"
    assert "dest@example.com" in msg["To"]
    assert "sender@example.com" in msg["From"]
    mail_from, rcpts = sink.envelopes[0]
    assert "sender@example.com" in mail_from
    assert "dest@example.com" in rcpts


def test_send_with_cc_and_bcc_routes_correctly(smtp_sink: Any, tmp_path: Path) -> None:
    sink, host, port = smtp_sink
    c = _client(tmp_path, host, port)
    r = c.post(
        "/mailboxes/sink/send",
        json={
            "to": ["a@example.com"],
            "cc": ["b@example.com"],
            "bcc": ["c@example.com"],
            "subject": "multi",
            "body_text": "hi",
        },
    )
    assert r.status_code == 200, r.text
    msg = email.message_from_bytes(sink.messages[0])
    # bcc must NOT appear in headers
    assert "c@example.com" not in (msg["To"] or "")
    assert "c@example.com" not in (msg["Cc"] or "")
    # but all 3 must be in the SMTP envelope rcpts
    _, rcpts = sink.envelopes[0]
    assert set(rcpts) == {"a@example.com", "b@example.com", "c@example.com"}
