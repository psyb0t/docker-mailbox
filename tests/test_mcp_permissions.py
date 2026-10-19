"""Per-token tool visibility, annotations, scope enforcement and validation over MCP."""

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app
from mailboxd.services import Service

LIMITED = "EXAMPLE-DO-NOT-USE-limited"
FULL = "EXAMPLE-DO-NOT-USE-full"
ADMIN_ONLY = "EXAMPLE-DO-NOT-USE-admin"
INLINE_CAP = 1024
HEADERS = {"Accept": "application/json, text/event-stream"}
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
SEND_ARGS = {"mailbox": "alpha", "to": ["x@example.org"], "subject": "s", "body_text": "b"}


@pytest.fixture(autouse=True)
def _state_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))


@pytest.fixture
def client(tmp_path: Path):
    credentials = {"host": "mail.invalid", "username": "alpha@example.org", "password": "p"}
    document: dict[str, Any] = {
        "auth": {
            "tokens": [
                {"name": "agent", "token": LIMITED, "scopes": ["read", "draft", "organize"]},
                {"name": "boss", "token": FULL},
                {"name": "ops", "token": ADMIN_ONLY, "scopes": ["admin"]},
            ]
        },
        "limits": {"mcp_inline_max_bytes": INLINE_CAP},
        "mailboxes": [
            {
                "name": "alpha",
                "imap": credentials,
                "smtp": {**credentials, "from_address": "alpha@example.org"},
            }
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with TestClient(create_app(load_config(str(path)))) as c:
        yield c


def _rpc(client, token, method, params=None):
    response = client.post(
        "/mcp",
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()["result"]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))["result"]
    raise AssertionError(response.text)


def _tools(client, token):
    return {t["name"]: t for t in _rpc(client, token, "tools/list")["tools"]}


def _call(client, token, name, arguments):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": arguments})


def _error(result):
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert result["structuredContent"] == body
    return body["error"]


def test_limited_token_does_not_see_send_tools(client):
    tools = _tools(client, LIMITED)
    for hidden in ("send", "send_draft", "audit_log", "health_check"):
        assert hidden not in tools
    for shown in ("mailboxes", "search", "create_draft", "reply", "mark_seen", "delete_message"):
        assert shown in tools


def test_permanent_argument_requires_delete_scope(client):
    limited = _tools(client, LIMITED)["delete_message"]["inputSchema"]["properties"]
    full = _tools(client, FULL)["delete_message"]["inputSchema"]["properties"]
    assert "permanent" not in limited
    assert "permanent" in full


def test_reply_without_send_scope_forces_draft(client):
    schema = _tools(client, LIMITED)["reply"]["inputSchema"]
    assert schema["properties"]["draft"]["const"] is True
    assert "draft" in schema["required"]
    assert "draft" not in _tools(client, FULL)["reply"]["inputSchema"].get("required", [])


def test_full_token_sees_everything_and_admin_token_only_admin(client):
    full = _tools(client, FULL)
    assert {"send", "send_draft", "audit_log", "health_check", "reply"} <= set(full)
    admin = _tools(client, ADMIN_ONLY)
    assert {"mailboxes", "audit_log", "health_check"} == set(admin)


def test_every_tool_has_annotations_and_strict_schema(client):
    tools = _tools(client, FULL)
    assert len(tools) == 26
    for name, tool in tools.items():
        annotations = tool["annotations"]
        assert annotations["title"], name
        assert all(annotations[h] is not None for h in HINTS), name
        assert tool["inputSchema"]["additionalProperties"] is False, name
        assert tool["description"], name
    assert tools["mailboxes"]["annotations"]["readOnlyHint"] is True
    assert tools["send"]["annotations"]["destructiveHint"] is True
    assert tools["send"]["annotations"]["openWorldHint"] is True
    assert tools["create_draft"]["annotations"]["openWorldHint"] is False
    assert tools["delete_message"]["annotations"]["destructiveHint"] is True
    for idempotent in ("mark_seen", "set_labels", "move_messages"):
        assert tools[idempotent]["annotations"]["idempotentHint"] is True


def test_calling_send_without_scope_is_permission_denied(client, monkeypatch):
    spy = Mock()
    monkeypatch.setattr(Service, "send", spy)
    error = _error(_call(client, LIMITED, "send", SEND_ARGS))
    assert error["code"] == "permission_denied"
    spy.assert_not_called()


def test_permanent_delete_and_direct_reply_denied_without_scope(client, monkeypatch):
    delete, reply = Mock(), Mock()
    monkeypatch.setattr(Service, "delete", delete)
    monkeypatch.setattr(Service, "reply", reply)
    args = {"mailbox": "alpha", "uid": "1", "folder": "Trash", "permanent": True}
    assert _error(_call(client, LIMITED, "delete_message", args))["code"] == "permission_denied"
    reply_args = {"mailbox": "alpha", "uid": "1", "body_text": "hi"}
    assert _error(_call(client, LIMITED, "reply", reply_args))["code"] == "permission_denied"
    delete.assert_not_called()
    reply.assert_not_called()


def test_validation_error_is_reported_as_tool_error(client):
    missing = _error(_call(client, LIMITED, "get_message", {"mailbox": "alpha"}))
    assert missing["code"] == "validation_error"
    extra = _error(_call(client, LIMITED, "mailboxes", {"bogus": 1}))
    assert extra["code"] == "validation_error"
    bad_type = _error(_call(client, LIMITED, "search", {"mailbox": "alpha", "limit": "ten"}))
    assert bad_type["code"] == "validation_error"


def test_unknown_tool_is_not_found(client):
    assert _error(_call(client, LIMITED, "nope", {}))["code"] == "not_found"


def test_success_returns_structured_content(client, monkeypatch):
    monkeypatch.setattr(Service, "folders", Mock(return_value={"folders": ["INBOX"]}))
    result = _call(client, LIMITED, "list_folders", {"mailbox": "alpha"})
    assert not result.get("isError")
    assert result["structuredContent"] == {"folders": ["INBOX"]}
    assert json.loads(result["content"][0]["text"]) == {"folders": ["INBOX"]}


def test_raw_message_is_truncated_at_inline_limit(client, monkeypatch):
    monkeypatch.setattr(Service, "get_raw", Mock(return_value=b"a" * (INLINE_CAP + 10)))
    result = _call(client, LIMITED, "get_raw_message", {"mailbox": "alpha", "uid": "1"})
    body = result["structuredContent"]
    assert body["truncated"] is True
    assert len(body["raw"]) == INLINE_CAP
    assert body["size"] == INLINE_CAP + 10


def test_attachment_is_base64_and_capped(client, monkeypatch):
    info = {"part_id": "2", "filename": "a.txt", "content_type": "text/plain"}
    args = {"mailbox": "alpha", "uid": "1", "part_id": "2"}
    monkeypatch.setattr(Service, "get_attachment", Mock(return_value=(info, b"hello")))
    body = _call(client, LIMITED, "get_attachment", args)["structuredContent"]
    assert base64.b64decode(body["data_base64"]) == b"hello"
    monkeypatch.setattr(
        Service, "get_attachment", Mock(return_value=(info, b"x" * (INLINE_CAP + 1)))
    )
    assert _error(_call(client, LIMITED, "get_attachment", args))["code"] == "too_large"
