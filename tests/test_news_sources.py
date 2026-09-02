from datetime import datetime, timezone

from trailsign import Settings

import news_sources
from tests.fixtures import (
    ARXIV_RESPONSE,
    GNEWS_RESPONSE,
    HACKERNEWS_RESPONSE,
    NEWSAPI_RESPONSE,
    PERIGON_RESPONSE,
    RSS_RESPONSE,
)


def _set_news_source_key(monkeypatch, **keys):
    """Replaces monkeypatch.setenv("XXX_API_KEY", ...) now that
    fetch_newsapi/fetch_gnews/fetch_perigon read news_source.<name>.api-key
    via Settings, not a raw env var -- e.g.
    _set_news_source_key(monkeypatch, newsapi="fake-key"). Omit a name
    entirely to leave it unconfigured (the "not enabled" state)."""
    news_source = {name: {"api-key": key} for name, key in keys.items()}
    fake_settings = Settings({"news_source": news_source})
    monkeypatch.setattr(news_sources, "get_settings", lambda: fake_settings)


def test_fetch_hackernews(requests_mock):
    requests_mock.get("https://hn.algolia.com/api/v1/search_by_date", json=HACKERNEWS_RESPONSE)

    articles = news_sources.fetch_hackernews("AI", 5)

    assert len(articles) == 2
    assert articles[0]["title"] == "Show HN: A tool for X"
    assert articles[0]["link"] == "https://example.com/show-hn-x"
    assert articles[0]["source"] == "Hacker News"
    # second hit has url=None in the fixture -> falls back to the HN item link
    assert articles[1]["link"] == "https://news.ycombinator.com/item?id=12346"
    assert articles[0]["published_dt"] == datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def test_fetch_hackernews_omits_numeric_filter_when_since_not_given(requests_mock):
    requests_mock.get("https://hn.algolia.com/api/v1/search_by_date", json=HACKERNEWS_RESPONSE)

    news_sources.fetch_hackernews("AI", 5)

    assert "numericFilters" not in requests_mock.last_request.qs


def test_fetch_hackernews_adds_numeric_filter_when_since_given(requests_mock):
    requests_mock.get("https://hn.algolia.com/api/v1/search_by_date", json=HACKERNEWS_RESPONSE)
    since = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

    news_sources.fetch_hackernews("AI", 5, since=since)

    # requests_mock lower-cases query string keys/values in .qs
    assert requests_mock.last_request.qs["numericfilters"] == [f"created_at_i>{int(since.timestamp())}"]


def test_fetch_arxiv(requests_mock):
    requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)

    articles = news_sources.fetch_arxiv("cat:cs.AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "A Study of Fake Papers for Testing"
    assert articles[0]["link"] == "https://arxiv.org/abs/2608.00001v1"
    assert articles[0]["source"] == "arXiv"
    assert "fake abstract" in articles[0]["summary"]
    assert "\n" not in articles[0]["title"]
    assert articles[0]["published_dt"] == datetime(2026, 8, 4, 0, 0, 0, tzinfo=timezone.utc)


def test_fetch_arxiv_omits_date_range_when_since_not_given(requests_mock):
    requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)

    news_sources.fetch_arxiv("cat:cs.AI", 5)

    assert requests_mock.last_request.qs["search_query"] == ["cat:cs.ai"]


def test_fetch_arxiv_adds_date_range_when_since_given(requests_mock):
    requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)
    since = datetime(2026, 8, 15, 12, 30, 0, tzinfo=timezone.utc)

    news_sources.fetch_arxiv("cat:cs.AI", 5, since=since)

    assert requests_mock.last_request.qs["search_query"] == ["cat:cs.ai and submitteddate:[202608151230 to 99991231235959]"]


def test_parse_iso_published_handles_missing_and_malformed():
    assert news_sources._parse_iso_published(None) is None
    assert news_sources._parse_iso_published("") is None
    assert news_sources._parse_iso_published("not a date") is None


