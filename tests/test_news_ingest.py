from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import news_cache
import news_embed
import news_ingest
import news_keyness
import news_push
import news_sources
import users_db
from tests.fakes import FakeEmbedder


def _article(link, title="Some title", source="TestSource", published_dt=None, summary=None):
    return {"title": title, "link": link, "source": source, "summary": summary, "published": None, "published_dt": published_dt}


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


def test_sections_for_source_rss_class_takes_one_call_with_no_section():
    """RSS feeds ignore anything passed to them -- one call is the whole
    feed regardless."""
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._sections_for_source("bbc_business", now) == [None]


def test_sections_for_source_uncapped_takes_every_section():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    sections = news_ingest._sections_for_source("arxiv", now)
    assert sections == news_sources.SOURCE_SECTIONS["arxiv"]
    assert "quant-ph" in sections and "physics.optics" in sections


def test_sections_for_source_capped_takes_exactly_one():
    """A scarce daily budget buys one section per pull."""
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    sections = news_ingest._sections_for_source("newsapi", now)
    assert len(sections) == 1
    assert sections[0] in news_sources.SOURCE_SECTIONS["newsapi"]


def test_a_capped_source_rotates_through_its_sections_over_time():
    """The rotation is what stops one section being pulled forever. It also
    replaces rotating through subscriber interests, which could only ever
    retrieve answers to questions someone had already asked -- a sampling
    bias that compounded every cycle."""
    seen = set()
    for day in range(1, 8):
        now = datetime(2026, 8, day, 0, 30, tzinfo=timezone.utc)
        seen.update(news_ingest._sections_for_source("newsapi", now))

    assert seen == set(news_sources.SOURCE_SECTIONS["newsapi"])


def test_a_source_with_no_declared_sections_takes_one_unsectioned_call():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert news_ingest._sections_for_source("perigon", now) == [None]


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


def test_run_ingestion_cycle_delays_between_multi_section_calls_to_same_source(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """Real incident: GNews's 1 req/sec limit returned 429 on 5 of 7
    back-to-back calls in one cycle. Confirms the fix without actually
    sleeping in the test suite. The calls are per SECTION now rather than
    per subscriber interest, but the rate limit is the same."""
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("arxiv", fetch)])
    sleep = MagicMock()
    monkeypatch.setattr(news_ingest.time, "sleep", sleep)

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    n = len(news_sources.SOURCE_SECTIONS["arxiv"])
    assert fetch.call_count == n, "one call per section"
    # delay happens BETWEEN calls, not before the first or after the last
    assert sleep.call_count == n - 1
    sleep.assert_called_with(news_ingest.REQUEST_DELAY_SECONDS)


def test_ingestion_passes_the_section_not_a_subscriber_interest(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """The bug this replaced: scheduled pulls used subscriber interest text
    as the query, so the corpus could only ever contain answers to
    questions someone had already asked."""
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_interests(1, ["bitcoin", "AAOI"])
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])
    monkeypatch.setattr(news_ingest.time, "sleep", MagicMock())

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    passed_sections = [c.kwargs.get("section") for c in fetch.call_args_list]
    assert passed_sections == news_sources.SOURCE_SECTIONS["hackernews"]
    for call in fetch.call_args_list:
        assert "bitcoin" not in str(call) and "AAOI" not in str(call)


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
    users_db.set_source_last_article_dt("hackernews:front_page", last_article_dt)
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
    users_db.set_source_last_article_dt("hackernews:front_page", last_article_dt)
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

    assert users_db.get_source_last_article_dt("hackernews:front_page") == now - timedelta(hours=1)


def test_run_ingestion_cycle_does_not_advance_last_article_dt_when_nothing_new(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    last_article_dt = now - timedelta(hours=4)
    users_db.set_source_last_article_dt("hackernews:front_page", last_article_dt)
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("hackernews", fetch)])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert users_db.get_source_last_article_dt("hackernews:front_page") == last_article_dt


