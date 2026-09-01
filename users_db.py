"""
Shared SQLite-backed subscriber store for bot.py (the public info bot) and
admin_bot.py (the approval bot). Both processes need to see the same
approval state, so this can't live in either bot's in-memory dict — see
docs/plans/bot-features-plan.md item 1.

DB_FILE is configurable via storage.subscribers_db_file (settings.yml,
see app_settings.py) so a containerized deployment can point both bots
at the same file on a shared volume (see docs/plans/deployment-plan.md).
"""

import hashlib
import json
import secrets
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app_settings import get_settings

# required=True, no default -- both bots need a real, shared db path;
# settings.yml not having it is a deployment mistake, not something to
# paper over. See
# docs/standaloneplan/01-settings-migration.md's "Migration methodology".
DB_FILE = get_settings().resolved("storage.subscribers_db_file", required=True)

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"

DEFAULT_PUSH_INTERVAL_HOURS = 24
MIN_PUSH_INTERVAL_HOURS = 1
# How long a sent article's link is remembered per subscriber. This is the
# ONLY thing that decides whether they have already seen an article -- see
# news_push.select_candidate_articles, which as of 2026-08-19 filters on
# this and nothing else.
#
# Pruned by AGE, not by count (it used to keep the most recent 200). The
# count cap was silently wrong at short push intervals: news_cache's TTL is
# 48h, so an article can be re-offered for at most that long, but a
# subscriber on the 1h minimum interval could take ~48 pushes in that
# window and overflow 200 links -- evicting a link that was still
# re-offerable and re-sending the article. Keying on time makes the
# retention window match the thing it actually has to outlast.
#
# Deliberately longer than news_cache.DEFAULT_TTL_HOURS (48h): once an
# article ages out of the cache it can never be a candidate again, so this
# only needs to cover that window, and the margin costs a few rows.
PUSHED_LINK_RETENTION_HOURS = 72


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
        # Marks a row created by the local test API rather than by a real
        # Telegram user. See mark_test_account.
        _ensure_column(conn, "subscribers", "is_test", "INTEGER")
        # A stable identifier safe to send off this machine. See
        # external_id() -- chat_id is a real Telegram account id and there
        # is no reason for it to reach a third party.
        _ensure_column(conn, "subscribers", "external_id", "TEXT")
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
        # Same shape and reasoning as interest_categories above: global,
        # not per-subscriber (the same interest text means the same
        # definition no matter who added it), keyed on the stable
        # normalized-English interest string so this is a cache hit for
        # any interest that's been added by any subscriber before.
        # See news_classify.expand_interest_for_retrieval for what's
        # stored here and why -- a generated definition used as the
        # embedding query in news_push's relevance filter and offbeat
        # gate, instead of the bare interest string, because the bare
        # string was measured to rank genuinely relevant articles below
        # unrelated ones for topics whose real coverage uses different
        # vocabulary than the topic name itself (e.g. "AI coding" vs.
        # articles about a product literally named "Cowork").
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interest_query_expansions (
                interest TEXT PRIMARY KEY,
                expansion TEXT NOT NULL
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
        # news_keyness.py's per-category "how foreign is this word to this
        # topic" scores, for news_push._pick_novelty_extra's novelty
        # extra -- see docs/analysis/cluster-measurements.md's "Offbeat selection,
        # take two" section. Unlike interest_categories/
        # interest_query_expansions above (single-row upserts, one entry
        # never invalidates another), a category's whole row set is
        # replaced together every news_ingest.py cycle (set_category_
        # keyness below) -- keyness is a full recompute over the current
        # cache each time, not an incrementally-accumulated cache, so a
        # stale leftover term from three cycles ago would silently outlive
        # its own relevance if rows were only ever upserted, never pruned.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS category_keyness (
                category TEXT NOT NULL,
                term TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (category, term)
            )
            """
        )
        # The article taxonomy -- see docs/plans/taxonomy-and-admin-plan.md.
        # `name` is the primary key rather than an integer id because cached
        # article files store category NAMES as strings, and that cache is a
        # separate store (YAML on a volume, not rows). An id would mean
        # rewriting every cached file on a rename, or a join the file store
        # can't do.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                description TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                merged_into TEXT,
                centroid BLOB,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # One row per time the classifier reached for a label outside the
        # taxonomy. A log rather than a counter on the category row because a
        # counter can't expire: three sightings in January and two in June
        # reads as "5", which is noise spread over six months rather than a
        # trend. Same reasoning as PUSHED_LINK_RETENTION_HOURS -- prune by
        # age, not by count.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS category_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                article_link TEXT,
                article_title TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS category_sightings_name_at "
            "ON category_sightings (name, seen_at)"
        )
        # For a database created before sort_order existed. Without this the
        # curated order is lost and the prompt alphabetizes, which separates
        # Stock from Finance even though Stock's description cross-references
        # it -- see get_active_categories.
        _ensure_column(conn, "categories", "sort_order", "INTEGER NOT NULL DEFAULT 0")
        # When the admin was last asked about this proposal. NULL = never.
        # A timestamp rather than a status value: "we have asked" is not a
        # step in the approval state machine, it is a fact about a proposal
        # still sitting in 'proposed'.
        _ensure_column(conn, "categories", "alerted_at", "TEXT")
        # One row per subscriber per push cycle that got far enough to have
        # an outcome -- see docs/plans/incident-monitoring-plan.md.
        #
        # This duplicates information news_push.py already prints, on
        # purpose. The print lines are for a human reading `docker logs`;
        # they are free text with no timestamp of their own, and a container
        # swap on deploy destroys them. Neither property survives being the
        # input to an alarm, and a process inside the container cannot read
        # its own `docker logs` anyway. So the outcome is written here as
        # well, from the same call site (news_push._record) that prints it,
        # so a new branch cannot record without logging or log without
        # recording.
        #
        # Deliberately NOT recorded: the "not due yet" branch. It fires for
        # every subscriber on every tick and would dominate the table for
        # no signal this local DB is the right place to carry -- job
        # liveness is Logfire's job now (news_push._emit_heartbeat,
        # news_ingest._pull_source's ingest_source_pull span).
        # One row per (subscriber, interest) that has ever been pushed.
        # Rotation state, and only that: which of a subscriber's interests
        # went out longest ago, so a cycle capped at
        # MAX_INTERESTS_PER_PUSH still reaches all of them over time
        # instead of always serving the first few.
        #
        # Keyed on the topic string rather than an index into the
        # subscribers.interests JSON list, because that list is edited:
        # removing the second of five interests would silently shift every
        # index after it onto a different topic. A never-pushed topic has
        # no row at all, which sorts first -- exactly the right default for
        # a newly-added interest.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interest_push_state (
                chat_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                last_pushed_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, topic)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        # Two indexes for the two shapes of question the criteria ask:
        # "what happened across everyone in the last 24h" (ratio alarm) and
        # "what were this subscriber's last N outcomes" (consecutive-failure
        # alarm).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS push_outcomes_at "
            "ON push_outcomes (recorded_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS push_outcomes_chat_at "
            "ON push_outcomes (chat_id, recorded_at)"
        )
        _seed_categories(conn)
        _migrate_split_policy(conn)


# The taxonomy the classifier started with, moved here from
# news_classify.Category. Seeded once on an empty table; after that the
# table is authoritative and this list is history, not configuration --
# editing it will not change a database that has already been seeded.
#
# Descriptions are load-bearing: they go into the classifier prompt
# verbatim, so wording here directly affects how every article is
# classified. Kept exactly as they read in the original prompt so seeding
# is behaviour-neutral.
# How many interests one subscriber may follow. A cap rather than no cap
# because every interest is its own retrieval, its own candidate set and
# (since 2026-08-24) its own push message -- so an unbounded list is an
# unbounded per-cycle cost and an unbounded number of messages.
#
# Set generously: the failure this guards against is a runaway list, not a
# subscriber with varied tastes. MAX_INTERESTS_PER_PUSH in news_push.py is
# what actually bounds the noise, and rotation covers the rest.
MAX_INTERESTS = 10


SEED_CATEGORIES: list[tuple[str, str]] = [
    ("AI", "AI models, research, agents, LLMs"),
    ("Software", "software products, dev tools, programming"),
    ("Hardware", "chips, semiconductors, devices, infrastructure hardware"),
    ("IT", "enterprise IT, cloud, infrastructure, enterprise software"),
    ("Startups", "funding rounds, new companies, venture capital"),
    ("Finance", "business/financial industry news, economics, corporate deals"),
    ("Stock", "stock price moves, market reactions specifically -- distinct "
              "from Finance, which covers business news generally"),
    # Policy was retired 2026-08-20 and split into the four below -- see
    # _migrate_split_policy. Kept in this list so seeding an old database
    # still creates the row the migration then retires, and so the history
    # is visible rather than looking like it never existed.
    ("Policy", "regulation, government, legal, antitrust"),
    ("Security", "cybersecurity, breaches, vulnerabilities"),
    ("Research", "academic papers, science"),
    ("Consumer", "consumer gadgets, reviews, product launches for individual users"),
    ("Robotics", "robotics specifically"),
    ("Crypto", "cryptocurrency/blockchain"),
    # The four Policy was split into, added 2026-08-20. They are literally
    # the four words Policy's own description listed, which is what made it
    # a bundle rather than a category: a probe of 94 general-news articles
    # found Policy absorbing 65% of all category assignments, including a
    # food-safety outbreak and a CDC appointment. Splitting it lets a
    # subscriber who cares about antitrust stop receiving cabinet
    # nominations.
    #
    # Added by hand rather than waiting for the classifier to propose them,
    # because it will not: the same probe produced ZERO proposed labels
    # against 94 articles. A category only gets proposed when nothing
    # plausible exists to absorb the article, and Policy was plausible
    # enough to absorb anything governmental. A3 finds gaps with no
    # neighbour; it cannot find a bundle that is too coarse.
    ("Regulation", "rules and compliance imposed on an industry -- export "
                   "controls, safety standards, licensing"),
    ("Government", "government action and process -- agencies, appointments, "
                   "budgets, procurement, public programmes"),
    ("Legal", "courts, lawsuits, rulings, liability, intellectual property"),
    ("Antitrust", "competition law specifically -- monopoly, market power, "
                  "mergers under review, breakup remedies"),
]

CATEGORY_SIGHTING_RETENTION_DAYS = 30

# The outcome of one subscriber's turn in one push cycle, as recorded in
# push_outcomes. Exhaustive by intent: every branch of news_push's
# per-subscriber loop that does not `continue` before doing any work ends
# at exactly one of these.
PUSH_DELIVERED = "delivered"            # digest generated, sent, accepted
PUSH_NOTHING_NEW = "nothing_new"        # due, but no candidate articles
PUSH_NOT_RELEVANT = "not_relevant"      # model saw candidates, wrote nothing
PUSH_BLOCKED = "blocked"                # digest failed the output guardrail
PUSH_NO_INTERESTS = "no_interests"      # push on, but nothing to push about
PUSH_CHAT_NOT_FOUND = "chat_not_found"  # generated, then Telegram refused
PUSH_MODEL_ERROR = "model_error"        # an LLM call raised
PUSH_CYCLE_FAILED = "cycle_failed"      # anything else raised
PUSH_DISABLED = "disabled"              # struck out; push turned off for them

# Outcomes that mean an LLM was actually called to write a digest -- the
# denominator of the delivered/generated ratio, which is criterion 3 in
# docs/plans/incident-monitoring-plan.md and the number that would have
# caught the 2026-08-21 leak on day one (3 delivered of 22 generated).
#
# nothing_new and no_interests are excluded because they return before
# write_push_digest, so no digest was paid for -- and counting them would
# let a flood of idle subscribers hide a collapsed delivery rate.
#
# "No spend at all" would be slightly too strong for nothing_new:
# resolve_interest_categories runs first and can make one classification
# call for an interest string never seen before. That is once per distinct
# interest ever, since the result is cached permanently in
# interest_categories, so it does not accumulate -- but the denominator
# does undercount by that one call, and the ratio is about digests
# specifically.
PUSH_GENERATED_OUTCOMES = frozenset({
    PUSH_DELIVERED,
    PUSH_NOT_RELEVANT,
    PUSH_BLOCKED,
    PUSH_CHAT_NOT_FOUND,
    PUSH_MODEL_ERROR,
})

# Longer than the 30-day sightings window: the ratio alarm reads 24 hours,
# but answering "was this normal?" after an incident means comparing
# against the weeks before it, and these rows are tiny.
PUSH_OUTCOME_RETENTION_DAYS = 90

# How many sightings inside the retention window before an admin is asked.
#
# A PLACEHOLDER, not a measured value. It was picked from a single
# observation of "Education" before any of this existed. As of 2026-08-20
# production has four proposals sitting at one sighting each, which cannot
# distinguish a real gap from a model slip -- that takes seeing the same
# label recur across separate cycles. Revisit once the sightings table has
# a distribution worth reading.
CATEGORY_PROPOSAL_THRESHOLD = 5

# Written by the code, never chosen by the model. It marks "the classifier
# looked and nothing applied", which needs to be distinguishable from "the
# classifier never ran" -- collapsing those two is what hid a three-day
# outage, since `categories: []` looked identical either way.
#
# Deliberately NOT offered to the model. status='system' keeps it out of
# get_active_categories(), and therefore out of the prompt: give an LLM
# classifier a catch-all option and it stops working for the answer,
# reaching for the escape hatch instead of deciding between Finance and
# Policy. The model still just returns an empty list; the code translates.
UNCLASSIFIABLE = "Other"


def _seed_categories(conn) -> None:
    """Populates the taxonomy on first run only. INSERT OR IGNORE rather
    than a count check so a category an admin later retired or renamed
    doesn't silently reappear on the next restart."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR IGNORE INTO categories "
        "(name, description, status, created_at, created_by, sort_order) "
        "VALUES (?, ?, 'active', ?, 'seed', ?)",
        [(name, description, now, i) for i, (name, description) in enumerate(SEED_CATEGORIES)],
    )
    # A name the model already PROPOSED is promoted to active rather than
    # skipped. INSERT OR IGNORE alone was too broad: it exists to stop a
    # category an admin retired from being resurrected, and that reasoning
    # covers rejected/retired/merged -- all of which are decisions someone
    # made. 'proposed' is not a decision, it is a pending one, and seeding a
    # name IS the decision it was waiting for.
    #
    # Not hypothetical. The 2026-08-20 Policy split shipped Regulation,
    # Government, Legal and Antitrust; on production, Legal had already been
    # proposed by the classifier two hours earlier, so INSERT OR IGNORE
    # skipped it and it stayed `proposed` with a NULL description -- absent
    # from the prompt. The split was 3/4 applied and nothing said so.
    conn.executemany(
        "UPDATE categories SET status = 'active', description = ?, sort_order = ? "
        "WHERE name = ? AND status = 'proposed'",
        [(description, i, name) for i, (name, description) in enumerate(SEED_CATEGORIES)],
    )
    # status='system', so get_active_categories() -- and therefore the
    # classifier prompt -- never sees it. It exists as a row so it resolves
    # like any other name and an admin can count how big the bucket is.
    conn.execute(
        "INSERT OR IGNORE INTO categories "
        "(name, description, status, created_at, created_by, sort_order) "
        "VALUES (?, ?, 'system', ?, 'seed', ?)",
        (UNCLASSIFIABLE, "the classifier found nothing applicable", now, len(SEED_CATEGORIES)),
    )


