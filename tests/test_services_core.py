"""Service: scopes, masking, reading, paging, checkpoints, organising, deletion."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from test_services_fakes import (
    FakeServer,
    Smtp,
    audit_rows,
    make_config,
    make_raw,
    make_service,
    not_found_message,
    principal,
)

from mailboxd import imap_client
from mailboxd.errors import Conflict, InvalidRequest, NotFound, PermissionDenied, TooLarge
from mailboxd.services import Service, decode_cursor, encode_cursor, labels_for

Env = tuple[Service, FakeServer, Smtp]


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return make_service(monkeypatch, tmp_path)


def test_cursor_roundtrip_and_garbage() -> None:
    c = encode_cursor({"v": "1", "b": 5})
    assert "=" not in c and decode_cursor(c) == {"v": "1", "b": 5}
    with pytest.raises(InvalidRequest):
        decode_cursor("!!not-a-cursor")


def test_labels_for() -> None:
    assert labels_for([r"\Seen", "$Suspect", r"\Flagged"]) == ["flagged", "suspect"]
    assert labels_for(["$Foo"], {"foo": "$Foo"}) == ["foo"]


def test_mailboxes_lists_only_visible(env: Env) -> None:
    svc, _, _ = env
    out = svc.mailboxes(principal("read", mailboxes=["ro", "main"]))["mailboxes"]
    assert [m["name"] for m in out] == ["main", "ro"]
    main = out[0]
    assert main["address"] == "contact@example.com" and main["smtp"] and main["imap"]
    assert [i["default"] for i in main["identities"]] == [True, False]


def test_hidden_mailbox_is_indistinguishable_from_unknown(env: Env) -> None:
    svc, _, _ = env
    p = principal("read", mailboxes=["ro"])
    hidden = not_found_message(svc, p, "main").replace("main", "X")
    assert hidden == not_found_message(svc, p, "ghost").replace("ghost", "X")
    with pytest.raises(NotFound):
        svc.send(principal(mailboxes=["ro"]), "main", None)  # type: ignore[arg-type]


def test_denied_mutation_is_audited(env: Env) -> None:
    svc, srv, _ = env
    uid = srv.add("INBOX", make_raw())
    with pytest.raises(PermissionDenied):
        svc.mark_seen(principal("read", name="reader"), "main", [str(uid)])
    with pytest.raises(PermissionDenied):
        svc.set_labels(principal("read", name="reader"), "main", [str(uid)], add=["fait"])
    rows = audit_rows(svc)
    assert [(r["action"], r["outcome"], r["token"]) for r in rows] == [
        ("set_labels", "denied", "reader"),
        ("mark_seen", "denied", "reader"),
    ]
    assert srv.get("INBOX", uid).flags == []


def test_missing_protocol_is_conflict(env: Env) -> None:
    svc, _, _ = env
    p = principal()
    with pytest.raises(Conflict):
        svc.list_messages(p, "out")
    with pytest.raises(Conflict):
        svc.identities(p, "ro")
    with pytest.raises(Conflict):
        svc.create_draft(p, "ro", None)  # type: ignore[arg-type]


def test_inbox_labels_and_label_filter(env: Env) -> None:
    svc, srv, _ = env
    srv.add("INBOX", make_raw("one"), ["$Suspect"])
    srv.add("INBOX", make_raw("two"))
    out = svc.inbox(principal("read"), mailbox="main,ro")
    by_subject = {m["subject"]: m for m in out["messages"]}
    assert by_subject["one"]["labels"] == ["suspect"]
    only = svc.inbox(principal("read"), mailbox="main", label="suspect")
    assert [m["subject"] for m in only["messages"]] == ["one"]
    with pytest.raises(InvalidRequest):
        svc.inbox(principal("read"), mailbox="main", label="nope")
    with pytest.raises(NotFound):
        svc.inbox(principal("read"), mailbox="ghost")
    with pytest.raises(PermissionDenied):
        svc.inbox(principal("send"))


def test_checkpoint_never_skips_mail(env: Env) -> None:
    svc, srv, _ = env
    p = principal("read")
    for i in range(5):
        srv.add("INBOX", make_raw(f"m{i}"))
    first = svc.inbox(p, mailbox="main", limit=2, checkpoint="agent")
    assert [m["uid"] for m in first["messages"]] == ["5", "4"]
    for i in range(5, 9):
        srv.add("INBOX", make_raw(f"m{i}"))
    seen: list[str] = []
    for _ in range(2):
        page = svc.inbox(p, mailbox="main", limit=2, checkpoint="agent")
        seen += [m["uid"] for m in page["messages"]]
    # oldest new first, two at a time: nothing skipped, nothing repeated
    assert seen == ["7", "6", "9", "8"]
    assert svc.inbox(p, mailbox="main", limit=2, checkpoint="agent")["messages"] == []
    # another checkpoint name starts independently
    assert len(svc.inbox(p, mailbox="main", limit=50, checkpoint="other")["messages"]) == 9


def test_since_cursor_and_uidvalidity_reset(env: Env) -> None:
    svc, srv, _ = env
    p = principal("read")
    for i in range(3):
        srv.add("INBOX", make_raw(f"m{i}"))
    cur = svc.inbox(p, mailbox="main")["next_since_cursor"]
    srv.add("INBOX", make_raw("new"))
    out = svc.inbox(p, mailbox="main", since_cursor=cur)
    assert [m["subject"] for m in out["messages"]] == ["new"] and "reset" not in out
    srv.uidvalidity = "999"
    out = svc.inbox(p, mailbox="main", since_cursor=cur)
    assert out["reset"] is True and len(out["messages"]) == 4
    with pytest.raises(InvalidRequest):
        svc.inbox(p, mailbox="main", since_cursor="garbage!!")


def test_list_messages_paging_and_search_checkpoint(env: Env) -> None:
    svc, srv, _ = env
    p = principal("read")
    for i in range(5):
        srv.add("INBOX", make_raw(f"m{i}"))
    a = svc.list_messages(p, "main", limit=2)
    assert [m["uid"] for m in a["messages"]] == ["5", "4"] and a["total"] == 5
    b = svc.list_messages(p, "main", limit=2, cursor=a["next_cursor"])
    assert [m["uid"] for m in b["messages"]] == ["3", "2"]
    c = svc.list_messages(p, "main", limit=2, cursor=b["next_cursor"])
    assert [m["uid"] for m in c["messages"]] == ["1"] and c["next_cursor"] is None
    srv.uidvalidity = "7"
    d = svc.list_messages(p, "main", limit=2, cursor=a["next_cursor"])
    assert d["reset"] is True and [m["uid"] for m in d["messages"]] == ["5", "4"]
    assert svc.search(p, "main", limit=3, checkpoint="s")["next_since_cursor"]
    srv.add("INBOX", make_raw("later"))
    s2 = svc.search(p, "main", limit=3, checkpoint="s")
    assert [m["subject"] for m in s2["messages"]] == ["later"]


def test_get_message_authentication_trusted_id(env: Env) -> None:
    svc, srv, _ = env
    forged = make_raw(
        "x",
        Authentication_Results="evil.example; spf=pass smtp.mailfrom=alice@example.org; dmarc=pass",
    )
    good = make_raw(
        "y", Authentication_Results="mx.good.example; spf=fail smtp.mailfrom=alice@example.org"
    )
    u1, u2 = srv.add("INBOX", forged), srv.add("INBOX", good)
    p = principal("read")
    a = svc.get_message(p, "main", str(u1))["authentication"]
    assert a["trusted"] is False and a["spf"] is None
    assert "authentication_untrusted" in a["signals"]
    b = svc.get_message(p, "main", str(u2))["authentication"]
    assert b["trusted"] is True and b["spf"]["result"] == "fail" and "spf_fail" in b["signals"]
    with pytest.raises(NotFound):
        svc.get_message(p, "main", "999")


def test_get_message_options(env: Env) -> None:
    svc, srv, _ = env
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.org", "contact@example.com", "h"
    m.set_content("plain")
    m.add_alternative('<p>hi<script>x()</script></p><a href="http://e.com">e</a>', subtype="html")
    uid = str(srv.add("INBOX", m.as_bytes()))
    p = principal("read")
    plain = svc.get_message(p, "main", uid)
    assert "headers" not in plain and "body_html_safe" not in plain
    assert "html_analysis" in plain
    full = svc.get_message(p, "main", uid, headers=True, safe_html=True)
    assert ["Subject", "h"] in full["headers"] and "<script" not in full["body_html_safe"]
    assert b"Subject: h" in svc.get_raw(p, "main", uid)


def test_get_attachment_and_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = make_config(limits={"attachment_max_bytes": 10})
    svc, srv, _ = make_service(monkeypatch, tmp_path, cfg)
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.org", "contact@example.com", "att"
    m.set_content("see")
    m.add_attachment(b"tiny", maintype="application", subtype="pdf", filename="a.pdf")
    m.add_attachment(b"x" * 50, maintype="application", subtype="pdf", filename="big.pdf")
    uid = str(srv.add("INBOX", m.as_bytes()))
    p = principal("read")
    info, data = svc.get_attachment(p, "main", uid, "2")
    assert data == b"tiny" and info["filename"] == "a.pdf"
    with pytest.raises(TooLarge):
        svc.get_attachment(p, "main", uid, "3")
    with pytest.raises(NotFound):
        svc.get_attachment(p, "main", uid, "1")


def test_delete_trashes_by_default(env: Env) -> None:
    svc, srv, _ = env
    uid = srv.add("INBOX", make_raw())
    res = svc.delete(principal("organize"), "main", [str(uid)])
    assert res["action"] == "trashed" and res["trash_folder"] == "Trash"
    assert srv.uids("INBOX") == [] and len(srv.uids("Trash")) == 1
    assert audit_rows(svc)[0]["action"] == "delete"
    in_trash = str(srv.uids("Trash")[0])
    with pytest.raises(Conflict):  # already in trash
        svc.delete(principal("organize"), "main", [in_trash], folder="Trash")
    with pytest.raises(InvalidRequest):
        svc.delete(principal("organize"), "main", ["1; DELETE"])


def test_permanent_delete_rules(env: Env) -> None:
    svc, srv, _ = env
    inbox_uid = srv.add("INBOX", make_raw("keep"))
    trash_uid = srv.add("Trash", make_raw("bye"))
    other = srv.add("Trash", make_raw("other"), [r"\Deleted"])
    with pytest.raises(PermissionDenied):
        svc.delete(principal("organize", name="o"), "main", [str(trash_uid)], "Trash", True)
    assert audit_rows(svc)[0]["outcome"] == "denied"
    with pytest.raises(Conflict):  # not the trash folder
        svc.delete(principal("delete"), "main", [str(inbox_uid)], permanent=True)
    assert srv.uids("INBOX") == [inbox_uid]
    res = svc.delete(principal("delete"), "main", [str(trash_uid)], "Trash", True)
    assert res["action"] == "deleted" and res["method"] == "uid-expunge"
    # UID EXPUNGE leaves the unrelated \Deleted message alone
    assert srv.uids("Trash") == [other] and srv.plain_expunges == []


def test_no_folder_wide_expunge_without_uidplus(env: Env) -> None:
    svc, srv, _ = env
    srv.caps = {"IMAP4REV1"}
    victim = srv.add("INBOX", make_raw("mv"))
    bystander = srv.add("INBOX", make_raw("by"), [r"\Deleted"])
    res = svc.delete(principal("organize"), "main", [str(victim)])
    assert res["method"] == "copy+flagged-deleted"
    assert srv.plain_expunges == []
    assert bystander in srv.uids("INBOX") and victim in srv.uids("INBOX")
    # permanent from Trash may fall back to a plain EXPUNGE, but only there
    t = srv.add("Trash", make_raw("t"))
    res = svc.delete(principal("delete"), "main", [str(t)], "Trash", True)
    assert res["method"] == "expunge-trash" and srv.plain_expunges == ["Trash"]
    assert bystander in srv.uids("INBOX")


def test_mark_labels_move_create_folder(env: Env) -> None:
    svc, srv, _ = env
    p = principal("organize")
    uid = str(srv.add("INBOX", make_raw()))
    svc.mark_seen(p, "main", [uid])
    assert r"\Seen" in srv.get("INBOX", int(uid)).flags
    svc.mark_seen(p, "main", [uid], seen=False)
    assert r"\Seen" not in srv.get("INBOX", int(uid)).flags
    svc.set_labels(p, "main", [uid], add=["suspect", "flagged"])
    assert set(srv.get("INBOX", int(uid)).flags) == {"$Suspect", r"\Flagged"}
    svc.set_labels(p, "main", [uid], remove=["suspect"])
    assert srv.get("INBOX", int(uid)).flags == [r"\Flagged"]
    with pytest.raises(InvalidRequest):
        svc.set_labels(p, "main", [uid], add=["bogus"])
    with pytest.raises(NotFound):
        svc.move(p, "main", [uid], "Nowhere")
    assert svc.create_folder(p, "main", "Archive")["folder"] == "Archive"
    with pytest.raises(Conflict):
        svc.create_folder(p, "main", "Archive")
    res = svc.move(p, "main", [uid], "Archive")
    assert res == {"ok": True, "method": "move", "to_folder": "Archive"}
    assert srv.uids("INBOX") == [] and len(srv.uids("Archive")) == 1
    assert {"move", "create_folder", "set_labels", "mark_seen"} <= {
        r["action"] for r in audit_rows(svc)
    }


def test_folders_and_identities(env: Env) -> None:
    svc, _, _ = env
    f = svc.folders(principal("read"), "main")
    assert "Trash" in f["folders"]
    assert {d["name"]: d["special"] for d in f["details"]}["Trash"] == "trash"
    ids = svc.identities(principal("read"), "main")["identities"]
    assert [i["address"] for i in ids] == ["contact@example.com", "support@example.com"]
    assert ids[0]["has_signature"] is False


def test_audit_log_admin_only(env: Env) -> None:
    svc, srv, _ = env
    uid = str(srv.add("INBOX", make_raw()))
    svc.mark_seen(principal("organize", name="a"), "main", [uid])
    with pytest.raises(PermissionDenied):
        svc.audit_log(principal("read"))
    out: Any = svc.audit_log(principal("admin"), action="mark_seen", token="a")
    assert len(out["entries"]) == 1 and out["entries"][0]["mailbox"] == "main"
    assert svc.audit_log(principal("admin"), since="2999-01-01T00:00:00")["entries"] == []


def test_search_page_after_uid_takes_oldest(env: Env) -> None:
    svc, srv, _ = env
    for i in range(6):
        srv.add("INBOX", make_raw(f"m{i}"))
    imap = svc.cfg.mailboxes[0].imap
    assert imap is not None
    page = imap_client.search_page(imap, "INBOX", limit=2, criteria="ALL", after_uid=1)
    assert [m["uid"] for m in page["messages"]] == ["3", "2"]
    assert page["next_after_uid"] == 3 and page["more_new"] is True
    done = imap_client.search_page(imap, "INBOX", limit=10, criteria="ALL", after_uid=3)
    assert done["next_after_uid"] == 6 and done["more_new"] is False
