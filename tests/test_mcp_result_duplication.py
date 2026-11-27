import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mailboxd.config import ConfigError, load_config
from mailboxd.mcp_server import _error, _ok
from mailboxd.server import create_app

_TOKEN = "result-content-token-0123456789abcdef"
_ATTACHMENT = base64.b64encode(b"\x00" * 300_000).decode()


@pytest.fixture(autouse=True)
def _state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", str(tmp_path / "state.db"))


def _config(tmp_path: Path, mcp_block: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
auth:
  tokens: [{_TOKEN}]
{mcp_block}
mailboxes:
  - name: alpha
    imap: {{host: imap.example.com, username: alpha@example.com, password: p}}
""")
    return path


def _call_mailboxes(tmp_path: Path, mcp_block: str) -> dict[str, Any]:
    app = create_app(load_config(str(_config(tmp_path, mcp_block))))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mailboxes", "arguments": {}},
            },
            follow_redirects=True,
        )
    assert response.status_code == 200
    if response.headers["content-type"].startswith("application/json"):
        return response.json()["result"]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))["result"]
    raise AssertionError(f"no JSON payload in {response.text!r}")


def test_both_returns_the_payload_in_text_and_structured_content() -> None:
    result = _ok({"content_base64": _ATTACHMENT}, "both")

    serialized = result.model_dump_json(by_alias=True, exclude_none=True)
    assert serialized.count(_ATTACHMENT) == 2
    assert json.loads(result.content[0].text) == result.structuredContent


def test_text_returns_the_payload_once_as_text() -> None:
    result = _ok({"content_base64": _ATTACHMENT}, "text")

    serialized = result.model_dump_json(by_alias=True, exclude_none=True)
    assert serialized.count(_ATTACHMENT) == 1
    assert result.structuredContent is None
    assert json.loads(result.content[0].text)["content_base64"] == _ATTACHMENT


def test_structured_returns_the_payload_once_as_structured_content() -> None:
    result = _ok({"content_base64": _ATTACHMENT}, "structured")

    serialized = result.model_dump_json(by_alias=True, exclude_none=True)
    assert serialized.count(_ATTACHMENT) == 1
    assert result.content == []
    assert result.structuredContent == {"content_base64": _ATTACHMENT}


def test_non_object_results_are_wrapped_in_every_mode() -> None:
    for mode in ("both", "structured"):
        assert _ok([1, 2], mode).structuredContent == {"result": [1, 2]}
    assert json.loads(_ok([1, 2], "text").content[0].text) == {"result": [1, 2]}


def test_errors_keep_their_text_block() -> None:
    result = _error("not_found", "message not found", uid=42)

    assert result.isError
    assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.parametrize(
    ("mcp_block", "has_text", "has_structured"),
    [
        ("", True, True),
        ("mcp: {result_content: both}", True, True),
        ("mcp: {result_content: text}", True, False),
        ("mcp: {result_content: structured}", False, True),
    ],
)
def test_configured_result_content_reaches_the_mcp_endpoint(
    tmp_path: Path, mcp_block: str, has_text: bool, has_structured: bool
) -> None:
    result = _call_mailboxes(tmp_path, mcp_block)

    assert not result.get("isError")
    assert bool(result.get("content")) is has_text
    assert ("structuredContent" in result) is has_structured
    if has_structured:
        payload = result["structuredContent"]
    else:
        payload = json.loads(result["content"][0]["text"])
    assert payload["mailboxes"][0]["name"] == "alpha"


def test_unknown_result_content_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="result_content"):
        load_config(str(_config(tmp_path, "mcp: {result_content: neither}")))