def test_parse_iso_published_assumes_utc_when_offset_is_missing():
    # Real incident, 2026-08-14: a source returning a timestamp with no
    # offset at all produced a naive datetime, which crashed every
    # news_push.py cycle when compared against an aware `since` value.
    result = news_sources._parse_iso_published("2026-08-13T22:00:00")
    assert result == datetime(2026, 8, 13, 22, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_rss_published_handles_missing():
    assert news_sources._parse_rss_published({}) is None


def test_fetch_rss_generic(requests_mock):
    requests_mock.get("https://example.com/feed.xml", text=RSS_RESPONSE)

    articles = news_sources._fetch_rss("https://example.com/feed.xml", "Fake Blog", 5)

    assert len(articles) == 2
    assert articles[0]["title"] == "Fake Blog Post One"
    assert articles[0]["link"] == "https://example.com/blog/post-one"
    assert articles[0]["source"] == "Fake Blog"


def test_fetch_rss_generic_respects_max_results(requests_mock):
    requests_mock.get("https://example.com/feed.xml", text=RSS_RESPONSE)

    articles = news_sources._fetch_rss("https://example.com/feed.xml", "Fake Blog", 1)

    assert len(articles) == 1


def test_make_rss_fetcher_uses_the_given_url_and_display_name(requests_mock):
    requests_mock.get("https://example.com/feed.xml", text=RSS_RESPONSE)

    fetch = news_sources._make_rss_fetcher("https://example.com/feed.xml", "Fake Blog")
    articles = fetch()

    assert len(articles) == 2
    assert articles[0]["source"] == "Fake Blog"


def test_make_rss_fetcher_ignores_query_and_respects_max_results(requests_mock):
    requests_mock.get("https://example.com/feed.xml", text=RSS_RESPONSE)

    fetch = news_sources._make_rss_fetcher("https://example.com/feed.xml", "Fake Blog")
    articles = fetch("this query is ignored, RSS has no query parameter", max_results=1)

    assert len(articles) == 1


def test_rss_sources_from_settings_builds_one_entry_per_config_row(monkeypatch):
    """The mechanism, not any specific real feed -- what's actually in
    news_source.rss is settings data now (see settings.yml), not a code
    invariant to lock in a unit test."""
    fake_settings = Settings({"news_source": {"rss": [
        {"key": "fake_a", "display_name": "Fake A", "url": "https://a.example.com/feed.xml"},
        {"key": "fake_b", "display_name": "Fake B", "url": "https://b.example.com/feed.xml"},
    ]}})
    monkeypatch.setattr(news_sources, "get_settings", lambda: fake_settings)

    entries = news_sources._rss_sources_from_settings()

    assert [key for key, *_ in entries] == ["fake_a", "fake_b"]
    assert all(required_env is None for _key, _fn, required_env, _cls in entries)
    assert all(source_class == "rss" for *_rest, source_class in entries)


def test_rss_sources_from_settings_entries_are_independently_callable(requests_mock, monkeypatch):
    requests_mock.get("https://a.example.com/feed.xml", text=RSS_RESPONSE)
    fake_settings = Settings({"news_source": {"rss": [
        {"key": "fake_a", "display_name": "Fake A", "url": "https://a.example.com/feed.xml"},
    ]}})
    monkeypatch.setattr(news_sources, "get_settings", lambda: fake_settings)

    [(_key, fetch, _required_env, _source_class)] = news_sources._rss_sources_from_settings()
    articles = fetch()

    assert articles[0]["source"] == "Fake A"


def test_rss_sources_from_settings_defaults_to_empty_list(monkeypatch):
    """A deployer with zero configured RSS feeds is a legitimate, sparse
    state -- fails open, doesn't raise."""
    monkeypatch.setattr(news_sources, "get_settings", lambda: Settings({}))

    assert news_sources._rss_sources_from_settings() == []


def test_fetch_newsapi(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, newsapi="fake-key")
    requests_mock.get("https://newsapi.org/v2/everything", json=NEWSAPI_RESPONSE)

    articles = news_sources.fetch_newsapi("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake NewsAPI Article"
    assert articles[0]["source"] == "Fake News Outlet"


def test_fetch_gnews(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="fake-key")
    requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)

    articles = news_sources.fetch_gnews("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake GNews Article"
    assert articles[0]["source"] == "Fake GNews Outlet"


def test_fetch_gnews_omits_from_when_since_not_given(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="fake-key")
    requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)

    news_sources.fetch_gnews("AI", 5)

    assert "from" not in requests_mock.last_request.qs


def test_fetch_gnews_adds_from_when_since_given(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="fake-key")
    requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)
    since = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

    news_sources.fetch_gnews("AI", 5, since=since)

    assert requests_mock.last_request.qs["from"] == ["2026-08-15t12:00:00z"]