def _migrate_split_policy(conn) -> None:
    """Retires `Policy` in favour of Regulation/Government/Legal/Antitrust,
    once.

    Policy's description was "regulation, government, legal, antitrust" --
    it was a bundle of four things, and a probe of 94 general-news articles
    measured it absorbing 65% of every category assignment made, including
    a food-safety outbreak and a CDC appointment. A subscriber tracking
    antitrust was necessarily also subscribed to cabinet nominations.

    Retired rather than deleted, and rather than merged into one of the
    four: articles cached with the `Policy` label keep it (retired
    categories keep old labels), and there is no single survivor a merge
    could point at -- that is what makes this a split rather than a merge.
    The tombstone machinery in the plan doc handles merges; a split has no
    equivalent, and this is why.

    Guarded by a marker in health_state rather than by checking Policy's
    status, so an admin who deliberately re-activates Policy later does not
    have it silently retired again on the next restart. That resurrection
    problem is the same one _seed_categories' INSERT OR IGNORE avoids in the
    other direction.

    Verified before writing this: no row in interest_categories mapped to
    Policy, so no subscriber's filter is emptied by the retirement. An
    interest left with no categories matches EVERY article -- see
    news_push.select_candidate_articles -- so retiring a category that
    subscribers do map to needs re-resolving those interests first, not
    just stripping the name."""
    done = conn.execute(
        "SELECT value FROM health_state WHERE key = 'policy_split_migrated'"
    ).fetchone()
    if done:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE categories SET status = 'retired', decided_at = ?, decided_by = 'migration' "
        "WHERE name = 'Policy' AND status = 'active'",
        (now,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO health_state (key, value) VALUES ('policy_split_migrated', ?)",
        (now,),
    )


