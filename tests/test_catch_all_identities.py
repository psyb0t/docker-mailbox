"""Catch-all identity selection through HTTP, IMAP and SMTP."""

import email
import json
from email import policy
from email.utils import parseaddr
from typing import Any

import pytest
from pydantic import ValidationError

from mailboxd.compose import build_message, resolve_identity
from mailboxd.config import Identity, SmtpConfig
from mailboxd.errors import InvalidIdentity

SEND_PATH = "/mailboxes/catchall/send"
REPLY_PATH = "/mailboxes/catchall/messages/1/reply"
PRIMARY = "primary@example.com"
KNOWN = "known@example.com"
PATTERN = "*@example.com"
COMPOSE = {"to": ["recipient@example.org"], "subject": "Test", "body_text": "Hello"}
DRAFTS_PATH = "/mailboxes/catchall/drafts"


def smtp_config(identities: list[Identity]) -> SmtpConfig:
    return SmtpConfig(
        host="localhost", username="", password="", from_address=PRIMARY, identities=identities
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "foo*@example.com",
        "*@*.example.com",
        "*@",
        "*@example..com",
        "*@-example.com",
        "*@example.com.",
        "*@example.com\x00",
    ],
)
def test_invalid_patterns_fail_at_configuration_load(pattern: str) -> None:
    with pytest.raises(ValidationError):
        Identity(address=pattern)


def test_patterns_require_a_concrete_default() -> None:
    with pytest.raises(ValidationError):
        smtp_config([Identity(address=PATTERN)])
    with pytest.raises(ValidationError):
        smtp_config([Identity(address=PRIMARY), Identity(address=PATTERN, default=True)])
    with pytest.raises(ValidationError):
        SmtpConfig(host="localhost", username="", password="", from_address=PATTERN)
    smtp = smtp_config([Identity(address=PATTERN), Identity(address=PRIMARY)])
    assert resolve_identity(smtp).address == PRIMARY


def test_expansion_inherits_metadata_without_mutating_configuration() -> None:
    pattern = Identity(
        address=PATTERN, name="Aliases", signature_text="Text", signature_html="<b>HTML</b>"
    )
    exact = Identity(address=KNOWN, name="Exact")
    smtp = smtp_config([pattern, exact])
    assert resolve_identity(smtp, KNOWN) == exact
    selected = resolve_identity(smtp, "Untrusted name <Shop+Tag@EXAMPLE.COM>")
    assert selected.address == "Shop+Tag@EXAMPLE.COM"
    assert selected.name == "Aliases"
    assert pattern.address == PATTERN
    message = build_message(
        selected,
        to=["recipient@example.org"],
        subject="Test",
        body_text="Hi",
        body_html="<p>Hi</p>",
    )
    assert "Text" in message.get_body(preferencelist=("plain",)).get_content()
    assert "<b>HTML</b>" in message.get_body(preferencelist=("html",)).get_content()


@pytest.mark.parametrize(
    "address",
    [
        PATTERN,
        "@example.com",
        "a@@example.com",
        "a..b@example.com",
        "a b@example.com",
        "a@example.com,b@example.com",
        "a@example.com\r\nBcc: victim@example.org",
        "a\x00@example.com",
        "alias@sub.example.com",
        "alias@example.com.evil.invalid",
        "alias@example.net",
    ],
)
def test_pattern_does_not_authorize_invalid_or_foreign_sender(address: str) -> None:
    smtp = smtp_config([Identity(address=PRIMARY), Identity(address=PATTERN)])
    with pytest.raises(InvalidIdentity):
        resolve_identity(smtp, address)


def test_multiple_domain_patterns_are_independent() -> None:
    smtp = smtp_config(
        [
            Identity(address=PRIMARY),
            Identity(address=PATTERN, name="First"),
            Identity(address="*@example.net", name="Second"),
        ]
    )
    assert resolve_identity(smtp, "alias@example.net").name == "Second"
    assert resolve_identity(smtp, "alias@example.com").name == "First"


@pytest.mark.parametrize("wildcard", [False, True], ids=["exact-only", "wildcard-config"])
@pytest.mark.parametrize("field", ["identity", "from_address"])
@pytest.mark.parametrize(
    "address",
    [
        "new-alias@example.com",
        "new-alias@sub.example.com",
        "new-alias@example.net",
        "new-alias@example.com.evil.invalid",
    ],
    ids=["catch-all-address", "subdomain", "different-domain", "deceptive-suffix"],
)
def test_sender_requires_exact_identity_or_matching_domain(
    catch_all_mailbox_factory: Any, wildcard: bool, field: str, address: str
) -> None:
    with catch_all_mailbox_factory(wildcard=wildcard) as (client, smtp, _):
        response = client.post(SEND_PATH, json={**COMPOSE, field: address})
        if wildcard and address == "new-alias@example.com":
            assert response.status_code == 200, response.text
            assert parseaddr(response.json()["from"]) == ("Catch-all", address)
            assert smtp.envelopes == [(address, ["recipient@example.org"])]
            message = email.message_from_bytes(smtp.messages[0], policy=policy.default)
            assert "Wildcard signature" in message.get_payload(decode=True).decode()
            return
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "invalid_identity"
        assert error["details"]["allowed"] == [PRIMARY, KNOWN] + ([PATTERN] if wildcard else [])
        assert smtp.messages == []
        assert smtp.envelopes == []


