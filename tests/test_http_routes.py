"""Every HTTP route delegates to the matching Service method with the right arguments."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app
from mailboxd.services import Service

TOKEN = "EXAMPLE-DO-NOT-USE"
CONFIG = f"""
auth:
  tokens: [{TOKEN}]
mailboxes:
  - name: alpha
    imap: {{host: imap.example.com, username: u, password: p}}
    smtp: {{host: smtp.example.com, username: u, password: p, from_address: u@example.com}}
"""

COMPOSE = {"to": ["a@example.com"], "subject": "s", "body_text": "b"}

# (method, path, json body, Service method, expected kwargs subset)
ROUTES: list[tuple[str, str, Any, str, dict[str, Any]]] = [
    ("GET", "/health/deep", None, "health_deep", {}),
    ("GET", "/mailboxes", None, "mailboxes", {}),
    ("GET", "/audit?action=send&limit=5", None, "audit_log", {"action": "send", "limit": 5}),
    (
        "GET",
        "/inbox?mailbox=alpha&label=suspect&from=bob&recipient=me&checkpoint=c&limit=3",
        None,
        "inbox",
        {"mailbox": "alpha", "label": "suspect", "from_": "bob", "recipient": "me",
         "checkpoint": "c", "limit": 3},
    ),
    ("GET", "/mailboxes/alpha/folders", None, "folders", {}),
    ("POST", "/mailboxes/alpha/folders", {"name": "X"}, "create_folder", {}),
    (
        "GET",
        "/mailboxes/alpha/messages?folder=F&limit=7&cursor=abc",
        None,
        "list_messages",
        {"folder": "F", "limit": 7, "cursor": "abc"},
    ),
    (
        "GET",
        "/mailboxes/alpha/search?subject=hi&has_attachments=true&since_cursor=z",
        None,
        "search",
        {"subject": "hi", "has_attachments": True, "since_cursor": "z"},
    ),
    (
        "GET",
        "/mailboxes/alpha/messages/5?reader=true&headers=true&safe_html=true",
        None,
        "get_message",
        {"reader": True, "headers": True, "safe_html": True},
    ),
    ("GET", "/mailboxes/alpha/messages/5/thread", None, "thread", {}),
    ("GET", "/mailboxes/alpha/threads?limit=4", None, "threads", {"limit": 4}),
    (
        "DELETE",
        "/mailboxes/alpha/messages/5?permanent=true&folder=Trash",
        None,
        "delete",
        {"permanent": True, "folder": "Trash"},
    ),
    (
        "POST",
        "/mailboxes/alpha/messages/delete",
        {"uids": ["1", "2"], "permanent": False},
        "delete",
        {"permanent": False},
    ),
    ("POST", "/mailboxes/alpha/messages/5/seen", {"seen": False}, "mark_seen", {"seen": False}),
    ("POST", "/mailboxes/alpha/messages/seen", {"uids": ["1"]}, "mark_seen", {"seen": True}),
    (
        "POST",
        "/mailboxes/alpha/messages/labels?folder=F",
        {"uids": ["1"], "add": ["suspect"]},
        "set_labels",
        {"add": ["suspect"], "remove": [], "folder": "F"},
    ),
    (
        "POST",
        "/mailboxes/alpha/messages/move",
        {"uids": ["1"], "to_folder": "Archive"},
        "move",
        {},
    ),
    ("POST", "/mailboxes/alpha/send", COMPOSE, "send", {}),
    ("POST", "/mailboxes/alpha/messages/5/reply", {"body_text": "ok"}, "reply", {}),
    ("GET", "/mailboxes/alpha/drafts?limit=2", None, "list_drafts", {"limit": 2}),
    ("POST", "/mailboxes/alpha/drafts", COMPOSE, "create_draft", {}),
    ("PUT", "/mailboxes/alpha/drafts/9", {"subject": "new"}, "update_draft", {}),
    ("DELETE", "/mailboxes/alpha/drafts/9", None, "delete_draft", {}),
    ("POST", "/mailboxes/alpha/drafts/9/send", {"dry_run": True}, "send_draft", {"dry_run": True}),
    ("GET", "/mailboxes/alpha/identities", None, "identities", {}),
    ("GET", "/mailboxes/alpha/stats", None, "stats", {}),
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    app = create_app(load_config(str(path)))
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.mark.parametrize("method,path,body,target,expected", ROUTES, ids=[r[1] for r in ROUTES])
def test_route_delegates_to_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: Any,
    target: str,
    expected: dict[str, Any],
) -> None:
    mock = Mock(return_value={"ok": True})
    monkeypatch.setattr(Service, target, mock)
    r = client.request(method, path, json=body)
    assert r.status_code == 200, r.text
    mock.assert_called_once()
    args, kwargs = mock.call_args
    assert args[0].name == "legacy-0"
    for key, value in expected.items():
        assert kwargs[key] == value, (key, kwargs)


def test_dependency_gives_each_request_its_own_principal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake(self: Any, p: Any, *a: Any, **k: Any) -> dict[str, Any]:
        seen.append(p.name)
        return {"mailboxes": []}

    monkeypatch.setattr(Service, "mailboxes", fake)
    client.get("/mailboxes")
    assert seen == ["legacy-0"]
