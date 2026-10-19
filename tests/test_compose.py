"""Tests for mailboxd.compose and the Bcc handling of smtp_client."""

from __future__ import annotations

import email
import email.message
from typing import Any

import pytest

from mailboxd import compose
from mailboxd.config import Identity, SmtpConfig
from mailboxd.errors import InvalidIdentity, InvalidRequest

IDENT = Identity(address="contact@example.com", name="L'équipe CGS", default=True)
SIGNED = Identity(
    address="support@example.com",
    name="Support",
    signature_text="Bien cordialement\nSupport CGS",
)


def _smtp(identities: list[Identity] | None = None) -> SmtpConfig:
    return SmtpConfig(
        host="s",
        username="u",
        password="p",
        from_address="Default <default@example.com>",
        identities=identities or [],
    )


# -- identities ----------------------------------------------------------


def test_resolve_identity_default_and_match() -> None:
    smtp = _smtp([IDENT, SIGNED])
    assert compose.resolve_identity(smtp).address == "contact@example.com"
    assert compose.resolve_identity(smtp, "SUPPORT@example.com").address == "support@example.com"
    assert compose.resolve_identity(smtp, None, "Support <support@example.com>") == SIGNED


def test_resolve_identity_rejects_unknown() -> None:
    smtp = _smtp([IDENT])
    with pytest.raises(InvalidIdentity) as exc:
        compose.resolve_identity(smtp, "ceo@example.com")
    assert exc.value.status == 422
    assert exc.value.details["allowed"] == ["contact@example.com"]
    with pytest.raises(InvalidIdentity):
        compose.resolve_identity(smtp, None, "ceo@evil.net")


def test_resolve_identity_derived_from_from_address() -> None:
    smtp = _smtp()
    assert compose.resolve_identity(smtp).address == "default@example.com"
    with pytest.raises(InvalidIdentity):
        compose.resolve_identity(smtp, "other@example.com")


# -- replies -------------------------------------------------------------


def _orig(raw: str) -> email.message.Message:
    return email.message_from_string(raw)


def test_reply_fields_simple_and_references_chain() -> None:
    orig = _orig(
        "From: Alice <alice@ext.com>\nTo: contact@example.com\nSubject: Question\n"
        "Message-ID: <m3@ext.com>\nReferences: <m1@ext.com> <m2@ext.com>\n\nhi\n"
    )
    r = compose.reply_fields(orig, {"contact@example.com"}, reply_all=False)
    assert r["to"] == ["Alice <alice@ext.com>"]
    assert r["cc"] == []
    assert r["subject"] == "Re: Question"
    assert r["in_reply_to"] == "<m3@ext.com>"
    assert r["references"] == ["<m1@ext.com>", "<m2@ext.com>", "<m3@ext.com>"]


def test_reply_fields_uses_in_reply_to_when_no_references_and_keeps_re() -> None:
    orig = _orig(
        "From: a@ext.com\nSubject: Re: Already\nMessage-ID: <b@ext.com>\n"
        "In-Reply-To: <a@ext.com>\n\nx\n"
    )
    r = compose.reply_fields(orig, set(), reply_all=False)
    assert r["subject"] == "Re: Already"
    assert r["references"] == ["<a@ext.com>", "<b@ext.com>"]


def test_reply_prefers_reply_to() -> None:
    orig = _orig("From: a@ext.com\nReply-To: Desk <desk@ext.com>\nSubject: s\n\nx\n")
    assert compose.reply_fields(orig, set(), False)["to"] == ["Desk <desk@ext.com>"]


def test_reply_all_removes_own_addresses_and_dedupes() -> None:
    orig = _orig(
        "From: alice@ext.com\nTo: Contact <contact@example.com>, bob@ext.com\n"
        "Cc: CONTACT@example.com, carol@ext.com, bob@ext.com\nSubject: s\n"
        "Message-ID: <x@ext.com>\n\nx\n"
    )
    r = compose.reply_fields(orig, {"contact@example.com"}, reply_all=True)
    assert r["to"] == ["alice@ext.com"]
    assert r["cc"] == ["bob@ext.com", "carol@ext.com"]
    plain = compose.reply_fields(orig, {"contact@example.com"}, reply_all=False)
    assert plain["cc"] == []


def test_reply_to_own_sent_mail_goes_to_original_recipients() -> None:
    orig = _orig(
        "From: contact@example.com\nTo: client@ext.com\nSubject: Offer\n"
        "Message-ID: <sent@example.com>\n\nx\n"
    )
    r = compose.reply_fields(orig, {"contact@example.com"}, reply_all=False)
    assert r["to"] == ["client@ext.com"]


def test_reply_without_usable_address_raises() -> None:
    with pytest.raises(InvalidRequest):
        compose.reply_fields(_orig("Subject: nobody\n\nx\n"), set(), False)


def test_reply_subject() -> None:
    assert compose.reply_subject("Hello") == "Re: Hello"
    assert compose.reply_subject("RE: Hello") == "RE: Hello"
    assert compose.reply_subject("AW: Hallo") == "AW: Hallo"


def test_quote_text() -> None:
    q = compose.quote_text("a\n\nb", "x@y.z", "Mon")
    assert "> a\n>\n> b" in q and "x@y.z wrote" in q


# -- signatures ----------------------------------------------------------


def test_signature_text_and_html_fallback() -> None:
    text, html = compose.apply_signature("Hi", "<p>Hi</p>", SIGNED)
    assert text == "Hi\n\n-- \nBien cordialement\nSupport CGS"
    assert html is not None and "Bien cordialement<br>Support CGS" in html
    assert html.endswith("</div>")


