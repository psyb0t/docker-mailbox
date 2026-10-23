"""imap_client.Session against the configurable mock IMAP server."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import imap_mock
import pytest

from mailboxd import imap_client
from mailboxd.config import ImapConfig
from mailboxd.imap_client import ImapError, Session

MODERN = {"MOVE", "UIDPLUS", "SPECIAL-USE"}

Start = Callable[..., tuple[imap_mock.Sink, ImapConfig]]


@pytest.fixture()
def start() -> Iterator[Start]:
    servers: list[Any] = []

    def _start(
        messages: dict[int, bytes] | None = None,
        caps: set[str] | None = None,
        folders: dict[str, Any] | None = None,
    ) -> tuple[imap_mock.Sink, ImapConfig]:
        sink, srv, host, port = imap_mock.start(messages or {}, caps=caps, folders=folders)
        servers.append(srv)
        cfg = ImapConfig(host=host, port=port, tls="none", username="u@example.com", password="p")
        # The special-folder cache is keyed by host/port/user; ports are unique per server.
        return sink, cfg

    yield _start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _msgs(*subjects: str) -> dict[int, bytes]:
    return {i: imap_mock.sample_message(s) for i, s in enumerate(subjects, start=1)}


# -- capabilities, LIST, special folders ---------------------------------


def test_capabilities_reflect_server(start: Start) -> None:
    _, cfg = start(caps=MODERN)
    with imap_client.session(cfg) as s:
        assert s.has("move") and s.has("UIDPLUS") and s.has("SPECIAL-USE")
        assert not s.has("QUOTA")
    _, cfg2 = start()
    with imap_client.session(cfg2) as s:
        assert not s.has("MOVE")


def test_list_parsing_names_with_spaces_and_literals(start: Start) -> None:
    folders = {
        "Sent Items": {},
        "Archive/2026": {},
        'Odd "name"': {"literal": True},
    }
    _, cfg = start(caps=MODERN, folders=folders)
    detailed = imap_client.list_folders_detailed(cfg)
    names = [f["name"] for f in detailed]
    assert names == ["INBOX", "Sent Items", "Archive/2026", 'Odd "name"']
    by_name = {f["name"]: f for f in detailed}
    assert by_name["Sent Items"]["flags"] == ["\\HasNoChildren", "\\Sent"]
    assert by_name["Sent Items"]["delimiter"] == "/"
    assert by_name["INBOX"]["flags"] == ["\\HasNoChildren"]
    assert imap_client.list_folders(cfg) == names


def test_special_folder_detected_by_flag(start: Start) -> None:
    folders = {
        "Envoyes": {"attrs": ["\\Sent"]},
        "Corbeille": {"attrs": ["\\Trash"]},
        "Brouillons": {"attrs": ["\\Drafts"]},
        "Pourriel": {"attrs": ["\\Junk"]},
        "Sent": {"attrs": []},  # a decoy: the flag wins over the name
    }
    _, cfg = start(caps=MODERN, folders=folders)
    with imap_client.session(cfg) as s:
        assert s.special_folder("sent") == "Envoyes"
        assert s.special_folder("trash") == "Corbeille"
        assert s.special_folder("drafts") == "Brouillons"
        assert s.special_folder("junk") == "Pourriel"


def test_special_folder_detected_by_name_without_special_use(start: Start) -> None:
    _, cfg = start(folders={"Sent Items": {}, "Drafts": {}, "Deleted Items": {}, "Spam": {}})
    with imap_client.session(cfg) as s:
        assert s.special_folder("sent") == "Sent Items"
        assert s.special_folder("drafts") == "Drafts"
        assert s.special_folder("trash") == "Deleted Items"
        assert s.special_folder("junk") == "Spam"


def test_special_folder_nested_leaf_and_override_and_missing(start: Start) -> None:
    _, cfg = start(folders={"[Gmail]/Trash": {}})
    with imap_client.session(cfg) as s:
        assert s.special_folder("trash") == "[Gmail]/Trash"
        assert s.special_folder("sent", override="Custom") == "Custom"
        with pytest.raises(ImapError, match="no sent folder"):
            s.special_folder("sent")
    assert imap_client.special_folder(cfg, imap_client.FoldersConfig(sent="X"), "sent") == "X"


# -- select / search / fetch ---------------------------------------------


def test_select_returns_uidvalidity(start: Start) -> None:
    _, cfg = start(folders={"Other": {"messages": {}, "uidvalidity": 777}})
    with imap_client.session(cfg) as s:
        assert s.select("INBOX") == "1"
        assert s.select("Other") == "777"


def test_search_criteria_against_flags_and_headers(start: Start) -> None:
    sink, cfg = start(
        _msgs("One", "Two", "Three"),
        caps=MODERN,
    )
    sink.folders["INBOX"].flags[1].update({"\\Seen", "$Suspect"})
    sink.folders["INBOX"].flags[3].add("\\Seen")
    with imap_client.session(cfg) as s:
        s.select("INBOX")
        assert s.search("ALL") == [1, 2, 3]
        assert s.search(imap_client.build_search_criteria(unseen=True)) == [2]
        assert s.search(imap_client.build_search_criteria(seen=True)) == [1, 3]
        assert s.search(imap_client.build_search_criteria(keyword="$Suspect")) == [1]
        assert s.search(imap_client.build_search_criteria(unkeyword="$Suspect")) == [2, 3]
        assert s.search(imap_client.build_search_criteria(message_id="Two@example.com")) == [2]
        assert s.search(imap_client.build_search_criteria(subject="three")) == [3]
        assert s.search(["UID", "2:*"]) == [2, 3]


def test_search_non_ascii_term_is_one_quoted_token(start: Start) -> None:
    raw = (
        "From: a@ext.com\r\nSubject: Caf\u00e9 cr\u00e8me\r\n"
        "Message-ID: <c@ext.com>\r\n\r\nbody\r\n"
    ).encode()
    sink, cfg = start({1: raw}, caps=MODERN)
    with imap_client.session(cfg) as s:
        s.select("INBOX")
        crit = imap_client.build_search_criteria(subject="Café crème")
        assert s.search(crit) == [1]
    line = next(c for c in sink.commands if "SEARCH" in c)
    assert 'SUBJECT "' in line and 'crème"' in line


def test_fetch_raw_uses_peek_and_does_not_set_seen(start: Start) -> None:
    sink, cfg = start(_msgs("Hello"), caps=MODERN)
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        raw = s.fetch_raw("1")
        assert b"Subject: Hello" in raw
        hdr = s.fetch_raw("1", header_only=True)
        assert b"Subject: Hello" in hdr and b"hello from the mock" not in hdr
        assert s.fetch_message("1")["Subject"] == "Hello"
    fetches = [c for c in sink.commands if "UID FETCH" in c and "(RFC822.SIZE)" not in c]
    assert fetches and all("BODY.PEEK[" in c for c in fetches)
    assert not any("RFC822" in c for c in fetches)
    assert sink.flags(1) == set()


def test_fetch_headers_honors_requested_fields(start: Start) -> None:
    _, cfg = start(_msgs("Alpha", "Beta"), caps=MODERN)
    with imap_client.session(cfg) as s:
        s.select("INBOX")
        out = s.fetch_headers([2, 1])
    assert [m["uid"] for m in out] == ["2", "1"]
    assert out[0]["subject"] == "Beta"
    assert out[0]["from"] == "alice@example.com"
    assert out[0]["message_id"] == "<Beta@example.com>"
    assert out[0]["size"] and out[0]["flags"] == []


def test_non_peek_fetch_would_set_seen_in_mock(start: Start) -> None:
    """Control: proves the mock models implicit \\Seen so the PEEK test above means something."""
    sink, cfg = start(_msgs("Hello"))
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        s.conn.uid("FETCH", "1", "(BODY[])")
    assert "\\Seen" in sink.flags(1)


def test_bodystructures_flag_attachments(start: Start) -> None:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "a@ext.com"
    m["Subject"] = "with file"
    m.set_content("body")
    m.add_attachment(b"data", maintype="application", subtype="pdf", filename="x.pdf")
    _, cfg = start({1: m.as_bytes(), 2: imap_mock.sample_message("plain")})
    page = imap_client.search_page(cfg, has_attachments=True)
    assert [x["uid"] for x in page["messages"]] == ["1"]
    page = imap_client.search_page(cfg, has_attachments=False)
    assert [x["uid"] for x in page["messages"]] == ["2"]


def test_search_page_paging_and_uidvalidity(start: Start) -> None:
    _, cfg = start(_msgs("a", "b", "c", "d"))
    page = imap_client.search_page(cfg, limit=2)
    assert [m["uid"] for m in page["messages"]] == ["4", "3"]
    assert page["next_before_uid"] == 3
    assert page["uidvalidity"] == "1"
    page2 = imap_client.search_page(cfg, limit=2, before_uid=3)
    assert [m["uid"] for m in page2["messages"]] == ["2", "1"]
    assert page2["next_before_uid"] is None


# -- store / move / expunge ----------------------------------------------


def test_store_updates_flags_and_requires_write_select(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), caps=MODERN)
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=True)
        with pytest.raises(ImapError, match="read-write"):
            s.store("1", "+FLAGS", ["\\Seen"])
        s.select("INBOX", readonly=False)
        s.store("1:2", "+FLAGS.SILENT", ["\\Seen", "$Fait"])
        s.store("2", "-FLAGS", ["\\Seen"])
        with pytest.raises(ImapError, match="invalid flag"):
            s.store("1", "+FLAGS", ["bad flag"])
    assert sink.flags(1) == {"\\Seen", "$Fait"}
    assert sink.flags(2) == {"$Fait"}


def test_mark_seen_and_store_flags_helpers(start: Start) -> None:
    sink, cfg = start(_msgs("a"))
    imap_client.mark_seen(cfg, "1")
    assert sink.flags(1) == {"\\Seen"}
    imap_client.mark_seen(cfg, ["1"], seen=False)
    assert sink.flags(1) == set()
    imap_client.store_flags(cfg, "1", add=["\\Flagged"])
    assert sink.flags(1) == {"\\Flagged"}


def test_move_with_move_capability(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), caps=MODERN, folders={"Trash": {}})
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        assert s.move("1", "Trash") == "move"
    assert sink.uids("INBOX") == [2]
    assert sink.uids("Trash") == [1]
    assert any("UID MOVE 1" in c for c in sink.commands)
    assert not sink.plain_expunges and not sink.uid_expunges


def test_move_with_copy_and_uid_expunge_when_uidplus(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), caps={"UIDPLUS"}, folders={"Trash": {}})
    sink.folders["INBOX"].flags[2].add("\\Deleted")  # someone else's pending delete
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        assert s.move("1", "Trash") == "copy+uid-expunge"
    assert sink.uids("INBOX") == [2]  # the unrelated \Deleted message survived
    assert sink.uids("Trash") == [1]
    assert sink.uid_expunges == [("INBOX", "1")]
    assert sink.plain_expunges == []


def test_move_without_uidplus_never_sends_plain_expunge(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), folders={"Trash": {}})
    sink.folders["INBOX"].flags[2].add("\\Deleted")
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        assert s.move("1", "Trash") == "copy+flagged-deleted"
        assert s.expunge_uids("1") is False
    assert sink.plain_expunges == []
    assert not any(v == "EXPUNGE" or v == "UID EXPUNGE" for v in sink.verbs())
    assert sink.uids("INBOX") == [1, 2]  # nothing purged
    assert "\\Deleted" in sink.flags(1)
    assert sink.uids("Trash") == [1]


def test_move_to_missing_folder_fails_cleanly(start: Start) -> None:
    sink, cfg = start(_msgs("a"), caps=MODERN)
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        with pytest.raises(ImapError):
            s.move("1", "Nope")
    assert sink.uids("INBOX") == [1]


def test_move_rejects_injection_in_uid_and_folder(start: Start) -> None:
    sink, cfg = start(_msgs("a"), caps=MODERN, folders={"Trash": {}})
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        with pytest.raises(ImapError):
            s.move("1\r\nZ1 EXPUNGE", "Trash")
        with pytest.raises(ImapError):
            s.move("1", "Trash\r\nZ2 EXPUNGE")
    assert not any(c.startswith(("Z1", "Z2")) for c in sink.commands)
    assert sink.plain_expunges == []


# -- append / create / status -------------------------------------------


def test_append_returns_appenduid_and_sets_flags(start: Start) -> None:
    sink, cfg = start(caps=MODERN, folders={"Drafts": {}})
    raw = imap_mock.sample_message("Draft one")
    with imap_client.session(cfg) as s:
        uid = s.append("Drafts", raw, ["\\Draft", "\\Seen"])
        uid2 = s.append("Drafts", raw, [])
    assert uid == "1" and uid2 == "2"
    assert sink.flags(1, "Drafts") == {"\\Draft", "\\Seen"}
    assert sink.flags(2, "Drafts") == set()
    assert sink.folders["Drafts"].msgs[1] == raw.replace(b"\n", b"\r\n")  # imaplib maps CRLF


def test_append_without_uidplus_returns_none(start: Start) -> None:
    sink, cfg = start(folders={"Sent Items": {}})
    with imap_client.session(cfg) as s:
        assert s.append("Sent Items", imap_mock.sample_message("x"), ["\\Seen"]) is None
    assert sink.uids("Sent Items") == [1]


def test_append_to_missing_folder_raises(start: Start) -> None:
    _, cfg = start(caps=MODERN)
    with imap_client.session(cfg) as s:
        with pytest.raises(ImapError):
            s.append("Nope", b"Subject: x\r\n\r\nbody", [])


def test_create_status_and_folder_cache_refresh(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), caps=MODERN)
    sink.folders["INBOX"].flags[1].add("\\Seen")
    with imap_client.session(cfg) as s:
        assert "Projects 2026" not in [f["name"] for f in s.folders()]
        s.create("Projects 2026")
        assert "Projects 2026" in [f["name"] for f in s.folders()]
        with pytest.raises(ImapError):
            s.create("Projects 2026")
        assert s.status("INBOX") == {"messages": 2, "unseen": 1, "recent": 0}
    assert any("SUBSCRIBE" in c for c in sink.commands)


# -- injection regression through the new surface -------------------------


def test_store_flags_and_session_never_issue_plain_expunge(start: Start) -> None:
    sink, cfg = start(_msgs("a", "b"), caps=MODERN, folders={"Trash": {}})
    imap_client.store_flags(cfg, ["1"], add=["\\Deleted"])
    with imap_client.session(cfg) as s:
        s.select("INBOX", readonly=False)
        s.move("2", "Trash")
        s.expunge_uids("1")
    assert sink.plain_expunges == []
    assert sink.uids("INBOX") == []


def test_session_is_exported_type() -> None:
    assert Session is imap_client.Session