def get_active_categories() -> list[tuple[str, str]]:
    """The (name, description) pairs the classifier should offer, in
    `sort_order`.

    The order is curated, not incidental. Ordering by name instead
    alphabetizes the list, which separates Stock from Finance -- and
    Stock's description reads "distinct from Finance, which covers business
    news generally", a cross-reference that only works when they are
    adjacent. A stable order also keeps the prompt string identical between
    runs, so classification differences are attributable to the input
    rather than to row ordering.

    Returned rather than read inside news_classify so that module stays
    free of a database dependency and its tests can pass a taxonomy
    directly, same reasoning as build_agent taking its model as a
    parameter (see CLAUDE.md)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, description FROM categories WHERE status = 'active' "
            "ORDER BY sort_order, name"
        ).fetchall()
    return [(name, description or "") for name, description in rows]


def resolve_category_name(name: str) -> str | None:
    """Follows `merged_into` so a name stored on an article cached before a
    merge still resolves to the surviving category.

    Returns None for a name that isn't in the taxonomy, and also for a
    merge cycle -- which should be impossible, since a merge targets an
    active category and an active category has no merged_into. If one
    exists the data is corrupt, so it is logged rather than sharing the
    ordinary not-found path silently: both make an article's category
    vanish from every filter, and only one of them is a bug."""
    seen = set()
    with _connect() as conn:
        while name and name not in seen:
            seen.add(name)
            row = conn.execute(
                "SELECT status, merged_into FROM categories WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            status, merged_into = row
            if status != "merged" or not merged_into:
                return name
            name = merged_into
    print(f"[users_db] merge cycle resolving category {name!r} -- taxonomy is corrupt")
    return None


# Telegram caps callback_data at 64 bytes and admin_bot packs
# "cat:into:{name}:{target}" into it, so a proposed name must be short and
# must not contain the delimiter. Proposed names are unsanitized model
# output -- a label containing ':' would silently mis-parse on the button
# press and the admin would be told "already decided" about a category
# that was never touched.
MAX_CATEGORY_NAME_LENGTH = 32


def normalize_category_name(name: str) -> str:
    """Makes a model-proposed label safe to round-trip through a Telegram
    callback. Normalized at the point it is RECORDED rather than parsed
    defensively later: one place to get right, and the table then only ever
    holds names the rest of the system can handle."""
    cleaned = " ".join(name.replace(":", " ").split())
    return cleaned[:MAX_CATEGORY_NAME_LENGTH].strip()


def record_category_sighting(name: str, seen_at: datetime, link: str | None = None,
                             title: str | None = None) -> None:
    """Logs that the classifier reached for `name`, which isn't active, and
    creates the proposed row if this is the first time.

    INSERT OR IGNORE on the category, not an upsert: if the row already
    exists as rejected, retired or merged, that decision stands. A sighting
    is evidence, and evidence does not resurrect a decision someone
    already made."""
    name = normalize_category_name(name)
    if not name:
        return
    ts = seen_at.isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories "
            "(name, status, created_at, created_by, sort_order) "
            "VALUES (?, 'proposed', ?, 'model', "
            "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories))",
            (name, ts),
        )
        conn.execute(
            "INSERT INTO category_sightings (name, seen_at, article_link, article_title) "
            "VALUES (?, ?, ?, ?)",
            (name, ts, link, title),
        )


def count_recent_sightings(now: datetime, days: int = CATEGORY_SIGHTING_RETENTION_DAYS
                           ) -> dict[str, int]:
    """{proposed category name: sightings inside the window}. Only
    'proposed' rows are counted -- a rejected category keeps accumulating
    sightings (they answer "was rejecting this right?" later) but must
    never trigger the admin prompt again."""
    cutoff = (now - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.name, COUNT(*) FROM category_sightings s "
            "JOIN categories c ON c.name = s.name "
            "WHERE c.status = 'proposed' AND s.seen_at >= ? "
            "GROUP BY s.name",
            (cutoff,),
        ).fetchall()
    return {name: count for name, count in rows}


def prune_category_sightings(now: datetime,
                             days: int = CATEGORY_SIGHTING_RETENTION_DAYS) -> int:
    """Drops sightings outside the window. Returns how many. Keeps the
    threshold question answerable as "how often recently" rather than
    "how often ever"."""
    cutoff = (now - timedelta(days=days)).isoformat()
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM category_sightings WHERE seen_at < ?", (cutoff,))
        return cursor.rowcount


def external_id(chat_id: int) -> str:
    """A stable, opaque id for this subscriber, minted once and stored.

    `chat_id` is a real Telegram account identifier. Our own database and
    our own logs may hold it -- they never leave the VM -- but telemetry
    does, and a trace shared with anyone (or held by a provider) should not
    carry it. This is hygiene rather than a privacy control: the data here
    is not sensitive today, and the point is that keeping the mapping on
    our side costs nothing while un-leaking an identifier later is
    impossible.

    Stored rather than derived so it survives a change of hashing scheme,
    and so the mapping is a row someone can look at when a trace needs to
    be tied back to a person during an incident.

    Falls back to a deterministic value for a chat_id with no subscriber
    row -- callers should not have to care, and a span attribute is never
    worth raising over."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT external_id FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        new_id = "sub_" + secrets.token_hex(6)
        cursor = conn.execute(
            "UPDATE subscribers SET external_id = ? WHERE chat_id = ? AND external_id IS NULL",
            (new_id, chat_id),
        )
        if cursor.rowcount:
            return new_id
    # No row to attach it to (a chat that was never a subscriber). Stable
    # for the lifetime of the database, and not reversible without it.
    return "anon_" + hashlib.sha256(f"{DB_FILE}:{chat_id}".encode()).hexdigest()[:12]