def test_signature_html_goes_before_body_end() -> None:
    ident = Identity(address="a@b.co", signature_html="<b>Sig</b>")
    _, html = compose.apply_signature(None, "<html><body><p>x</p></body></html>", ident)
    assert html is not None
    assert html.index("<b>Sig</b>") < html.index("</body>")


def test_signature_html_is_escaped_when_derived_from_text() -> None:
    ident = Identity(address="a@b.co", signature_text="<script>x</script>")
    _, html = compose.apply_signature(None, "<p>x</p>", ident)
    assert html is not None and "<script>" not in html and "&lt;script&gt;" in html


def test_build_message_applies_signature_once_and_marks_it() -> None:
    msg = compose.build_message(SIGNED, ["a@ext.com"], "S", body_text="Hello")
    assert msg["X-Mailboxd-Signed"] == "1"
    body = msg.get_body(("plain",))
    assert body is not None and body.get_content().count("Support CGS") == 1
    unsigned = compose.build_message(SIGNED, ["a@ext.com"], "S", body_text="Hello", signature=False)
    assert unsigned["X-Mailboxd-Signed"] is None
    ubody = unsigned.get_body(("plain",))
    assert ubody is not None and "Support CGS" not in ubody.get_content()


# -- build_message -------------------------------------------------------


def test_build_message_headers_and_threading() -> None:
    msg = compose.build_message(
        IDENT,
        ["Bob <bob@ext.com>"],
        "Sujet accentué é",
        body_text="Hi",
        body_html="<p>Hi</p>",
        cc=["c@ext.com"],
        reply_to="rt@example.com",
        in_reply_to="<a@ext.com>",
        references=["<z@ext.com>", "<a@ext.com>"],
        extra_headers={"X-Mailboxd-Draft-By": "mila"},
    )
    assert "contact@example.com" in msg["From"]
    assert msg["To"] == "Bob <bob@ext.com>"
    assert msg["Cc"] == "c@ext.com"
    assert msg["Reply-To"] == "rt@example.com"
    assert msg["In-Reply-To"] == "<a@ext.com>"
    assert msg["References"] == "<z@ext.com> <a@ext.com>"
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg["X-Mailboxd-Draft-By"] == "mila"
    assert msg.get_body(("html",)) is not None
    # Round-trips through bytes with the accent intact.
    parsed = email.message_from_bytes(msg.as_bytes())
    assert str(email.header.make_header(email.header.decode_header(parsed["Subject"]))) == (
        "Sujet accentué é"
    )


def test_build_message_attachments() -> None:
    att = compose.OutgoingAttachment("report.pdf", "application/pdf", b"%PDF")
    guess = compose.OutgoingAttachment("notes.txt", "", b"abc")
    msg = compose.build_message(IDENT, ["a@ext.com"], "S", body_text="x", attachments=[att, guess])
    names = {p.get_filename(): p.get_content_type() for p in msg.iter_attachments()}
    assert names == {"report.pdf": "application/pdf", "notes.txt": "text/plain"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject": "ok\r\nBcc: evil@x.com"},
        {"subject": "ok\nX-Injected: 1"},
        {"to": ["a@ext.com\r\nBcc: evil@x.com"]},
        {"cc": ["a@ext.com\nX: y"]},
        {"bcc": ["a@ext.com\x00"]},
        {"reply_to": "a@ext.com\r\nX: y"},
        {"to": ["not-an-address"]},
        {"to": []},
        {"body_text": None},
    ],
)
def test_build_message_rejects_header_injection_and_bad_input(kwargs: dict) -> None:
    args: dict = {"to": ["a@ext.com"], "subject": "s", "body_text": "x"}
    args.update(kwargs)
    with pytest.raises(InvalidRequest):
        compose.build_message(IDENT, **args)


def test_bcc_kept_in_message_but_stripped_by_without_bcc() -> None:
    msg = compose.build_message(
        IDENT, ["a@ext.com"], "S", body_text="x", cc=["c@ext.com"], bcc=["secret@ext.com"]
    )
    assert msg["Bcc"] == "secret@ext.com"
    assert compose.envelope_recipients(msg) == ["a@ext.com", "c@ext.com", "secret@ext.com"]
    wire = compose.without_bcc(msg)
    assert wire["Bcc"] is None
    assert b"secret@ext.com" not in wire.as_bytes()
    assert msg["Bcc"] == "secret@ext.com"  # original untouched


def test_strip_internal_headers() -> None:
    msg = compose.build_message(
        SIGNED,
        ["a@ext.com"],
        "S",
        body_text="x",
        extra_headers={"X-Mailboxd-Draft-By": "mila", "X-Keep": "1"},
    )
    compose.strip_internal_headers(msg)
    assert not [k for k in msg.keys() if k.lower().startswith("x-mailboxd-")]
    assert msg["X-Keep"] == "1"


def test_smtp_send_message_never_puts_bcc_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    from mailboxd import smtp_client

    sent: dict = {}

    class FakeSMTP:
        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def send_message(self, msg: Any, from_addr: str | None, to_addrs: list[str]) -> None:
            sent.update(raw=msg.as_bytes(), from_addr=from_addr, to=to_addrs)

    monkeypatch.setattr(smtp_client, "_open", lambda cfg: FakeSMTP())
    msg = compose.build_message(IDENT, ["a@ext.com"], "S", body_text="x", bcc=["secret@ext.com"])
    smtp_client.send_message(_smtp([IDENT]), msg, compose.envelope_recipients(msg))
    assert b"secret@ext.com" not in sent["raw"]
    assert "secret@ext.com" in sent["to"]  # still delivered via the envelope
    assert sent["from_addr"] == "contact@example.com"
    with pytest.raises(smtp_client.SmtpError):
        smtp_client.send_message(_smtp([IDENT]), msg, [])