@pytest.mark.parametrize("wildcard", [False, True], ids=["exact-only", "wildcard-config"])
@pytest.mark.parametrize(
    "to,cc",
    [("Alias <new-alias@example.com>", None), (None, "new-alias@example.com"), (None, None)],
    ids=["unlisted-to", "unlisted-cc", "bcc-only"],
)
def test_reply_selects_catch_all_or_concrete_default(
    catch_all_mailbox_factory: Any, wildcard: bool, to: str | None, cc: str | None
) -> None:
    with catch_all_mailbox_factory(wildcard=wildcard, to=to, cc=cc) as (client, smtp, imap):
        response = client.post(REPLY_PATH, json={"body_text": "Reply", "quote": False})
        assert response.status_code == 200, response.text
        matched = wildcard and bool(to or cc)
        name, address = ("Catch-all", "new-alias@example.com") if matched else ("Primary", PRIMARY)
        assert parseaddr(response.json()["from"]) == (name, address)
        assert smtp.envelopes == [(address, ["author@example.org"])]
        assert len(smtp.messages) == 1
        message = email.message_from_bytes(smtp.messages[0], policy=policy.default)
        assert parseaddr(str(message["From"])) == (name, address)
        assert message["In-Reply-To"] == "<original@example.org>"
        text = message.get_payload(decode=True).decode()
        assert ("Wildcard signature" if matched else "Primary signature") in text
        assert ("Primary signature" if matched else "Wildcard signature") not in text
        assert any("+FLAGS" in command and "Answered" in command for command in imap.commands)


@pytest.mark.parametrize(
    "to,cc",
    [("Known <KNOWN@EXAMPLE.COM>", None), ("new-alias@example.com", "known@example.com")],
    ids=["case-insensitive-to", "exact-cc-beats-unlisted-to"],
)
def test_reply_selects_configured_exact_identity(
    catch_all_mailbox_factory: Any, to: str, cc: str | None
) -> None:
    with catch_all_mailbox_factory(wildcard=True, to=to, cc=cc) as (client, smtp, _):
        response = client.post(REPLY_PATH, json={"body_text": "Reply", "quote": False})
        assert response.status_code == 200, response.text
        assert parseaddr(response.json()["from"]) == ("Known", KNOWN)
        assert smtp.envelopes == [(KNOWN, ["author@example.org"])]
        message = email.message_from_bytes(smtp.messages[0], policy=policy.default)
        assert "Known signature" in message.get_payload(decode=True).decode()


@pytest.mark.parametrize("field", ["identity", "from_address"])
def test_explicitly_listed_sender_still_sends(catch_all_mailbox_factory: Any, field: str) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, _):
        response = client.post(SEND_PATH, json={**COMPOSE, field: "Known <KNOWN@EXAMPLE.COM>"})
        assert response.status_code == 200, response.text
        assert smtp.envelopes == [(KNOWN, ["recipient@example.org"])]


def test_wildcard_string_cannot_be_sent_literally(catch_all_mailbox_factory: Any) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, _):
        response = client.post(SEND_PATH, json={**COMPOSE, "identity": PATTERN})
        assert response.status_code == 422, response.text
        assert smtp.envelopes == []


@pytest.mark.parametrize("wildcard", [False, True])
def test_explicit_reply_identity_requires_opt_in(
    catch_all_mailbox_factory: Any, wildcard: bool
) -> None:
    with catch_all_mailbox_factory(wildcard=wildcard, to="new-alias@example.com") as (
        client,
        smtp,
        _,
    ):
        response = client.post(
            REPLY_PATH, json={"body_text": "Reply", "identity": "new-alias@example.com"}
        )
        assert response.status_code == (200 if wildcard else 422), response.text
        if wildcard:
            assert smtp.envelopes == [("new-alias@example.com", ["author@example.org"])]
        else:
            assert response.json()["error"]["code"] == "invalid_identity"
            assert smtp.messages == []


@pytest.mark.parametrize("authorization", ["", "Bearer EXAMPLE-WRONG-TOKEN"])
def test_sender_selection_does_not_bypass_authentication(
    catch_all_mailbox_factory: Any, authorization: str
) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, _):
        response = client.post(
            SEND_PATH,
            json={**COMPOSE, "identity": KNOWN},
            headers={"Authorization": authorization},
        )
        assert response.status_code == 401
        assert smtp.messages == []