def record_push_outcome(chat_id: int, outcome: str, recorded_at: datetime,
                        detail: str | None = None) -> None:
    """Records what happened to one subscriber in one push cycle.

    Call this from news_push._record rather than directly: the point of
    that helper is that the human log line and this row are written
    together, so the two can't disagree about what happened.

    `detail` is free text for a human reading the row back (an exception
    repr, "3 of 8 candidates"). Nothing queries it -- anything an alarm
    needs to test belongs in `outcome`, which is a closed set."""
    ts = recorded_at if recorded_at.tzinfo else recorded_at.replace(tzinfo=timezone.utc)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO push_outcomes (chat_id, outcome, recorded_at, detail) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, outcome, ts.isoformat(), detail),
        )


def push_outcome_counts(since: datetime) -> dict[str, int]:
    """{outcome: count} across all subscribers since `since`. The shape
    /status renders and the ratio alarm reads."""
    cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) FROM push_outcomes WHERE recorded_at >= ? "
            "GROUP BY outcome",
            (cutoff.isoformat(),),
        ).fetchall()
    return {outcome: count for outcome, count in rows}


def push_delivery_ratio(since: datetime) -> tuple[int, int]:
    """(delivered, generated) since `since` -- criterion 3's numerator and
    denominator, derived from the same counts so they cannot drift apart.

    Returns (0, 0) when nothing was generated, which callers must treat as
    "no opinion" rather than as a 0% delivery rate: a window in which every
    subscriber was idle is not an outage."""
    counts = push_outcome_counts(since)
    generated = sum(n for outcome, n in counts.items()
                    if outcome in PUSH_GENERATED_OUTCOMES)
    return counts.get(PUSH_DELIVERED, 0), generated


