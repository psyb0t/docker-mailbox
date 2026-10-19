"""Scoped tokens, mailbox allow-lists, error body shape and rate limiting over HTTP."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.errors import RateLimited
from mailboxd.imap_client import ImapError
from mailboxd.server import create_app
from mailboxd.services import Service
from mailboxd.smtp_client import SmtpError

LIMITED = "LIMITED-EXAMPLE-DO-NOT-USE"
ADMIN = "ADMIN-EXAMPLE-DO-NOT-USE"
READER = "READER-EXAMPLE-DO-NOT-USE"

CONFIG = f"""
auth:
  tokens:
    - name: limited
      token: {LIMITED}
      scopes: [read, draft, organize]
      mailboxes: [alpha]
    - name: admin
      token: {ADMIN}
      scopes: [read, draft, organize, send, delete, admin]
    - name: reader
      token: {READER}
      scopes: [read]
mailboxes:
  - name: alpha
    imap: {{host: imap.example.com, username: alpha@example.org, password: p}}
    smtp: {{host: smtp.example.com, username: alpha@example.org, password: p,
           from_address: alpha@example.org}}
  - name: beta
    imap: {{host: imap.example.com, username: beta@example.org, password: p}}
    smtp: {{host: smtp.example.com, username: beta@example.org, password: p,
           from_address: beta@example.org}}
"""

SEND_BODY = {"to": ["dest@example.com"], "subject": "s", "body_text": "b"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return TestClient(create_app(load_config(str(path))))


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_limited_token_sees_only_its_mailbox(client: TestClient) -> None:
    r = client.get("/mailboxes", headers=_h(LIMITED))
    assert r.status_code == 200
    assert [m["name"] for m in r.json()["mailboxes"]] == ["alpha"]
    admin = client.get("/mailboxes", headers=_h(ADMIN))
    assert [m["name"] for m in admin.json()["mailboxes"]] == ["alpha", "beta"]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/mailboxes/alpha/send", SEND_BODY),
        ("POST", "/mailboxes/alpha/drafts/1/send", {}),
        ("DELETE", "/mailboxes/alpha/messages/1?permanent=true", None),
        ("POST", "/mailboxes/alpha/messages/delete", {"uids": ["1"], "permanent": True}),
    ],
)
def test_missing_scope_is_403(client: TestClient, method: str, path: str, body: object) -> None:
    r = client.request(method, path, json=body, headers=_h(LIMITED))
    assert r.status_code == 403, r.text
    payload = r.json()
    assert payload["error"]["code"] == "permission_denied"
    assert payload["detail"] == payload["error"]["message"]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/mailboxes/beta/folders", None),
        ("GET", "/mailboxes/beta/messages", None),
        ("GET", "/mailboxes/beta/messages/1", None),
        ("GET", "/mailboxes/beta/identities", None),
        ("POST", "/mailboxes/beta/send", SEND_BODY),
        ("POST", "/mailboxes/beta/drafts", SEND_BODY),
    ],
)
def test_hidden_mailbox_is_404(client: TestClient, method: str, path: str, body: object) -> None:
    r = client.request(method, path, json=body, headers=_h(LIMITED))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"


def test_hidden_mailbox_looks_like_unknown(client: TestClient) -> None:
    hidden = client.get("/mailboxes/beta/folders", headers=_h(LIMITED))
    unknown = client.get("/mailboxes/nope/folders", headers=_h(LIMITED))
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json()["error"]["code"] == unknown.json()["error"]["code"]


def test_reading_visible_mailbox_is_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ret = {"folders": ["INBOX"], "details": []}
    monkeypatch.setattr(Service, "folders", Mock(return_value=ret))
    r = client.get("/mailboxes/alpha/folders", headers=_h(LIMITED))
    assert r.status_code == 200
    assert r.json()["folders"] == ["INBOX"]


def test_inbox_filter_on_hidden_mailbox_is_404(client: TestClient) -> None:
    r = client.get("/inbox?mailbox=beta", headers=_h(LIMITED))
    assert r.status_code == 404


def test_organize_needs_scope(client: TestClient) -> None:
    r = client.post("/mailboxes/alpha/messages/seen", json={"uids": ["1"]}, headers=_h(READER))
    assert r.status_code == 403
    r = client.post(
        "/mailboxes/alpha/messages/move",
        json={"uids": ["1"], "to_folder": "X"},
        headers=_h(READER),
    )
    assert r.status_code == 403


def test_audit_is_admin_only(client: TestClient) -> None:
    assert client.get("/audit", headers=_h(LIMITED)).status_code == 403
    assert client.get("/health/deep", headers=_h(LIMITED)).status_code == 403
    r = client.get("/audit", headers=_h(ADMIN))
    assert r.status_code == 200
    assert "entries" in r.json()


def test_denied_mutation_is_audited(client: TestClient) -> None:
    client.post("/mailboxes/alpha/send", json=SEND_BODY, headers=_h(LIMITED))
    entries = client.get("/audit", headers=_h(ADMIN)).json()["entries"]
    assert any(e.get("token") == "limited" and e.get("outcome") != "ok" for e in entries)


def test_rate_limited_sets_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RateLimited("send rate limit reached", retry_after=42)

    monkeypatch.setattr(Service, "send", boom)
    r = client.post("/mailboxes/alpha/send", json=SEND_BODY, headers=_h(ADMIN))
    assert r.status_code == 429
    assert r.headers["retry-after"] == "42"
    body = r.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["details"] == {"retry_after": 42}


@pytest.mark.parametrize(
    "exc,code",
    [(ImapError("imap down"), "imap_error"), (SmtpError("smtp down"), "smtp_error")],
)
def test_upstream_errors_are_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: str
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise exc

    monkeypatch.setattr(Service, "folders", boom)
    r = client.get("/mailboxes/alpha/folders", headers=_h(ADMIN))
    assert r.status_code == 502
    assert r.json() == {"error": {"code": code, "message": str(exc)}, "detail": str(exc)}


def test_validation_error_shape(client: TestClient) -> None:
    r = client.post(
        "/mailboxes/alpha/send", json={"to": [], "subject": "x"}, headers=_h(ADMIN)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_401_body_unchanged(client: TestClient) -> None:
    r = client.get("/mailboxes")
    assert r.status_code == 401
    assert r.json() == {"detail": "missing or invalid Bearer token"}
    assert r.headers["www-authenticate"] == "Bearer"


def test_attachment_download_headers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"filename": '../ev"il.html', "content_type": "text/html"}
    monkeypatch.setattr(Service, "get_attachment", Mock(return_value=(info, b"<script>x</script>")))
    r = client.get("/mailboxes/alpha/messages/1/attachments/2", headers=_h(LIMITED))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "/" not in disposition.split("filename=")[1].split(";")[0]
    assert r.content == b"<script>x</script>"


def test_raw_source_is_rfc822_attachment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Service, "get_raw", Mock(return_value=b"Subject: x\r\n\r\nbody"))
    r = client.get("/mailboxes/alpha/messages/7/raw", headers=_h(LIMITED))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("message/rfc822")
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.content == b"Subject: x\r\n\r\nbody"
