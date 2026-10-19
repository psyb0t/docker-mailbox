from __future__ import annotations

import email
import hashlib
import hmac
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from mailboxd import watch
from mailboxd.config import Config, ImapConfig, MailboxConfig, WatchConfig, WebhookConfig
from mailboxd.state import State

RAW = (
    b"From: Alice <alice@example.com>\r\nTo: me@example.com\r\nSubject: Hi\r\n"
    b"Date: Mon, 01 Jan 2024 10:00:00 +0000\r\nMessage-ID: <m%d@x>\r\n"
    b"Authentication-Results: mx.example.com; spf=pass smtp.mailfrom=example.com;"
    b" dkim=pass header.d=example.com; dmarc=pass header.from=example.com\r\n\r\nSECRET BODY"
)


class Received:
    def __init__(self) -> None:
        self.items: list[tuple[dict[str, str], bytes]] = []
        self.status = 200
        self.url = ""


@pytest.fixture
def hook_server() -> Iterator[Received]:
    rec = Received()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            rec.items.append((dict(self.headers), body))
            self.send_response(rec.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rec.url = f"http://127.0.0.1:{srv.server_port}/hook"
    yield rec
    srv.shutdown()
    srv.server_close()


class FakeSession:
    def __init__(self, uids: list[int], validity: str = "7") -> None:
        self.uids = uids
        self.validity = validity

    def select(self, folder: str, readonly: bool = True) -> str:
        return self.validity

    def search(self, criteria: str) -> list[int]:
        if criteria == "ALL":
            return list(self.uids)
        low = int(criteria.split()[1].split(":")[0])
        hits = [u for u in self.uids if u >= low]
        return hits or self.uids[-1:]  # UID n:* quirk

    def fetch_message(self, uid: str, header_only: bool = False) -> Any:
        return email.message_from_bytes(RAW % int(uid))


def make_cfg(
    url: str, tmp_path: Path, mailboxes: Any = "*"
) -> tuple[Config, State, MailboxConfig]:
    mb = MailboxConfig(
        name="m", imap=ImapConfig(host="h", username="me@example.com", password="p")
    )
    cfg = Config(
        mailboxes=[mb],
        watch=WatchConfig(webhooks=[WebhookConfig(url=url, secret="s3cret", mailboxes=mailboxes)]),
    )
    return cfg, State(str(tmp_path / "state.db")), mb


def check(cfg: Config, state: State, mb: MailboxConfig, s: FakeSession) -> int:
    return watch.check_folder(cfg, state, mb, s, "INBOX")  # type: ignore[arg-type]


def test_sign() -> None:
    expect = hmac.new(b"k", b"body", hashlib.sha256).hexdigest()
    assert watch.sign("k", b"body") == "sha256=" + expect


def test_first_run_sets_cursor_without_notifying(tmp_path: Path, hook_server: Received) -> None:
    cfg, state, mb = make_cfg(hook_server.url, tmp_path)
    assert check(cfg, state, mb, FakeSession([1, 2, 3])) == 3
    assert hook_server.items == []
    assert state.get_cursor("watch", "watch", "m", "INBOX") == ("7", 3)


def test_new_mail_posts_signed_event_without_body(tmp_path: Path, hook_server: Received) -> None:
    cfg, state, mb = make_cfg(hook_server.url, tmp_path)
    state.set_cursor("watch", "watch", "m", "INBOX", "7", 3)
    s = FakeSession([1, 2, 3, 4, 5])
    assert check(cfg, state, mb, s) == 5
    assert len(hook_server.items) == 2
    headers, body = hook_server.items[0]
    assert headers["X-Mailboxd-Signature"] == watch.sign("s3cret", body)
    assert headers["X-Mailboxd-Timestamp"].isdigit()
    ev = json.loads(body)
    assert ev["event"] == "message.new" and ev["uid"] == "4" and ev["mailbox"] == "m"
    assert ev["subject"] == "Hi" and ev["message_id"] == "<m4@x>"
    auth = {"spf": "pass", "dkim": "pass", "dmarc": "pass", "trusted": False}
    assert ev["authentication"] == auth
    assert isinstance(ev["signals"], list)
    assert b"SECRET BODY" not in body
    assert state.get_cursor("watch", "watch", "m", "INBOX") == ("7", 5)
    # nothing new afterwards, despite the UID n:* quirk
    assert check(cfg, state, mb, s) == 5
    assert len(hook_server.items) == 2


def test_uidvalidity_change_resets(tmp_path: Path, hook_server: Received) -> None:
    cfg, state, mb = make_cfg(hook_server.url, tmp_path)
    state.set_cursor("watch", "watch", "m", "INBOX", "1", 1)
    check(cfg, state, mb, FakeSession([1, 2], validity="9"))
    assert hook_server.items == []
    assert state.get_cursor("watch", "watch", "m", "INBOX") == ("9", 2)


def test_deliver_retries_then_gives_up(hook_server: Received) -> None:
    hook_server.status = 500
    hook = WebhookConfig(url=hook_server.url, secret="s")
    sleeps: list[float] = []
    assert watch.deliver(hook, b"{}", sleep=sleeps.append) is False
    assert len(hook_server.items) == 3
    assert sleeps == [1.0, 2.0]


def test_deliver_rejects_non_http() -> None:
    assert watch.deliver(WebhookConfig(url="file:///etc/passwd", secret="s"), b"{}") is False


def test_webhook_mailbox_filter(tmp_path: Path) -> None:
    cfg, _, _ = make_cfg("http://x/", tmp_path, mailboxes=["other"])
    assert watch.webhooks_for(cfg, "m") == []
    assert watch.watched_mailboxes(cfg) == []
    assert watch.run(cfg) == 2


class FakeConn:
    def __init__(self, sock: socket.socket, peer: socket.socket) -> None:
        self.sock = sock
        self.peer = peer
        self.file = sock.makefile("rb")
        self.sent: list[bytes] = []

    def _new_tag(self) -> bytes:
        return b"A1"

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        if data == b"DONE\r\n":
            self.peer.sendall(b"A1 OK done\r\n")

    def readline(self) -> bytes:
        return self.file.readline()


def test_idle_wakes_on_exists() -> None:
    a, b = socket.socketpair()
    conn = FakeConn(a, b)
    b.sendall(b"+ idling\r\n* 5 EXISTS\r\n")
    assert watch.idle_wait(conn, 5, slice_seconds=0.1) is True
    assert conn.sent == [b"A1 IDLE\r\n", b"DONE\r\n"]
    a.close()
    b.close()


def test_idle_times_out() -> None:
    a, b = socket.socketpair()
    conn = FakeConn(a, b)
    b.sendall(b"+ idling\r\n")
    assert watch.idle_wait(conn, 0.3, slice_seconds=0.1) is False
    a.close()
    b.close()