def recent_outcomes_for(chat_id: int, limit: int = 20) -> list[str]:
    """This subscriber's most recent outcomes, newest first.

    Raw and unfiltered on purpose -- see consecutive_chat_not_found for
    the one policy currently layered on top of it."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT outcome FROM push_outcomes WHERE chat_id = ? "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [row[0] for row in rows]


def consecutive_chat_not_found(chat_id: int, limit: int = 50) -> int:
    """How many `chat_not_found` outcomes have piled up since the last
    proof this chat was reachable.

    Only `delivered` breaks the streak, and only `chat_not_found` extends
    it; every other outcome is skipped rather than treated as either. That
    is not a shortcut -- it follows from what each outcome is evidence OF.
    A `nothing_new` cycle attempts no send at all, so it says nothing about
    whether the chat still exists; letting it reset the count would leave a
    dead chat billing digests indefinitely, which is the exact failure this
    is here to stop. A successful delivery is the only positive proof, so
    it is the only thing that clears the record.

    `limit` bounds the scan rather than the answer: a streak longer than
    this cannot be distinguished from one exactly this long, which is
    harmless because every threshold is far smaller."""
    streak = 0
    for outcome in recent_outcomes_for(chat_id, limit):
        if outcome == PUSH_DELIVERED:
            break
        if outcome == PUSH_CHAT_NOT_FOUND:
            streak += 1
    return streak


def prune_push_outcomes(now: datetime,
                        days: int = PUSH_OUTCOME_RETENTION_DAYS) -> int:
    """Drops outcomes outside the window. Returns how many."""
    cutoff = (now - timedelta(days=days)).isoformat()
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM push_outcomes WHERE recorded_at < ?", (cutoff,))
        return cursor.rowcount


def categories_ready_for_review(now: datetime,
                               threshold: int = CATEGORY_PROPOSAL_THRESHOLD,
                               days: int = CATEGORY_SIGHTING_RETENTION_DAYS
                               ) -> list[tuple[str, int]]:
    """[(name, hits)] for proposals that have crossed the threshold inside
    the window and have NOT been raised with an admin yet.

    `alerted_at IS NULL` is what stops this nagging: an admin who has been
    asked and hasn't answered is not asked again every ingestion cycle. The
    proposal stays in the table and a future /proposals command can list
    it; the alert is a one-time push, not a reminder loop."""
    cutoff = (now - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.name, COUNT(s.id) AS hits FROM categories c "
            "JOIN category_sightings s ON s.name = c.name "
            "WHERE c.status = 'proposed' AND c.alerted_at IS NULL AND s.seen_at >= ? "
            "GROUP BY c.name HAVING hits >= ? ORDER BY hits DESC",
            (cutoff, threshold),
        ).fetchall()
    return [(name, hits) for name, hits in rows]


def category_examples(name: str, limit: int = 3) -> list[tuple[str, str]]:
    """[(title, link)] of articles that triggered this proposal. The admin
    is being asked to write a description that goes into the classifier
    prompt for every article afterwards, so they need to see what the label
    was actually reaching for -- a bare name is not enough to decide on."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT article_title, article_link FROM category_sightings "
            "WHERE name = ? AND article_title IS NOT NULL "
            # id breaks the tie: sightings from one ingestion cycle all
            # share a timestamp, so seen_at alone leaves which examples the
            # admin sees unspecified and irreproducible between runs.
            "ORDER BY seen_at DESC, id DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    return [(title, link or "") for title, link in rows]


