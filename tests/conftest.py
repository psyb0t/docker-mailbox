import imaplib
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
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
    monkeypatch.setattr("mailboxd.imap_client.imaplib.IMAP4_SSL", factory)
    monkeypatch.setattr("mailboxd.imap_client.imaplib.IMAP4", factory)
    return connection


@pytest.fixture(autouse=True)
def _isolated_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own SQLite state file (audit, rate limits, cursors)."""
    path = tmp_path / "mailboxd-state.db"
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(path))
    return path
