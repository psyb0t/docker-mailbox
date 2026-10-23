"""Certificate and hostname validation against real IMAP/SMTP TLS sockets."""

import socketserver
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import imap_mock
import pytest
import test_smtp_integration

from mailboxd import imap_client, smtp_client
from mailboxd.config import ImapConfig, SmtpConfig


@pytest.fixture
def tls_server(tmp_path: Path):
    cert = tmp_path / "certificate.pem"
    key = tmp_path / "private.key"
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    (tmp_path / "certificate-generation.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    servers = []

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(5)
            try:
                return context.wrap_socket(sock, server_side=True), address
            except OSError:
                sock.close()
                raise

    def start(protocol: str) -> tuple[int, str]:
        if protocol == "imap":
            handler = imap_mock._make_handler(imap_mock.Sink({}))
        else:
            handler = test_smtp_integration._make_handler(test_smtp_integration._Sink())
        server = Server(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1], str(cert)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("protocol", ["imap", "smtp"])
@pytest.mark.parametrize(
    "trust,host", [(False, "localhost"), (True, "localhost"), (True, "127.0.0.1")]
)
def test_tls_verifies_peer(
    tls_server: Any, monkeypatch: Any, protocol: str, trust: bool, host: str
) -> None:
    port, certificate = tls_server(protocol)
    if trust:
        monkeypatch.setenv("SSL_CERT_FILE", certificate)

    def operation() -> None:
        if protocol == "imap":
            cfg = ImapConfig(host=host, port=port, tls="ssl", username="u", password="REPLACE_ME")
            imap_client.list_folders(cfg)
            return
        cfg = SmtpConfig(
            host=host,
            port=port,
            tls="ssl",
            username="",
            password="",
            from_address="sender@example.org",
        )
        smtp_client.check(cfg)

    if trust and host == "localhost":
        operation()
    else:
        with pytest.raises((ssl.SSLCertVerificationError, smtp_client.SmtpError)):
            operation()


@pytest.mark.parametrize("protocol", ["imap", "smtp"])
def test_starttls_requires_certificate_and_hostname_validation(
    monkeypatch: Any, protocol: str
) -> None:
    connection = Mock()
    if protocol == "imap":
        monkeypatch.setattr(imap_client, "_CappedIMAP4", Mock(return_value=connection))
        cfg = ImapConfig(host="mail.invalid", tls="starttls", username="u", password="REPLACE_ME")
        with imap_client._connect(cfg):
            pass
    else:
        monkeypatch.setattr(smtp_client.smtplib, "SMTP", Mock(return_value=connection))
        cfg = SmtpConfig(
            host="mail.invalid",
            tls="starttls",
            username="u",
            password="REPLACE_ME",
            from_address="sender@example.org",
        )
        smtp_client._open(cfg)
    args, kwargs = connection.starttls.call_args
    context = kwargs.get("ssl_context") or kwargs.get("context") or (args[0] if args else None)
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