def mark_category_alerted(name: str, now: datetime, description: str | None = None) -> None:
    """Records that the admin was asked, and stores the drafted description
    alongside. The draft lives in the row rather than being carried through
    Telegram's callback_data (64 bytes, far too small) or re-derived on the
    button press (which would need a model in the admin bot). The admin sees
    the exact text in the message and it is the exact text that ships."""
    with _connect() as conn:
        if description is None:
            conn.execute("UPDATE categories SET alerted_at = ? WHERE name = ?",
                         (now.isoformat(), name))
        else:
            conn.execute("UPDATE categories SET alerted_at = ?, description = ? WHERE name = ?",
                         (now.isoformat(), description, name))


def activate_category(name: str, by: str, now: datetime,
                      description: str | None = None) -> bool:
    """Promotes a proposal to a real category. Returns False if it wasn't
    'proposed' any more -- two admins, or a double-tapped button.

    Also clears interest_categories. Those rows map a subscriber's interest
    text to category names, and get_cached_interest_categories treats any
    existing row as a hit, so a newly active category is invisible to every
    already-mapped interest until they are recomputed. The next push cycle
    re-resolves them.

    Deleting rather than re-mapping in place is the pre-A5 shape: A5 makes
    these mappings persisted derived state recomputed at write time, at
    which point this becomes an explicit migration instead of an
    invalidation. The distinction matters for where failures surface, not
    for what ends up in the table."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE categories SET status = 'active', "
            "description = COALESCE(?, description), decided_at = ?, "
            "decided_by = ?, sort_order = (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories) "
            "WHERE name = ? AND status = 'proposed'",
            (description, now.isoformat(), by, name),
        )
        if not cursor.rowcount:
            return False
        conn.execute("DELETE FROM interest_categories")
    return True


def reject_category(name: str, by: str, now: datetime) -> bool:
    """Sightings keep accumulating afterwards -- they cost a row each and
    answer "was rejecting this right?" later -- but count_recent_sightings
    only counts 'proposed', so it never alerts again."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE categories SET status = 'rejected', decided_at = ?, decided_by = ? "
            "WHERE name = ? AND status = 'proposed'",
            (now.isoformat(), by, name),
        )
        return bool(cursor.rowcount)


