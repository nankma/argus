"""
Shared SQLite-backed subscriber store for bot.py (the public info bot) and
admin_bot.py (the approval bot). Both processes need to see the same
approval state, so this can't live in either bot's in-memory dict — see
docs/plans/bot-features-plan.md item 1.

DB_FILE is configurable via SUBSCRIBERS_DB_FILE so a containerized
deployment can point both bots at the same file on a shared volume (see
docs/plans/deployment-plan.md) — same reasoning as agent.py's PHOENIX_ENDPOINT.
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


def _ensure_column(conn, table: str, column: str, sql_type: str) -> None:
    """Adds `column` to `table` if an older schema (from before this
    column existed) doesn't already have it -- ALTER TABLE ADD COLUMN
    isn't naturally idempotent like CREATE TABLE IF NOT EXISTS, so check
    first. A no-op for a freshly-created table, which already has every
    column from its own CREATE TABLE statement."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _migrate_api_budget_table(conn) -> None:
    """The original api_budget schema kept exactly one row per source
    (today's count only, silently overwritten whenever the date rolled
    over) -- there was no way to answer "how many calls have we made in
    total" or "what did we use yesterday," only "what have we used
    today." Migrated 2026-08-16 to one row per (source, date), so usage
    has real queryable history (see get_api_budget_history/
    get_total_api_calls) instead of losing every prior day's count.
    Detects the old single-column-PK schema via PRAGMA and carries
    forward whatever row already existed (that source's most recently
    recorded day) rather than silently dropping it. A no-op on a fresh
    database (table doesn't exist yet -- CREATE TABLE IF NOT EXISTS
    below creates the new schema directly) or one already migrated."""
    columns = conn.execute("PRAGMA table_info(api_budget)").fetchall()
    if not columns:
        return
    pk_columns = [row[1] for row in columns if row[5] > 0]
    if pk_columns != ["source"]:
        return
    conn.execute("ALTER TABLE api_budget RENAME TO api_budget_old")
    conn.execute(
        """
        CREATE TABLE api_budget (
            source TEXT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (source, date)
        )
        """
    )
    conn.execute("INSERT INTO api_budget (source, date, count) SELECT source, date, count FROM api_budget_old")
    conn.execute("DROP TABLE api_budget_old")


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
        _ensure_column(conn, "subscribers", "interests", "TEXT")
        _ensure_column(conn, "subscribers", "push_enabled", "INTEGER")
        _ensure_column(conn, "subscribers", "push_interval_hours", "INTEGER")
        _ensure_column(conn, "subscribers", "last_push_at", "TEXT")
        _ensure_column(conn, "subscribers", "pushed_links", "TEXT")
        _ensure_column(conn, "subscribers", "language", "TEXT")
        _ensure_column(conn, "subscribers", "restricted_sources_enabled", "INTEGER")
        _migrate_api_budget_table(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_budget (
                source TEXT NOT NULL,
                date TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (source, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_pull_state (
                source TEXT PRIMARY KEY,
                last_pulled_at TEXT NOT NULL
            )
            """
        )
        # last_article_dt: the published_dt of the newest article actually
        # SEEN from this source, not when the job last ran -- see
        # get_source_last_article_dt's docstring for why this is a
        # different, more robust value than last_pulled_at for "since"
        # filtering specifically.
        _ensure_column(conn, "source_pull_state", "last_article_dt", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interest_categories (
                interest TEXT PRIMARY KEY,
                categories TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def try_consume_api_budget(source: str, daily_cap: int, today: str) -> bool:
    """Global (not per-user) daily call budget for a rate-limited news
    source -- see docs/plans/local-news-cache-plan.md's Perigon/NewsAPI worked
    examples. Returns True and records the call if `source` is still under
    `daily_cap` for `today`; returns False (without incrementing) if the
    cap is already reached. `today` is passed in rather than computed here so
    callers/tests control the date deterministically, same reasoning as
    news_push.py's `now` parameter."""
    with _connect() as conn:
        row = conn.execute("SELECT count FROM api_budget WHERE source = ? AND date = ?", (source, today)).fetchone()
        if row is None:
            conn.execute("INSERT INTO api_budget (source, date, count) VALUES (?, ?, 1)", (source, today))
            return True
        if row[0] >= daily_cap:
            return False
        conn.execute(
            "UPDATE api_budget SET count = count + 1 WHERE source = ? AND date = ?", (source, today)
        )
        return True


def record_api_call(source: str, today: str) -> None:
    """Records one API call against `source` for `today` WITHOUT checking
    or enforcing any cap -- for callers that want usage visibility but
    aren't subject to news_ingest.py's own scheduled-pull budget (e.g.
    agent.py's search_news, an on-demand admin path that was previously
    invisible to this table entirely -- see docs/current/ai-news-sources.md's
    "Restricted sources" section). Shares the same api_budget table/rows
    as try_consume_api_budget, so get_api_budget_history/
    get_total_api_calls reflect combined usage from both call sites."""
    with _connect() as conn:
        row = conn.execute("SELECT count FROM api_budget WHERE source = ? AND date = ?", (source, today)).fetchone()
        if row is None:
            conn.execute("INSERT INTO api_budget (source, date, count) VALUES (?, ?, 1)", (source, today))
        else:
            conn.execute(
                "UPDATE api_budget SET count = count + 1 WHERE source = ? AND date = ?", (source, today)
            )


def get_api_budget_history(source: str, limit: int = 30) -> list[dict]:
    """Most recent `limit` days' recorded call counts for `source`,
    newest first -- the queryable history the old single-row-per-source
    schema couldn't provide."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date, count FROM api_budget WHERE source = ? ORDER BY date DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    return [{"date": date, "count": count} for date, count in rows]


def get_total_api_calls(source: str) -> int:
    """Sum of every recorded call for `source` across all days retained
    in the table -- combines news_ingest.py's budget-enforced calls and
    agent.py's search_news' merely-recorded calls, since both write into
    the same table."""
    with _connect() as conn:
        row = conn.execute("SELECT SUM(count) FROM api_budget WHERE source = ?", (source,)).fetchone()
    return row[0] or 0


def get_cached_interest_categories(interests: list[str]) -> dict[str, list[str]]:
    """Returns {interest: categories} for whichever of `interests` already
    have a cached classification -- an interest with no cached mapping is
    simply absent from the result, not present with an empty list (that
    distinction matters to the caller: "not yet classified" and
    "classified as belonging to no category" need different handling, see
    news_push.py's resolve_interest_categories). Global, not per-user --
    the same interest text (e.g. "AI") means the same categories no
    matter which subscriber set it, so this is shared cache, not scoped
    to a chat_id, the same reasoning as api_budget/source_pull_state
    above."""
    if not interests:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" * len(interests))
        rows = conn.execute(
            f"SELECT interest, categories FROM interest_categories WHERE interest IN ({placeholders})",
            interests,
        ).fetchall()
    return {interest: json.loads(categories_json) for interest, categories_json in rows}


def set_interest_categories(interest: str, categories: list[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO interest_categories (interest, categories) VALUES (?, ?)
            ON CONFLICT(interest) DO UPDATE SET categories = excluded.categories
            """,
            (interest, json.dumps(categories)),
        )


def get_source_last_pulled_at(source: str) -> datetime | None:
    """Drives news_ingest.py's per-source due-check, same shape as
    get_last_push_at/record_push for subscribers -- a source's own pull
    frequency (docs/plans/local-news-cache-plan.md) is independent of any one
    subscriber's push schedule. Also reused (with the synthetic keys
    healthcheck.INGEST_TICK_KEY/PUSH_TICK_KEY, not a real source name) to
    track whether the ingest/push jobs are ticking AT ALL, independent of
    any individual source/subscriber's own due-check -- see
    healthcheck.py. Same table, same shape, no separate schema needed:
    "when did X last run" is the same question whether X is a source
    pull or a whole job's tick."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_pulled_at FROM source_pull_state WHERE source = ?", (source,)
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def set_source_last_pulled_at(source: str, when: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO source_pull_state (source, last_pulled_at) VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_pulled_at = excluded.last_pulled_at
            """,
            (source, when.isoformat()),
        )


def get_source_last_article_dt(source: str) -> datetime | None:
    """The published_dt of the newest article actually SEEN from this
    source so far -- deliberately a different value from
    get_source_last_pulled_at (when the job last RAN). news_ingest.py
    uses this one as the "since" cutoff for time-filterable sources,
    2026-08-16, after a real design correction: using last_pulled_at (wall-
    clock job time) for that meant an article that a source indexes with
    a delay (confirmed live for NewsAPI's free tier -- up to ~36h, see
    docs/current/ai-news-sources.md) could be silently skipped forever, because
    last_pulled_at keeps advancing every cycle regardless of whether
    anything new was actually found, and a delayed article's own
    published_dt can fall behind a since-cutoff that already moved past it
    by the time the source finally surfaces it. This value only advances
    when a newer article is actually observed, so it can't outrun what's
    genuinely been seen the way a wall-clock timestamp can."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_article_dt FROM source_pull_state WHERE source = ?", (source,)
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def set_source_last_article_dt(source: str, when: datetime) -> None:
    """Upserts -- same reasoning as set_source_last_pulled_at, a separate
    column on the same row rather than a new table, since both are just
    "one timestamp per source." Callers should only call this with a value
    that's >= the current one (news_ingest.py only ever passes the max
    published_dt actually observed this cycle) -- this function itself
    doesn't enforce monotonicity, since the only caller already guarantees
    it and a defensive check here would just be unreachable code."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO source_pull_state (source, last_pulled_at, last_article_dt) VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET last_article_dt = excluded.last_article_dt
            """,
            (source, when.isoformat(), when.isoformat()),
        )


def get_health_state() -> list[str]:
    """The last set of healthcheck.py problem descriptions that were
    actually alerted on -- used to debounce repeat alerts for a problem
    that's still ongoing (see healthcheck.run_health_check). Empty list
    if never set (e.g. never unhealthy, or a fresh database)."""
    with _connect() as conn:
        row = conn.execute("SELECT value FROM health_state WHERE key = 'last_alerted_problems'").fetchone()
    return json.loads(row[0]) if row else []


def set_health_state(problems: list[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO health_state (key, value) VALUES ('last_alerted_problems', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps(problems),),
        )


def list_all_interests() -> list[str]:
    """Distinct interests across every subscriber with any stored --
    used by news_ingest.py to query the budget-capped, query-capable
    sources (Perigon, NewsAPI) against real topics people actually care
    about, rather than a generic default that would never surface
    something specific like the AAOI case that originally motivated
    adding these sources. Order is stable (first-seen) but not otherwise
    meaningful -- callers needing a priority order (e.g. truncating to fit
    a tight daily budget) should impose their own."""
    with _connect() as conn:
        rows = conn.execute("SELECT interests FROM subscribers WHERE interests IS NOT NULL").fetchall()
    seen = []
    for (interests_json,) in rows:
        for topic in json.loads(interests_json):
            if topic not in seen:
                seen.append(topic)
    return seen


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
            SELECT chat_id, interests, push_interval_hours, last_push_at, pushed_links, language,
                   restricted_sources_enabled
            FROM subscribers
            WHERE status = ? AND push_enabled = 1
            """,
            (APPROVED,),
        ).fetchall()
    result = []
    for chat_id, interests_json, interval_hours, last_push_at, pushed_links_json, language, restricted in rows:
        result.append(
            {
                "chat_id": chat_id,
                "interests": json.loads(interests_json) if interests_json else [],
                "push_interval_hours": interval_hours if interval_hours is not None else DEFAULT_PUSH_INTERVAL_HOURS,
                "last_push_at": datetime.fromisoformat(last_push_at) if last_push_at else None,
                "pushed_links": json.loads(pushed_links_json) if pushed_links_json else [],
                "language": language,
                "restricted_sources_enabled": bool(restricted),
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


def get_restricted_sources_enabled(chat_id: int) -> bool:
    """Per-user gate on search_news's use of news_sources.RESTRICTED_SOURCES
    (NewsAPI, Perigon) -- deliberately not tied to admin status in code, so
    it can be granted to someone else later with a plain DB update, not a
    new code path. Defaults False for everyone; set True explicitly (see
    set_restricted_sources_enabled) -- bot.py grants it to the admin's own
    chat_id at startup, nobody else by default."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT restricted_sources_enabled FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return bool(row and row[0])


def set_restricted_sources_enabled(chat_id: int, enabled: bool) -> None:
    """Upserts -- same reasoning as set_language()."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, restricted_sources_enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET restricted_sources_enabled = excluded.restricted_sources_enabled
            """,
            (chat_id, APPROVED, datetime.now().isoformat(), int(enabled)),
        )
