import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

import news_cache
import news_push
import news_sources
import users_db
from tests.fakes import FakeToolCallingModel


def _article(link, published_dt=None, title="Some title", source="TestSource", categories=None, source_key="test"):
    return {
        "title": title,
        "link": link,
        "source": source,
        "source_key": source_key,
        "summary": None,
        "published_dt": published_dt,
        "categories": categories or [],
    }


# --- select_candidate_articles (stage 1: category filter) -----------------


def test_select_candidate_articles_filters_by_published_dt():
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    old = _article("https://example.com/old", published_dt=datetime(2026, 7, 1, tzinfo=timezone.utc))
    new = _article("https://example.com/new", published_dt=datetime(2026, 8, 5, tzinfo=timezone.utc))

    result = news_push.select_candidate_articles([old, new], ["AI"], {}, since, set())

    assert [a["link"] for a in result] == ["https://example.com/new"]


def test_select_candidate_articles_falls_back_to_pushed_links_for_unparsed_dates():
    seen = _article("https://example.com/seen", published_dt=None)
    unseen = _article("https://example.com/unseen", published_dt=None)

    result = news_push.select_candidate_articles(
        [seen, unseen], ["AI"], {}, None, {"https://example.com/seen"}
    )

    assert [a["link"] for a in result] == ["https://example.com/unseen"]


def test_select_candidate_articles_dedupes_across_topics():
    article = _article("https://example.com/shared", published_dt=datetime(2026, 8, 5, tzinfo=timezone.utc))

    result = news_push.select_candidate_articles([article], ["AI", "robotics"], {}, None, set())

    assert len(result) == 1


def test_select_candidate_articles_unrestricted_topic_matches_any_category():
    # A topic with no cached category mapping (classifier miss) shouldn't
    # be starved -- it matches an article regardless of that article's
    # own categories.
    article = _article("https://example.com/a", categories=["Policy"])

    result = news_push.select_candidate_articles([article], ["AAOI"], {}, None, set())

    assert len(result) == 1


def test_select_candidate_articles_excludes_off_category_article():
    # The Nikkei Asia incident this was built to fix: an uncategorized
    # (or off-category) article shouldn't reach a subscriber whose topic
    # DID classify into real categories.
    earthquake = _article("https://example.com/earthquake", categories=[])
    ai_topic_categories = {"AI": ["AI", "Research"]}

    result = news_push.select_candidate_articles([earthquake], ["AI"], ai_topic_categories, None, set())

    assert result == []


def test_select_candidate_articles_includes_overlapping_category_article():
    article = _article("https://example.com/a", categories=["AI", "Startups"])
    topic_categories = {"AI": ["AI", "Research"]}

    result = news_push.select_candidate_articles([article], ["AI"], topic_categories, None, set())

    assert len(result) == 1


def test_select_candidate_articles_excludes_restricted_sources_by_default():
    article = _article("https://example.com/a", source_key="perigon")

    result = news_push.select_candidate_articles([article], ["AI"], {}, None, set())

    assert result == []


def test_select_candidate_articles_includes_restricted_sources_when_enabled():
    article = _article("https://example.com/a", source_key="perigon")

    result = news_push.select_candidate_articles([article], ["AI"], {}, None, set(), include_restricted=True)

    assert len(result) == 1


def test_select_candidate_articles_caps_per_topic():
    articles = [
        _article(f"https://example.com/{i}", published_dt=datetime(2026, 8, i + 1, tzinfo=timezone.utc))
        for i in range(1, 8)
    ]

    result = news_push.select_candidate_articles(articles, ["AI"], {}, None, set(), max_per_topic=3)

    assert len(result) == 3
    # newest-first
    assert result[0]["link"] == "https://example.com/7"


# --- resolve_interest_categories -------------------------------------------


def test_resolve_interest_categories_uses_cache_when_available(monkeypatch):
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {"AI": ["AI"]})
    classify = MagicMock()
    monkeypatch.setattr(news_push.news_classify, "classify_interests", classify)

    result = news_push.resolve_interest_categories("fake-model", ["AI"])

    assert result == {"AI": ["AI"]}
    classify.assert_not_called()


def test_resolve_interest_categories_classifies_and_caches_misses(monkeypatch):
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {})
    monkeypatch.setattr(news_push.news_classify, "classify_interests", lambda model, interests: {"AAOI": ["Stock"]})
    set_categories = MagicMock()
    monkeypatch.setattr(users_db, "set_interest_categories", set_categories)

    result = news_push.resolve_interest_categories("fake-model", ["AAOI"])

    assert result == {"AAOI": ["Stock"]}
    set_categories.assert_called_once_with("AAOI", ["Stock"])


def test_resolve_interest_categories_caches_empty_result_too(monkeypatch):
    monkeypatch.setattr(users_db, "get_cached_interest_categories", lambda interests: {})
    monkeypatch.setattr(news_push.news_classify, "classify_interests", lambda model, interests: {})
    set_categories = MagicMock()
    monkeypatch.setattr(users_db, "set_interest_categories", set_categories)

    result = news_push.resolve_interest_categories("fake-model", ["Some obscure ticker"])

    assert result == {"Some obscure ticker": []}
    set_categories.assert_called_once_with("Some obscure ticker", [])


# --- write_push_digest ------------------------------------------------------


def test_write_push_digest_returns_model_output():
    model = FakeToolCallingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    result = news_push.write_push_digest(model, articles)

    assert result == "<b>Digest</b>"