def test_run_ingestion_cycle_client_side_filter_keeps_articles_with_unparseable_date(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    # Can't tell if it's new -- kept rather than dropped, same "fails
    # open" instinct as the rest of this codebase. Harmless either way:
    # news_cache dedups by link hash, so re-caching an old one is a no-op
    # overwrite, not a growing duplicate.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    users_db.set_source_last_article_dt("hackernews:front_page", now - timedelta(hours=4))
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


def test_proposals_are_reported_even_on_a_cycle_with_nothing_new(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, capsys
):
    """Regression test. The report call sat after `if not fetched: return`,
    so a quiet cycle pruned the accumulated evidence but never showed it --
    exactly the "it's in the logs if you go looking" failure this reporting
    exists to avoid."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now - timedelta(days=1))
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [])

    news_ingest.run_ingestion_cycle(_fake_classifying_model(), now)

    assert "Education x1" in capsys.readouterr().out


def test_a_cycle_does_not_resurrect_a_rejected_category(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """End-to-end version of the users_db unit test. An admin's rejection
    has to survive the classifier reaching for that label again -- otherwise
    the same proposal comes back every cycle and the admin re-litigates a
    decision they already made.

    Worth having at this level rather than only on record_category_sighting:
    the two bugs already fixed on this branch were both in how the pieces
    joined up, not in the pieces."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    users_db.record_category_sighting("Education", now - timedelta(days=1))
    with users_db._connect() as conn:
        conn.execute("UPDATE categories SET status = 'rejected' WHERE name = 'Education'")

    article = _article("https://s.edu/a", title="Stanford launches AI curriculum")
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [article])])
    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Education"]}), now)

    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Education'"
        ).fetchone()[0]
    assert status == "rejected"
    assert users_db.count_recent_sightings(now) == {}, "and it never alerts again"


def test_a_cycle_survives_an_out_of_range_index_from_the_model(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """The out-of-range guard and the sighting write are tested separately;
    this is the seam between them. news_ingest's callback does
    article.get("link"), so it receives the empty dict the guard produces
    and must record a sighting with no example rather than raising."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    article = _article("https://example.com/1", title="Real article")
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [article])])

    news_ingest.run_ingestion_cycle(
        _fake_classifying_model({0: ["AI"], 99: ["Education"]}), now
    )

    assert users_db.count_recent_sightings(now) == {"Education": 1}
    with users_db._connect() as conn:
        link, title = conn.execute(
            "SELECT article_link, article_title FROM category_sightings"
        ).fetchone()
    assert (link, title) == (None, None)
    assert news_cache.read_all()[0]["categories"] == ["AI"], "the good article is unaffected"


# --- three distinct classification outcomes -------------------------------
#
# `categories: []` used to mean two different things, and that ambiguity is
# what let a three-day classification outage look exactly like normal
# operation. Each of these pins one of the three states apart.


def test_an_article_the_model_found_no_category_for_is_marked_Other(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """The model answered, and its answer was "nothing applies". That is a
    real result, so it gets a real marker rather than an empty list."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1")])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: []}), now)

    assert news_cache.read_all()[0]["categories"] == [users_db.UNCLASSIFIABLE]


def test_an_article_the_classifier_never_reached_is_recorded_as_unknown(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, capsys
):
    """The chunk failed, so nothing is known about this article. None, not
    an empty list -- and said out loud, because the silent version of this
    is precisely what hid the outage."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1")])])

    failing = MagicMock()
    failing.with_structured_output.return_value.invoke.side_effect = RuntimeError("boom")
    news_ingest.run_ingestion_cycle(failing, now)

    assert news_cache.read_all()[0]["categories"] is None
    assert "WITHOUT being classified" in capsys.readouterr().out


def test_Other_is_never_offered_to_the_classifier(isolated_subscribers_db):
    """Give an LLM classifier a catch-all and it stops working for the
    answer. "Other" is assigned by code, never chosen by the model, so it
    must not appear in the prompt."""
    names = [name for name, _ in users_db.get_active_categories()]

    assert users_db.UNCLASSIFIABLE not in names
    taxonomy = news_ingest.news_classify.Taxonomy.from_rows(users_db.get_active_categories())
    assert users_db.UNCLASSIFIABLE not in taxonomy.prompt_fragment()


def test_Other_still_exists_as_a_row(isolated_subscribers_db):
    """Not active, but present -- so it resolves like any other name and an
    admin can count how big the bucket has become."""
    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = ?", (users_db.UNCLASSIFIABLE,)
        ).fetchone()
    assert status == ("system",)


def test_Other_does_not_widen_what_a_subscriber_receives(
    isolated_subscribers_db, isolated_news_cache
):
    """Behaviour must be unchanged at the one place that reads categories:
    an "Other" article is excluded from a topic with real categories,
    exactly as an empty list was."""
    article = {"link": "https://e.com/1", "categories": [users_db.UNCLASSIFIABLE],
               "published_dt": None, "fetched_at": None, "source_key": "bbc_business",
               "title": "t", "summary": None, "source": "s"}

    result = news_push.select_candidate_articles(
        [article], ["AI"], {"AI": ["AI", "Research"]}, None, set()
    )

    assert result == []


def test_each_section_keeps_its_own_since_cutoff(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """Sections advance at wildly different rates -- cs.AI produces dozens
    of papers a day, physics.optics a handful. A shared cutoff lets the fast
    one drag it past the slow one's genuinely-new articles, which are then
    never offered again. Same class of bug as last_pulled_at vs
    last_article_dt, one level down."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    fast = _article("https://arxiv/fast", published_dt=now - timedelta(hours=1))
    slow = _article("https://arxiv/slow", published_dt=now - timedelta(days=3))

    def fetch(query, n, since=None, section=None):
        return [fast] if section == "cs.AI" else [slow] if section == "physics.optics" else []

    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("arxiv", fetch)])
    monkeypatch.setattr(news_ingest.time, "sleep", MagicMock())
    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["AI"], 1: ["AI"]}), now)

    fast_cut = users_db.get_source_last_article_dt("arxiv:cs.AI")
    slow_cut = users_db.get_source_last_article_dt("arxiv:physics.optics")
    assert fast_cut == fast["published_dt"]
    assert slow_cut == slow["published_dt"]
    assert slow_cut < fast_cut, "the slow section is not dragged forward by the fast one"


