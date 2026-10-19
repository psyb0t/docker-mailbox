"""YAML config loader.

Single file, validated via pydantic. Each mailbox can have any subset of
{imap, smtp} — at least one is required. `name` must be unique and
URL-safe (it becomes a path segment in the HTTP API).

String values may reference environment variables as `${NAME}` so secrets
(passwords, tokens) can stay out of the file.
"""

from __future__ import annotations

import logging
import os
import re
from email.headerregistry import Address
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = "/etc/mailboxd/config.yaml"
DEFAULT_STATE_PATH = "/var/lib/mailboxd/state.db"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

log = logging.getLogger(__name__)

TlsMode = Literal["ssl", "starttls", "none"]
Scope = Literal["read", "draft", "organize", "send", "delete", "admin"]
ALL_SCOPES: tuple[str, ...] = ("read", "draft", "organize", "send", "delete", "admin")


class ImapConfig(BaseModel):
    host: str
    port: int = 993
    tls: TlsMode = "ssl"
    username: str
    password: str
    default_folder: str = "INBOX"


class Identity(BaseModel):
    """A sender identity a mailbox may send as (address + display name)."""

    address: str
    name: str = ""
    default: bool = False
    signature_text: str | None = None
    signature_html: str | None = None

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or any(c in v for c in "<>\r\n ,"):
            raise ValueError(f"identity address {v!r} must be a bare email address")
        return v

    def formatted(self) -> str:
        # Unencoded (e.g. "L'équipe CGS" <contact@x>); the SMTP policy encodes it on the wire.
        if not self.name:
            return self.address
        return str(Address(display_name=self.name, addr_spec=self.address))


class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    tls: TlsMode = "starttls"
    username: str
    password: str
    from_address: str
    identities: list[Identity] = Field(default_factory=list)
    # Most servers do not file outgoing mail themselves; Gmail does (set false there).
    save_sent: bool = True
    # Per-mailbox override of send_guard.per_hour.
    per_hour: int | None = Field(default=None, ge=0)

    def all_identities(self) -> list[Identity]:
        """Configured identities, or one derived from `from_address`."""
        if self.identities:
            return list(self.identities)
        name, addr = parseaddr(self.from_address)
        return [Identity(address=addr or self.from_address, name=name, default=True)]

    def default_identity(self) -> Identity:
        ids = self.all_identities()
        for i in ids:
            if i.default:
                return i
        return ids[0]


class FoldersConfig(BaseModel):
    """Special folders. `None` means auto-detect (SPECIAL-USE, then usual names)."""

    sent: str | None = None
    drafts: str | None = None
    trash: str | None = None
    junk: str | None = None


class MailboxConfig(BaseModel):
    name: str
    description: str = ""
    imap: ImapConfig | None = None
    smtp: SmtpConfig | None = None
    folders: FoldersConfig = Field(default_factory=FoldersConfig)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"mailbox name {v!r} invalid: must match [a-zA-Z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _has_at_least_one_protocol(self) -> "MailboxConfig":
        if self.imap is None and self.smtp is None:
            raise ValueError(
                f"mailbox {self.name!r} declares no protocols — " "set at least one of imap / smtp"
            )
        return self

    def own_addresses(self) -> set[str]:
        out: set[str] = set()
        if self.imap is not None and "@" in self.imap.username:
            out.add(self.imap.username.lower())
        if self.smtp is not None:
            out.update(i.address.lower() for i in self.smtp.all_identities())
        return out


class TokenConfig(BaseModel):
    """A bearer token and what it may do."""

    name: str
    token: str
    scopes: list[Scope] = Field(default_factory=lambda: list(ALL_SCOPES))  # type: ignore[arg-type]
    mailboxes: list[str] | Literal["*"] = "*"
    # Force dry-run sends for this token (a request can enable dry-run, never disable it).
    dry_run: bool = False

    @field_validator("token")
    @classmethod
    def _no_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("auth.tokens must not contain empty strings")
        return v


