"""Mailbox selection and credential forwarding through the production MCP route."""

import json
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.principal import resolve_mailbox
from mailboxd.server import create_app
from mailboxd.services import Service

TOKENS = ["EXAMPLE-DO-NOT-USE-primary", "EXAMPLE-DO-NOT-USE-secondary"]
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
SELECTORS = [
    pytest.param("alpha", "alpha", id="alpha-name"),
    pytest.param("alpha@example.org", "alpha", id="alpha-address"),
    pytest.param("beta", "beta", id="beta-name"),
    pytest.param("beta@example.org", "beta", id="beta-address"),
]
OPERATIONS = [
    pytest.param("get_message", "get_message", {"uid": "42"}, id="read"),
    pytest.param("delete_message", "delete", {"uid": "42"}, id="delete"),
    pytest.param(
        "send",
        "send",
        {"to": ["recipient@example.org"], "subject": "test", "body_text": "test"},
        id="send",
    ),
]
EMPTY_AUTH = [
    pytest.param(None, id="auth-omitted"),
    pytest.param({}, id="auth-empty"),
    pytest.param({"tokens": []}, id="tokens-empty"),
]
INVALID_HEADERS = [
    pytest.param({}, id="missing"),
    pytest.param({"Authorization": "Bearer EXAMPLE-DO-NOT-USE-wrong"}, id="wrong"),
    pytest.param({"Authorization": f"Basic {TOKENS[0]}"}, id="not-bearer"),
]
MAIL_RESULT = {"uid": "42", "subject": "test", "ok": True}
AUTH_ERROR = {"detail": "missing or invalid Bearer token"}


def _call(client, headers, tool, selector, arguments):
    return client.post(
        "/mcp",
        headers={**MCP_HEADERS, **headers},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"mailbox": selector, **arguments}},
        },
    )


def _result(response, expect_error=None):
    assert response.status_code == HTTPStatus.OK, response.text
    if response.headers["content-type"].startswith("application/json"):
        payload = response.json()
    else:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert len(events) == 1
        payload = events[0]
    assert payload["id"] == 1
    assert "error" not in payload
    result = payload["result"]
    assert bool(result.get("isError", False)) == (expect_error is not None)
    return json.loads(result["content"][0]["text"])


class _Boundary:
    """Stands in for one Service method.

    Like the real service it resolves the mailbox selector for the calling principal first,
    so what it records is exactly the credentials that would be used for IMAP/SMTP.
    """

    def __init__(self):
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def assert_not_called(self):
        assert not self.calls

    def call(self, service, principal, mailbox, *args, **kwargs):
        mb = resolve_mailbox(service.cfg, principal, mailbox)
        self.calls.append((principal.name, mb, args, kwargs))
        return MAIL_RESULT


def _boundary(monkeypatch, method):
    operation = _Boundary()
    monkeypatch.setattr(
        Service, method, lambda service, *args, **kwargs: operation.call(service, *args, **kwargs)
    )
    return operation


def _assert_credentials(operation, method, mailbox, token):
    assert operation.call_count == 1
    principal_name, mb, args, kwargs = operation.calls[0]
    assert principal_name.startswith("legacy-")
    assert token in TOKENS
    assert mb.name == mailbox
    assert mb.imap.host == "mail.invalid"
    assert mb.imap.username == f"{mailbox}@example.org"
    assert mb.imap.password == f"EXAMPLE-DO-NOT-USE-{mailbox}"
    assert mb.smtp.password == f"EXAMPLE-DO-NOT-USE-{mailbox}"
    assert mb.smtp.from_address == f"{mailbox}@example.org"
    if method == "send":
        (request,) = args
        assert request.to == ["recipient@example.org"]
        assert request.subject == "test"
    elif method == "delete":
        assert args == (["42"],)
        assert kwargs["permanent"] is False
    else:
        assert args == ("42",)


@pytest.fixture(autouse=True)
def _state_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("selector,mailbox", SELECTORS)
@pytest.mark.parametrize("tool,method,arguments", OPERATIONS)
def test_each_token_can_operate_on_either_mailbox_by_name_or_address(
    security_config_factory, monkeypatch, token, selector, mailbox, tool, method, arguments
):
    operation = _boundary(monkeypatch, method)
    config = load_config(str(security_config_factory({"tokens": TOKENS})))
    with TestClient(create_app(config)) as client:
        response = _call(client, {"Authorization": f"Bearer {token}"}, tool, selector, arguments)
    assert _result(response) == MAIL_RESULT
    _assert_credentials(operation, method, mailbox, token)


@pytest.mark.parametrize("auth", EMPTY_AUTH)
@pytest.mark.parametrize("selector,mailbox", SELECTORS)
@pytest.mark.parametrize("tool,method,arguments", OPERATIONS)
def test_empty_auth_blocks_mcp_mail_operations(
    security_config_factory, monkeypatch, auth, selector, mailbox, tool, method, arguments
):
    operation = _boundary(monkeypatch, method)
    config = load_config(str(security_config_factory(auth)))
    with TestClient(create_app(config)) as client:
        response = _call(client, {}, tool, selector, arguments)
    assert (
        response.status_code == HTTPStatus.UNAUTHORIZED
    ), f"Anonymous MCP {tool} admitted for {selector}; mail calls={operation.call_count}"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == AUTH_ERROR
    operation.assert_not_called()


@pytest.mark.parametrize("headers", INVALID_HEADERS)
@pytest.mark.parametrize("selector,mailbox", SELECTORS)
@pytest.mark.parametrize("tool,method,arguments", OPERATIONS)
def test_invalid_auth_cannot_select_mailbox_credentials(
    security_config_factory, monkeypatch, headers, selector, mailbox, tool, method, arguments
):
    operation = _boundary(monkeypatch, method)
    config = load_config(str(security_config_factory({"tokens": TOKENS})))
    with TestClient(create_app(config)) as client:
        response = _call(client, headers, tool, selector, arguments)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == AUTH_ERROR
    operation.assert_not_called()


@pytest.mark.parametrize("selector", ["unknown", ""])
@pytest.mark.parametrize("tool,method,arguments", OPERATIONS)
def test_invalid_selector_never_reaches_mail_service(
    security_config_factory, monkeypatch, selector, tool, method, arguments
):
    operation = _boundary(monkeypatch, method)
    config = load_config(str(security_config_factory({"tokens": TOKENS})))
    with TestClient(create_app(config)) as client:
        response = _call(
            client, {"Authorization": f"Bearer {TOKENS[0]}"}, tool, selector, arguments
        )
    body = _result(response, expect_error=True)
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == (
        "unknown mailbox: 'unknown'" if selector else "`mailbox` is required"
    )
    operation.assert_not_called()
