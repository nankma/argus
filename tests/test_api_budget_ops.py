import api_budget_ops
import storage


def test_try_consume_api_budget_allows_up_to_the_cap(isolated_subscribers_db):
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is True
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is True
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is True


def test_try_consume_api_budget_denies_once_cap_reached(isolated_subscribers_db):
    for _ in range(3):
        api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14")
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is False


def test_try_consume_api_budget_resets_on_a_new_day(isolated_subscribers_db):
    for _ in range(3):
        api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14")
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is False
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-15") is True


def test_try_consume_api_budget_tracks_sources_independently(isolated_subscribers_db):
    for _ in range(1):
        api_budget_ops.try_consume_api_budget("newsapi", 1, "2026-08-14")
    assert api_budget_ops.try_consume_api_budget("newsapi", 1, "2026-08-14") is False
    # perigon's budget is untouched by newsapi's being exhausted
    assert api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14") is True


def test_try_consume_api_budget_preserves_history_across_days(isolated_subscribers_db):
    # Real gap the (source, date) migration fixed: the old schema
    # overwrote a source's single row on every new day, losing yesterday's
    # count entirely.
    for _ in range(3):
        api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14")
    api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-15")

    history = api_budget_ops.get_api_budget_history("perigon")

    assert {"date": "2026-08-14", "count": 3} in history
    assert {"date": "2026-08-15", "count": 1} in history


def test_get_total_api_calls_sums_across_all_recorded_days(isolated_subscribers_db):
    for _ in range(3):
        api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-14")
    api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-15")

    assert api_budget_ops.get_total_api_calls("perigon") == 4


def test_get_total_api_calls_zero_for_unknown_source(isolated_subscribers_db):
    assert api_budget_ops.get_total_api_calls("perigon") == 0


def test_record_api_call_does_not_enforce_a_cap(isolated_subscribers_db):
    # Unlike try_consume_api_budget, record_api_call never returns False --
    # it's for visibility (agent.py's search_news), not gating.
    for _ in range(5):
        api_budget_ops.record_api_call("perigon", "2026-08-14")

    assert api_budget_ops.get_api_budget_history("perigon") == [{"date": "2026-08-14", "count": 5}]


def test_record_api_call_and_try_consume_api_budget_share_the_same_count(isolated_subscribers_db):
    api_budget_ops.try_consume_api_budget("perigon", 10, "2026-08-14")
    api_budget_ops.record_api_call("perigon", "2026-08-14")

    assert api_budget_ops.get_total_api_calls("perigon") == 2


def test_api_budget_migration_preserves_existing_rows_from_the_old_schema(isolated_subscribers_db):
    import sqlite3

    # Simulate a database still on the pre-2026-08-16 schema (one row per
    # source, no date in the primary key) to confirm init_db's migration
    # carries the existing row forward instead of dropping it.
    conn = sqlite3.connect(isolated_subscribers_db)
    conn.execute("DROP TABLE IF EXISTS api_budget")
    conn.execute("CREATE TABLE api_budget (source TEXT PRIMARY KEY, date TEXT NOT NULL, count INTEGER NOT NULL)")
    conn.execute("INSERT INTO api_budget (source, date, count) VALUES ('perigon', '2026-08-10', 2)")
    conn.commit()
    conn.close()

    storage.init_db()

    assert api_budget_ops.get_api_budget_history("perigon") == [{"date": "2026-08-10", "count": 2}]
    # the new schema allows a second day's row to coexist
    api_budget_ops.try_consume_api_budget("perigon", 3, "2026-08-11")
    assert len(api_budget_ops.get_api_budget_history("perigon")) == 2
