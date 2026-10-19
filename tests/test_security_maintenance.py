"""Connection cleanup, network defaults and security-dependency contracts."""

import imaplib
import logging
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import httpx
import jwt
import pytest

from mailboxd.cli import main
from mailboxd.config import ImapConfig, MailboxConfig, SmtpConfig
from mailboxd.imap_client import ImapError, list_folders, unified_search

SIGNING_KEY = "EXAMPLE-DO-NOT-USE-signing-key-with-sufficient-length"
SYNTHETIC_PASSWORD = "EXAMPLE-DO-NOT-USE-password"


def _config(tls="ssl"):
    return ImapConfig(host="mail.invalid", username="user", password=SYNTHETIC_PASSWORD, tls=tls)


@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_cli_loopback_default_and_explicit_override(monkeypatch, host):
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setenv("WORKERS", "1")
    arguments = ["http"] if host is None else ["http", "--host", host]
    assert main(arguments) == 0
    assert run.call_args.kwargs["host"] == (host or "127.0.0.1")
    assert run.call_args.kwargs["factory"] is True


def test_real_default_listener_is_only_on_loopback(
    security_config_factory, security_listener_factory
):
    with security_listener_factory(security_config_factory(None), network=False) as client:
        assert client.get("/health").is_success
        assert client.get("/mailboxes").status_code == 401
        address = socket.gethostbyname(socket.gethostname())
        with httpx.Client(trust_env=False, timeout=1) as external:
            with pytest.raises(httpx.ConnectError):
                external.get(f"http://{address}:{client.base_url.port}/health")


@pytest.mark.parametrize("failure", [imaplib.IMAP4.error, OSError])
def test_logout_failure_preserves_success_and_does_not_log_secrets(
    imap_connection, caplog, failure
):
    imap_connection.logout.side_effect = failure(SYNTHETIC_PASSWORD)
    with caplog.at_level(logging.WARNING):
        assert list_folders(_config()) == ["INBOX"]
    assert len(caplog.records) == 1
    assert caplog.records[0].error_type == failure.__name__
    assert SYNTHETIC_PASSWORD not in caplog.text
    imap_connection.shutdown.assert_called_once()


def test_login_failure_closes_connection_and_preserves_original_error(imap_connection):
    error = imaplib.IMAP4.error("authentication refused")
    imap_connection.login.side_effect = error
    imap_connection.logout.side_effect = OSError("cleanup refused")
    with pytest.raises(ImapError) as failure:
        list_folders(_config())
    assert failure.value.__cause__ is error
    imap_connection.logout.assert_called_once()
    imap_connection.shutdown.assert_called_once()


def test_starttls_failure_closes_connection(imap_connection):
    error = OSError("TLS unavailable")
    imap_connection.starttls.side_effect = error
    with pytest.raises(OSError) as failure:
        list_folders(_config("starttls"))
    assert failure.value is error
    imap_connection.login.assert_not_called()
    imap_connection.logout.assert_called_once()


def test_unified_search_ignores_smtp_only_mailboxes(imap_connection):
    smtp = SmtpConfig(
        host="smtp.invalid", username="u", password=SYNTHETIC_PASSWORD, from_address="u@example.org"
    )
    mailboxes = [MailboxConfig(name="smtp", smtp=smtp), MailboxConfig(name="imap", imap=_config())]
    result = unified_search(mailboxes)
    assert result["messages"] == [] and result["errors"] == []
    assert list(result["positions"]) == ["imap"]  # the SMTP-only mailbox is skipped
    imap_connection.login.assert_called_once_with("user", SYNTHETIC_PASSWORD)


def test_jwt_signatures_and_invalid_alphabet():
    token = jwt.encode({"sub": "synthetic-user"}, SIGNING_KEY, algorithm="HS256")
    assert jwt.decode(token, SIGNING_KEY, algorithms=["HS256"])["sub"] == "synthetic-user"
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token, "EXAMPLE-DO-NOT-USE-other-key-with-sufficient-length", algorithms=["HS256"]
        )
    with pytest.raises(jwt.DecodeError):
        jwt.decode(token + "!!!!", SIGNING_KEY, algorithms=["HS256"])


def test_dependency_cutoff_enforces_seven_days(tmp_path):
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.uv]\nexclude-newer = "old"\nvalue = "preserve"\n')
    script = Path(__file__).resolve().parents[1] / "scripts/bump_exclude_newer.sh"
    before = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    result = subprocess.run(["bash", str(script), str(config)], capture_output=True, text=True)
    after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    assert result.returncode == 0, result.stderr
    assert any(f'exclude-newer = "{cutoff}"' in config.read_text() for cutoff in (before, after))
    assert 'value = "preserve"' in config.read_text()


@pytest.mark.parametrize("exists", [False, True])
def test_dependency_cutoff_rejects_missing_file_or_field(tmp_path, exists):
    config = tmp_path / "pyproject.toml"
    if exists:
        config.write_text('[tool.uv]\nvalue = "preserve"\n')
    script = Path(__file__).resolve().parents[1] / "scripts/bump_exclude_newer.sh"
    result = subprocess.run(["bash", str(script), str(config)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "bump_exclude_newer:" in result.stderr
    if exists:
        assert config.read_text() == '[tool.uv]\nvalue = "preserve"\n'


def test_socket_close_failure_does_not_mask_original_error(imap_connection, caplog):
    error = imaplib.IMAP4.error("authentication refused")
    imap_connection.login.side_effect = error
    imap_connection.logout.side_effect = OSError(SYNTHETIC_PASSWORD)
    imap_connection.shutdown.side_effect = OSError(SYNTHETIC_PASSWORD)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ImapError) as failure:
            list_folders(_config())
    assert failure.value.__cause__ is error
    assert len(caplog.records) == 2
    assert SYNTHETIC_PASSWORD not in caplog.text
