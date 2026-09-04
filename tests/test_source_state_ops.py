import sqlite3
from datetime import datetime, timedelta, timezone

import source_state_ops
import storage


def test_init_db_migrates_schema_missing_last_article_dt_column(isolated_subscribers_db):
    # Simulate a source_pull_state table from before last_article_dt
    # existed (pre-2026-08-16).
    with sqlite3.connect(isolated_subscribers_db) as conn:
        conn.execute("DROP TABLE IF EXISTS source_pull_state")
        conn.execute("CREATE TABLE source_pull_state (source TEXT PRIMARY KEY, last_pulled_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO source_pull_state (source, last_pulled_at) VALUES ('perigon', '2026-08-14T09:00:00+00:00')"
        )
    storage.init_db()  # should migrate without error, preserving the existing row

    assert source_state_ops.get_source_last_pulled_at("perigon") == datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    assert source_state_ops.get_source_last_article_dt("perigon") is None
    source_state_ops.set_source_last_article_dt("perigon", datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc))
    assert source_state_ops.get_source_last_article_dt("perigon") == datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)


def test_get_source_last_pulled_at_unknown_source_returns_none(isolated_subscribers_db):
    assert source_state_ops.get_source_last_pulled_at("perigon") is None


def test_set_and_get_source_last_pulled_at(isolated_subscribers_db):
    when = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    source_state_ops.set_source_last_pulled_at("perigon", when)
    assert source_state_ops.get_source_last_pulled_at("perigon") == when


def test_set_source_last_pulled_at_upserts(isolated_subscribers_db):
    t1 = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 14, 13, 0, 0, tzinfo=timezone.utc)
    source_state_ops.set_source_last_pulled_at("perigon", t1)
    source_state_ops.set_source_last_pulled_at("perigon", t2)
    assert source_state_ops.get_source_last_pulled_at("perigon") == t2


def test_get_source_last_article_dt_unknown_source_returns_none(isolated_subscribers_db):
    assert source_state_ops.get_source_last_article_dt("perigon") is None


def test_set_and_get_source_last_article_dt(isolated_subscribers_db):
    when = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    source_state_ops.set_source_last_article_dt("perigon", when)
    assert source_state_ops.get_source_last_article_dt("perigon") == when


def test_set_source_last_article_dt_upserts(isolated_subscribers_db):
    t1 = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 14, 13, 0, 0, tzinfo=timezone.utc)
    source_state_ops.set_source_last_article_dt("perigon", t1)
    source_state_ops.set_source_last_article_dt("perigon", t2)
    assert source_state_ops.get_source_last_article_dt("perigon") == t2


def test_source_last_pulled_at_and_last_article_dt_are_independent(isolated_subscribers_db):
    # Two different questions ("when did the job last run" vs "what's the
    # newest article we've seen") stored on the same row -- setting one
    # must not disturb the other. See get_source_last_article_dt's
    # docstring for why these are deliberately different values.
    pulled_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    article_dt = datetime(2026, 8, 14, 8, 0, 0, tzinfo=timezone.utc)
    source_state_ops.set_source_last_pulled_at("perigon", pulled_at)
    source_state_ops.set_source_last_article_dt("perigon", article_dt)
    assert source_state_ops.get_source_last_pulled_at("perigon") == pulled_at
    assert source_state_ops.get_source_last_article_dt("perigon") == article_dt

    new_pulled_at = pulled_at + timedelta(hours=8)
    source_state_ops.set_source_last_pulled_at("perigon", new_pulled_at)
    assert source_state_ops.get_source_last_pulled_at("perigon") == new_pulled_at
    assert source_state_ops.get_source_last_article_dt("perigon") == article_dt  # unchanged