@pytest.mark.parametrize("draft", [False, True])
def test_reply_uses_first_matching_recipient_and_excludes_own_aliases(
    catch_all_mailbox_factory: Any, draft: bool
) -> None:
    with catch_all_mailbox_factory(
        wildcard=True,
        to="outsider@external.example, First <first@example.com>, second@example.com",
        cc="third@example.com, other@external.example",
    ) as (client, smtp, imap):
        response = client.post(
            REPLY_PATH,
            json={"body_text": "Reply", "quote": False, "reply_all": True, "draft": draft},
        )
        assert response.status_code == 200, response.text
        if draft:
            assert smtp.messages == []
            raw = imap.folders["Drafts"].msgs[int(response.json()["uid"])]
        else:
            raw = smtp.messages[0]
            assert smtp.envelopes == [
                (
                    "first@example.com",
                    ["author@example.org", "outsider@external.example", "other@external.example"],
                )
            ]
        message = email.message_from_bytes(raw, policy=policy.default)
        assert parseaddr(str(message["From"])) == ("Catch-all", "first@example.com")
        assert str(message["Cc"]) == "outsider@external.example, other@external.example"
        assert "Wildcard signature" in message.get_content()
        assert message["In-Reply-To"] == "<original@example.org>"


def test_catch_all_draft_create_update_and_send(catch_all_mailbox_factory: Any) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, imap):
        response = client.post(DRAFTS_PATH, json={**COMPOSE, "identity": "alias@example.com"})
        assert response.status_code == 200, response.text
        uid = response.json()["uid"]
        assert smtp.messages == []
        updated = client.put(f"{DRAFTS_PATH}/{uid}", json={"subject": "Updated"})
        assert updated.status_code == 200, updated.text
        new_uid = updated.json()["uid"]
        assert int(uid) not in imap.folders["Drafts"].msgs
        raw = email.message_from_bytes(
            imap.folders["Drafts"].msgs[int(new_uid)], policy=policy.default
        )
        assert parseaddr(str(raw["From"])) == ("Catch-all", "alias@example.com")
        assert raw.get_content().count("Wildcard signature") == 1
        sent = client.post(f"{DRAFTS_PATH}/{new_uid}/send")
        assert sent.status_code == 200, sent.text
        assert smtp.envelopes == [("alias@example.com", ["recipient@example.org"])]
        assert int(new_uid) not in imap.folders["Drafts"].msgs
        message = email.message_from_bytes(smtp.messages[0], policy=policy.default)
        assert message["Subject"] == "Updated"
        assert message.get_content().count("Wildcard signature") == 1


@pytest.mark.parametrize("sender", [PATTERN, "outsider@example.net"])
def test_saved_draft_sender_is_revalidated_before_smtp(
    catch_all_mailbox_factory: Any, sender: str
) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, imap):
        message = email.message.EmailMessage()
        message["From"] = sender
        message["To"] = "recipient@example.org"
        message["Subject"] = "Untrusted draft"
        message.set_content("Hello")
        uid = imap.folders["Drafts"].add(message.as_bytes())
        response = client.post(f"{DRAFTS_PATH}/{uid}/send")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_identity"
        assert smtp.messages == []
        assert uid in imap.folders["Drafts"].msgs


@pytest.mark.parametrize("tool", ["send", "reply"])
def test_mcp_uses_the_same_catch_all_sender_rules(
    catch_all_mailbox_factory: Any, tool: str
) -> None:
    with catch_all_mailbox_factory(wildcard=True, to="alias@example.com") as (client, smtp, _):
        arguments = (
            {**COMPOSE, "identity": "alias@example.com"}
            if tool == "send"
            else {"uid": "1", "body_text": "Reply", "quote": False}
        )
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"mailbox": "catchall", **arguments}},
            },
        )
        assert response.status_code == 200, response.text
        if response.headers["content-type"].startswith("application/json"):
            payload = response.json()
        else:
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert len(events) == 1
            payload = events[0]
        assert "error" not in payload
        assert not payload["result"].get("isError"), payload
        result = json.loads(payload["result"]["content"][0]["text"])
        assert parseaddr(result["from"]) == ("Catch-all", "alias@example.com")
        assert smtp.envelopes[0][0] == "alias@example.com"


def test_identity_discovery_keeps_pattern_separate_from_default(
    catch_all_mailbox_factory: Any,
) -> None:
    with catch_all_mailbox_factory(wildcard=True) as (client, smtp, _):
        response = client.get("/mailboxes/catchall/identities")
        assert response.status_code == 200, response.text
        identities = response.json()["identities"]
        assert [i["address"] for i in identities if i["default"]] == [PRIMARY]
        assert next(i for i in identities if i["address"] == PATTERN) == {
            "address": PATTERN,
            "name": "Catch-all",
            "default": False,
            "has_signature": True,
        }
        assert smtp.messages == []
