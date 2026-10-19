"""Build outgoing messages: new mail, replies, drafts.

The server, not the caller, decides the headers that matter for safety:
the From identity must be a configured one, and a reply's recipients,
subject and threading headers are derived from the original message.
"""

from __future__ import annotations

import html as html_lib
import mimetypes
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from typing import Iterable

from .config import Identity, SmtpConfig
from .errors import InvalidIdentity, InvalidRequest
from .mime import decode, message_ids

INTERNAL_HEADER_PREFIX = "X-Mailboxd-"
SIGNATURE_MARKER_HEADER = "X-Mailboxd-Signed"
_RE_PREFIX = re.compile(r"^\s*(re|aw|sv|fw|fwd|tr|réf)\s*:\s*", re.I)


@dataclass
class OutgoingAttachment:
    filename: str
    content_type: str
    data: bytes


def resolve_identity(
    smtp: SmtpConfig, identity: str | None = None, from_address: str | None = None
) -> Identity:
    """Pick the configured identity matching `identity` / `from_address`, else the default.

    Anything that does not match a configured identity is refused: an agent
    cannot invent a sender address.
    """
    wanted = identity or from_address
    if not wanted:
        return smtp.default_identity()
    _, addr = parseaddr(wanted)
    addr = (addr or wanted).strip().lower()
    for ident in smtp.all_identities():
        if ident.address.lower() == addr:
            return ident
    allowed = [i.address for i in smtp.all_identities()]
    raise InvalidIdentity(f"{wanted!r} is not a configured sender identity", allowed=allowed)


def _check_addresses(field: str, values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = str(v).strip()
        if not v:
            continue
        if any(c in v for c in "\r\n\x00"):
            raise InvalidRequest(f"invalid {field} address: control characters")
        _, addr = parseaddr(v)
        if "@" not in addr:
            raise InvalidRequest(f"invalid {field} address: {v!r}")
        out.append(v)
    return out


def apply_signature(
    body_text: str | None, body_html: str | None, ident: Identity
) -> tuple[str | None, str | None]:
    if body_text is not None and ident.signature_text:
        body_text = body_text.rstrip("\n") + "\n\n-- \n" + ident.signature_text
    if body_html is not None:
        sig_html = ident.signature_html
        if sig_html is None and ident.signature_text:
            sig_html = html_lib.escape(ident.signature_text).replace("\n", "<br>")
        if sig_html:
            block = f'<div class="signature">-- <br>{sig_html}</div>'
            if re.search(r"</body\s*>", body_html, re.I):
                body_html = re.sub(r"</body\s*>", block + "</body>", body_html, count=1, flags=re.I)
            else:
                body_html = body_html + block
    return body_text, body_html


def quote_text(original_text: str, original_from: str, original_date: str) -> str:
    quoted = "\n".join("> " + line if line else ">" for line in original_text.splitlines())
    return f"\n\nOn {original_date}, {original_from} wrote:\n{quoted}\n"


def reply_subject(subject: str) -> str:
    return subject if _RE_PREFIX.match(subject or "") else f"Re: {subject}".strip()


def reply_fields(original: Message, own: set[str], reply_all: bool) -> dict[str, object]:
    """Recipients and threading headers for a reply, derived from the original only."""
    reply_to = decode(original.get("Reply-To"))
    sender = decode(original.get("From"))
    primary = [a for a in getaddresses([reply_to or sender]) if "@" in a[1]]
    to: list[str] = []
    seen: set[str] = set()

    def add(dst: list[str], pairs: list[tuple[str, str]]) -> None:
        for name, addr in pairs:
            key = addr.lower()
            if "@" not in addr or key in seen or key in own:
                continue
            seen.add(key)
            dst.append(f"{name} <{addr}>" if name else addr)

    add(to, primary)
    if not to and primary:
        # Replying to our own sent message: keep the original recipients.
        add(to, getaddresses([decode(original.get("To"))]))
    cc: list[str] = []
    if reply_all:
        add(cc, getaddresses([decode(original.get("To")), decode(original.get("Cc"))]))
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    if not to:
        raise InvalidRequest("original message has no usable reply address")

    msgid = (message_ids(original.get("Message-ID")) or [""])[0]
    refs = message_ids(original.get("References")) or message_ids(original.get("In-Reply-To"))
    if msgid:
        refs = [r for r in refs if r != msgid] + [msgid]
    return {
        "to": to,
        "cc": cc,
        "subject": reply_subject(decode(original.get("Subject"))),
        "in_reply_to": msgid or None,
        "references": refs[-20:],
    }


def build_message(
    ident: Identity,
    to: list[str],
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[OutgoingAttachment] | None = None,
    signature: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> EmailMessage:
    """Build the full message. Bcc is kept as a header (for drafts and the
    Sent copy); the SMTP transport strips it before delivery."""
    to = _check_addresses("to", to)
    cc = _check_addresses("cc", cc or [])
    bcc = _check_addresses("bcc", bcc or [])
    if not (to or cc or bcc):
        raise InvalidRequest("at least one recipient is required")
    if body_text is None and body_html is None:
        raise InvalidRequest("must supply body_text and/or body_html")
    if any(c in subject for c in "\r\n"):
        raise InvalidRequest("subject must not contain line breaks")

    signed = False
    if signature and (ident.signature_text or ident.signature_html):
        body_text, body_html = apply_signature(body_text, body_html, ident)
        signed = True

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = ident.formatted()
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=ident.address.rsplit("@", 1)[-1])
    if reply_to:
        msg["Reply-To"] = ", ".join(_check_addresses("reply_to", [reply_to]))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    if signed:
        msg[SIGNATURE_MARKER_HEADER] = "1"

    if body_text is not None:
        msg.set_content(body_text)
    if body_html is not None:
        if body_text is None:
            msg.set_content("This message requires an HTML-capable client.")
        msg.add_alternative(body_html, subtype="html")
    for att in attachments or []:
        maintype, _, subtype = (att.content_type or "").partition("/")
        if not maintype or not subtype:
            guessed = mimetypes.guess_type(att.filename)[0] or "application/octet-stream"
            maintype, _, subtype = guessed.partition("/")
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)
    return msg


def strip_internal_headers(msg: Message) -> None:
    for k in {k for k in msg.keys() if k.lower().startswith(INTERNAL_HEADER_PREFIX.lower())}:
        del msg[k]


def envelope_recipients(msg: Message) -> list[str]:
    fields = [decode(msg.get(h)) for h in ("To", "Cc", "Bcc")]
    return [a for _, a in getaddresses([f for f in fields if f]) if "@" in a]


def without_bcc(msg: Message) -> Message:
    import copy

    out = copy.deepcopy(msg)
    del out["Bcc"]
    return out