def test_a_sectionless_source_still_uses_the_plain_source_key(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """RSS sources have no section, so their cutoff key is unchanged --
    no migration needed for rows already in the table."""
    assert news_ingest._cutoff_key("bbc_business", None) == "bbc_business"
    assert news_ingest._cutoff_key("arxiv", "cs.AI") == "arxiv:cs.AI"


# --- is_latin_script: the ingestion-time language gate -------------------
# A SCRIPT test rather than a language test, deliberately -- see the
# function's own docstring and docs/analysis/cluster-measurements.md's
# correction section for why the Spanish case below is a pass, not a bug.

def test_non_latin_titles_are_rejected():
    assert news_ingest.is_latin_script("費半急殺4.98%拖累，日經指數早盤急挫3%") is False
    assert news_ingest.is_latin_script("Кремль объявил о новых санкциях") is False
    assert news_ingest.is_latin_script("삼성전자 새로운 반도체 공장 건설 발표") is False


def test_english_titles_are_kept():
    assert news_ingest.is_latin_script(
        "Genesis joins the giant electric SUV club with new GV90") is True


def test_a_latin_script_language_that_is_not_english_is_kept():
    """Accepted leakage. Catching it needs real language detection, and the
    zero-dependency substitute (English function-word frequency) misfired on
    7% of the snapshot's titles -- arxiv headlines barely use function words."""
    assert news_ingest.is_latin_script(
        "El Nino y la crisis de los semiconductores en Espana") is True


def test_a_few_foreign_characters_do_not_condemn_an_english_title():
    assert news_ingest.is_latin_script(
        "OpenAI launches 日本語 support for ChatGPT users worldwide") is True


def test_a_mostly_foreign_title_is_rejected_despite_latin_words():
    """The real huggingface_blog case from the snapshot: a Latin product
    name in front of an otherwise Japanese title."""
    assert news_ingest.is_latin_script(
        "Nemotron-Personas-Japan: ソブリン AI のための合成データセット") is False


def test_a_title_with_no_letters_is_kept():
    """Fails open, like the rest of this pipeline: there is nothing to
    judge, so judging it would be inventing a verdict."""
    assert news_ingest.is_latin_script("") is True
    assert news_ingest.is_latin_script("2026 // 4.98% -- $100") is True


def test_emoji_neither_save_nor_condemn_a_title():
    assert news_ingest.is_latin_script("Baseten on Hugging Face Providers 🔥") is True


def test_a_non_latin_article_is_never_cached(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, capsys
):
    """The whole point of gating at ingestion rather than at selection: a
    language outlier that is only filtered from digests still pollutes the
    embeddings, which is what put a Chinese fund prospectus at the top of
    "most novel article in Finance"."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [
            _article("https://e.com/en", title="Chip maker reports record quarter"),
            _article("https://e.com/zh", title="費半急殺4.98%拖累，日經指數早盤急挫3%"),
        ])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Hardware"]}), now)

    links = [a["link"] for a in news_cache.read_all()]
    assert links == ["https://e.com/en"]
    assert "dropped 1 non-Latin-script" in capsys.readouterr().out


def test_a_cycle_of_only_non_latin_articles_classifies_nothing(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """The dropped articles must not reach the classifier either -- they are
    the paid step, and a batch of them costs real money for rows that will
    never be cached."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [
            _article("https://e.com/zh", title="費半急殺4.98%拖累，日經指數早盤急挫3%"),
        ])])
    model = _fake_classifying_model({0: ["Hardware"]})

    news_ingest.run_ingestion_cycle(model, now)

    assert news_cache.read_all() == []
    model.with_structured_output.return_value.invoke.assert_not_called()


