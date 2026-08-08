import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

import news_push
import news_sources
import users_db
from tests.fakes import FakeToolCallingModel


def _article(link, published_dt=None, title="Some title", source="TestSource"):
    return {"title": title, "link": link, "source": source, "summary": None, "published_dt": published_dt}


def test_fetch_new_articles_filters_by_published_dt(monkeypatch):
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    old = _article("https://example.com/old", published_dt=datetime(2026, 7, 1, tzinfo=timezone.utc))
    new = _article("https://example.com/new", published_dt=datetime(2026, 8, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(
        news_sources, "enabled_sources", lambda: [("test", lambda query, n: [old, new])]
    )

    result = news_push.fetch_new_articles(["AI"], since, set())

    assert [a["link"] for a in result] == ["https://example.com/new"]


def test_fetch_new_articles_falls_back_to_pushed_links_for_unparsed_dates(monkeypatch):
    unparsed_seen = _article("https://example.com/seen", published_dt=None)
    unparsed_unseen = _article("https://example.com/unseen", published_dt=None)
    monkeypatch.setattr(
        news_sources,
        "enabled_sources",
        lambda: [("test", lambda query, n: [unparsed_seen, unparsed_unseen])],
    )

    result = news_push.fetch_new_articles(["AI"], None, {"https://example.com/seen"})

    assert [a["link"] for a in result] == ["https://example.com/unseen"]


def test_fetch_new_articles_dedupes_across_topics(monkeypatch):
    article = _article("https://example.com/shared", published_dt=datetime(2026, 8, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(
        news_sources, "enabled_sources", lambda: [("test", lambda query, n: [article])]
    )

    result = news_push.fetch_new_articles(["AI", "robotics"], None, set())

    assert len(result) == 1


def test_fetch_new_articles_isolates_source_errors(monkeypatch):
    def failing_source(query, n):
        raise RuntimeError("boom")

    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("broken", failing_source)])

    result = news_push.fetch_new_articles(["AI"], None, set())

    assert result == []


def test_write_push_digest_returns_model_output():
    model = FakeToolCallingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    result = news_push.write_push_digest(model, articles)

    assert result == "<b>Digest</b>"


def test_is_subscriber_due_true_when_never_pushed():
    assert news_push.is_subscriber_due(None, 24, datetime.now(timezone.utc)) is True


def test_is_subscriber_due_false_within_interval():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    last_push = now - timedelta(hours=2)
    assert news_push.is_subscriber_due(last_push, 24, now) is False


def test_is_subscriber_due_true_after_interval():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    last_push = now - timedelta(hours=25)
    assert news_push.is_subscriber_due(last_push, 24, now) is True


def _subscriber(chat_id, interests=("AI",), interval=24, last_push_at=None, pushed_links=()):
    return {
        "chat_id": chat_id,
        "interests": list(interests),
        "push_interval_hours": interval,
        "last_push_at": last_push_at,
        "pushed_links": list(pushed_links),
    }


def test_run_push_cycle_skips_subscriber_with_no_interests(monkeypatch):
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(1, interests=[])])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send))

    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_skips_subscriber_not_due(monkeypatch):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    recently_pushed = _subscriber(2, last_push_at=now - timedelta(hours=1), interval=24)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [recently_pushed])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    fetch = MagicMock()
    monkeypatch.setattr(news_push, "fetch_new_articles", fetch)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    fetch.assert_not_called()
    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_sends_and_records_when_new_articles_found(monkeypatch):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(3)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    monkeypatch.setattr(news_push, "fetch_new_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_called_once_with(3, "<b>Digest</b>")
    record_push.assert_called_once_with(3, ["https://example.com/new"], now)


def test_run_push_cycle_no_new_articles_records_without_sending(monkeypatch):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(4)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    monkeypatch.setattr(news_push, "fetch_new_articles", MagicMock(return_value=[]))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(4, [], now)


def test_run_push_cycle_blocked_by_output_guardrail_does_not_send(monkeypatch):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(5)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    monkeypatch.setattr(news_push, "fetch_new_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="off-topic drift"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=False))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(5, ["https://example.com/new"], now)


def test_run_push_cycle_isolates_one_subscribers_failure(monkeypatch):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(6), _subscriber(7)]
    )
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)

    call_count = {"n": 0}

    def fetch_side_effect(topics, since, pushed_links):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return [{**_article("https://example.com/ok"), "topic": "AI"}]

    monkeypatch.setattr(news_push, "fetch_new_articles", MagicMock(side_effect=fetch_side_effect))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    # subscriber 6 failed silently; subscriber 7 still got its digest
    send.assert_called_once_with(7, "<b>Digest</b>")
