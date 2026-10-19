"""Typed errors shared by the HTTP API and the MCP server.

Every error carries a stable machine-readable `code` and the HTTP status the
API answers with. The MCP layer reports the same `{code, message}` pair inside
an `isError` tool result, so agents can branch on `code` either way.
"""

from __future__ import annotations

from typing import Any


class MailboxdError(Exception):
    code = "error"
    status = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class NotFound(MailboxdError):
    code = "not_found"
    status = 404


class PermissionDenied(MailboxdError):
    code = "permission_denied"
    status = 403


class Conflict(MailboxdError):
    code = "conflict"
    status = 409


class InvalidRequest(MailboxdError):
    code = "validation_error"
    status = 422


class InvalidIdentity(MailboxdError):
    code = "invalid_identity"
    status = 422


class TooLarge(MailboxdError):
    code = "too_large"
    status = 413


class RateLimited(MailboxdError):
    code = "rate_limited"
    status = 429

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after
