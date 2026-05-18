"""YAML config loader.

Single file, validated via pydantic. Each mailbox can have any subset of
{imap, smtp} — at least one is required. `name` must be unique and
URL-safe (it becomes a path segment in the HTTP API).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = "/etc/mailboxd/config.yaml"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


TlsMode = Literal["ssl", "starttls", "none"]


class ImapConfig(BaseModel):
    host: str
    port: int = 993
    tls: TlsMode = "ssl"
    username: str
    password: str
    default_folder: str = "INBOX"


class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    tls: TlsMode = "starttls"
    username: str
    password: str
    from_address: str


class MailboxConfig(BaseModel):
    name: str
    description: str = ""
    imap: ImapConfig | None = None
    smtp: SmtpConfig | None = None

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


class AuthConfig(BaseModel):
    """Bearer-token auth. Empty `tokens` list disables auth entirely."""

    tokens: list[str] = Field(default_factory=list)

    @field_validator("tokens")
    @classmethod
    def _no_empty_tokens(cls, v: list[str]) -> list[str]:
        for t in v:
            if not t or not t.strip():
                raise ValueError("auth.tokens must not contain empty strings")
        return v


class Config(BaseModel):
    log_level: str = "INFO"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    mailboxes: list[MailboxConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "Config":
        seen: set[str] = set()
        for m in self.mailboxes:
            if m.name in seen:
                raise ValueError(f"duplicate mailbox name: {m.name!r}")
            seen.add(m.name)
        return self

    def get(self, name: str) -> MailboxConfig | None:
        for m in self.mailboxes:
            if m.name == name:
                return m
        return None


class ConfigError(Exception):
    """Raised when config can't be loaded or is invalid."""


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
    try:
        return Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"config {cfg_path} invalid: {e}") from e