def test_fetch_newsapi_has_no_since_parameter():
    # Deliberate -- see news_sources.py's comment above fetch_newsapi for
    # why (NewsAPI's free-tier delay makes a server-side date filter
    # counterproductive; news_ingest.py relies on client-side filtering
    # for this source instead).
    import inspect

    assert "since" not in inspect.signature(news_sources.fetch_newsapi).parameters


def test_fetch_perigon_has_no_since_parameter():
    # Deliberate -- unverified, see news_sources.py's comment.
    import inspect

    assert "since" not in inspect.signature(news_sources.fetch_perigon).parameters


def test_fetch_perigon(requests_mock, monkeypatch):
    # NOTE: Perigon's real response shape is unverified (no API key available
    # to test against the live service — see docs/current/ai-news-sources.md). This
    # test only locks in that fetch_perigon parses the shape it's coded to
    # expect; it is not a guarantee that shape matches Perigon's actual API.
    _set_news_source_key(monkeypatch, perigon="fake-key")
    requests_mock.get("https://api.perigon.io/v1/all", json=PERIGON_RESPONSE)

    articles = news_sources.fetch_perigon("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake Perigon Article"
    assert articles[0]["source"] == "example.com"


def test_enabled_sources_always_includes_free_sources(monkeypatch):
    """hackernews/arxiv are the two hardcoded free sources; fake_rss_source
    is conftest.py's injected news_source.rss entry, standing in for
    whatever a deployment's own RSS list actually contains -- that list is
    settings data now, not a code invariant this test should hardcode."""
    for var in ("NEWSAPI_API_KEY", "GNEWS_API_KEY", "PERIGON_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    names = [name for name, _ in news_sources.enabled_sources()]

    for free_source in ("hackernews", "arxiv", "fake_rss_source"):
        assert free_source in names
    for gated_source in ("newsapi", "gnews", "perigon"):
        assert gated_source not in names


def test_enabled_sources_gates_on_env_var(monkeypatch):
    _set_news_source_key(monkeypatch)  # nothing configured
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" not in names

    _set_news_source_key(monkeypatch, newsapi="fake-key")
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" in names


def test_enabled_sources_include_restricted_true_by_default(monkeypatch):
    _set_news_source_key(monkeypatch, newsapi="fake-key", perigon="fake-key")
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" in names
    assert "perigon" in names


def test_enabled_sources_excludes_restricted_when_false(monkeypatch):
    _set_news_source_key(monkeypatch, newsapi="fake-key", perigon="fake-key", gnews="fake-key")
    names = [name for name, _ in news_sources.enabled_sources(include_restricted=False)]
    assert "newsapi" not in names
    assert "perigon" not in names
    # gnews is a real api-class source but not in RESTRICTED_SOURCES -- its
    # budget has real headroom beyond what news_ingest.py alone uses
    assert "gnews" in names
    # unrestricted, always-on sources are unaffected
    assert "hackernews" in names
    assert "fake_rss_source" in names


def test_traced_fetch_returns_the_underlying_fetch_result():
    def fake_fetch(query, max_results):
        return [{"title": "a"}, {"title": "b"}]

    result = news_sources.traced_fetch("hackernews", fake_fetch, "AI", 5)

    assert result == [{"title": "a"}, {"title": "b"}]


def test_traced_fetch_reraises_on_error():
    def failing_fetch(query, max_results):
        raise RuntimeError("boom")

    try:
        news_sources.traced_fetch("hackernews", failing_fetch, "AI", 5)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as exc:
        assert str(exc) == "boom"


def test_traced_fetch_passes_query_and_max_results_through():
    captured = {}

    def fake_fetch(query, max_results):
        captured["query"] = query
        captured["max_results"] = max_results
        return []

    news_sources.traced_fetch("hackernews", fake_fetch, "robotics", 7)

    assert captured == {"query": "robotics", "max_results": 7}


def test_redact_strips_api_keys_from_error_text():
    # Real incident, 2026-08-19: GNews's and Perigon's live keys were found in
    # plaintext in `docker logs`, because requests puts the full request URL
    # into an HTTPError message and news_ingest.py logs the exception. These
    # are the exact two shapes that leaked, with fake key values.
    gnews = ("400 Client Error: Bad Request for url: https://gnews.io/api/v4/search"
             "?q=Edge+AI&lang=en&max=50&apikey=DEADBEEFCAFE1234&from=2026-08-19T01%3A18%3A39Z")
    perigon = ("403 Client Error: Forbidden for url: https://api.perigon.io/v1/all"
               "?q=quantum&size=50&apiKey=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert "DEADBEEFCAFE1234" not in news_sources._redact(gnews)
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in news_sources._redact(perigon)
    # the non-secret parts stay, so the message is still diagnosable
    assert "q=Edge+AI" in news_sources._redact(gnews)
    assert "400 Client Error" in news_sources._redact(gnews)


def test_raise_for_status_redacts_the_key(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="SUPERSECRETKEY123")
    requests_mock.get("https://gnews.io/api/v4/search", status_code=403, json={})

    try:
        news_sources.fetch_gnews("AI", 5)
        assert False, "expected the 403 to propagate"
    except Exception as exc:
        assert "SUPERSECRETKEY123" not in str(exc), f"key leaked into: {exc}"
        assert "403" in str(exc)


def test_traced_fetch_redacts_the_key_before_it_reaches_telemetry(monkeypatch):
    # traced_fetch's span attribute is shipped to the telemetry backend
    # (Logfire), so it is the last point the value could escape the process.
    recorded = {}

    class FakeSpan:
        def set_attribute(self, k, v):
            recorded[k] = v
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(news_sources._tracer, "start_as_current_span", lambda name: FakeSpan())

    def failing(query, max_results):
        raise RuntimeError("401 for url: https://x/y?apiKey=LEAKYVALUE999")

    try:
        news_sources.traced_fetch("gnews", failing, "AI", 5)
    except RuntimeError:
        pass
    assert "LEAKYVALUE999" not in recorded.get("error", "")
    assert "<redacted>" in recorded.get("error", "")


# --- registry breadth -----------------------------------------------------
#
# What USED to be registered here (widening the registry past AI-only
# feeds, 2026-08-20, see docs/analysis/cluster-measurements.md) is now
# settings data, not a code invariant -- see settings.yml's news_source.rss
# for the actual list and its own comment for the "don't let the cache go
# AI-only again" reasoning. The tests below cover the MECHANISM breadth
# depends on: distinct config entries never collapse into the same
# fetcher, and every RSS entry is unconditionally unrestricted/keyless by
# construction (also asserted directly in
# test_rss_sources_from_settings_builds_one_entry_per_config_row above).


def test_two_different_rss_config_entries_produce_two_different_fetchers(monkeypatch):
    """The registry-breadth guarantee, generically: two distinct
    news_source.rss rows never collapse into the same fetch function (e.g.
    a general feed and its AI-only counterpart, techcrunch vs
    techcrunch_ai in the current settings.yml, stay independently
    callable)."""
    fake_settings = Settings({"news_source": {"rss": [
        {"key": "fake_general", "display_name": "Fake General", "url": "https://a.example.com/feed.xml"},
        {"key": "fake_ai_only", "display_name": "Fake AI Only", "url": "https://b.example.com/feed.xml"},
    ]}})
    monkeypatch.setattr(news_sources, "get_settings", lambda: fake_settings)

    entries = news_sources._rss_sources_from_settings()
    fns = [fn for _key, fn, _env, _cls in entries]

    assert fns[0] is not fns[1]


def test_traced_fetch_records_the_section_rather_than_a_placeholder_query():
    """A section pull ignores the query, so recording it would stamp the
    same placeholder on every ingestion span and hide the one thing worth
    knowing when diagnosing one."""
    captured = {}

    class FakeSpan:
        def set_attribute(self, k, v):
            captured[k] = v
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import contextlib
    from unittest.mock import patch

    @contextlib.contextmanager
    def fake_span(name):
        yield FakeSpan()

    with patch.object(news_sources._tracer, "start_as_current_span", fake_span):
        news_sources.traced_fetch("arxiv", lambda q, n: [], "technology", 5,
                                  section="quant-ph")
    assert captured.get("section") == "quant-ph"
    assert "query" not in captured

    captured.clear()
    with patch.object(news_sources._tracer, "start_as_current_span", fake_span):
        news_sources.traced_fetch("arxiv", lambda q, n: [], "user question", 5)
    assert captured.get("query") == "user question"
    assert "section" not in captured


# --- section mode (scheduled ingestion) -----------------------------------
#
# The section branch of each fetcher is new code on a live path. The query
# branch stays for agent.search_news, which passes a real user question.


def test_hackernews_section_uses_the_ranking_endpoint(requests_mock):
    """front_page is a RANKING. search_by_date would re-sort it into
    chronological order and throw away the only thing it was for."""
    m = requests_mock.get("https://hn.algolia.com/api/v1/search", json=HACKERNEWS_RESPONSE)

    news_sources.fetch_hackernews("IGNORED", 5, section="front_page")

    assert m.last_request.qs["tags"] == ["front_page"]
    assert "query" not in m.last_request.qs, "the query is not sent in section mode"


def test_hackernews_section_still_honours_since(requests_mock):
    m = requests_mock.get("https://hn.algolia.com/api/v1/search", json=HACKERNEWS_RESPONSE)
    since = datetime(2026, 8, 20, tzinfo=timezone.utc)

    news_sources.fetch_hackernews("IGNORED", 5, since=since, section="front_page")

    assert m.last_request.qs["numericfilters"] == [f"created_at_i>{int(since.timestamp())}"]


def test_hackernews_query_mode_is_unchanged(requests_mock):
    """agent.search_news must keep hitting the chronological search with a
    real query -- its failure mode is silent, since it would still return
    articles, just the wrong ones."""
    m = requests_mock.get("https://hn.algolia.com/api/v1/search_by_date", json=HACKERNEWS_RESPONSE)

    news_sources.fetch_hackernews("nvidia earnings", 5)

    assert m.last_request.qs["query"] == ["nvidia earnings"]
    assert m.last_request.qs["tags"] == ["story"]


def test_arxiv_section_becomes_a_subject_class(requests_mock):
    m = requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)

    news_sources.fetch_arxiv("IGNORED", 5, section="quant-ph")

    assert m.last_request.qs["search_query"] == ["cat:quant-ph"]


def test_arxiv_section_and_since_compose(requests_mock):
    """arxiv is uncapped AND server-side-since, so every production pull
    takes both."""
    m = requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)
    since = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)

    news_sources.fetch_arxiv("IGNORED", 5, since=since, section="physics.optics")

    q = m.last_request.qs["search_query"][0]
    assert q.startswith("cat:physics.optics"), "the section, not the ignored query"
    assert "submitteddate:[202608200900" in q


