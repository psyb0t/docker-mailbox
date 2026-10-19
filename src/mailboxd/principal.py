"""Who is calling, and what they may do.

The bearer middleware resolves the presented token to a `Principal` and
stores it on the ASGI scope; both the HTTP routes and the MCP tools read it
from there and call `require()` before touching a mailbox.

Scopes:
  read      list / search / read messages, raw source, attachments, threads, stats
  draft     create, update and delete drafts (incl. reply drafts)
  organize  mark seen, labels, move, create folders, move to Trash
  send      send mail, send a draft, reply directly
  delete    permanent deletion (from Trash only)
  admin     audit log, deep health check
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Mapping

from .config import Config, MailboxConfig
from .errors import NotFound, PermissionDenied

SCOPE_KEY = "mailboxd.principal"


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]
    # None means every mailbox.
    mailboxes: frozenset[str] | None = None
    dry_run: bool = False

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def can_see(self, mailbox: str) -> bool:
        return self.mailboxes is None or mailbox in self.mailboxes

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionDenied(
                f"token {self.name!r} lacks the {scope!r} scope", required_scope=scope
            )


def authenticate(cfg: Config, presented: str) -> Principal | None:
    """Constant-time lookup of a presented bearer token."""
    match = None
    for t in cfg.auth.token_configs():
        # No early exit: compare against every token so timing does not leak which matched.
        if hmac.compare_digest(t.token.encode(), presented.encode()):
            match = t
    if match is None:
        return None
    return Principal(
        name=match.name,
        scopes=frozenset(match.scopes),
        mailboxes=None if match.mailboxes == "*" else frozenset(match.mailboxes),
        dry_run=match.dry_run,
    )


def from_scope(scope: Mapping[str, Any]) -> Principal:
    p = scope.get(SCOPE_KEY)
    if not isinstance(p, Principal):
        raise PermissionDenied("request is not authenticated")
    return p


def visible_mailboxes(cfg: Config, p: Principal) -> list[MailboxConfig]:
    return [m for m in cfg.mailboxes if p.can_see(m.name)]


def resolve_mailbox(
    cfg: Config, p: Principal, ident: str, by_address: bool = True
) -> MailboxConfig:
    """Find a mailbox by name (or address). Hidden mailboxes look exactly like unknown ones."""
    if not ident:
        raise NotFound("`mailbox` is required")
    for m in cfg.mailboxes:
        hit = m.name == ident
        if by_address and not hit:
            hit = (m.imap is not None and m.imap.username == ident) or (
                m.smtp is not None and m.smtp.username == ident
            )
        if hit and p.can_see(m.name):
            return m
    raise NotFound(f"unknown mailbox: {ident!r}")
