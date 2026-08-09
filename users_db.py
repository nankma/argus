"""
Shared SQLite-backed subscriber store for bot.py (the public info bot) and
admin_bot.py (the approval bot). Both processes need to see the same
approval state, so this can't live in either bot's in-memory dict — see
docs/bot-features-plan.md item 1.

DB_FILE is configurable via SUBSCRIBERS_DB_FILE so a containerized
deployment can point both bots at the same file on a shared volume (see
docs/deployment-plan.md) — same reasoning as agent.py's PHOENIX_ENDPOINT.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_FILE = os.environ.get("SUBSCRIBERS_DB_FILE", "subscribers.db")

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"

DEFAULT_PUSH_INTERVAL_HOURS = 24
MIN_PUSH_INTERVAL_HOURS = 1
# Bounds how many previously-pushed article links are remembered per user --
# a fallback dedup check for articles whose "published" date didn't parse
# (see news_push.py), not meant as a full history. Cheap to keep generous
# at this project's scale (owner + a few friends).
_MAX_REMEMBERED_PUSHED_LINKS = 200


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, column: str, sql_type: str) -> None:
    """Adds `column` to subscribers if an older schema (from before this
    column existed) doesn't already have it -- ALTER TABLE ADD COLUMN
    isn't naturally idempotent like CREATE TABLE IF NOT EXISTS, so check
    first. A no-op for a freshly-created table, which already has every
    column from the CREATE TABLE statement below."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(subscribers)")}
    if column not in existing:
        conn.execute(f"ALTER TABLE subscribers ADD COLUMN {column} {sql_type}")


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
                decided_at TEXT,
                interests TEXT,
                push_enabled INTEGER,
                push_interval_hours INTEGER,
                last_push_at TEXT,
                pushed_links TEXT,
                language TEXT
            )
            """
        )
        _ensure_column(conn, "interests", "TEXT")
        _ensure_column(conn, "push_enabled", "INTEGER")
        _ensure_column(conn, "push_interval_hours", "INTEGER")
        _ensure_column(conn, "last_push_at", "TEXT")
        _ensure_column(conn, "pushed_links", "TEXT")
        _ensure_column(conn, "language", "TEXT")


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


def get_interests(chat_id: int) -> list[str]:
    with _connect() as conn:
        row = conn.execute("SELECT interests FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row or not row[0]:
        return []
    return json.loads(row[0])


def set_interests(chat_id: int, interests: list[str]) -> None:
    """Upserts -- a chat_id may not have a subscribers row yet (e.g. the
    admin, who bypasses the approval flow in check_access() entirely and
    so never goes through request_access())."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, interests)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET interests = excluded.interests
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), json.dumps(interests)),
        )


# Real incident, 2026-08-08: a user asked to add the same conceptual
# interest twice (once blocked by an unrelated guardrail flake, then
# resent), and the agent -- which composes free-text topic labels rather
# than picking from a fixed enum -- generated two near-identical but not
# byte-identical strings ("...NVIDIA Jetson, etc.)" vs "...NVIDIA
# Jetson)"). An exact case-insensitive match missed it. Word-set Jaccard
# similarity catches near-duplicate phrasing like that while still
# treating genuinely different topics that happen to share one common
# word (e.g. "AI" and "AI regulation") as distinct -- a naive substring
# check would have false-positived on that case.
_DUPLICATE_TOPIC_SIMILARITY_THRESHOLD = 0.7


