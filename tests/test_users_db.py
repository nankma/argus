import sqlite3
from datetime import datetime, timezone

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
    assert users_db.get_pushed_links(18) == ["https://example.com/a", "https://example.com/b"]


def test_record_push_merges_and_dedupes_links_newest_first(isolated_subscribers_db):
    users_db.record_push(19, ["https://example.com/old"], datetime(2026, 8, 1, tzinfo=timezone.utc))
    users_db.record_push(
        19, ["https://example.com/new", "https://example.com/old"], datetime(2026, 8, 8, tzinfo=timezone.utc)
    )
    assert users_db.get_pushed_links(19) == ["https://example.com/new", "https://example.com/old"]


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


def test_list_all_interests_empty_when_nobody_has_any(isolated_subscribers_db):
    assert users_db.list_all_interests() == []


def test_list_all_interests_deduplicates_across_subscribers(isolated_subscribers_db):
    users_db.set_interests(1, ["bitcoin", "AI"])
    users_db.set_interests(2, ["AI", "robotics"])
    assert users_db.list_all_interests() == ["bitcoin", "AI", "robotics"]


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
