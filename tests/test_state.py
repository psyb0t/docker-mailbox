"""Tests for mailboxd.state (SQLite audit / rate limit / cursors)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mailboxd.errors import RateLimited
from mailboxd.state import State


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "nested" / "state.db")


def test_audit_roundtrip_and_filters(db: str) -> None:
    s = State(db)
    s.audit("mila", "send", "contact", "INBOX", ["1", "2"], outcome="ok", subject_len=5)
    s.audit("mila", "delete", "support", "INBOX", "7", outcome="denied", reason="scope")
    s.audit("bob", "send", "contact", None, None)
    rows = s.list_audit()
    assert [r["token"] for r in rows] == ["bob", "mila", "mila"]  # newest first
    send = s.list_audit(action="send", token="mila")
    assert len(send) == 1
    assert send[0]["uids"] == "1,2"
    assert send[0]["folder"] == "INBOX"
    assert send[0]["details"] == {"subject_len": 5}
    assert s.list_audit(mailbox="support")[0]["outcome"] == "denied"
    assert s.list_audit(since=time.time() + 100) == []
    assert len(s.list_audit(limit=1)) == 1
    assert rows[0]["details"] is None


def test_audit_persists_across_instances(db: str) -> None:
    State(db).audit("t", "move", "m")
    assert State(db).list_audit()[0]["action"] == "move"


def test_rate_limit_per_mailbox_and_token(db: str) -> None:
    s = State(db)
    s.check_and_record_send("m", "t1", 2, 10)
    s.check_and_record_send("m", "t2", 2, 10)
    with pytest.raises(RateLimited) as exc:
        s.check_and_record_send("m", "t3", 2, 10)
    assert exc.value.status == 429
    assert 1 <= exc.value.retry_after <= 3600
    # Another mailbox is unaffected, but the per-token limit applies.
    s.check_and_record_send("other", "t1", 2, 2)
    with pytest.raises(RateLimited):
        s.check_and_record_send("other2", "t1", 5, 2)


def test_rate_limit_shared_between_state_instances(db: str) -> None:
    a, b = State(db), State(db)
    a.check_and_record_send("m", "t", 2, 2)
    b.check_and_record_send("m", "t", 2, 2)
    with pytest.raises(RateLimited):
        a.check_and_record_send("m", "t", 2, 2)
    with pytest.raises(RateLimited):
        b.check_and_record_send("m", "t", 2, 2)


def test_rate_limit_zero_means_unlimited(db: str) -> None:
    s = State(db)
    for _ in range(5):
        s.check_and_record_send("m", "t", 0, 0)


def test_rate_limit_rejected_send_is_not_recorded(db: str) -> None:
    s = State(db)
    s.check_and_record_send("m", "t", 1, 0)
    for _ in range(3):
        with pytest.raises(RateLimited):
            s.check_and_record_send("m", "t", 1, 0)
    # Still just the single recorded send: another token on a new mailbox passes.
    s.check_and_record_send("m2", "t", 1, 0)


def test_rate_limit_concurrent_sends_never_exceed_limit(db: str) -> None:
    s1, s2 = State(db), State(db)
    ok: list[int] = []
    lock = threading.Lock()

    def worker(state: State) -> None:
        for _ in range(5):
            try:
                state.check_and_record_send("m", "t", 4, 0)
            except RateLimited:
                continue
            with lock:
                ok.append(1)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (s1, s2, s1, s2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 4


def test_old_sends_leave_the_window(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    s = State(db)
    s.check_and_record_send("m", "t", 1, 0)
    real = time.time
    monkeypatch.setattr("mailboxd.state.time.time", lambda: real() + 3601)
    s.check_and_record_send("m", "t", 1, 0)


def test_cursors(db: str) -> None:
    s = State(db)
    assert s.get_cursor("t", "daily", "m", "INBOX") is None
    s.set_cursor("t", "daily", "m", "INBOX", "42", 10)
    assert s.get_cursor("t", "daily", "m", "INBOX") == ("42", 10)
    s.set_cursor("t", "daily", "m", "INBOX", "42", 25)
    assert State(db).get_cursor("t", "daily", "m", "INBOX") == ("42", 25)
    # Scoped per token, name, mailbox and folder.
    assert s.get_cursor("other", "daily", "m", "INBOX") is None
    assert s.get_cursor("t", "weekly", "m", "INBOX") is None
    assert s.get_cursor("t", "daily", "m2", "INBOX") is None
    assert s.get_cursor("t", "daily", "m", "Sent") is None
