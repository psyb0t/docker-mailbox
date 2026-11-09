"""Unreachable mail servers and invalid configs fail with a clear, bounded error."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from mailboxd import imap_client
from mailboxd.cli import main
from mailboxd.config import ConfigError, ImapConfig, load_config
from mailboxd.imap_client import ImapError
from mailboxd.server import create_app

TOKEN = "EXAMPLE-DO-NOT-USE-token"
SECRET = "EXAMPLE-DO-NOT-USE-password"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _config(tmp_path: Path, port: int) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
auth:
  tokens: [{{name: t, token: {TOKEN}, scopes: [read], mailboxes: "*"}}]
mailboxes:
  - name: alpha
    imap: {{host: 127.0.0.1, port: {port}, tls: none, username: a@example.org, password: p}}
""")
    return str(path)


def test_session_reports_refused_connection_as_imap_error() -> None:
    cfg = ImapConfig(host="127.0.0.1", port=_closed_port(), tls="none", username="u", password="p")
    with pytest.raises(ImapError):
        with imap_client.session(cfg):
            pass


def test_session_reports_socket_failure_mid_command_as_imap_error(imap_connection: Mock) -> None:
    imap_connection.list.side_effect = TimeoutError("timed out")
    cfg = ImapConfig(host="mail.invalid", username="u", password="p")
    with pytest.raises(ImapError, match="timed out"):
        imap_client.list_folders(cfg)


def test_unreachable_imap_server_is_502_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))
    client = TestClient(create_app(load_config(_config(tmp_path, _closed_port()))))
    response = client.get("/mailboxes/alpha/folders", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "imap_error",
        "message": "mail server operation failed",
    }


def test_config_error_names_the_field_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
auth:
  tokens: [{{name: t, token: {SECRET}, mailboxes: [nope]}}]
mailboxes:
  - name: alpha
    imap: {{host: h, port: not-a-port, username: u, password: {SECRET}}}
""")
    with pytest.raises(ConfigError) as caught:
        load_config(str(path))
    message = str(caught.value)
    assert "mailboxes.0.imap.port" in message
    assert SECRET not in message


def test_http_refuses_invalid_config_before_starting_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("mailboxes: [{name: alpha}]\n")
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    assert main(["http", "--config", str(path)]) == 2
    run.assert_not_called()
    assert "mailboxes.0" in capsys.readouterr().err
