"""Bearer-token auth on the HTTP API and the streamable-HTTP MCP transport."""

from pathlib import Path

from fastapi.testclient import TestClient

from mailboxd.config import load_config
from mailboxd.server import create_app


def _write_cfg(tmp_path: Path, tokens: list[str]) -> Path:
    tokens_yaml = (
        "\n".join(f'    - "{t}"' for t in tokens) if tokens else "    []"
    )
    if not tokens:
        auth_block = "auth: {tokens: []}"
    else:
        auth_block = "auth:\n  tokens:\n" + tokens_yaml
    p = tmp_path / "config.yaml"
    p.write_text(
        f"""
{auth_block}
mailboxes:
  - name: alpha
    imap: {{host: imap.example.com, username: u, password: p}}
    smtp: {{host: smtp.example.com, username: u, password: p, from_address: u@example.com}}
"""
    )
    return p


def test_no_tokens_means_no_auth(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, [])
    c = TestClient(create_app(load_config(str(cfg_path))))
    assert c.get("/health").status_code == 200
    assert c.get("/mailboxes").status_code == 200


def test_health_is_always_exempt(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    r = c.get("/health")
    assert r.status_code == 200


def test_missing_bearer_is_401(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    r = c.get("/mailboxes")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_malformed_bearer_is_401(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    r = c.get("/mailboxes", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


def test_wrong_bearer_is_401(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    r = c.get("/mailboxes", headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401


def test_valid_bearer_passes(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1", "secret-token-2"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    r = c.get("/mailboxes", headers={"Authorization": "Bearer secret-token-2"})
    assert r.status_code == 200


def test_mcp_endpoint_requires_bearer(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    c = TestClient(create_app(load_config(str(cfg_path))))
    # Hit the MCP transport without a token — must 401, must NOT reach the
    # MCP handler.
    r = c.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_mcp_endpoint_passes_with_bearer(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, ["secret-token-1"])
    # The MCP session manager needs the FastAPI lifespan to fire, which
    # TestClient only does when used as a context manager.
    with TestClient(create_app(load_config(str(cfg_path)))) as c:
        r = c.post(
            "/mcp",
            headers={
                "Authorization": "Bearer secret-token-1",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
    # Auth let us through — handler's response code is whatever (200 / 202 /
    # session-id-issued); what matters is it is NOT 401.
    assert r.status_code != 401
