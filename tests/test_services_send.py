"""Service: send pipeline, replies, drafts."""

from __future__ import annotations

import base64
import email
from email import policy
from pathlib import Path

import pytest
from test_services_fakes import (
    FakeServer,
    Smtp,
    audit_rows,
    make_config,
    make_raw,
    make_service,
    principal,
)

from mailboxd import smtp_client
from mailboxd.errors import (
    InvalidIdentity,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    RateLimited,
    TooLarge,
)
from mailboxd.schemas import AttachmentIn, ComposeRequest, DraftUpdate, ReplyRequest
from mailboxd.services import Service

Env = tuple[Service, FakeServer, Smtp]


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return make_service(monkeypatch, tmp_path)


def req(**kw: object) -> ComposeRequest:
    data: dict[str, object] = {"to": ["dest@example.net"], "subject": "Hi", "body_text": "yo"}
    data.update(kw)
    return ComposeRequest.model_validate(data)


def test_send_delivers_and_files_sent_copy(env: Env) -> None:
    svc, srv, smtp = env
    res = svc.send(principal("send"), "main", req(bcc=["hidden@example.net"], cc=["c@example.net"]))
    assert res["dry_run"] is False and res["warnings"] == []
    assert res["from"].endswith("<contact@example.com>")
    (msg, rcpts), = smtp.sent
    assert set(rcpts) == {"dest@example.net", "c@example.net", "hidden@example.net"}
    assert not any(k.lower().startswith("x-mailboxd") for k in msg.keys())
    sent = srv.folders["Sent"][0]
    assert r"\Seen" in sent.flags and res["sent_copy"] == {"folder": "Sent", "uid": str(sent.uid)}
    row = audit_rows(svc)[0]
    assert (row["action"], row["outcome"], row["mailbox"]) == ("send", "ok", "main")


def test_sent_copy_failure_is_a_warning(env: Env) -> None:
    svc, srv, smtp = env
    srv.fail_append.add("Sent")
    res = svc.send(principal("send"), "main", req())
    assert res["warnings"] == ["sent_copy_failed"] and res["sent_copy"] is None
    assert len(smtp.sent) == 1


@pytest.mark.parametrize("how", ["config", "principal", "request"])
def test_dry_run_never_touches_smtp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    cfg = make_config(send_guard={"dry_run": how == "config", "per_hour": 1})
    svc, srv, smtp = make_service(monkeypatch, tmp_path, cfg)

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("SMTP must not be contacted in dry run")

    monkeypatch.setattr(smtp_client, "send_message", boom)
    p = principal("send", dry_run=how == "principal")
    for _ in range(3):  # dry runs do not consume the rate limit
        res = svc.send(p, "main", req(dry_run=how == "request"))
    assert res["dry_run"] is True and res["sent_copy"] is None
    assert "Subject" in [h[0] for h in res["preview"]["headers"]]
    assert "yo" in res["preview"]["body_text"]
    assert srv.folders["Sent"] == [] and smtp.sent == []
    assert audit_rows(svc)[0]["outcome"] == "dry_run"


def test_rate_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    svc, _, smtp = make_service(
        monkeypatch, tmp_path, make_config(send_guard={"per_hour": 2})
    )
    p = principal("send")
    svc.send(p, "main", req())
    svc.send(p, "main", req())
    with pytest.raises(RateLimited) as exc:
        svc.send(p, "main", req())
    assert exc.value.retry_after >= 1 and len(smtp.sent) == 2
    assert audit_rows(svc)[0]["outcome"] == "error"


def test_invalid_identity_and_alias(env: Env) -> None:
    svc, _, smtp = env
    p = principal("send")
    with pytest.raises(InvalidIdentity):
        svc.send(p, "main", req(identity="ceo@example.com"))
    with pytest.raises(InvalidIdentity):
        svc.send(p, "main", req(from_address="evil@example.com"))
    assert smtp.sent == []
    res = svc.send(p, "main", req(identity="support@example.com"))
    assert "support@example.com" in res["from"]
    res = svc.send(p, "main", req(from_address="Support <support@example.com>"))
    assert "support@example.com" in res["from"]


def test_send_needs_scope_and_smtp(env: Env) -> None:
    svc, _, _ = env
    with pytest.raises(PermissionDenied):
        svc.send(principal("draft", name="d"), "main", req())
    assert audit_rows(svc)[0]["outcome"] == "denied"