class AuthConfig(BaseModel):
    """Bearer-token auth. Empty token lists deny access to protected endpoints.

    Entries are either a bare string (legacy: every scope on every mailbox)
    or a `TokenConfig` mapping with a name, scopes and a mailbox allow-list.
    """

    tokens: list[str | TokenConfig] = Field(default_factory=list)
    # Only Authentication-Results headers stamped by this authserv-id are trusted.
    trusted_authserv_id: str | None = None

    @field_validator("tokens")
    @classmethod
    def _no_empty_tokens(cls, v: list[str | TokenConfig]) -> list[str | TokenConfig]:
        for t in v:
            if isinstance(t, str) and (not t or not t.strip()):
                raise ValueError("auth.tokens must not contain empty strings")
        return v

    def token_configs(self) -> list[TokenConfig]:
        out: list[TokenConfig] = []
        for i, t in enumerate(self.tokens):
            if isinstance(t, TokenConfig):
                out.append(t)
            else:
                out.append(TokenConfig(name=f"legacy-{i}", token=t))
        return out


class SendGuardConfig(BaseModel):
    dry_run: bool = False
    # Max sends per rolling hour, per mailbox and per token. 0 disables the limit.
    per_hour: int = Field(default=60, ge=0)


class LimitsConfig(BaseModel):
    attachment_max_bytes: int = Field(default=15 * 1024 * 1024, ge=1)
    # Total size of attachments added to an outgoing message.
    outgoing_attachments_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    # Raw source and base64 attachments returned inline through MCP are capped.
    mcp_inline_max_bytes: int = Field(default=512 * 1024, ge=1024)


class StateConfig(BaseModel):
    path: str = Field(
        default_factory=lambda: os.environ.get("MAILBOXD_STATE_PATH", DEFAULT_STATE_PATH)
    )


class WebhookConfig(BaseModel):
    url: str
    secret: str
    mailboxes: list[str] | Literal["*"] = "*"


class WatchConfig(BaseModel):
    webhooks: list[WebhookConfig] = Field(default_factory=list)
    folder: str = "INBOX"
    # Fallback poll interval when the server lacks IDLE.
    poll_seconds: int = Field(default=60, ge=5)


def _default_labels() -> dict[str, str]:
    return {"suspect": "$Suspect", "a_traiter": "$ATraiter", "fait": "$Fait"}


class Config(BaseModel):
    log_level: str = "INFO"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    mailboxes: list[MailboxConfig] = Field(default_factory=list)
    state: StateConfig = Field(default_factory=StateConfig)
    send_guard: SendGuardConfig = Field(default_factory=SendGuardConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    # Friendly label -> IMAP keyword.
    labels: dict[str, str] = Field(default_factory=_default_labels)
    watch: WatchConfig = Field(default_factory=WatchConfig)

    @model_validator(mode="after")
    def _unique_names(self) -> "Config":
        seen: set[str] = set()
        for m in self.mailboxes:
            if m.name in seen:
                raise ValueError(f"duplicate mailbox name: {m.name!r}")
            seen.add(m.name)
        token_names: set[str] = set()
        for t in self.auth.token_configs():
            if t.name in token_names:
                raise ValueError(f"duplicate token name: {t.name!r}")
            token_names.add(t.name)
            if t.mailboxes != "*":
                unknown = [n for n in t.mailboxes if n not in seen]
                if unknown:
                    raise ValueError(f"token {t.name!r} references unknown mailboxes: {unknown}")
        for label, kw in self.labels.items():
            if not kw or any(c in kw for c in ' ()"\\%*{]\r\n') or kw.startswith("\\"):
                raise ValueError(f"label {label!r} maps to invalid IMAP keyword {kw!r}")
        return self

    def get(self, name: str) -> MailboxConfig | None:
        for m in self.mailboxes:
            if m.name == name:
                return m
        return None


class ConfigError(Exception):
    """Raised when config can't be loaded or is invalid."""


def _interpolate(value: Any, where: str = "") -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise ConfigError(f"environment variable {var!r} referenced at {where} is unset")
            return os.environ[var]

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, f"{where}.{k}" if where else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def load_config(path: str | None = None) -> Config:
    cfg_path = path or os.environ.get("MAILBOXD_CONFIG") or DEFAULT_CONFIG_PATH
    p = Path(cfg_path)
    if not p.is_file():
        raise ConfigError(f"config not found: {cfg_path}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"config {cfg_path} is not valid YAML: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config {cfg_path} must be a YAML mapping")
    raw = _interpolate(raw)
    try:
        cfg = Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"config {cfg_path} invalid: {e}") from e
    legacy = [t for t in cfg.auth.tokens if isinstance(t, str)]
    if legacy:
        log.warning(
            "%d bare auth token(s) grant every scope on every mailbox; "
            "prefer {name, token, scopes, mailboxes} entries",
            len(legacy),
        )
    return cfg
