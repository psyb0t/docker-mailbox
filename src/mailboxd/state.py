"""Small SQLite store for the bits that must survive restarts and be shared
between uvicorn workers: the audit log, the send rate limiter and the
"new since last read" checkpoints.

One short-lived connection per call (WAL mode, busy timeout) keeps it safe
across threads and processes without a pool. Message bodies are never stored.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import RateLimited

_SCHEMA = [
    # v1
    """
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        token TEXT NOT NULL,
        action TEXT NOT NULL,
        mailbox TEXT,
        folder TEXT,
        uids TEXT,
        outcome TEXT NOT NULL,
        details TEXT
    );
    CREATE INDEX audit_ts ON audit (ts);
    CREATE TABLE send_log (
        ts REAL NOT NULL,
        mailbox TEXT NOT NULL,
        token TEXT NOT NULL
    );
    CREATE INDEX send_log_mailbox ON send_log (mailbox, ts);
    CREATE INDEX send_log_token ON send_log (token, ts);
    CREATE TABLE cursors (
        token TEXT NOT NULL,
        name TEXT NOT NULL,
        mailbox TEXT NOT NULL,
        folder TEXT NOT NULL,
        uidvalidity TEXT NOT NULL,
        last_uid INTEGER NOT NULL,
        updated REAL NOT NULL,
        PRIMARY KEY (token, name, mailbox, folder)
    );
    """,
]

WINDOW_SECONDS = 3600


class State:
    def __init__(self, path: str) -> None:
        self.path = path
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self._ensure()
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            yield conn
        finally:
            conn.close()

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                try:
                    for i, ddl in enumerate(_SCHEMA[version:], start=version + 1):
                        for stmt in ddl.split(";"):
                            if stmt.strip():
                                conn.execute(stmt)
                        conn.execute(f"PRAGMA user_version = {i}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            self._ready = True

    # ── Audit ────────────────────────────────────────────────────────────

    def audit(
        self,
        token: str,
        action: str,
        mailbox: str | None = None,
        folder: str | None = None,
        uids: list[str] | str | None = None,
        outcome: str = "ok",
        **details: Any,
    ) -> None:
        if isinstance(uids, list):
            uids = ",".join(uids)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit (ts, token, action, mailbox, folder, uids, outcome, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    token,
                    action,
                    mailbox,
                    folder,
                    uids,
                    outcome,
                    json.dumps(details, default=str, ensure_ascii=False) if details else None,
                ),
            )

    def list_audit(
        self,
        since: float | None = None,
        action: str | None = None,
        token: str | None = None,
        mailbox: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q = "SELECT id, ts, token, action, mailbox, folder, uids, outcome, details FROM audit"
        where: list[str] = []
        args: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if action:
            where.append("action = ?")
            args.append(action)
        if token:
            where.append("token = ?")
            args.append(token)
        if mailbox:
            where.append("mailbox = ?")
            args.append(mailbox)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "ts": r[1],
                    "token": r[2],
                    "action": r[3],
                    "mailbox": r[4],
                    "folder": r[5],
                    "uids": r[6],
                    "outcome": r[7],
                    "details": json.loads(r[8]) if r[8] else None,
                }
            )
        return out

    # ── Send rate limit ──────────────────────────────────────────────────

    def check_and_record_send(
        self, mailbox: str, token: str, mailbox_limit: int, token_limit: int
    ) -> None:
        """Atomically refuse (RateLimited) or record one send. A limit of 0 is unlimited."""
        now = time.time()
        floor = now - WINDOW_SECONDS
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM send_log WHERE ts < ?", (floor,))
                for column, value, limit in (
                    ("mailbox", mailbox, mailbox_limit),
                    ("token", token, token_limit),
                ):
                    if limit <= 0:
                        continue
                    count, oldest = conn.execute(
                        f"SELECT COUNT(*), MIN(ts) FROM send_log WHERE {column} = ? AND ts >= ?",
                        (value, floor),
                    ).fetchone()
                    if count >= limit:
                        retry = int(max(1, (oldest or now) + WINDOW_SECONDS - now))
                        raise RateLimited(
                            f"send limit reached for {column} {value!r}: {limit} per hour",
                            retry_after=retry,
                        )
                conn.execute(
                    "INSERT INTO send_log (ts, mailbox, token) VALUES (?, ?, ?)",
                    (now, mailbox, token),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ── Checkpoints ──────────────────────────────────────────────────────

    def get_cursor(
        self, token: str, name: str, mailbox: str, folder: str
    ) -> tuple[str, int] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT uidvalidity, last_uid FROM cursors"
                " WHERE token = ? AND name = ? AND mailbox = ? AND folder = ?",
                (token, name, mailbox, folder),
            ).fetchone()
        return (str(row[0]), int(row[1])) if row else None

    def set_cursor(
        self, token: str, name: str, mailbox: str, folder: str, uidvalidity: str, last_uid: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cursors (token, name, mailbox, folder, uidvalidity, last_uid, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (token, name, mailbox, folder) DO UPDATE SET"
                " uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid,"
                " updated = excluded.updated",
                (token, name, mailbox, folder, uidvalidity, last_uid, time.time()),
            )