def test_a_dropped_article_is_not_counted_as_already_cached(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, capsys
):
    """The per-source line derives "already cached" by subtraction, so a new
    counter that isn't subtracted turns into a silent miscount in the one
    place a human would look to check this filter's blast radius."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [
            _article("https://e.com/en", title="Chip maker reports record quarter"),
            _article("https://e.com/zh", title="費半急殺4.98%拖累，日經指數早盤急挫3%"),
        ])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Hardware"]}), now)

    out = capsys.readouterr().out
    assert "1 new, 0 already cached, 1 dropped as non-Latin" in out


# --- embedding wiring at ingestion --------------------------------------

def test_a_cycle_with_no_embedder_caches_articles_with_no_embedding(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """The default -- every existing test and call site gets this, and the
    pipeline must behave exactly as it did before news_embed.py existed."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1")])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Hardware"]}), now)

    assert news_cache.read_all()[0]["embedding"] is None


def test_a_cycle_with_an_embedder_stores_a_real_embedding(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1", title="Nvidia launches new GPU")])])

    news_ingest.run_ingestion_cycle(
        _fake_classifying_model({0: ["Hardware"]}), now, embedder=FakeEmbedder())

    embedding = news_cache.read_all()[0]["embedding"]
    assert embedding is not None
    assert len(embedding) == FakeEmbedder.DIM


def test_each_article_in_a_batch_gets_its_own_correctly_aligned_embedding(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """Regression for an index-alignment mistake that would be very easy
    to make here: embeddings must line up with the SAME article as
    categories does (both indexed against `fetched`), not with each
    other by construction coincidence."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [
            _article("https://e.com/1", title="Nvidia launches new GPU"),
            _article("https://e.com/2", title="Bitcoin price surges"),
        ])])

    news_ingest.run_ingestion_cycle(
        _fake_classifying_model({0: ["Hardware"], 1: ["Finance"]}), now, embedder=FakeEmbedder())

    by_link = {a["link"]: a for a in news_cache.read_all()}
    sim_to_gpu_article = news_embed.cosine_similarity(
        by_link["https://e.com/1"]["embedding"],
        news_embed.embed_one(FakeEmbedder(), "Nvidia launches new GPU"))
    sim_to_bitcoin_article = news_embed.cosine_similarity(
        by_link["https://e.com/2"]["embedding"],
        news_embed.embed_one(FakeEmbedder(), "Bitcoin price surges"))
    assert sim_to_gpu_article > 0.99
    assert sim_to_bitcoin_article > 0.99


def test_ingestion_embeds_title_and_summary_together(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """Measured 2026-08-25 (docs/analysis/cluster-measurements.md,
    'Title+summary embedding') to matter for headlines written in a
    business-outcome style with no topic vocabulary in them -- the
    on-topic content lives only in the summary. A business-outcome
    headline plus a summary naming a query's actual vocabulary must
    embed closer to that query than the headline alone would."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [_article(
            "https://e.com/1",
            title="Company ships faster with new tools",
            summary="Nvidia unveils a new GPU architecture for AI training",
        )])])

    news_ingest.run_ingestion_cycle(
        _fake_classifying_model({0: ["Hardware"]}), now, embedder=FakeEmbedder())

    cached_embedding = news_cache.read_all()[0]["embedding"]
    query_embedding = news_embed.embed_one(FakeEmbedder(), "Nvidia GPU architecture")
    title_only_embedding = news_embed.embed_one(FakeEmbedder(), "Company ships faster with new tools")

    sim_to_cached = news_embed.cosine_similarity(cached_embedding, query_embedding)
    sim_title_only = news_embed.cosine_similarity(title_only_embedding, query_embedding)
    assert sim_to_cached > sim_title_only


