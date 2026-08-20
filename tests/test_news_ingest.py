from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import news_cache
import news_ingest
import news_sources
import users_db


def _article(link, title="Some title", source="TestSource", published_dt=None):
    return {"title": title, "link": link, "source": source, "summary": None, "published": None, "published_dt": published_dt}


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


def test_run_ingestion_cycle_delays_between_multi_query_calls_to_same_source(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """Real incident: GNews's 1 req/sec limit returned 429 on 5 of 7
    back-to-back queries in one cycle. Confirms the fix without actually
    sleeping in the test suite."""
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_interests(1, ["bitcoin", "AI", "robotics"])
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])
    sleep = MagicMock()
    monkeypatch.setattr(news_ingest.time, "sleep", sleep)

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert fetch.call_count == 3  # one call per distinct interest
    # delay happens BETWEEN calls, not before the first or after the last
    assert sleep.call_count == 2
    sleep.assert_called_with(news_ingest.REQUEST_DELAY_SECONDS)


def test_run_ingestion_cycle_no_delay_for_single_query_sources(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_interests(1, ["bitcoin", "AI"])
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", fetch)])
    sleep = MagicMock()
    monkeypatch.setattr(news_ingest.time, "sleep", sleep)

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    fetch.assert_called_once()
    sleep.assert_not_called()


def test_run_ingestion_cycle_no_new_articles_skips_classification_call(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", lambda q, n: [])])

    model = MagicMock()
    news_ingest.run_ingestion_cycle(model, now)

    model.with_structured_output.assert_not_called()


def test_run_ingestion_cycle_passes_since_to_server_side_since_sources(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # hackernews is in _SERVER_SIDE_SINCE_SOURCES -- confirmed live
    # 2026-08-16 that its numericFilters date param actually works, see
    # news_sources.fetch_hackernews's docstring. The cutoff is
    # last_article_dt (newest article actually seen), not last_pulled_at
    # (wall-clock job time) -- see news_ingest.py's module docstring for
    # why that distinction matters.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    last_article_dt = now - timedelta(hours=4)
    users_db.set_source_last_article_dt("hackernews", last_article_dt)
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    _args, kwargs = fetch.call_args
    assert kwargs.get("since") == last_article_dt


def test_run_ingestion_cycle_does_not_pass_since_to_newsapi(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    # newsapi is time-filterable (api-class) but deliberately NOT in
    # _SERVER_SIDE_SINCE_SOURCES -- its free-tier delay makes a server-side
    # `from=` counterproductive (see news_sources.py's comment). It still
    # relies on the client-side filter below, just not a since kwarg.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_article_dt("newsapi", now - timedelta(hours=24))
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("newsapi", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    _args, kwargs = fetch.call_args
    assert "since" not in kwargs


def test_run_ingestion_cycle_client_side_filter_drops_articles_not_newer_than_last_article_dt(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    last_article_dt = now - timedelta(hours=4)
    users_db.set_source_last_article_dt("hackernews", last_article_dt)
    old_article = _article("https://example.com/old", published_dt=last_article_dt - timedelta(minutes=1))
    new_article = _article("https://example.com/new", published_dt=last_article_dt + timedelta(minutes=1))
    fetch = MagicMock(return_value=[old_article, new_article])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    cached_links = {a["link"] for a in news_cache.read_all()}
    assert cached_links == {"https://example.com/new"}


def test_run_ingestion_cycle_advances_last_article_dt_to_the_newest_seen(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # The high-water mark only moves forward to the newest article
    # actually observed this cycle -- not to `now` (wall-clock), which is
    # the exact distinction that makes it robust against a source's own
    # indexing delay (see news_ingest.py's module docstring).
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    older = _article("https://example.com/older", published_dt=now - timedelta(hours=3))
    newest = _article("https://example.com/newest", published_dt=now - timedelta(hours=1))
    fetch = MagicMock(return_value=[older, newest])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: [], 1: []}), now)

    assert users_db.get_source_last_article_dt("hackernews") == now - timedelta(hours=1)


def test_run_ingestion_cycle_does_not_advance_last_article_dt_when_nothing_new(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    last_article_dt = now - timedelta(hours=4)
    users_db.set_source_last_article_dt("hackernews", last_article_dt)
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert users_db.get_source_last_article_dt("hackernews") == last_article_dt


def test_run_ingestion_cycle_client_side_filter_keeps_articles_with_unparseable_date(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # Can't tell if it's new -- kept rather than dropped, same "fails
    # open" instinct as the rest of this codebase. Harmless either way:
    # news_cache dedups by link hash, so re-caching an old one is a no-op
    # overwrite, not a growing duplicate.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_article_dt("hackernews", now - timedelta(hours=4))
    undated = _article("https://example.com/undated", published_dt=None)
    fetch = MagicMock(return_value=[undated])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    assert {a["link"] for a in news_cache.read_all()} == {"https://example.com/undated"}


def test_run_ingestion_cycle_rss_source_not_time_filtered(monkeypatch, isolated_subscribers_db, isolated_news_cache):
    # RSS sources have no query/date-range parameter at all -- an "old"
    # article still gets cached, since there's nothing to filter by.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_pulled_at("bbc_business", now - timedelta(hours=4))
    old_article = _article("https://example.com/old", published_dt=now - timedelta(hours=10))
    fetch = MagicMock(return_value=[old_article])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    assert {a["link"] for a in news_cache.read_all()} == {"https://example.com/old"}
    _args, kwargs = fetch.call_args
    assert "since" not in kwargs


def test_run_ingestion_cycle_uses_raised_max_results_for_time_filterable_sources(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    hn_fetch = MagicMock(return_value=[])
    rss_fetch = MagicMock(return_value=[])
    monkeypatch.setattr(
        news_sources, "enabled_sources", lambda: [("hackernews", hn_fetch), ("bbc_business", rss_fetch)]
    )

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    hn_args, _hn_kwargs = hn_fetch.call_args
    assert hn_args[1] == news_ingest.MAX_RESULTS_PER_SOURCE_SINCE_LAST_PULL
    rss_args, _rss_kwargs = rss_fetch.call_args
    assert rss_args[1] == news_ingest.MAX_RESULTS_PER_SOURCE_RSS


def test_run_ingestion_cycle_first_pull_has_no_since_and_no_filtering(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # last_article_dt is None (never pulled before) -- nothing to filter
    # against yet, so everything up to the safety cap is kept regardless
    # of published_dt, and no since kwarg is passed at all.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    old_article = _article("https://example.com/old", published_dt=now - timedelta(days=10))
    fetch = MagicMock(return_value=[old_article])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    assert {a["link"] for a in news_cache.read_all()} == {"https://example.com/old"}
    _args, kwargs = fetch.call_args
    assert "since" not in kwargs


def test_run_ingestion_cycle_skips_reclassifying_already_cached_links(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # Matters most for RSS-class sources now that their cap is 200, not 5
    # -- most of a 200-item pull is typically the same links as last
    # cycle, and without this check every one of them would cost a real
    # paid classification call every cycle for no reason.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    already_cached = _article("https://example.com/seen")
    news_cache.write_article("bbc_business", already_cached, ["IT"], now - timedelta(hours=1))
    fresh = _article("https://example.com/fresh")
    fetch = MagicMock(return_value=[already_cached, fresh])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", fetch)])
    classify_mock = MagicMock(return_value={0: ["IT"]})
    monkeypatch.setattr(news_ingest.news_classify, "classify_articles", classify_mock)

    news_ingest.run_ingestion_cycle(MagicMock(), now)

    classified_articles = classify_mock.call_args[0][1]
    assert [a["link"] for a in classified_articles] == ["https://example.com/fresh"]
    cached_links = {a["link"] for a in news_cache.read_all()}
    assert cached_links == {"https://example.com/seen", "https://example.com/fresh"}


def test_run_ingestion_cycle_all_articles_already_cached_skips_classification_call(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    already_cached = _article("https://example.com/seen")
    news_cache.write_article("bbc_business", already_cached, ["IT"], now - timedelta(hours=1))
    fetch = MagicMock(return_value=[already_cached])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", fetch)])

    model = MagicMock()
    news_ingest.run_ingestion_cycle(model, now)

    model.with_structured_output.assert_not_called()


# --- A3: taxonomy gaps recorded from a real cycle -------------------------


def test_ingestion_records_a_sighting_for_a_label_outside_the_taxonomy(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """End-to-end: the classifier reaches for a label the taxonomy doesn't
    have, and the cycle leaves evidence in the database rather than only a
    log line nobody greps for. The three-day classification outage was
    invisible for exactly that reason."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    article = _article("https://s.edu/a", title="Stanford launches AI curriculum")
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [article])])

    model = _fake_classifying_model({0: ["AI", "Education"]})
    news_ingest.run_ingestion_cycle(model, now)

    assert users_db.count_recent_sightings(now) == {"Education": 1}
    # the valid label still lands on the article
    assert news_cache.read_all()[0]["categories"] == ["AI"]


def test_ingestion_prunes_sightings_past_retention(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now - timedelta(days=60))
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert users_db.count_recent_sightings(now) == {}


def test_a_sighting_does_not_make_the_label_usable(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """A proposed category must not start being offered to the classifier
    just because it was seen. Only an admin activating it does that."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    article = _article("https://s.edu/a", title="Stanford launches AI curriculum")
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [article])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Education"]}), now)

    assert "Education" not in [name for name, _ in users_db.get_active_categories()]
