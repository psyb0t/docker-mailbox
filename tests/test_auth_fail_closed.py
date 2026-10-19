"""Fail-closed regressions for GHSA-q49x-3w8v-hc73 through REST, MCP, and TCP."""

import json
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app


@pytest.fixture(autouse=True)
def _state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))


TOKEN = "EXAMPLE-DO-NOT-USE-primary"
SECOND_TOKEN = "EXAMPLE-DO-NOT-USE-secondary"
EMPTY_AUTH = [
    pytest.param(None, id="auth-omitted"),
    pytest.param({}, id="auth-empty"),
    pytest.param({"tokens": []}, id="tokens-empty"),
]
OPERATIONS = [
    pytest.param("GET", "/mailboxes", None, None, id="metadata"),
    pytest.param("GET", "/mailboxes/alpha/messages/1", None, "get_message", id="read"),
    pytest.param("DELETE", "/mailboxes/alpha/messages/1", None, "delete", id="delete"),
    pytest.param(
        "POST",
        "/mailboxes/alpha/send",
        {"to": ["recipient@example.org"], "subject": "test", "body_text": "test"},
        "send",
        id="send",
    ),
]
INVALID_AUTH = [
    pytest.param({}, id="missing"),
    pytest.param({"Authorization": "Bearer EXAMPLE-DO-NOT-USE-wrong"}, id="wrong"),
    pytest.param({"Authorization": f"Basic {TOKEN}"}, id="not-bearer"),
]
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
MCP_METHODS = [
    pytest.param(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
        id="initialize",
    ),
    pytest.param("tools/call", {"name": "mailboxes", "arguments": {}}, id="tool-call"),
]
AUTH_ERROR = {"detail": "missing or invalid Bearer token"}


def _mail_boundary(monkeypatch, name):
    if name is None:
        return None
    operation = Mock(return_value={"uid": "1", "subject": "test", "ok": True})
    monkeypatch.setattr(f"mailboxd.services.Service.{name}", operation)
    return operation


def _assert_denied(response, operation=None):
    calls = operation.call_count if operation is not None else 0
    assert (
        response.status_code == HTTPStatus.UNAUTHORIZED
    ), f"Unauthorized request admitted: status={response.status_code}, mail calls={calls}"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == AUTH_ERROR
    if operation is not None:
        operation.assert_not_called()


@pytest.mark.parametrize("auth", EMPTY_AUTH)
@pytest.mark.parametrize("headers", [{}, {"Authorization": f"Bearer {TOKEN}"}])
@pytest.mark.parametrize("method,path,body,boundary", OPERATIONS)
def test_empty_auth_denies_http_operations(
    security_config_factory, monkeypatch, auth, headers, method, path, body, boundary
):
    operation = _mail_boundary(monkeypatch, boundary)
    config = load_config(str(security_config_factory(auth)))
    with TestClient(create_app(config)) as client:
        response = client.request(method, path, json=body, headers=headers)
    _assert_denied(response, operation)


@pytest.mark.parametrize("headers", INVALID_AUTH)
@pytest.mark.parametrize("method,path,body,boundary", OPERATIONS)
def test_configured_auth_blocks_invalid_credentials_before_mail_calls(
    security_config_factory, monkeypatch, headers, method, path, body, boundary
):
    operation = _mail_boundary(monkeypatch, boundary)
    config = load_config(str(security_config_factory({"tokens": [TOKEN]})))
    with TestClient(create_app(config)) as client:
        response = client.request(method, path, json=body, headers=headers)
    _assert_denied(response, operation)


@pytest.mark.parametrize("method,path,body,boundary", OPERATIONS)
def test_valid_bearer_reaches_http_operations(
    security_config_factory, monkeypatch, method, path, body, boundary
):
    operation = _mail_boundary(monkeypatch, boundary)
    config = load_config(str(security_config_factory({"tokens": [TOKEN]})))
    with TestClient(create_app(config)) as client:
        response = client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == HTTPStatus.OK, response.text
    if operation is not None:
        operation.assert_called_once()


@pytest.mark.parametrize("auth", EMPTY_AUTH)
@pytest.mark.parametrize("headers", [{}, {"Authorization": f"Bearer {TOKEN}"}])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("method,params", MCP_METHODS)
def test_empty_auth_denies_mcp(security_config_factory, auth, headers, path, method, params):
    config = load_config(str(security_config_factory(auth)))
    with TestClient(create_app(config), follow_redirects=False) as client:
        response = client.post(
            path,
            headers={**MCP_HEADERS, **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    _assert_denied(response)


@pytest.mark.parametrize("headers", INVALID_AUTH)
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("method,params", MCP_METHODS)
def test_configured_auth_denies_invalid_mcp(security_config_factory, headers, path, method, params):
    config = load_config(str(security_config_factory({"tokens": [TOKEN]})))
    with TestClient(create_app(config), follow_redirects=False) as client:
        response = client.post(
            path,
            headers={**MCP_HEADERS, **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    _assert_denied(response)


@pytest.mark.parametrize("token", [TOKEN, SECOND_TOKEN])
def test_rotation_tokens_preserve_shared_mailbox_scope(security_config_factory, token):
    config = load_config(str(security_config_factory({"tokens": [TOKEN, SECOND_TOKEN]})))
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    with TestClient(create_app(config)) as client:
        mailboxes = client.get("/mailboxes", headers=headers)
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mailboxes", "arguments": {}},
            },
        )
    assert mailboxes.status_code == HTTPStatus.OK
    assert {item["name"] for item in mailboxes.json()["mailboxes"]} == {"alpha", "beta"}
    assert response.status_code == HTTPStatus.OK
    if response.headers["content-type"].startswith("application/json"):
        payload = response.json()
    else:
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert len(payloads) == 1
        payload = payloads[0]
    result = json.loads(payload["result"]["content"][0]["text"])
    assert {item["name"] for item in result["mailboxes"]} == {"alpha", "beta"}


@pytest.mark.parametrize("auth", EMPTY_AUTH)
def test_explicit_cli_network_bind_denies_anonymous_access(
    security_config_factory, security_listener_factory, auth
):
    with security_listener_factory(security_config_factory(auth)) as client:
        assert client.get("/health").status_code == HTTPStatus.OK
        _assert_denied(client.get("/mailboxes"))


def test_cli_network_bearer_controls(security_config_factory, security_listener_factory):
    config_path = security_config_factory({"tokens": [TOKEN]})
    with security_listener_factory(config_path) as client:
        _assert_denied(client.get("/mailboxes"))
        _assert_denied(
            client.get("/mailboxes", headers={"Authorization": "Bearer EXAMPLE-DO-NOT-USE-wrong"})
        )
        response = client.get("/mailboxes", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == HTTPStatus.OK
    assert {item["name"] for item in response.json()["mailboxes"]} == {"alpha", "beta"}
