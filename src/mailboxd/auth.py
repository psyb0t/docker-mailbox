"""Bearer-token auth shared by the HTTP API and the streamable-HTTP MCP server.

An empty `cfg.auth.tokens` list disables auth (everything 200s). Once any
token is configured, every request to a protected endpoint must carry
`Authorization: Bearer <token>` matching one of the configured tokens
(constant-time compared). 401 responses include `WWW-Authenticate: Bearer`.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from .config import AuthConfig

_BEARER_PREFIX = "Bearer "


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    if not header.startswith(_BEARER_PREFIX):
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


def check_bearer(auth_cfg: AuthConfig, request: Request) -> None:
    """Validate the Authorization header against the configured token set.

    No-op when no tokens are configured. Raises 401 otherwise.
    """
    if not auth_cfg.tokens:
        return
    presented = _extract_token(request)
    if presented is None:
        raise HTTPException(
            status_code=401,
            detail="missing or malformed Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    for tok in auth_cfg.tokens:
        if hmac.compare_digest(tok, presented):
            return
    raise HTTPException(
        status_code=401,
        detail="invalid Bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
