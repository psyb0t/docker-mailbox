import imaplib
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from email.message import EmailMessage
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

STARTUP_TIMEOUT = 15
REQUEST_TIMEOUT = 1
POLL_INTERVAL = 0.05
SHUTDOWN_TIMEOUT = 5


@pytest.fixture
def security_config_factory(tmp_path: Path) -> Callable[[dict[str, Any] | None], Path]:
    def write_config(auth: dict[str, Any] | None) -> Path:
        document: dict[str, Any] = {"mailboxes": []}
        for name in ("alpha", "beta"):
            credentials = {
                "host": "mail.invalid",
                "username": f"{name}@example.org",
                "password": f"EXAMPLE-DO-NOT-USE-{name}",
            }
            document["mailboxes"].append(
                {
                    "name": name,
                    "imap": credentials,
                    "smtp": {**credentials, "from_address": f"{name}@example.org"},
                }
            )
        if auth is not None:
            document["auth"] = auth
        path = tmp_path / "security-config.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return path

    return write_config


@pytest.fixture
def security_listener_factory(tmp_path: Path):
    @contextmanager
    def start(config_path: Path, network: bool = True) -> Iterator[httpx.Client]:
        address = socket.gethostbyname(socket.gethostname()) if network else "127.0.0.1"
        if network:
            assert not address.startswith("127."), "Network test needs a non-loopback address"
        with socket.socket() as reservation:
            reservation.bind((address, 0))
            port = reservation.getsockname()[1]
        log_path = tmp_path / "mailbox-cli.log"
        with log_path.open("w", encoding="utf-8") as output:
            arguments = [
                sys.executable,
                "-m",
                "mailboxd",
                "http",
                "--port",
                str(port),
                "--workers",
                "1",
                "--config",
                str(config_path),
            ]
            if network:
                arguments.extend(["--host", address])
            process = subprocess.Popen(
                arguments,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                with httpx.Client(
                    base_url=f"http://{address}:{port}",
                    trust_env=False,
                    timeout=REQUEST_TIMEOUT,
                ) as client:
                    deadline = time.monotonic() + STARTUP_TIMEOUT
                    last_error = "no response"
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            pytest.fail(f"CLI exited: {log_path.read_text()}")
                        try:
                            response = client.get("/health")
                        except httpx.TransportError as error:
                            last_error = str(error)
                            time.sleep(POLL_INTERVAL)
                            continue
                        if response.is_success:
                            break
                        last_error = f"health status {response.status_code}"
                        time.sleep(POLL_INTERVAL)
                    else:
                        pytest.fail(f"CLI startup timeout: {last_error}; {log_path.read_text()}")
                    yield client
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=SHUTDOWN_TIMEOUT)

    return start


@pytest.fixture
def imap_connection(monkeypatch):
    connection = Mock()
    connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
    connection.select.return_value = ("OK", [b"0"])
    connection.uid.return_value = ("OK", [b""])
    factory = Mock(return_value=connection)
    factory.error = imaplib.IMAP4.error
    monkeypatch.setattr("mailboxd.imap_client._CappedIMAP4_SSL", factory)
    monkeypatch.setattr("mailboxd.imap_client._CappedIMAP4", factory)
    return connection


@pytest.fixture(autouse=True)
def _isolated_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own SQLite state file (audit, rate limits, cursors)."""
    path = tmp_path / "mailboxd-state.db"
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(path))
    return path


@pytest.fixture
def catch_all_mailbox_factory():
    """Run HTTP with local wire-level IMAP/SMTP peers for sender identity tests."""
    import imap_mock
    from fastapi.testclient import TestClient
    from test_smtp_integration import _make_handler, _Server, _Sink

    from mailboxd.config import Config
    from mailboxd.server import create_app

    @contextmanager
    def start(wildcard: bool = False, to: str | None = None, cc: str | None = None):
        original = EmailMessage()
        original["From"] = "Author <author@example.org>"
        original["Message-ID"] = "<original@example.org>"
        original["Subject"] = "Catch-all question"
        if to is not None:
            original["To"] = to
        if cc is not None:
            original["Cc"] = cc
        original.set_content("Original message")
        identities = [
            {
                "address": "primary@example.com",
                "name": "Primary",
                "default": True,
                "signature_text": "Primary signature",
            },
            {
                "address": "known@example.com",
                "name": "Known",
                "signature_text": "Known signature",
            },
        ]
        if wildcard:
            identities.append(
                {
                    "address": "*@example.com",
                    "name": "Catch-all",
                    "signature_text": "Wildcard signature",
                }
            )
        with ExitStack() as cleanup:
            imap_sink, imap_server, imap_host, imap_port = imap_mock.start(
                {1: original.as_bytes()}, caps=imap_mock.MODERN_CAPS, folders={"Drafts": {}}
            )
            cleanup.callback(imap_server.server_close)
            cleanup.callback(imap_server.shutdown)
            smtp_sink = _Sink()
            smtp_server = _Server(("127.0.0.1", 0), _make_handler(smtp_sink))
            cleanup.callback(smtp_server.server_close)
            cleanup.callback(smtp_server.shutdown)
            thread = Thread(target=smtp_server.serve_forever, daemon=True)
            thread.start()
            smtp_host, smtp_port = smtp_server.server_address[:2]
            config = Config.model_validate(
                {
                    "auth": {"tokens": ["EXAMPLE-DO-NOT-USE"]},
                    "mailboxes": [
                        {
                            "name": "catchall",
                            "imap": {
                                "host": imap_host,
                                "port": imap_port,
                                "tls": "none",
                                "username": "primary@example.com",
                                "password": "EXAMPLE-DO-NOT-USE",
                            },
                            "smtp": {
                                "host": smtp_host,
                                "port": smtp_port,
                                "tls": "none",
                                "username": "",
                                "password": "",
                                "from_address": "primary@example.com",
                                "identities": identities,
                                "save_sent": False,
                            },
                        }
                    ],
                }
            )
            client = cleanup.enter_context(
                TestClient(
                    create_app(config),
                    headers={"Authorization": "Bearer EXAMPLE-DO-NOT-USE"},
                )
            )
            yield client, smtp_sink, imap_sink

    return start