def _topic_words(topic: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", topic.lower()))


def _is_duplicate_topic(a: str, b: str) -> bool:
    words_a, words_b = _topic_words(a), _topic_words(b)
    if not words_a or not words_b:
        return False
    union = words_a | words_b
    return len(words_a & words_b) / len(union) >= _DUPLICATE_TOPIC_SIMILARITY_THRESHOLD


def add_interest(chat_id: int, topic: str) -> list[str]:
    """Adds `topic` if not already present -- a fuzzy (word-overlap) check,
    not just exact/case-insensitive, since the topic string is LLM-
    generated free text that varies phrasing between calls even for the
    same underlying interest. Stores the topic as given. Returns the
    resulting full list."""
    interests = get_interests(chat_id)
    if not any(_is_duplicate_topic(t, topic) for t in interests):
        interests.append(topic)
        set_interests(chat_id, interests)
    return interests


def remove_interest(chat_id: int, topic: str) -> list[str]:
    """Removes `topic` (case-insensitive match) if present. Returns the
    resulting full list."""
    interests = [t for t in get_interests(chat_id) if t.lower() != topic.lower()]
    set_interests(chat_id, interests)
    return interests


def get_push_enabled(chat_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT push_enabled FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return bool(row[0]) if row and row[0] is not None else False


def set_push_enabled(chat_id: int, enabled: bool) -> None:
    """Upserts -- same reasoning as set_interests()."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, push_enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET push_enabled = excluded.push_enabled
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), int(enabled)),
        )


def get_push_interval_hours(chat_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT push_interval_hours FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row and row[0] is not None else DEFAULT_PUSH_INTERVAL_HOURS


def set_push_interval_hours(chat_id: int, hours: int) -> None:
    """Upserts -- same reasoning as set_interests(). Suggested presets are
    24/12/6/4h (per the user's own request), but any integer >= the
    project-wide floor is accepted -- MIN_PUSH_INTERVAL_HOURS exists so a
    typo or an over-eager agent can't schedule something that would hammer
    news sources/DeepSeek every few minutes."""
    if hours < MIN_PUSH_INTERVAL_HOURS:
        raise ValueError(f"push interval must be at least {MIN_PUSH_INTERVAL_HOURS} hour(s)")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, push_interval_hours)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET push_interval_hours = excluded.push_interval_hours
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), hours),
        )


def get_pushed_links(chat_id: int) -> list[str]:
    with _connect() as conn:
        row = conn.execute("SELECT pushed_links FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row or not row[0]:
        return []
    return json.loads(row[0])


def get_last_push_at(chat_id: int) -> datetime | None:
    with _connect() as conn:
        row = conn.execute("SELECT last_push_at FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0])


def record_push(chat_id: int, article_links: list[str], pushed_at: datetime) -> None:
    """Called after a periodic digest is actually sent (or after a due
    check finds nothing new -- see news_push.py) to advance the dedup
    state: last_push_at resets the "how long until due again" clock, and
    pushed_links is the fallback dedup list for articles whose published
    date didn't parse (see news_sources.py's published_dt). Capped to the
    most recent _MAX_REMEMBERED_PUSHED_LINKS, newest first."""
    existing = get_pushed_links(chat_id)
    merged = (article_links + [link for link in existing if link not in article_links])[
        :_MAX_REMEMBERED_PUSHED_LINKS
    ]
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, last_push_at, pushed_links)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_push_at = excluded.last_push_at,
                pushed_links = excluded.pushed_links
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), pushed_at.isoformat(), json.dumps(merged)),
        )


def list_push_enabled_subscribers() -> list[dict]:
    """Approved subscribers with push_enabled=true, with everything
    news_push.py's scheduler needs to decide who's due and what to filter
    against -- avoids the scheduler doing its own row-by-row SQL."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, interests, push_interval_hours, last_push_at, pushed_links, language
            FROM subscribers
            WHERE status = ? AND push_enabled = 1
            """,
            (APPROVED,),
        ).fetchall()
    result = []
    for chat_id, interests_json, interval_hours, last_push_at, pushed_links_json, language in rows:
        result.append(
            {
                "chat_id": chat_id,
                "interests": json.loads(interests_json) if interests_json else [],
                "push_interval_hours": interval_hours if interval_hours is not None else DEFAULT_PUSH_INTERVAL_HOURS,
                "last_push_at": datetime.fromisoformat(last_push_at) if last_push_at else None,
                "pushed_links": json.loads(pushed_links_json) if pushed_links_json else [],
                "language": language,
            }
        )
    return result


def get_language(chat_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT language FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    return row[0] if row and row[0] else None


def set_language(chat_id: int, language: str | None) -> None:
    """Upserts -- same reasoning as set_interests(). `language` is free
    text (e.g. "Spanish", "繁體中文"), not a constrained code list -- same
    trust-the-LLM approach as interests' topic strings. None/empty clears
    the preference, falling back to matching whatever language the user's
    own message is written in (the existing default behavior)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, language)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET language = excluded.language
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), language or None),
        )
