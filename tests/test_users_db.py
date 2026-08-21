import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import users_db


def test_get_status_unknown_chat_returns_none(isolated_subscribers_db):
    assert users_db.get_status(12345) is None


def test_request_access_sets_pending(isolated_subscribers_db):
    users_db.request_access(1, "alice", "Alice")
    assert users_db.get_status(1) == users_db.PENDING


def test_request_access_does_not_reset_a_decision(isolated_subscribers_db):
    users_db.request_access(1, "alice", "Alice")
    users_db.decide(1, approved=True)
    users_db.request_access(1, "alice", "Alice")  # re-message after decision
    assert users_db.get_status(1) == users_db.APPROVED


def test_decide_approve(isolated_subscribers_db):
    users_db.request_access(2, "bob", "Bob")
    users_db.decide(2, approved=True)
    assert users_db.get_status(2) == users_db.APPROVED


def test_decide_deny(isolated_subscribers_db):
    users_db.request_access(3, "carol", "Carol")
    users_db.decide(3, approved=False)
    assert users_db.get_status(3) == users_db.DENIED


def test_list_pending_only_returns_pending(isolated_subscribers_db):
    users_db.request_access(4, "dave", "Dave")
    users_db.request_access(5, "erin", "Erin")
    users_db.decide(5, approved=True)
    pending = users_db.list_pending()
    assert [row[0] for row in pending] == [4]


def test_get_interests_empty_for_unset_chat(isolated_subscribers_db):
    assert users_db.get_interests(6) == []


def test_set_and_get_interests(isolated_subscribers_db):
    users_db.request_access(7, "frank", "Frank")
    users_db.set_interests(7, ["AI", "robotics"])
    assert users_db.get_interests(7) == ["AI", "robotics"]


def test_set_interests_upserts_when_no_existing_row(isolated_subscribers_db):
    # e.g. the admin, who never goes through request_access()
    users_db.set_interests(999, ["semiconductors"])
    assert users_db.get_interests(999) == ["semiconductors"]
    assert users_db.get_status(999) == users_db.APPROVED


def test_set_interests_overwrites_previous_value(isolated_subscribers_db):
    users_db.request_access(8, "grace", "Grace")
    users_db.set_interests(8, ["AI"])
    users_db.set_interests(8, ["quantum computing"])
    assert users_db.get_interests(8) == ["quantum computing"]


