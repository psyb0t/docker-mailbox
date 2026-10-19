from __future__ import annotations

import email
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from mailboxd import imap_client, insights, smtp_client
from mailboxd.config import ImapConfig, MailboxConfig, SmtpConfig
from mailboxd.errors import Conflict
from mailboxd.imap_client import ImapError
from mailboxd.principal import Principal

P = Principal(name="t", scopes=frozenset({"read"}))


def hdr(
    uid: int,
    mid: str,
    date: str,
    subject: str = "Hello",
    refs: list[str] | None = None,
    irt: str = "",
    flags: list[str] | None = None,
    frm: str = "a@x.org",
) -> dict[str, Any]:
    return {
        "uid": str(uid),
        "flags": flags or [],
        "from": frm,
        "to": "me@x.org",
        "cc": "",
        "subject": subject,
        "date": date,
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
    }


D1 = "Mon, 01 Jan 2024 10:00:00 +0000"
D2 = "Mon, 01 Jan 2024 11:00:00 +0000"
D3 = "Mon, 01 Jan 2024 12:00:00 +0000"
D4 = "Mon, 01 Jan 2024 13:00:00 +0000"

FOLDERS = {
    "INBOX": [
        hdr(1, "<a@x>", D1),
        hdr(2, "<b@x>", D3, "Re: Hello", ["<a@x>"], "<a@x>", ["\\Seen"]),
        hdr(3, "<c@x>", D4, "Other"),
    ],
    "Sent": [hdr(1, "<r@x>", D2, "Re: Hello", ["<a@x>"], "<a@x>", ["\\Seen"], "me@x.org")],
}


class Conn:
    def noop(self) -> tuple[str, list[Any]]:
        return "OK", []


class FakeSession:
    def __init__(self) -> None:
        self.data = FOLDERS
        self.cur = ""
        self.conn = Conn()
        self.criteria: list[Any] = []

    def select(self, folder: str, readonly: bool = True) -> str:
        if folder not in self.data:
            raise ImapError("no such folder")
        self.cur = folder
        return "1"

    def special_folder(self, kind: str, override: str | None = None) -> str:
        if override:
            return override
        if kind == "sent":
            return "Sent"
        raise ImapError("none")

    def search(self, criteria: Any) -> list[int]:
        self.criteria.append(criteria)
        msgs = self.data[self.cur]
        if criteria == "ALL":
            return [int(m["uid"]) for m in msgs]
        wanted = {c.strip('"') for c in criteria if isinstance(c, str) and c.startswith('"')}
        return [
            int(m["uid"]) for m in msgs if wanted & ({m["message_id"]} | set(m["references"]))
        ]

    def fetch_headers(self, uids: list[int]) -> list[dict[str, Any]]:
        by = {int(m["uid"]): dict(m) for m in self.data[self.cur]}
        return [by[u] for u in uids if u in by]

    def fetch_message(self, uid: str, header_only: bool = False) -> Any:
        m = next(m for m in self.data[self.cur] if m["uid"] == uid)
        raw = f"Message-ID: {m['message_id']}\r\nSubject: {m['subject']}\r\n"
        if m["references"]:
            raw += "References: " + " ".join(m["references"]) + "\r\n"
        if m["in_reply_to"]:
            raw += f"In-Reply-To: {m['in_reply_to']}\r\n"
        return email.message_from_string(raw + "\r\n")

    def folders(self) -> list[dict[str, Any]]:
        return [
            {"name": "INBOX", "delimiter": "/", "flags": []},
            {"name": "Sent", "delimiter": "/", "flags": ["\\Sent"]},
            {"name": "Archive", "delimiter": "/", "flags": ["\\Noselect"]},
        ]

    def status(self, folder: str) -> dict[str, int]:
        return {"messages": len(self.data[folder]), "unseen": 1, "recent": 0}

    def quota(self) -> list[dict[str, Any]]:
        return [{"resource": "storage", "used": 1, "limit": 10}]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    s = FakeSession()

    @contextmanager
    def sess(cfg: ImapConfig) -> Iterator[FakeSession]:
        yield s

    monkeypatch.setattr(imap_client, "session", sess)
    return s


def mailbox(imap: bool = True, smtp: bool = False) -> MailboxConfig:
    return MailboxConfig(
        name="m",
        imap=ImapConfig(host="h", username="me@x.org", password="p") if imap else None,
        smtp=(
            SmtpConfig(host="h", username="u", password="p", from_address="me@x.org")
            if smtp
            else None
        ),
    )


def test_thread_spans_folder_and_sent(fake: FakeSession) -> None:
    out = insights.thread(None, P, mailbox(), "2", None)
    assert out["root"] == "<a@x>"
    assert out["count"] == 3
    assert [m["message_id"] for m in out["messages"]] == ["<a@x>", "<r@x>", "<b@x>"]
    assert {m["folder"] for m in out["messages"]} == {"INBOX", "Sent"}
    assert any(c[0] == "OR" for c in fake.criteria)


def test_thread_requires_imap() -> None:
    with pytest.raises(Conflict):
        insights.thread(None, P, mailbox(imap=False, smtp=True), "1", None)


def test_threads_grouping_and_order(fake: FakeSession) -> None:
    out = insights.threads(None, P, mailbox(), None, 10)["threads"]
    assert [t["subject"] for t in out] == ["Other", "Hello"]
    hello = out[1]
    assert hello["count"] == 3
    assert hello["unread"] == 1  # only <a@x> (INBOX uid 1) is unseen
    assert hello["thread_id"] == "<a@x>"
    assert hello["last_date"] == D3
    assert "a@x.org" in hello["participants"]
    assert [m["uid"] for m in hello["messages"]] == ["1", "1", "2"]
    assert insights.threads(None, P, mailbox(), None, 1)["threads"][0]["subject"] == "Other"


def test_group_messages_subject_fallback_only_without_ids() -> None:
    a = hdr(1, "", D1, "Re: Plan")
    b = hdr(2, "", D2, "Fwd: plan")
    c = hdr(3, "<z@x>", D3, "Plan")
    groups = insights.group_messages([a, b, c])
    assert sorted(len(g) for g in groups) == [1, 2]


def test_stats_skips_noselect(fake: FakeSession) -> None:
    out = insights.stats(None, P, mailbox())
    assert [f["name"] for f in out["folders"]] == ["INBOX", "Sent"]
    assert out["folders"][1]["special"] == "sent"
    assert out["folders"][0]["messages"] == 3
    assert out["quota"][0]["resource"] == "storage"


def test_health_deep(fake: FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(cfg: SmtpConfig) -> None:
        raise smtp_client.SmtpError("down")

    monkeypatch.setattr(smtp_client, "check", bad)
    res = insights.health_deep(None, P, [mailbox(smtp=True)])
    assert res["ok"] is False
    mb = res["mailboxes"][0]
    assert mb["imap"]["ok"] is True
    assert mb["smtp"]["ok"] is False and "down" in mb["smtp"]["error"]

    monkeypatch.setattr(smtp_client, "check", lambda cfg: None)
    assert insights.health_deep(None, P, [mailbox(smtp=True)])["ok"] is True
    assert insights.health_deep(None, P, [])["ok"] is True


def test_health_deep_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def boom(cfg: ImapConfig) -> Iterator[Any]:
        raise ImapError("login failed")
        yield

    monkeypatch.setattr(imap_client, "session", boom)
    res = insights.health_deep(None, P, [mailbox()])
    assert res["ok"] is False
    assert res["mailboxes"][0]["smtp"] is None
    assert "login failed" in res["mailboxes"][0]["imap"]["error"]
