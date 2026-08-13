from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import news_cache
import news_ingest
import news_sources
import users_db


def _article(link, title="Some title", source="TestSource"):
    return {"title": title, "link": link, "source": source, "summary": None, "published": None, "published_dt": None}


def _fake_classifying_model(categories_by_index=None):
    fake_structured = MagicMock()
    items = [
        news_ingest.news_classify.ArticleCategories(index=i, categories=cats)
        for i, cats in (categories_by_index or {}).items()
    ]
    fake_structured.invoke.return_value = news_ingest.news_classify.ClassificationBatch(items=items)
    model = MagicMock()
    model.with_structured_output.return_value = fake_structured
    return model


def test_is_source_due_when_never_pulled():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._is_source_due("bbc_business", None, now) is True


def test_is_source_due_respects_default_interval():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._is_source_due("bbc_business", now - timedelta(hours=3), now) is False
    assert news_ingest._is_source_due("bbc_business", now - timedelta(hours=4), now) is True


def test_is_source_due_respects_perigon_8h_interval():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._is_source_due("perigon", now - timedelta(hours=7), now) is False
    assert news_ingest._is_source_due("perigon", now - timedelta(hours=8), now) is True


def test_is_source_due_respects_newsapi_24h_interval():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._is_source_due("newsapi", now - timedelta(hours=23), now) is False
    assert news_ingest._is_source_due("newsapi", now - timedelta(hours=24), now) is True


def test_queries_for_source_rss_class_ignores_interests():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._queries_for_source("bbc_business", now, ["bitcoin", "AI"]) == ["technology"]


def test_queries_for_source_uncapped_api_class_uses_every_interest():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._queries_for_source("hackernews", now, ["bitcoin", "AI"]) == ["bitcoin", "AI"]


def test_queries_for_source_capped_uses_exactly_one_query():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    queries = news_ingest._queries_for_source("perigon", now, ["bitcoin", "AI", "robotics"])
    assert len(queries) == 1
    assert queries[0] in ["bitcoin", "AI", "robotics"]


def test_queries_for_source_falls_back_to_default_with_no_interests():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._queries_for_source("perigon", now, []) == ["technology"]
    assert news_ingest._queries_for_source("hackernews", now, []) == ["technology"]


def test_run_ingestion_cycle_fetches_classifies_and_caches(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    article = _article("https://example.com/1", title="Nvidia deal")
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", lambda q, n: [article])])

    model = _fake_classifying_model({0: ["IT", "Finance"]})
    news_ingest.run_ingestion_cycle(model, now)

    cached = news_cache.read_all()
    assert len(cached) == 1
    assert cached[0]["link"] == "https://example.com/1"
    assert cached[0]["categories"] == ["IT", "Finance"]
    assert cached[0]["source_key"] == "bbc_business"


def test_run_ingestion_cycle_skips_sources_not_yet_due(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    fetch = MagicMock(return_value=[_article("https://example.com/1")])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", fetch)])
    users_db.set_source_last_pulled_at("bbc_business", now - timedelta(hours=1))

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    fetch.assert_not_called()
    assert news_cache.read_all() == []


def test_run_ingestion_cycle_respects_daily_cap(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    fetch = MagicMock(return_value=[_article("https://example.com/1")])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("perigon", fetch)])
    for _ in range(3):
        users_db.try_consume_api_budget("perigon", 3, now.date().isoformat())

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    fetch.assert_not_called()


def test_run_ingestion_cycle_advances_last_pulled_at(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources", lambda: [("bbc_business", lambda q, n: [_article("https://example.com/1")])]
    )

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert users_db.get_source_last_pulled_at("bbc_business") == now


def test_run_ingestion_cycle_one_source_failing_does_not_block_others(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

    def failing(q, n):
        raise RuntimeError("boom")

    ok_article = _article("https://example.com/ok")
    monkeypatch.setattr(
        news_sources,
        "enabled_sources",
        lambda: [("broken", failing), ("bbc_business", lambda q, n: [ok_article])],
    )

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    cached = news_cache.read_all()
    assert len(cached) == 1
    assert cached[0]["link"] == "https://example.com/ok"


def test_run_ingestion_cycle_cleans_up_expired_entries_first(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    news_cache.write_article("bbc_business", _article("https://example.com/old"), [], now - timedelta(hours=49))
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert news_cache.read_all() == []


def test_run_ingestion_cycle_no_new_articles_skips_classification_call(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", lambda q, n: [])])

    model = MagicMock()
    news_ingest.run_ingestion_cycle(model, now)

    model.with_structured_output.assert_not_called()