def test_arxiv_query_mode_is_unchanged(requests_mock):
    m = requests_mock.get("http://export.arxiv.org/api/query", text=ARXIV_RESPONSE)

    news_sources.fetch_arxiv("photonic computing", 5)

    assert m.last_request.qs["search_query"] == ["photonic computing"]


def test_newsapi_section_uses_top_headlines(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, newsapi="k")
    m = requests_mock.get("https://newsapi.org/v2/top-headlines", json=NEWSAPI_RESPONSE)

    news_sources.fetch_newsapi("IGNORED", 5, section="science")

    assert m.last_request.qs["category"] == ["science"]
    assert m.last_request.qs["language"] == ["en"]
    assert "q" not in m.last_request.qs


def test_newsapi_query_mode_keeps_the_language_pin(requests_mock, monkeypatch):
    """The pin applies to search too -- an unconstrained query here returned
    65 of 65 Chinese articles."""
    _set_news_source_key(monkeypatch, newsapi="k")
    m = requests_mock.get("https://newsapi.org/v2/everything", json=NEWSAPI_RESPONSE)

    news_sources.fetch_newsapi("AOI", 5)

    assert m.last_request.qs["q"] == ["aoi"]
    assert m.last_request.qs["language"] == ["en"]


def test_gnews_section_uses_top_headlines(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="k")
    m = requests_mock.get("https://gnews.io/api/v4/top-headlines", json=GNEWS_RESPONSE)

    news_sources.fetch_gnews("IGNORED", 5, section="technology")

    assert m.last_request.qs["topic"] == ["technology"]
    assert m.last_request.qs["lang"] == ["en"]
    assert "q" not in m.last_request.qs


def test_gnews_section_and_since_compose(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="k")
    m = requests_mock.get("https://gnews.io/api/v4/top-headlines", json=GNEWS_RESPONSE)
    since = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)

    news_sources.fetch_gnews("IGNORED", 5, since=since, section="business")

    assert m.last_request.qs["topic"] == ["business"]
    assert m.last_request.qs["from"] == ["2026-08-20t09:00:00z"]


def test_gnews_query_mode_is_unchanged(requests_mock, monkeypatch):
    _set_news_source_key(monkeypatch, gnews="k")
    m = requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)

    news_sources.fetch_gnews("nvidia earnings", 5)

    assert m.last_request.qs["q"] == ["nvidia earnings"]