def merge_category(name: str, into: str, by: str, now: datetime) -> bool:
    """Points `name` at `into` and rewrites any interest that referenced it.

    Interests are REWRITTEN, not invalidated: the new mapping is known, so
    there is nothing to re-derive and no reason to pay for a model call.
    Article files are left alone -- resolve_category_name follows the
    tombstone on read, which beats rewriting the whole cache for a rename.

    Refuses to merge into anything that isn't active, which would otherwise
    create a chain or a cycle whose only symptom is articles quietly
    resolving to nothing."""
    with _connect() as conn:
        target = conn.execute(
            "SELECT status FROM categories WHERE name = ?", (into,)
        ).fetchone()
        if not target or target[0] != "active":
            return False
        cursor = conn.execute(
            "UPDATE categories SET status = 'merged', merged_into = ?, decided_at = ?, "
            "decided_by = ? WHERE name = ? AND status IN ('proposed', 'active')",
            (into, now.isoformat(), by, name),
        )
        if not cursor.rowcount:
            return False
        for interest, raw in conn.execute(
            "SELECT interest, categories FROM interest_categories"
        ).fetchall():
            cats = json.loads(raw)
            if name not in cats:
                continue
            rewritten = list(dict.fromkeys(into if c == name else c for c in cats))
            conn.execute("UPDATE interest_categories SET categories = ? WHERE interest = ?",
                         (json.dumps(rewritten), interest))
    return True


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


def get_interest_query_expansion(interest: str) -> str | None:
    """None means never generated (agent.py's _add_one_interest calls
    news_classify.expand_interest_for_retrieval and caches it via
    set_interest_query_expansion below on a cache miss) -- callers
    (news_push.py's _resolve_query_text) fall back to the bare interest
    string in that case, same fail-open shape as everywhere else this
    session's embedding work touches the pipeline."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT expansion FROM interest_query_expansions WHERE interest = ?", (interest,)
        ).fetchone()
    return row[0] if row else None


def set_interest_query_expansion(interest: str, expansion: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO interest_query_expansions (interest, expansion) VALUES (?, ?)
            ON CONFLICT(interest) DO UPDATE SET expansion = excluded.expansion
            """,
            (interest, expansion),
        )


def set_category_keyness(category: str, scores: dict[str, float]) -> None:
    """Replaces `category`'s ENTIRE row set atomically -- delete then
    insert in one transaction, not an upsert -- because news_keyness.py
    recomputes this fresh from the current cache every news_ingest.py
    cycle. A term that no longer scores (dropped below the corpus-wide
    minimum document-frequency floor, or the category itself shrank) must
    not linger as a stale row an upsert-only write would leave behind."""
    with _connect() as conn:
        conn.execute("DELETE FROM category_keyness WHERE category = ?", (category,))
        conn.executemany(
            "INSERT INTO category_keyness (category, term, score) VALUES (?, ?, ?)",
            [(category, term, score) for term, score in scores.items()],
        )


def get_category_keyness(category: str) -> dict[str, float]:
    """{} when nothing has been computed for this category yet (a fresh
    category, or news_ingest.py hasn't completed a cycle since it was
    added) -- news_push.py's offbeat scoring already treats "no keyness
    signal" as a normal, expected case (falls back to the keyword rule
    alone, or to pure recency if that's empty too), same fail-open shape
    as every other cache in this module."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT term, score FROM category_keyness WHERE category = ?", (category,)
        ).fetchall()
    return {term: score for term, score in rows}


def get_source_last_pulled_at(source: str) -> datetime | None:
    """Drives news_ingest.py's per-source due-check, same shape as
    get_last_push_at/record_push for subscribers -- a source's own pull
    frequency (docs/plans/local-news-cache-plan.md) is independent of any one
    subscriber's push schedule."""
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