def test_an_embedder_failure_does_not_block_caching(
    monkeypatch, isolated_subscribers_db, isolated_news_cache
):
    """embed_texts already fails open (tested in test_news_embed.py) --
    this confirms the ingestion cycle actually relies on that rather than
    wrapping its own call in a try/except that could diverge from it."""
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1")])])

    class Boom:
        def encode(self, texts):
            raise RuntimeError("model died")

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Hardware"]}), now, embedder=Boom())

    cached = news_cache.read_all()
    assert len(cached) == 1
    assert cached[0]["embedding"] is None


# --- category keyness refresh (2026-08-26) ----------------------------------
# news_keyness.py's per-category "how foreign is this word" scores, for
# news_push._pick_novelty_extra's novelty extra -- computed fresh every
# ingestion cycle over the WHOLE cache (not just this cycle's new
# articles), so there's no staleness window between an article being
# ingested and its category's keyness table reflecting it. See
# news_ingest._refresh_category_keyness's own docstring and
# docs/analysis/cluster-measurements.md's "Offbeat selection, take two".

def test_ingestion_cycle_refreshes_category_keyness(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, fake_nltk
):
    # Two real floors to clear, not just "some articles exist": a term
    # needs global document frequency >= news_keyness.MIN_GLOBAL_DF (5)
    # to be scored at all, and category_keyness needs an "outside the
    # topic" comparison group (returns {} when n_rest == 0) -- the
    # Finance-tagged article is that group, not incidental filler.
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    ai_titles = [f"openai releases update {i}" for i in range(news_keyness.MIN_GLOBAL_DF)]
    fetched = [_article(f"https://e.com/{i}", title=t) for i, t in enumerate(ai_titles)]
    fetched.append(_article(f"https://e.com/{len(ai_titles)}", title="market report for investors"))
    monkeypatch.setattr(news_sources, "enabled_sources", lambda: [("bbc_business", lambda q, n: fetched)])
    categories_by_index = {i: ["AI"] for i in range(len(ai_titles))}
    categories_by_index[len(ai_titles)] = ["Finance"]

    news_ingest.run_ingestion_cycle(_fake_classifying_model(categories_by_index), now)

    scores = users_db.get_category_keyness("AI")
    assert "openai" in scores


def test_ingestion_cycle_keyness_reflects_the_whole_cache_not_just_this_batch(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, fake_nltk
):
    """A term only present in an ALREADY-cached article (from a previous
    cycle, not this one's fetch) must still show up in the refreshed
    keyness table -- news_ingest.py reads news_cache.read_all() for this
    step, not just `fetched`, specifically so keyness reflects the real
    current corpus."""
    # Same two floors as the test above: MIN_GLOBAL_DF and a real
    # outside-the-topic comparison group.
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(news_keyness.MIN_GLOBAL_DF):
        news_cache.write_article(
            "bbc_business", _article(f"https://e.com/existing{i}", title=f"quantum research breakthrough {i}"),
            ["AI"], now - timedelta(hours=1),
        )
    news_cache.write_article(
        "bbc_business", _article("https://e.com/finance", title="market report for investors"),
        ["Finance"], now - timedelta(hours=1),
    )
    monkeypatch.setattr(
        news_sources, "enabled_sources",
        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/new", title="openai releases update")])])

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["AI"]}), now)

    scores = users_db.get_category_keyness("AI")
    assert "quantum" in scores


def test_keyness_refresh_failure_does_not_block_ingestion(
    monkeypatch, isolated_subscribers_db, isolated_news_cache, fake_nltk
):
    """A broken keyness step is a push-quality regression, never a reason
    an otherwise-successful ingestion cycle should fail to cache its
    articles."""
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(news_sources, "enabled_sources",
                        lambda: [("bbc_business", lambda q, n: [_article("https://e.com/1")])])
    monkeypatch.setattr(
        news_ingest.news_keyness, "build_noun_index",
        MagicMock(side_effect=RuntimeError("keyness died")),
    )

    news_ingest.run_ingestion_cycle(_fake_classifying_model({0: ["Hardware"]}), now)

    assert len(news_cache.read_all()) == 1


def test_keyness_refresh_does_nothing_on_an_empty_cache(isolated_subscribers_db, isolated_news_cache, fake_nltk):
    """No articles at all yet -- nothing to compute keyness over, and
    nothing should raise."""
    news_ingest._refresh_category_keyness(datetime(2026, 8, 26, tzinfo=timezone.utc))
    assert users_db.get_category_keyness("AI") == {}