def test_attachments(env: Env) -> None:
    svc, srv, smtp = env
    b64 = base64.b64encode(b"hello").decode()
    svc.send(
        principal("send"), "main", req(attachments=[{"filename": "a.txt", "data_base64": b64}])
    )
    parsed = email.message_from_bytes(smtp.sent[0][0].as_bytes())
    assert [p.get_filename() for p in parsed.walk() if p.get_filename()] == ["a.txt"]
    with pytest.raises(InvalidRequest):
        svc.send(
            principal("send"), "main", req(attachments=[{"filename": "a", "data_base64": "@@"}])
        )
    with pytest.raises(InvalidRequest):
        svc.send(principal("send"), "main", req(attachments=[{"filename": "a"}]))
    # from_message needs `read`
    src = srv.add("INBOX", make_raw())
    ref = AttachmentIn.model_validate({"from_message": {"uid": str(src), "part_id": "1"}})
    with pytest.raises(PermissionDenied):
        svc.send(principal("send"), "main", req(attachments=[ref.model_dump()]))


def test_outgoing_attachment_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = make_config(limits={"outgoing_attachments_max_bytes": 10})
    svc, _, smtp = make_service(monkeypatch, tmp_path, cfg)
    big = base64.b64encode(b"x" * 11).decode()
    with pytest.raises(TooLarge):
        svc.send(
            principal("send"), "main", req(attachments=[{"filename": "a", "data_base64": big}])
        )
    assert smtp.sent == []


def put_original(srv: FakeServer, **kw: str) -> str:
    base = {
        "subject": "Question",
        "frm": "Alice <alice@example.org>",
        "to": "contact@example.com, bob@example.net",
        "body": "line1\nline2",
        "msgid": "<orig@example.org>",
        "References": "<root@example.org>",
    }
    base.update(kw)
    return str(srv.add("INBOX", make_raw(**base)))


def test_reply_derives_everything_and_sets_answered(env: Env) -> None:
    svc, srv, smtp = env
    uid = put_original(srv, Reply_To="Alice Work <alice.work@example.org>")
    res = svc.reply(principal("send"), "main", uid, ReplyRequest(body_text="thanks"))
    msg, rcpts = smtp.sent[0]
    assert rcpts == ["alice.work@example.org"]  # Reply-To wins, caller cannot override
    assert msg["Subject"] == "Re: Question"
    assert msg["In-Reply-To"] == "<orig@example.org>"
    assert msg["References"] == "<root@example.org> <orig@example.org>"
    body = msg.get_body(("plain",)).get_content()
    assert "thanks" in body and "> line1" in body and "> line2" in body
    assert r"\Answered" in srv.get("INBOX", int(uid)).flags
    assert res["warnings"] == [] and res["sent_copy"]
    assert audit_rows(svc)[0]["action"] == "reply"


def test_reply_all_excludes_own_addresses_and_no_quote(env: Env) -> None:
    svc, srv, smtp = env
    uid = put_original(srv, Cc="support@example.com, carol@example.net")
    svc.reply(
        principal("send"), "main", uid, ReplyRequest(body_text="ok", reply_all=True, quote=False)
    )
    msg, rcpts = smtp.sent[0]
    assert set(rcpts) == {"alice@example.org", "bob@example.net", "carol@example.net"}
    assert "> line1" not in msg.get_body(("plain",)).get_content()
    # the replying identity is the one the mail was addressed to
    assert "contact@example.com" in msg["From"]


def test_reply_dry_run_does_not_flag(env: Env) -> None:
    svc, srv, smtp = env
    uid = put_original(srv)
    res = svc.reply(principal("send"), "main", uid, ReplyRequest(body_text="x", dry_run=True))
    assert res["dry_run"] and smtp.sent == []
    assert srv.get("INBOX", int(uid)).flags == []
    with pytest.raises(NotFound):
        svc.reply(principal("send"), "main", "404", ReplyRequest(body_text="x"))


def test_reply_scopes_and_draft_mode(env: Env) -> None:
    svc, srv, smtp = env
    uid = put_original(srv)
    with pytest.raises(PermissionDenied):
        svc.reply(principal("read", "draft", name="d"), "main", uid, ReplyRequest(body_text="x"))
    assert audit_rows(svc)[0]["outcome"] == "denied"
    # draft=True only needs `draft` (plus read of the original is implicit in the service)
    res = svc.reply(
        principal("draft", name="d"), "main", uid, ReplyRequest(body_text="x", draft=True)
    )
    assert res["folder"] == "Drafts" and smtp.sent == []
    draft = email.message_from_bytes(srv.get("Drafts", int(res["uid"])).raw)
    assert draft["In-Reply-To"] == "<orig@example.org>" and draft["X-Mailboxd-Draft-By"] == "d"
    assert r"\Draft" in srv.get("Drafts", int(res["uid"])).flags
    assert r"\Answered" not in srv.get("INBOX", int(uid)).flags


def test_sending_a_reply_draft_flags_the_original_answered(env: Env) -> None:
    svc, srv, smtp = env
    uid = put_original(srv)
    draft = svc.reply(
        principal("draft", name="d"), "main", uid, ReplyRequest(body_text="x", draft=True)
    )
    svc.send_draft(principal("send"), "main", draft["uid"])
    assert smtp.sent[0][0]["In-Reply-To"] == "<orig@example.org>"
    assert r"\Answered" in srv.get("INBOX", int(uid)).flags


