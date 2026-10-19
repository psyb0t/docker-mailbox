"""SMTP transport. Message construction lives in `compose`."""

from __future__ import annotations

import smtplib
from email.message import Message
from email.utils import parseaddr

from .compose import without_bcc
from .config import SmtpConfig


class SmtpError(Exception):
    """Raised when an SMTP send fails."""


def _open(cfg: SmtpConfig) -> smtplib.SMTP:
    if cfg.tls == "ssl":
        s: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30)
    else:
        s = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
        s.ehlo()
        if cfg.tls == "starttls":
            s.starttls()
            s.ehlo()
    if cfg.username:
        s.login(cfg.username, cfg.password)
    return s


def send_message(cfg: SmtpConfig, msg: Message, rcpts: list[str]) -> None:
    """Deliver `msg` to `rcpts`. Any Bcc header is removed from what goes on the wire."""
    if not rcpts:
        raise SmtpError("no recipients")
    _, envelope_from = parseaddr(str(msg.get("From", "")))
    wire = without_bcc(msg)
    try:
        with _open(cfg) as s:
            s.send_message(wire, from_addr=envelope_from or None, to_addrs=rcpts)
    except (smtplib.SMTPException, OSError) as e:
        raise SmtpError(f"send failed: {e}") from e


def check(cfg: SmtpConfig) -> None:
    """Connect and authenticate without sending (deep health check)."""
    try:
        with _open(cfg) as s:
            s.noop()
    except (smtplib.SMTPException, OSError) as e:
        raise SmtpError(f"smtp check failed: {e}") from e