def test_init_db_migrates_schema_missing_interests_column(isolated_subscribers_db):
    # Simulate a DB created before the interests column existed.
    with sqlite3.connect(users_db.DB_FILE) as conn:
        conn.execute("DROP TABLE subscribers")
        conn.execute(
            """
            CREATE TABLE subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
    users_db.init_db()  # should migrate without error
    users_db.request_access(9, "henry", "Henry")
    users_db.set_interests(9, ["AI"])
    assert users_db.get_interests(9) == ["AI"]


def test_init_db_migrates_schema_missing_last_article_dt_column(isolated_subscribers_db):
    # Simulate a source_pull_state table from before last_article_dt
    # existed (pre-2026-08-16).
    with sqlite3.connect(users_db.DB_FILE) as conn:
        conn.execute("DROP TABLE IF EXISTS source_pull_state")
        conn.execute("CREATE TABLE source_pull_state (source TEXT PRIMARY KEY, last_pulled_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO source_pull_state (source, last_pulled_at) VALUES ('perigon', '2026-08-14T09:00:00+00:00')"
        )
    users_db.init_db()  # should migrate without error, preserving the existing row

    assert users_db.get_source_last_pulled_at("perigon") == datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    assert users_db.get_source_last_article_dt("perigon") is None
    users_db.set_source_last_article_dt("perigon", datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc))
    assert users_db.get_source_last_article_dt("perigon") == datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)


def test_add_interest_appends(isolated_subscribers_db):
    users_db.request_access(10, "iris", "Iris")
    users_db.set_interests(10, ["AI"])
    result = users_db.add_interest(10, "robotics")
    assert result == ["AI", "robotics"]
    assert users_db.get_interests(10) == ["AI", "robotics"]


def test_add_interest_is_idempotent_case_insensitive(isolated_subscribers_db):
    users_db.request_access(11, "jack", "Jack")
    users_db.set_interests(11, ["AI"])
    result = users_db.add_interest(11, "ai")  # different case, same topic
    assert result == ["AI"]  # not duplicated


def test_add_interest_catches_near_duplicate_llm_phrasing(isolated_subscribers_db):
    # Real incident, 2026-08-08: the agent phrased the same conceptual
    # interest two different ways on two calls; exact-match dedup missed
    # it, leaving both in the list.
    users_db.request_access(23, "ruth", "Ruth")
    users_db.set_interests(23, ["Edge AI development boards (Raspberry Pi, NVIDIA Jetson, etc.)"])
    result = users_db.add_interest(23, "Edge AI development boards (Raspberry Pi, NVIDIA Jetson)")
    assert result == ["Edge AI development boards (Raspberry Pi, NVIDIA Jetson, etc.)"]


def test_add_interest_does_not_dedupe_unrelated_topics_sharing_one_word(isolated_subscribers_db):
    users_db.request_access(24, "sam", "Sam")
    users_db.set_interests(24, ["AI"])
    result = users_db.add_interest(24, "AI regulation")
    assert result == ["AI", "AI regulation"]


def test_add_interest_creates_row_when_none_exists(isolated_subscribers_db):
    result = users_db.add_interest(999, "semiconductors")
    assert result == ["semiconductors"]
    assert users_db.get_status(999) == users_db.APPROVED


def test_remove_interest(isolated_subscribers_db):
    users_db.request_access(12, "kate", "Kate")
    users_db.set_interests(12, ["AI", "robotics"])
    result = users_db.remove_interest(12, "AI")
    assert result == ["robotics"]
    assert users_db.get_interests(12) == ["robotics"]


def test_remove_interest_not_present_is_a_noop(isolated_subscribers_db):
    users_db.request_access(13, "liam", "Liam")
    users_db.set_interests(13, ["AI"])
    result = users_db.remove_interest(13, "robotics")
    assert result == ["AI"]


def test_get_push_enabled_defaults_false(isolated_subscribers_db):
    assert users_db.get_push_enabled(14) is False


def test_set_and_get_push_enabled(isolated_subscribers_db):
    users_db.request_access(15, "mia", "Mia")
    users_db.set_push_enabled(15, True)
    assert users_db.get_push_enabled(15) is True
    users_db.set_push_enabled(15, False)
    assert users_db.get_push_enabled(15) is False


def test_set_push_enabled_upserts_when_no_existing_row(isolated_subscribers_db):
    users_db.set_push_enabled(999, True)
    assert users_db.get_push_enabled(999) is True
    assert users_db.get_status(999) == users_db.APPROVED


def test_get_push_interval_hours_defaults(isolated_subscribers_db):
    assert users_db.get_push_interval_hours(16) == users_db.DEFAULT_PUSH_INTERVAL_HOURS


def test_set_and_get_push_interval_hours(isolated_subscribers_db):
    users_db.request_access(16, "noah", "Noah")
    users_db.set_push_interval_hours(16, 6)
    assert users_db.get_push_interval_hours(16) == 6


def test_set_push_interval_hours_rejects_below_minimum(isolated_subscribers_db):
    with pytest.raises(ValueError):
        users_db.set_push_interval_hours(16, 0)


def test_get_pushed_links_empty_for_unset_chat(isolated_subscribers_db):
    assert users_db.get_pushed_links(17) == []


def test_get_last_push_at_none_for_unset_chat(isolated_subscribers_db):
    assert users_db.get_last_push_at(17) is None


def test_record_push_sets_last_push_at_and_links(isolated_subscribers_db):
    pushed_at = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    users_db.record_push(18, ["https://example.com/a", "https://example.com/b"], pushed_at)
    assert users_db.get_last_push_at(18) == pushed_at
    assert set(users_db.get_pushed_links(18, now=pushed_at)) == {
        "https://example.com/a", "https://example.com/b"
    }


def test_record_push_merges_and_dedupes_links(isolated_subscribers_db):
    t1 = datetime(2026, 8, 8, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    users_db.record_push(19, ["https://example.com/old"], t1)
    users_db.record_push(19, ["https://example.com/new", "https://example.com/old"], t2)
    assert set(users_db.get_pushed_links(19, now=t2)) == {
        "https://example.com/old", "https://example.com/new"
    }


def test_pushed_links_are_pruned_by_age_not_by_count(isolated_subscribers_db):
    """The count cap this replaced was silently wrong at short push
    intervals -- see users_db.PUSHED_LINK_RETENTION_HOURS."""
    recent = datetime(2026, 8, 8, tzinfo=timezone.utc)
    # sent a day ago -- comfortably inside the retention window
    users_db.record_push(30, ["https://example.com/yesterday"], recent - timedelta(hours=24))

    # far more links than the old 200-entry cap, all in one later push
    users_db.record_push(30, [f"https://example.com/{i}" for i in range(500)], recent)

    links = users_db.get_pushed_links(30, now=recent)
    assert len(links) == 501, "nothing should be evicted just for being numerous"
    assert "https://example.com/yesterday" in links, (
        "under the old count cap this would have been evicted by the 500 newer links, "
        "and the article resent"
    )

    # ...but once it falls outside the retention window it goes
    much_later = recent + timedelta(hours=users_db.PUSHED_LINK_RETENTION_HOURS + 1)
    assert users_db.get_pushed_links(30, now=much_later) == []


def test_pushed_links_reads_the_legacy_plain_list_format(isolated_subscribers_db):
    """Live subscriber rows predate the {link: sent_at} format. They must
    still dedupe rather than being dropped, which would resend articles the
    subscriber has already seen."""
    import json as _json
    import sqlite3 as _sqlite3

    users_db.request_access(31, "legacy", "Legacy")
    conn = _sqlite3.connect(users_db.DB_FILE)
    conn.execute("UPDATE subscribers SET pushed_links = ? WHERE chat_id = 31",
                 (_json.dumps(["https://example.com/seen-before"]),))
    conn.commit()
    conn.close()

    assert users_db.get_pushed_links(31) == ["https://example.com/seen-before"]


def test_list_push_enabled_subscribers_only_returns_approved_and_enabled(isolated_subscribers_db):
    users_db.request_access(20, "olivia", "Olivia")
    users_db.decide(20, approved=True)
    users_db.set_interests(20, ["AI"])
    users_db.set_push_enabled(20, True)

    users_db.request_access(21, "peter", "Peter")
    users_db.decide(21, approved=True)
    users_db.set_push_enabled(21, False)  # opted out -- shouldn't show up

    users_db.request_access(22, "quinn", "Quinn")  # still pending -- shouldn't show up
    users_db.set_push_enabled(22, True)

    subscribers = users_db.list_push_enabled_subscribers()
    assert [s["chat_id"] for s in subscribers] == [20]
    assert subscribers[0]["interests"] == ["AI"]
    assert subscribers[0]["push_interval_hours"] == users_db.DEFAULT_PUSH_INTERVAL_HOURS
    assert subscribers[0]["last_push_at"] is None
    assert subscribers[0]["pushed_links"] == []
    assert subscribers[0]["language"] is None


def test_list_push_enabled_subscribers_includes_restricted_sources_flag(isolated_subscribers_db):
    users_db.request_access(23, "rex", "Rex")
    users_db.decide(23, approved=True)
    users_db.set_push_enabled(23, True)
    users_db.set_restricted_sources_enabled(23, True)

    users_db.request_access(24, "sam", "Sam")
    users_db.decide(24, approved=True)
    users_db.set_push_enabled(24, True)  # restricted flag left at its default (False)

    subscribers = {s["chat_id"]: s for s in users_db.list_push_enabled_subscribers()}
    assert subscribers[23]["restricted_sources_enabled"] is True
    assert subscribers[24]["restricted_sources_enabled"] is False


def test_get_language_none_for_unset_chat(isolated_subscribers_db):
    assert users_db.get_language(25) is None


def test_set_and_get_language(isolated_subscribers_db):
    users_db.request_access(25, "quinn2", "Quinn2")
    users_db.set_language(25, "Spanish")
    assert users_db.get_language(25) == "Spanish"


def test_set_language_upserts_when_no_existing_row(isolated_subscribers_db):
    users_db.set_language(999, "Chinese")
    assert users_db.get_language(999) == "Chinese"
    assert users_db.get_status(999) == users_db.APPROVED


def test_set_language_none_clears_it(isolated_subscribers_db):
    users_db.request_access(26, "rita", "Rita")
    users_db.set_language(26, "Japanese")
    users_db.set_language(26, None)
    assert users_db.get_language(26) is None


def test_list_push_enabled_subscribers_includes_language(isolated_subscribers_db):
    users_db.request_access(27, "sam2", "Sam2")
    users_db.decide(27, approved=True)
    users_db.set_push_enabled(27, True)
    users_db.set_language(27, "French")

    subscribers = users_db.list_push_enabled_subscribers()
    assert subscribers[0]["language"] == "French"


def test_try_consume_api_budget_allows_up_to_the_cap(isolated_subscribers_db):
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is True
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is True
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is True


def test_try_consume_api_budget_denies_once_cap_reached(isolated_subscribers_db):
    for _ in range(3):
        users_db.try_consume_api_budget("perigon", 3, "2026-08-14")
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is False


def test_try_consume_api_budget_resets_on_a_new_day(isolated_subscribers_db):
    for _ in range(3):
        users_db.try_consume_api_budget("perigon", 3, "2026-08-14")
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is False
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-15") is True


def test_try_consume_api_budget_tracks_sources_independently(isolated_subscribers_db):
    for _ in range(1):
        users_db.try_consume_api_budget("newsapi", 1, "2026-08-14")
    assert users_db.try_consume_api_budget("newsapi", 1, "2026-08-14") is False
    # perigon's budget is untouched by newsapi's being exhausted
    assert users_db.try_consume_api_budget("perigon", 3, "2026-08-14") is True


def test_try_consume_api_budget_preserves_history_across_days(isolated_subscribers_db):
    # Real gap the (source, date) migration fixed: the old schema
    # overwrote a source's single row on every new day, losing yesterday's
    # count entirely.
    for _ in range(3):
        users_db.try_consume_api_budget("perigon", 3, "2026-08-14")
    users_db.try_consume_api_budget("perigon", 3, "2026-08-15")

    history = users_db.get_api_budget_history("perigon")

    assert {"date": "2026-08-14", "count": 3} in history
    assert {"date": "2026-08-15", "count": 1} in history


def test_get_total_api_calls_sums_across_all_recorded_days(isolated_subscribers_db):
    for _ in range(3):
        users_db.try_consume_api_budget("perigon", 3, "2026-08-14")
    users_db.try_consume_api_budget("perigon", 3, "2026-08-15")

    assert users_db.get_total_api_calls("perigon") == 4


def test_get_total_api_calls_zero_for_unknown_source(isolated_subscribers_db):
    assert users_db.get_total_api_calls("perigon") == 0


def test_record_api_call_does_not_enforce_a_cap(isolated_subscribers_db):
    # Unlike try_consume_api_budget, record_api_call never returns False --
    # it's for visibility (agent.py's search_news), not gating.
    for _ in range(5):
        users_db.record_api_call("perigon", "2026-08-14")

    assert users_db.get_api_budget_history("perigon") == [{"date": "2026-08-14", "count": 5}]


def test_record_api_call_and_try_consume_api_budget_share_the_same_count(isolated_subscribers_db):
    users_db.try_consume_api_budget("perigon", 10, "2026-08-14")
    users_db.record_api_call("perigon", "2026-08-14")

    assert users_db.get_total_api_calls("perigon") == 2


def test_api_budget_migration_preserves_existing_rows_from_the_old_schema(isolated_subscribers_db):
    import sqlite3

    # Simulate a database still on the pre-2026-08-16 schema (one row per
    # source, no date in the primary key) to confirm init_db's migration
    # carries the existing row forward instead of dropping it.
    conn = sqlite3.connect(users_db.DB_FILE)
    conn.execute("DROP TABLE IF EXISTS api_budget")
    conn.execute("CREATE TABLE api_budget (source TEXT PRIMARY KEY, date TEXT NOT NULL, count INTEGER NOT NULL)")
    conn.execute("INSERT INTO api_budget (source, date, count) VALUES ('perigon', '2026-08-10', 2)")
    conn.commit()
    conn.close()

    users_db.init_db()

    assert users_db.get_api_budget_history("perigon") == [{"date": "2026-08-10", "count": 2}]
    # the new schema allows a second day's row to coexist
    users_db.try_consume_api_budget("perigon", 3, "2026-08-11")
    assert len(users_db.get_api_budget_history("perigon")) == 2


def test_get_source_last_pulled_at_unknown_source_returns_none(isolated_subscribers_db):
    assert users_db.get_source_last_pulled_at("perigon") is None


def test_set_and_get_source_last_pulled_at(isolated_subscribers_db):
    when = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at("perigon", when)
    assert users_db.get_source_last_pulled_at("perigon") == when


def test_set_source_last_pulled_at_upserts(isolated_subscribers_db):
    t1 = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 14, 13, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at("perigon", t1)
    users_db.set_source_last_pulled_at("perigon", t2)
    assert users_db.get_source_last_pulled_at("perigon") == t2


def test_get_source_last_article_dt_unknown_source_returns_none(isolated_subscribers_db):
    assert users_db.get_source_last_article_dt("perigon") is None


def test_set_and_get_source_last_article_dt(isolated_subscribers_db):
    when = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_article_dt("perigon", when)
    assert users_db.get_source_last_article_dt("perigon") == when


def test_set_source_last_article_dt_upserts(isolated_subscribers_db):
    t1 = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 14, 13, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_article_dt("perigon", t1)
    users_db.set_source_last_article_dt("perigon", t2)
    assert users_db.get_source_last_article_dt("perigon") == t2


def test_source_last_pulled_at_and_last_article_dt_are_independent(isolated_subscribers_db):
    # Two different questions ("when did the job last run" vs "what's the
    # newest article we've seen") stored on the same row -- setting one
    # must not disturb the other. See get_source_last_article_dt's
    # docstring for why these are deliberately different values.
    pulled_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    article_dt = datetime(2026, 8, 14, 8, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at("perigon", pulled_at)
    users_db.set_source_last_article_dt("perigon", article_dt)
    assert users_db.get_source_last_pulled_at("perigon") == pulled_at
    assert users_db.get_source_last_article_dt("perigon") == article_dt

    new_pulled_at = pulled_at + timedelta(hours=8)
    users_db.set_source_last_pulled_at("perigon", new_pulled_at)
    assert users_db.get_source_last_pulled_at("perigon") == new_pulled_at
    assert users_db.get_source_last_article_dt("perigon") == article_dt  # unchanged


def test_list_all_interests_empty_when_nobody_has_any(isolated_subscribers_db):
    assert users_db.list_all_interests() == []


def test_list_all_interests_deduplicates_across_subscribers(isolated_subscribers_db):
    users_db.set_interests(1, ["bitcoin", "AI"])
    users_db.set_interests(2, ["AI", "robotics"])
    assert users_db.list_all_interests() == ["bitcoin", "AI", "robotics"]


def test_get_cached_interest_categories_empty_when_nothing_cached(isolated_subscribers_db):
    assert users_db.get_cached_interest_categories(["AI"]) == {}


def test_get_cached_interest_categories_empty_input_returns_empty(isolated_subscribers_db):
    assert users_db.get_cached_interest_categories([]) == {}


def test_set_and_get_cached_interest_categories(isolated_subscribers_db):
    users_db.set_interest_categories("AI", ["AI", "Research"])
    assert users_db.get_cached_interest_categories(["AI"]) == {"AI": ["AI", "Research"]}


def test_get_cached_interest_categories_only_returns_known_interests(isolated_subscribers_db):
    users_db.set_interest_categories("AI", ["AI"])
    result = users_db.get_cached_interest_categories(["AI", "AAOI"])
    assert result == {"AI": ["AI"]}
    assert "AAOI" not in result


def test_set_interest_categories_can_store_empty_list(isolated_subscribers_db):
    # A classifier miss (interest doesn't map to any category) is a real,
    # cacheable result -- distinct from "not yet classified at all".
    users_db.set_interest_categories("some obscure ticker", [])
    assert users_db.get_cached_interest_categories(["some obscure ticker"]) == {"some obscure ticker": []}


def test_set_interest_categories_upserts(isolated_subscribers_db):
    users_db.set_interest_categories("AI", ["AI"])
    users_db.set_interest_categories("AI", ["AI", "Research"])
    assert users_db.get_cached_interest_categories(["AI"]) == {"AI": ["AI", "Research"]}


def test_get_restricted_sources_enabled_defaults_false(isolated_subscribers_db):
    assert users_db.get_restricted_sources_enabled(1) is False


def test_set_restricted_sources_enabled_true(isolated_subscribers_db):
    users_db.set_restricted_sources_enabled(1, True)
    assert users_db.get_restricted_sources_enabled(1) is True
    # unrelated chat_id is unaffected
    assert users_db.get_restricted_sources_enabled(2) is False


def test_set_restricted_sources_enabled_can_be_revoked(isolated_subscribers_db):
    users_db.set_restricted_sources_enabled(1, True)
    users_db.set_restricted_sources_enabled(1, False)
    assert users_db.get_restricted_sources_enabled(1) is False


def test_set_restricted_sources_enabled_upserts_for_unknown_chat(isolated_subscribers_db):
    """No prior row for this chat_id (e.g. the admin, who bypasses
    request_access() entirely -- see check_access())."""
    assert users_db.get_status(42) is None
    users_db.set_restricted_sources_enabled(42, True)
    assert users_db.get_restricted_sources_enabled(42) is True


# --- categories taxonomy (docs/plans/taxonomy-and-admin-plan.md A1) -------


def test_init_seeds_the_taxonomy_as_active(isolated_subscribers_db):
    """Counts the seed rather than hardcoding a number: the taxonomy is data
    now, and a test asserting "13" turns every future category addition into
    a test failure that says nothing useful."""
    active = dict(users_db.get_active_categories())

    # everything seeded except Policy, which _migrate_split_policy retires
    expected = {name for name, _ in users_db.SEED_CATEGORIES} - {"Policy"}
    assert set(active) == expected
    assert active["Stock"].startswith("stock price moves")


def test_seeding_is_idempotent_and_does_not_resurrect_a_retired_category(
    isolated_subscribers_db,
):
    """INSERT OR IGNORE rather than a count check: an admin who retires a
    category must not find it back after the next restart."""
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status = 'retired' WHERE name = 'Crypto'")

    users_db.init_db()

    names = [name for name, _ in users_db.get_active_categories()]
    assert "Crypto" not in names


def test_get_active_categories_excludes_non_active_statuses(isolated_subscribers_db):
    users_db.record_category_sighting("Education", datetime(2026, 8, 20, tzinfo=timezone.utc))

    names = [name for name, _ in users_db.get_active_categories()]
    assert "Education" not in names, "a proposed category is not offered to the classifier"


def test_active_category_order_is_stable(isolated_subscribers_db):
    """The prompt is built from this order, so an unstable one would change
    the prompt string between runs for no reason."""
    assert users_db.get_active_categories() == users_db.get_active_categories()


def test_recording_a_sighting_creates_a_proposed_category(isolated_subscribers_db):
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    users_db.record_category_sighting("Education", now, "https://e.com/a", "A title")

    assert users_db.count_recent_sightings(now) == {"Education": 1}


def test_a_sighting_does_not_resurrect_a_decided_category(isolated_subscribers_db):
    """Evidence must not overturn a decision someone already made. A
    rejected label keeps accumulating sightings -- they answer "was
    rejecting this right?" later -- but never returns to 'proposed'."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now)
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status = 'rejected' WHERE name = 'Education'")

    users_db.record_category_sighting("Education", now)

    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Education'"
        ).fetchone()[0]
    assert status == "rejected"
    assert users_db.count_recent_sightings(now) == {}, "rejected never alerts again"


