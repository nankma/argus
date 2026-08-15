import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import healthcheck
import users_db


def test_check_health_empty_when_both_jobs_recently_ticked(isolated_subscribers_db):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at(healthcheck.INGEST_TICK_KEY, now - timedelta(minutes=10))
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now - timedelta(minutes=5))

    assert healthcheck.check_health(now) == []


def test_check_health_flags_a_job_that_never_ticked(isolated_subscribers_db):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now)

    problems = healthcheck.check_health(now)

    assert len(problems) == 1
    assert "news_ingest" in problems[0]
    assert "never ticked" in problems[0]


def test_check_health_flags_a_stale_job(isolated_subscribers_db):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at(healthcheck.INGEST_TICK_KEY, now - timedelta(hours=3))
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now - timedelta(minutes=5))

    problems = healthcheck.check_health(now)

    assert len(problems) == 1
    assert "news_ingest" in problems[0]
    assert "3.0h ago" in problems[0]


def test_check_health_does_not_false_positive_within_the_threshold(isolated_subscribers_db):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at(healthcheck.INGEST_TICK_KEY, now - timedelta(minutes=59))
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now - timedelta(minutes=59))

    assert healthcheck.check_health(now) == []


def _patch_bot(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(healthcheck, "Bot", MagicMock(return_value=MagicMock(send_message=sent)))
    return sent


def test_run_health_check_alerts_on_new_problem(monkeypatch, isolated_subscribers_db):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(healthcheck, "check_health", lambda now=None: ["news_ingest has never ticked"])

    asyncio.run(healthcheck.run_health_check("admin-token", 999))

    sent.assert_called_once()
    assert "news_ingest has never ticked" in sent.call_args.kwargs["text"]
    assert users_db.get_health_state() == ["news_ingest has never ticked"]


def test_run_health_check_no_repeat_alert_for_unchanged_problem(monkeypatch, isolated_subscribers_db):
    sent = _patch_bot(monkeypatch)
    users_db.set_health_state(["news_ingest has never ticked"])
    monkeypatch.setattr(healthcheck, "check_health", lambda now=None: ["news_ingest has never ticked"])

    asyncio.run(healthcheck.run_health_check("admin-token", 999))

    sent.assert_not_called()


def test_run_health_check_sends_recovery_alert(monkeypatch, isolated_subscribers_db):
    sent = _patch_bot(monkeypatch)
    users_db.set_health_state(["news_ingest has never ticked"])
    monkeypatch.setattr(healthcheck, "check_health", lambda now=None: [])

    asyncio.run(healthcheck.run_health_check("admin-token", 999))

    sent.assert_called_once()
    assert "recovered" in sent.call_args.kwargs["text"]
    assert users_db.get_health_state() == []


def test_run_health_check_no_alert_when_still_healthy(monkeypatch, isolated_subscribers_db):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(healthcheck, "check_health", lambda now=None: [])

    asyncio.run(healthcheck.run_health_check("admin-token", 999))

    sent.assert_not_called()
