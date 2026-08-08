"""
Shared SQLite-backed subscriber store for bot.py (the public info bot) and
admin_bot.py (the approval bot). Both processes need to see the same
approval state, so this can't live in either bot's in-memory dict — see
docs/bot-features-plan.md item 1.

DB_FILE is configurable via SUBSCRIBERS_DB_FILE so a containerized
deployment can point both bots at the same file on a shared volume (see
docs/deployment-plan.md) — same reasoning as agent.py's PHOENIX_ENDPOINT.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_FILE = os.environ.get("SUBSCRIBERS_DB_FILE", "subscribers.db")

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )


def get_status(chat_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row else None


def request_access(chat_id: int, username: str | None, first_name: str | None) -> None:
    """Insert a pending request. A no-op if this chat_id already has a row
    (pending, approved, or denied) — re-messaging shouldn't reset a
    decision back to pending."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, username, first_name, status, requested_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO NOTHING
            """,
            (chat_id, username, first_name, PENDING, datetime.now().isoformat()),
        )


def decide(chat_id: int, approved: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE subscribers SET status = ?, decided_at = ? WHERE chat_id = ?",
            (APPROVED if approved else DENIED, datetime.now().isoformat(), chat_id),
        )


def list_pending() -> list[tuple]:
    with _connect() as conn:
        return conn.execute(
            "SELECT chat_id, username, first_name FROM subscribers WHERE status = ?",
            (PENDING,),
        ).fetchall()