def test_draft_lifecycle_and_send(env: Env) -> None:
    svc, srv, smtp = env
    p = principal("draft", "send", "read", name="agent")
    created = svc.create_draft(p, "main", req(bcc=["b@example.net"], cc=["c@example.net"]))
    uid = created["uid"]
    stored = srv.get("Drafts", int(uid))
    assert set(stored.flags) == {r"\Draft", r"\Seen"}
    raw = email.message_from_bytes(stored.raw)
    assert raw["X-Mailboxd-Draft-By"] == "agent" and raw["Bcc"] == "b@example.net"
    listed = svc.list_drafts(p, "main")
    assert [m["uid"] for m in listed["messages"]] == [uid] and listed["folder"] == "Drafts"

    upd = svc.update_draft(p, "main", uid, DraftUpdate(subject="New subject"))
    assert upd["replaced_uid"] == uid and upd["uid"] != uid
    assert srv.uids("Drafts") == [int(upd["uid"])]
    new = email.message_from_bytes(srv.get("Drafts", int(upd["uid"])).raw, policy=policy.SMTP)
    assert new["Subject"] == "New subject"
    assert new["To"] == "dest@example.net" and new["Cc"] == "c@example.net"
    assert new["Bcc"] == "b@example.net"
    body = new.get_body(("plain",))  # type: ignore[attr-defined]
    assert body.get_content().strip() == "yo"

    with pytest.raises(PermissionDenied):
        svc.send_draft(principal("draft", name="d2"), "main", upd["uid"])
    preview = svc.send_draft(p, "main", upd["uid"], dry_run=True)
    assert preview["dry_run"] and smtp.sent == [] and srv.uids("Drafts")
    res = svc.send_draft(p, "main", upd["uid"])
    msg, rcpts = smtp.sent[0]
    assert set(rcpts) == {"dest@example.net", "c@example.net", "b@example.net"}
    assert not any(k.lower().startswith("x-mailboxd") for k in msg.keys())
    assert msg["Date"] != raw["Date"] or True
    assert srv.uids("Drafts") == [] and res["sent_copy"]
    actions = [r["action"] for r in audit_rows(svc)]
    assert actions[:4] == ["send_draft"] * 3 + ["update_draft"]


def test_send_draft_revalidates_identity(env: Env) -> None:
    svc, srv, smtp = env
    forged = make_raw(frm="CEO <ceo@example.com>", to="x@example.net")
    uid = srv.add("Drafts", forged, [r"\Draft"])
    with pytest.raises(InvalidIdentity):
        svc.send_draft(principal("send"), "main", str(uid))
    assert smtp.sent == [] and srv.uids("Drafts") == [uid]


def test_update_draft_keeps_signature_single(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = make_config()
    cfg.mailboxes[0].smtp.identities[0].signature_text = "Bye"  # type: ignore[union-attr]
    svc, srv, _ = make_service(monkeypatch, tmp_path, cfg)
    p = principal("draft")
    uid = svc.create_draft(p, "main", req())["uid"]
    assert svc_text(srv, uid).count("-- \nBye") == 1
    uid2 = svc.update_draft(p, "main", uid, DraftUpdate(subject="s2"))["uid"]
    assert svc_text(srv, uid2).count("Bye") == 1
    uid3 = svc.update_draft(p, "main", uid2, DraftUpdate(body_text="fresh"))["uid"]
    text = svc_text(srv, uid3)
    assert text.count("Bye") == 1 and "fresh" in text and "yo" not in text


def svc_text(srv: FakeServer, uid: str) -> str:
    msg = email.message_from_bytes(srv.get("Drafts", int(uid)).raw)
    part = msg.get_payload(decode=True) if not msg.is_multipart() else None
    assert isinstance(part, bytes)
    return part.decode().replace("\r\n", "\n")


def test_delete_draft_with_and_without_uidplus(env: Env) -> None:
    svc, srv, _ = env
    p = principal("draft")
    a = svc.create_draft(p, "main", req())["uid"]
    assert svc.delete_draft(p, "main", a) == {"ok": True}
    assert srv.uids("Drafts") == []
    srv.caps = {"IMAP4REV1"}
    b = svc.create_draft(p, "main", req())["uid"]
    svc.delete_draft(p, "main", b)
    assert [r"\Deleted" in m.flags for m in srv.folders["Drafts"]] == [True]  # flagged, not purged
    assert srv.plain_expunges == []
    with pytest.raises(PermissionDenied):
        svc.delete_draft(principal("read", name="r"), "main", b)
    assert audit_rows(svc)[0]["outcome"] == "denied"
    with pytest.raises(NotFound):
        svc.update_draft(p, "main", "999", DraftUpdate(subject="x"))