def test_sightings_outside_the_window_do_not_count(isolated_subscribers_db):
    """The threshold asks "how often recently", not "how often ever" -- a
    counter column could not express that, which is why sightings are a log."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now - timedelta(days=60))
    users_db.record_category_sighting("Education", now - timedelta(days=2))

    assert users_db.count_recent_sightings(now, days=30) == {"Education": 1}


def test_pruning_drops_only_sightings_past_retention(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now - timedelta(days=60))
    users_db.record_category_sighting("Education", now - timedelta(days=2))

    assert users_db.prune_category_sightings(now, days=30) == 1
    assert users_db.count_recent_sightings(now, days=30) == {"Education": 1}


def test_resolve_category_name_follows_a_merge(isolated_subscribers_db):
    """An article cached before a merge still carries the old name; it must
    resolve to the survivor rather than dropping out of every filter."""
    with users_db._connect() as conn:
        conn.execute(
            "UPDATE categories SET status = 'merged', merged_into = 'Finance' "
            "WHERE name = 'Stock'"
        )

    assert users_db.resolve_category_name("Stock") == "Finance"
    assert users_db.resolve_category_name("Finance") == "Finance"
    assert users_db.resolve_category_name("Nonexistent") is None


def test_resolve_category_name_survives_a_merge_cycle(isolated_subscribers_db):
    """A merge chain that loops would otherwise hang the push cycle."""
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status='merged', merged_into='Finance' WHERE name='Stock'")
        conn.execute("UPDATE categories SET status='merged', merged_into='Stock' WHERE name='Finance'")

    assert users_db.resolve_category_name("Stock") is None


# --- Policy split (2026-08-20) --------------------------------------------


def test_policy_is_retired_and_replaced_by_its_four_parts(isolated_subscribers_db):
    """Policy's description was literally "regulation, government, legal,
    antitrust" -- a bundle of four things, measured absorbing 65% of every
    category assignment on a general-news probe."""
    active = {name for name, _ in users_db.get_active_categories()}

    assert "Policy" not in active
    assert {"Regulation", "Government", "Legal", "Antitrust"} <= active


def test_retired_policy_still_resolves(isolated_subscribers_db):
    """Articles cached before the split carry the Policy label and must not
    silently resolve to nothing -- that would drop them out of every filter."""
    assert users_db.resolve_category_name("Policy") == "Policy"


def test_split_does_not_re_retire_a_deliberately_reactivated_policy(
    isolated_subscribers_db,
):
    """The migration is guarded by a marker, not by Policy's own status. An
    admin who reactivates Policy on purpose must not find it retired again
    after the next restart -- the same resurrection problem _seed_categories'
    INSERT OR IGNORE avoids in the other direction."""
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status = 'active' WHERE name = 'Policy'")

    users_db.init_db()

    assert "Policy" in {name for name, _ in users_db.get_active_categories()}


def test_split_is_recorded_as_a_migration_not_an_admin_decision(isolated_subscribers_db):
    with users_db._connect() as conn:
        row = conn.execute(
            "SELECT status, decided_by FROM categories WHERE name = 'Policy'"
        ).fetchone()
    assert row == ("retired", "migration")


def _undo_policy_split(conn) -> None:
    """Rolls a database that already went through init_db() (and therefore
    already ran _migrate_split_policy once, via isolated_subscribers_db) back
    to a pre-migration state: Policy active again, the four new rows gone,
    the migration marker gone. Lets a test simulate "a database that looked
    like production before this migration ever ran" without needing a
    checkout of main's users_db.py -- the table SCHEMA is unchanged by this
    branch, only SEED_CATEGORIES' data and the new _migrate_split_policy
    call, so undoing just those two effects reconstructs the pre-migration
    state exactly."""
    conn.execute(
        "UPDATE categories SET status = 'active', decided_at = NULL, decided_by = NULL "
        "WHERE name = 'Policy'"
    )
    conn.execute(
        "DELETE FROM categories WHERE name IN ('Regulation', 'Government', 'Legal', 'Antitrust')"
    )
    conn.execute("DELETE FROM health_state WHERE key = 'policy_split_migrated'")


def test_migration_is_safe_on_a_realistic_pre_existing_database(isolated_subscribers_db):
    """Builds a database shaped like production before this migration ever
    ran -- 45 subscribers spanning approved/pending/denied, 13
    interest_categories mappings (none pointing at Policy, matching what was
    confirmed against the live DB), 5 proposed categories, and an unrelated
    health_state row -- then runs init_db() (which retires Policy and adds
    the four new categories) and asserts every pre-existing table/row is
    byte-identical afterwards, except Policy's own status/decided_at/
    decided_by. A migration that mutates existing rows (unlike a purely
    additive one) is exactly the kind of change a narrow "does Policy get
    retired" test can pass while still silently corrupting something else."""
    with users_db._connect() as conn:
        _undo_policy_split(conn)

        now = datetime.now(timezone.utc).isoformat()
        for i in range(1, 46):
            status = "approved" if i <= 40 else ("pending" if i <= 43 else "denied")
            conn.execute(
                "INSERT INTO subscribers (chat_id, username, first_name, status, requested_at, "
                "decided_at, interests, push_enabled, push_interval_hours, last_push_at, "
                "pushed_links, language) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    2000 + i, f"user{i}", f"First{i}", status, now,
                    now if status != "pending" else None,
                    json.dumps(["AI", "Finance"] if i % 2 == 0 else ["Policy watch"]),
                    1 if i % 3 == 0 else 0, 24, None, "{}", "en",
                ),
            )

        interest_rows = [
            ("AI", ["AI"]), ("Finance", ["Finance", "Stock"]),
            ("robotics stuff", ["Robotics"]), ("crypto news", ["Crypto"]),
            ("chip shortage", ["Hardware"]), ("cybersecurity", ["Security"]),
            ("startup funding", ["Startups"]), ("cloud computing", ["IT"]),
            ("gadgets", ["Consumer"]), ("academic ai research", ["Research"]),
            ("dev tools", ["Software"]), ("earnings calls", ["Finance"]),
            ("some obscure ticker", []),
        ]
        assert len(interest_rows) == 13  # matches the live DB's row count
        for interest, cats in interest_rows:
            conn.execute(
                "INSERT INTO interest_categories (interest, categories) VALUES (?, ?)",
                (interest, json.dumps(cats)),
            )

        conn.execute(
            "INSERT INTO health_state (key, value) VALUES ('some_other_marker', ?)",
            (json.dumps(["unrelated"]),),
        )

        proposed = ["Robotaxi", "Quantum", "Espionage", "Wearables", "Chips Act"]
        for i, name in enumerate(proposed):
            conn.execute(
                "INSERT INTO categories (name, description, status, created_at, created_by, sort_order) "
                "VALUES (?, NULL, 'proposed', ?, 'model', ?)",
                (name, now, 100 + i),
            )

    def snapshot():
        with users_db._connect() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            return {t: set(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}

    before = snapshot()

    users_db.init_db()

    after = snapshot()

    # Every table except `categories` and `health_state` must be untouched.
    for table in before:
        if table in ("categories", "health_state"):
            continue
        assert after[table] == before[table], f"{table} changed by the migration"

    # health_state: the pre-existing unrelated row survives untouched, and
    # only the new migration marker is added.
    with users_db._connect() as conn:
        marker = conn.execute(
            "SELECT value FROM health_state WHERE key = 'some_other_marker'"
        ).fetchone()
    assert marker is not None and json.loads(marker[0]) == ["unrelated"]

    # categories: only Policy's status/decided_at/decided_by changed, and
    # exactly the four new rows were added -- nothing else in the table
    # (the 5 proposed rows, the system "Other" row, the other 12 seed rows)
    # moved at all.
    before_cats = {row[0]: row for row in before["categories"]}
    after_cats = {row[0]: row for row in after["categories"]}
    changed_names = {
        name for name in before_cats
        if name in after_cats and before_cats[name] != after_cats[name]
    }
    assert changed_names == {"Policy"}
    assert set(after_cats) - set(before_cats) == {"Regulation", "Government", "Legal", "Antitrust"}
    assert not set(before_cats) - set(after_cats)  # nothing disappeared

    for name in ["Robotaxi", "Quantum", "Espionage", "Wearables", "Chips Act"]:
        assert after_cats[name][2] == "proposed"  # status column untouched


def test_migration_is_idempotent_on_a_realistic_database(isolated_subscribers_db):
    """Running init_db() a second time after the migration already applied
    must be a true no-op -- not just "Policy stays retired" (already covered
    by test_split_does_not_re_retire_a_deliberately_reactivated_policy) but
    literally zero rows anywhere change."""
    with users_db._connect() as conn:
        _undo_policy_split(conn)

    users_db.init_db()  # first application of the migration

    def snapshot():
        with users_db._connect() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            return {t: set(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}

    once = snapshot()
    users_db.init_db()
    users_db.init_db()
    twice = snapshot()

    assert once == twice


def test_policy_split_does_not_touch_pre_existing_interest_category_mappings(
    isolated_subscribers_db,
):
    """The plan doc's general "Retire" lifecycle operation
    (docs/plans/taxonomy-and-admin-plan.md, A5a) calls for re-mapping any
    interest that pointed at the retired category, so it doesn't end up
    matching every article (see news_push.select_candidate_articles: an
    interest mapped to an empty category list is unrestricted). That
    re-mapping machinery is explicitly NOT built yet (A4 onward, per the
    doc's own Status line) -- this migration's docstring says it verified
    by hand that nothing live mapped to Policy and skipped the step on that
    basis, not because the step was performed.

    This pins that skip as real, observable behaviour: an interest that
    already mapped to ["Policy"] before the migration keeps that exact
    mapping afterwards, unre-mapped and unstripped. This is not itself a
    bug for THIS migration (verified against production data), but it is
    the gap that would matter for a future retirement that DOES have live
    mappings -- there is nothing here, or anywhere else in this codebase,
    that would re-map or even flag that case."""
    with users_db._connect() as conn:
        _undo_policy_split(conn)
        conn.execute(
            "INSERT INTO interest_categories (interest, categories) VALUES (?, ?)",
            ("legacy policy watcher", json.dumps(["Policy"])),
        )

    users_db.init_db()

    # Unchanged -- not re-mapped to the four new categories, not stripped,
    # not emptied. Still literally ["Policy"].
    assert users_db.get_cached_interest_categories(["legacy policy watcher"]) == {
        "legacy policy watcher": ["Policy"]
    }


def test_seeding_promotes_a_name_the_model_had_already_proposed(isolated_subscribers_db):
    """Regression test for a live defect. The Policy split added Legal as a
    seed category, but the classifier had proposed "Legal" two hours
    earlier, so INSERT OR IGNORE skipped it: on production it stayed
    `proposed` with a NULL description and never entered the prompt. The
    split was 3/4 applied and nothing reported it.

    A seed is a deliberate decision that the category should exist, and
    `proposed` is a decision waiting to be made -- so the seed wins."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status = 'proposed', description = NULL, "
                     "created_by = 'model' WHERE name = 'Legal'")

    users_db.init_db()

    active = dict(users_db.get_active_categories())
    assert "Legal" in active
    assert active["Legal"], "and it gets its seeded description, not NULL"


def test_seeding_does_not_promote_a_name_an_admin_decided_on(isolated_subscribers_db):
    """The other half. rejected/retired/merged are decisions someone made,
    and seeding must not overturn them -- which is what INSERT OR IGNORE was
    protecting in the first place."""
    for decided in ("rejected", "retired", "merged"):
        with users_db._connect() as conn:
            conn.execute("UPDATE categories SET status = ? WHERE name = 'Legal'", (decided,))

        users_db.init_db()

        names = {name for name, _ in users_db.get_active_categories()}
        assert "Legal" not in names, f"seeding overturned an admin '{decided}'"


# --- A4: category review lifecycle ----------------------------------------


def _propose(name, now, hits=1):
    for i in range(hits):
        users_db.record_category_sighting(name, now, f"https://e.com/{name}{i}", f"{name} story {i}")


def test_only_proposals_past_the_threshold_are_raised(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)
    _propose("Media", now, hits=2)

    ready = users_db.categories_ready_for_review(now, threshold=5)

    assert ready == [("Healthcare", 5)]


def test_a_proposal_is_raised_once_not_every_cycle(isolated_subscribers_db):
    """An admin who has been asked and hasn't answered must not be asked
    again every four hours. The proposal stays in the table for a future
    /proposals command; the alert is a push, not a reminder loop."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)

    assert users_db.categories_ready_for_review(now, threshold=5)
    users_db.mark_category_alerted("Healthcare", now, "hospitals, drugs, clinical tech")
    assert users_db.categories_ready_for_review(now, threshold=5) == []


def test_activating_uses_the_description_drafted_at_alert_time(isolated_subscribers_db):
    """The draft is stored on the row rather than carried through Telegram's
    callback_data (64 bytes) or re-derived on the button press. What the
    admin read in the message is what ships into the classifier prompt."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)
    users_db.mark_category_alerted("Healthcare", now, "hospitals, drugs, clinical tech")

    assert users_db.activate_category("Healthcare", "admin:1", now) is True

    assert dict(users_db.get_active_categories())["Healthcare"] == "hospitals, drugs, clinical tech"


def test_activating_clears_interest_mappings_so_the_new_category_applies(
    isolated_subscribers_db,
):
    """get_cached_interest_categories treats any existing row as a hit, so a
    newly active category is invisible to every already-mapped interest
    until those rows are gone."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.set_interest_categories("robotics", ["Robotics"])
    _propose("Healthcare", now, hits=5)

    users_db.activate_category("Healthcare", "admin:1", now)

    assert users_db.get_cached_interest_categories(["robotics"]) == {}


def test_a_second_press_of_activate_changes_nothing(isolated_subscribers_db):
    """Two admins, or one double-tap. Guarded by `AND status = 'proposed'`."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)

    assert users_db.activate_category("Healthcare", "admin:1", now) is True
    assert users_db.activate_category("Healthcare", "admin:2", now) is False


def test_rejecting_stops_it_being_raised_but_keeps_recording_sightings(
    isolated_subscribers_db,
):
    """Sightings after a rejection cost a row each and answer "was
    rejecting this right?" later. count_recent_sightings only counts
    'proposed', so it never alerts again."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Media", now, hits=5)
    assert users_db.reject_category("Media", "admin:1", now) is True

    _propose("Media", now, hits=10)

    assert users_db.categories_ready_for_review(now, threshold=1) == []
    with users_db._connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM category_sightings WHERE name = 'Media'"
        ).fetchone()[0]
    assert total == 15, "still recorded, just never raised"


