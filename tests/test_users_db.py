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