def test_write_push_digest_includes_language_directive_when_set():
    captured = {}

    class RecordingModel(FakeToolCallingModel):
        def invoke(self, messages, *args, **kwargs):
            captured["system_prompt"] = messages[0]["content"]
            return super().invoke(messages, *args, **kwargs)

    model = RecordingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    news_push.write_push_digest(model, articles, language="Spanish")

    assert "Spanish" in captured["system_prompt"]


def test_write_push_digest_no_language_directive_when_unset():
    captured = {}

    class RecordingModel(FakeToolCallingModel):
        def invoke(self, messages, *args, **kwargs):
            captured["system_prompt"] = messages[0]["content"]
            return super().invoke(messages, *args, **kwargs)

    model = RecordingModel(responses=[AIMessage(content="<b>Digest</b>")])
    articles = [{**_article("https://example.com/a"), "topic": "AI"}]

    news_push.write_push_digest(model, articles)

    assert captured["system_prompt"] == news_push._PUSH_DIGEST_PROMPT


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


def _subscriber(
    chat_id,
    interests=("AI",),
    interval=24,
    last_push_at=None,
    pushed_links=(),
    language=None,
    restricted_sources_enabled=False,
):
    return {
        "chat_id": chat_id,
        "interests": list(interests),
        "push_interval_hours": interval,
        "last_push_at": last_push_at,
        "pushed_links": list(pushed_links),
        "language": language,
        "restricted_sources_enabled": restricted_sources_enabled,
    }


def _stub_cache_and_categories(monkeypatch, cached_articles=(), topic_categories=None):
    monkeypatch.setattr(news_cache, "read_all", lambda: list(cached_articles))
    monkeypatch.setattr(
        news_push, "resolve_interest_categories", lambda model, interests: topic_categories or {}
    )


def test_run_push_cycle_skips_subscriber_with_no_interests(monkeypatch, isolated_subscribers_db):
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(1, interests=[])])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send))

    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_skips_subscriber_not_due(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    recently_pushed = _subscriber(2, last_push_at=now - timedelta(hours=1), interval=24)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [recently_pushed])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    select = MagicMock()
    monkeypatch.setattr(news_push, "select_candidate_articles", select)
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    select.assert_not_called()
    send.assert_not_called()
    record_push.assert_not_called()


def test_run_push_cycle_sends_and_records_when_new_articles_found(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(3)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_called_once_with(3, "<b>Digest</b>")
    record_push.assert_called_once_with(3, ["https://example.com/new"], now)


def test_run_push_cycle_passes_subscriber_language_to_digest(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(8, language="French")]
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    write_digest = MagicMock(return_value="<b>Digest</b>")
    monkeypatch.setattr(news_push, "write_push_digest", write_digest)
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    write_digest.assert_called_once_with("fake-model", new_articles, "French")


def test_run_push_cycle_passes_subscribers_own_restricted_sources_flag(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db,
        "list_push_enabled_subscribers",
        lambda: [_subscriber(9, restricted_sources_enabled=True), _subscriber(10, restricted_sources_enabled=False)],
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    _stub_cache_and_categories(monkeypatch)
    select = MagicMock(return_value=[])
    monkeypatch.setattr(news_push, "select_candidate_articles", select)

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    assert select.call_args_list[0].kwargs["include_restricted"] is True
    assert select.call_args_list[1].kwargs["include_restricted"] is False


def test_run_push_cycle_reads_cache_once_and_reuses_across_subscribers(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(11), _subscriber(12)]
    )
    monkeypatch.setattr(users_db, "record_push", MagicMock())
    read_all = MagicMock(return_value=[])
    monkeypatch.setattr(news_cache, "read_all", read_all)
    monkeypatch.setattr(news_push, "resolve_interest_categories", lambda model, interests: {})
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=AsyncMock(), now=now))

    read_all.assert_called_once()


def test_run_push_cycle_no_new_articles_records_without_sending(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(4)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=[]))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(4, [], now)


def test_run_push_cycle_empty_digest_records_without_sending(monkeypatch, isolated_subscribers_db):
    # Stage 2 (the model's own judgment inside write_push_digest) decided
    # none of the stage-1 candidates were genuinely relevant.
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(13)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="   "))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(13, ["https://example.com/new"], now)


def test_run_push_cycle_blocked_by_output_guardrail_does_not_send(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(users_db, "list_push_enabled_subscribers", lambda: [_subscriber(5)])
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    new_articles = [{**_article("https://example.com/new"), "topic": "AI"}]
    _stub_cache_and_categories(monkeypatch)
    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(return_value=new_articles))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="off-topic drift"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=False))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    send.assert_not_called()
    record_push.assert_called_once_with(5, ["https://example.com/new"], now)


def test_run_push_cycle_isolates_one_subscribers_failure(monkeypatch, isolated_subscribers_db):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        users_db, "list_push_enabled_subscribers", lambda: [_subscriber(6), _subscriber(7)]
    )
    record_push = MagicMock()
    monkeypatch.setattr(users_db, "record_push", record_push)
    _stub_cache_and_categories(monkeypatch)

    call_count = {"n": 0}

    def select_side_effect(cached_articles, topics, topic_categories, since, pushed_links, include_restricted=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return [{**_article("https://example.com/ok"), "topic": "AI"}]

    monkeypatch.setattr(news_push, "select_candidate_articles", MagicMock(side_effect=select_side_effect))
    monkeypatch.setattr(news_push, "write_push_digest", MagicMock(return_value="<b>Digest</b>"))
    monkeypatch.setattr(news_push.guardrails, "is_output_on_topic", MagicMock(return_value=True))
    send = AsyncMock()

    asyncio.run(news_push.run_push_cycle(model="fake-model", send=send, now=now))

    # subscriber 6 failed silently; subscriber 7 still got its digest
    send.assert_called_once_with(7, "<b>Digest</b>")