def test_merging_rewrites_interests_without_a_model_call(isolated_subscribers_db):
    """The new mapping is known, so there is nothing to re-derive. Contrast
    activate, which must invalidate because the correct answer is unknown."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Antitrust2", now, hits=5)
    users_db.activate_category("Antitrust2", "admin:1", now)
    users_db.set_interest_categories("competition law", ["Antitrust2", "Legal"])

    assert users_db.merge_category("Antitrust2", "Antitrust", "admin:1", now) is True

    assert users_db.get_cached_interest_categories(["competition law"]) == {
        "competition law": ["Antitrust", "Legal"]
    }
    assert users_db.resolve_category_name("Antitrust2") == "Antitrust"


def test_merging_deduplicates_when_both_names_were_present(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Antitrust2", now, hits=5)
    users_db.activate_category("Antitrust2", "admin:1", now)
    users_db.set_interest_categories("x", ["Antitrust2", "Antitrust"])

    users_db.merge_category("Antitrust2", "Antitrust", "admin:1", now)

    assert users_db.get_cached_interest_categories(["x"]) == {"x": ["Antitrust"]}


def test_merging_into_a_non_active_category_is_refused(isolated_subscribers_db):
    """Merging into a retired or already-merged category builds a chain
    whose only symptom is articles quietly resolving to nothing."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Media", now, hits=5)

    assert users_db.merge_category("Media", "Policy", "admin:1", now) is False, "Policy is retired"
    assert users_db.merge_category("Media", "Nonexistent", "admin:1", now) is False