def mark_test_account(chat_id: int) -> None:
    """Flags a subscriber as created by test_api.py, so push cycles skip it.

    Smoke tests drive the real pipeline through test_api, which means they
    create real subscriber rows -- including turning push on, since
    verifying that the router extracts "every 6 hours" requires actually
    performing the setting. Those rows then received digests forever.

    By 2026-08-21 that had produced 54 abandoned accounts, 19 of them
    push-enabled, against 5 real subscribers. Each one cost a digest
    generation and a guardrail call every 6 hours, delivered to a Telegram
    user that does not exist -- the generation is billed, only the send
    fails. It exhausted the DeepSeek balance and took real subscribers'
    pushes down with it.

    Excluding them structurally beats asking the test to clean up after
    itself: a cleanup step that is skipped when a test fails early is
    exactly when the mess gets made.

    Upserts, because the row may not exist yet -- test_api marks the id
    before the pipeline has had a chance to create it."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (chat_id, status, requested_at, is_test)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET is_test = 1
            """,
            (chat_id, APPROVED, datetime.now().isoformat()),
        )


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
    resulting full list.

    Raises ValueError when the subscriber is already at MAX_INTERESTS,
    rather than silently dropping the addition -- same convention as
    set_push_interval_hours, and dispatch_settings turns it into a
    sentence the subscriber actually reads. Re-adding something already
    present is never refused, since that changes nothing."""
    interests = get_interests(chat_id)
    if any(_is_duplicate_topic(t, topic) for t in interests):
        return interests
    if len(interests) >= MAX_INTERESTS:
        raise ValueError(
            f"you already follow {len(interests)} interests, which is the "
            f"maximum of {MAX_INTERESTS} -- remove one first")
    interests.append(topic)
    set_interests(chat_id, interests)
    return interests


def interests_by_staleness(chat_id: int, interests: list[str]) -> list[str]:
    """`interests`, reordered longest-un-pushed first. Never-pushed topics
    lead, in their existing order.

    Returns a list rather than filtering, so the caller decides how many
    to serve -- an interest with no new articles must not consume one of
    the cycle's slots, and that is only knowable after candidate
    selection."""
    if not interests:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT topic, last_pushed_at FROM interest_push_state WHERE chat_id = ?",
            (chat_id,)).fetchall()
    seen = {r[0]: r[1] for r in rows}
    # "" sorts before any ISO timestamp, so never-pushed comes first.
    # sorted() is itself stable, and the generator below already yields in
    # `interests` order, so ties keep their original order for free -- no
    # secondary key needed.
    return sorted(interests, key=lambda t: seen.get(t, ""))


def mark_interest_pushed(chat_id: int, topic: str, when: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO interest_push_state (chat_id, topic, last_pushed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, topic) DO UPDATE SET last_pushed_at = excluded.last_pushed_at
            """,
            (chat_id, topic, when.isoformat()))


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


def _parse_pushed_links(raw: str | None, now: datetime | None = None) -> dict[str, str]:
    """Returns {link: iso_timestamp} for links still inside the retention
    window. Accepts the pre-2026-08-19 format too -- a bare JSON list of
    links with no timestamps -- since live subscriber rows are still in it.
    Those legacy links are treated as sent `now`, i.e. given a full fresh
    retention window rather than being dropped: re-sending an article the
    subscriber already saw is a worse failure than remembering it slightly
    too long."""
    if not raw:
        return {}
    now = now or datetime.now(timezone.utc)
    data = json.loads(raw)
    if isinstance(data, list):  # legacy format
        return {link: now.isoformat() for link in data}
    cutoff = now - timedelta(hours=PUSHED_LINK_RETENTION_HOURS)
    kept = {}
    for link, sent_at in data.items():
        try:
            if datetime.fromisoformat(sent_at) > cutoff:
                kept[link] = sent_at
        except (TypeError, ValueError):
            kept[link] = now.isoformat()  # unparseable -- keep, don't risk a resend
    return kept


def get_pushed_links(chat_id: int, now: datetime | None = None) -> list[str]:
    """Links sent to this subscriber inside the retention window. `now` is a
    parameter so the pruning is deterministic in tests, same convention as
    news_push.run_push_cycle."""
    with _connect() as conn:
        row = conn.execute("SELECT pushed_links FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    return list(_parse_pushed_links(row[0] if row else None, now))


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
    pushed_links records what this subscriber has actually been sent.

    `article_links` must be the links that genuinely appeared in the
    delivered digest, NOT the candidate list -- a candidate the
    digest-writing model judged irrelevant and left out was never seen by
    the subscriber, so marking it as sent would silently retire an article
    nobody read. news_push extracts these from the digest's own <a href>
    tags for that reason.

    Stored as {link: sent_at} and pruned by age, not truncated by count --
    see PUSHED_LINK_RETENTION_HOURS."""
    now = pushed_at if pushed_at.tzinfo else pushed_at.replace(tzinfo=timezone.utc)
    with _connect() as conn:
        row = conn.execute("SELECT pushed_links FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
    merged = _parse_pushed_links(row[0] if row else None, now)
    for link in article_links:
        merged[link] = now.isoformat()
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
    against -- avoids the scheduler doing its own row-by-row SQL.

    Excludes accounts flagged by test_api -- see mark_test_account for why
    that is done structurally here rather than as cleanup in the tests."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, interests, push_interval_hours, last_push_at, pushed_links, language,
                   restricted_sources_enabled
            FROM subscribers
            WHERE status = ? AND push_enabled = 1
              AND (is_test IS NULL OR is_test = 0)
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
                "pushed_links": list(_parse_pushed_links(pushed_links_json)),
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
