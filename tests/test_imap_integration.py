"""IMAP client tests against an in-process mock IMAP server.

Two things are covered:

1. Happy path — list/fetch/search drive the mock and parse real responses,
   proving the client speaks IMAP correctly.
2. Command injection (issue #1) — a `folder`/`uid`/`search` value carrying a
   CRLF must be refused before it can inject a second IMAP command onto the
   authenticated connection, and an embedded quote must be escaped on the
   wire rather than breaking out of the mailbox-name string.

The mock records every command line it receives (`Sink.commands`), so the
security tests can assert directly that no STORE/EXPUNGE was smuggled through.
"""

from __future__ import annotations

import imaplib
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

import imap_mock
from mailboxd.config import ImapConfig, load_config
from mailboxd.imap_client import (
    ImapError,
    delete_message,
    fetch_message,
    list_messages,
    search_messages,
)

# The verbatim payload from the issue #1 PoC: a quote to break out of the
# mailbox-name string, then CRLFs that splice STORE+EXPUNGE (full-folder
# deletion) and a re-SELECT onto the same authenticated connection, all through
# a read-only route.
INJECT = 'INBOX"\r\nA9999 STORE 1:* +FLAGS (\\Deleted)\r\nA9998 EXPUNGE\r\nA9997 SELECT "INBOX'

Factory = Callable[[dict[int, bytes]], "tuple[imap_mock.Sink, str, int]"]


@pytest.fixture()
def imap_factory() -> Any:
    servers = []

    def _make(messages: dict[int, bytes]) -> tuple[imap_mock.Sink, str, int]:
        sink, srv, host, port = imap_mock.start(messages)
        servers.append(srv)
        return sink, host, port

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _cfg(host: str, port: int) -> ImapConfig:
    return ImapConfig(
        host=host, port=port, tls="none", username="u", password="p", default_folder="INBOX"
    )


def _no_destructive(commands: list[str]) -> bool:
    joined = " ".join(commands).upper()
    return "STORE" not in joined and "EXPUNGE" not in joined


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_list_messages_parses_and_uses_uid_commands(imap_factory: Factory) -> None:
    msgs = {1: imap_mock.sample_message("First"), 2: imap_mock.sample_message("Second")}
    sink, host, port = imap_factory(msgs)
    out = list_messages(_cfg(host, port), limit=10)
    subjects = {m["subject"] for m in out}
    assert subjects == {"First", "Second"}
    assert all(m["from"] == "alice@example.com" for m in out)
    verbs = " ".join(sink.commands).upper()
    assert "UID SEARCH" in verbs
    assert "UID FETCH" in verbs


def test_fetch_message_parses_body(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    out = fetch_message(_cfg(host, port), uid="1")
    assert out["subject"] == "Hello"
    assert "hello from the mock imap server" in (out["body_text"] or "")
    assert any("UID FETCH 1 (RFC822)" in c for c in sink.commands)


def test_search_messages_structured(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    out = search_messages(_cfg(host, port), subject="Hello")
    assert len(out) == 1
    assert any('SUBJECT "Hello"' in c for c in sink.commands)


# --------------------------------------------------------------------------
# Command injection (issue #1)
# --------------------------------------------------------------------------


def test_folder_crlf_injection_is_rejected(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    with pytest.raises(ImapError):
        list_messages(_cfg(host, port), folder=INJECT)
    # Rejected before connecting: the server sees nothing, least of all a STORE.
    assert _no_destructive(sink.commands)


def test_uid_crlf_injection_is_rejected(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    with pytest.raises(ImapError):
        fetch_message(_cfg(host, port), uid="1\r\nZ99 UID STORE 1 +FLAGS (\\Deleted)")
    with pytest.raises(ImapError):
        delete_message(_cfg(host, port), uid="1\r\nZ99 EXPUNGE")
    assert _no_destructive(sink.commands)


def test_search_crlf_injection_is_rejected(imap_factory: Factory) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    with pytest.raises(ImapError):
        list_messages(_cfg(host, port), search="ALL\r\nZ99 UID STORE 1 +FLAGS (\\Deleted)")
    with pytest.raises(ImapError):
        search_messages(_cfg(host, port), subject="hi\r\nZ99 EXPUNGE")
    assert _no_destructive(sink.commands)


def test_folder_quote_is_escaped_on_the_wire(imap_factory: Factory) -> None:
    # No CRLF, so this is NOT rejected; it must instead be escaped so the `"`
    # stays inside one quoted mailbox name rather than splitting the atom.
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    list_messages(_cfg(host, port), folder='INBOX" bad')
    select_lines = [c for c in sink.commands if " EXAMINE " in c or " SELECT " in c]
    assert len(select_lines) == 1
    assert 'EXAMINE "INBOX\\" bad"' in select_lines[0]
    assert _no_destructive(sink.commands)


def test_mock_detects_injected_command_via_raw_imaplib(imap_factory: Factory) -> None:
    # Control test: proves the vulnerability is real (imaplib passes CRLF
    # through unsanitized) AND that the harness can see an injected command,
    # which is what makes the assertions above meaningful.
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hi")})
    conn = imaplib.IMAP4(host, port)
    try:
        conn.login("u", "p")
        try:
            conn.select('"INBOX"\r\nZ99 NOOP')
        except Exception:
            pass
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    assert any(c.startswith("Z99") for c in sink.commands)


# --------------------------------------------------------------------------
# End-to-end: the read-only GET route the PoC abused
# --------------------------------------------------------------------------


def _http_client(tmp_path: Path, host: str, port: int) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
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

    return TestClient(create_app(load_config(str(cfg))), raise_server_exceptions=False)


def test_get_message_route_rejects_crlf_folder(imap_factory: Factory, tmp_path: Path) -> None:
    sink, host, port = imap_factory({1: imap_mock.sample_message("Hello")})
    client = _http_client(tmp_path, host, port)
    # httpx percent-encodes the CRLF; FastAPI decodes it back to a raw \r\n,
    # exactly as the PoC's %0D%0A request does.
    resp = client.get("/mailboxes/m/messages/1", params={"folder": INJECT})
    # Handled as a clean upstream error (502), not an uncaught 500 crash.
    assert resp.status_code == 502, resp.text
    assert _no_destructive(sink.commands)