def test_category_examples_come_back_newest_first(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.record_category_sighting("Media", now - timedelta(days=2), "https://e/old", "Older")
    users_db.record_category_sighting("Media", now, "https://e/new", "Newer")

    examples = users_db.category_examples("Media", limit=2)

    assert [t for t, _ in examples] == ["Newer", "Older"]


def test_examples_from_one_cycle_are_returned_deterministically(isolated_subscribers_db):
    """Every sighting in an ingestion cycle shares a timestamp, so ordering
    by seen_at alone leaves which examples the admin sees unspecified --
    different between runs, for no reason."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    for i in range(5):
        users_db.record_category_sighting("Media", now, f"https://e/{i}", f"Story {i}")

    first = users_db.category_examples("Media", limit=3)
    assert first == users_db.category_examples("Media", limit=3)
    assert [t for t, _ in first] == ["Story 4", "Story 3", "Story 2"], "most recent first"


def test_a_proposed_name_containing_a_colon_is_normalized(isolated_subscribers_db):
    """Telegram callback_data packs "cat:into:{name}:{target}" and the
    handler splits on ':'. A model-proposed label containing one would
    mis-parse on the button press -- the admin would be told "already
    decided" about a category that was never touched. Normalized where it
    is recorded, so the table only ever holds names the rest of the system
    can round-trip."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    users_db.record_category_sighting("Health: Policy", now, "https://e/1", "T")

    assert users_db.count_recent_sightings(now) == {"Health Policy": 1}


def test_an_overlong_proposed_name_is_truncated(isolated_subscribers_db):
    """callback_data caps at 64 bytes and the merge keyboard packs both a
    name and a target into it."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    users_db.record_category_sighting("A" * 100, now)

    proposed = list(users_db.count_recent_sightings(now))
    assert len(proposed[0]) == users_db.MAX_CATEGORY_NAME_LENGTH


def test_a_name_that_normalizes_to_nothing_is_dropped(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    users_db.record_category_sighting("   :  ", now)

    assert users_db.count_recent_sightings(now) == {}
