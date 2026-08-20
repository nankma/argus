from datetime import datetime, timezone

import news_sources
from tests.fixtures import (
    ARXIV_RESPONSE,
    GNEWS_RESPONSE,
    HACKERNEWS_RESPONSE,
    NEWSAPI_RESPONSE,
    PERIGON_RESPONSE,
    RSS_RESPONSE,
)


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


def test_fetch_openai_blog_uses_correct_url_and_source_name(requests_mock):
    requests_mock.get("https://openai.com/news/rss.xml", text=RSS_RESPONSE)

    articles = news_sources.fetch_openai_blog()

    assert len(articles) == 2
    assert articles[0]["source"] == "OpenAI Blog"


def test_fetch_newsapi(requests_mock, monkeypatch):
    monkeypatch.setenv("NEWSAPI_API_KEY", "fake-key")
    requests_mock.get("https://newsapi.org/v2/everything", json=NEWSAPI_RESPONSE)

    articles = news_sources.fetch_newsapi("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake NewsAPI Article"
    assert articles[0]["source"] == "Fake News Outlet"


def test_fetch_gnews(requests_mock, monkeypatch):
    monkeypatch.setenv("GNEWS_API_KEY", "fake-key")
    requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)

    articles = news_sources.fetch_gnews("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake GNews Article"
    assert articles[0]["source"] == "Fake GNews Outlet"


def test_fetch_gnews_omits_from_when_since_not_given(requests_mock, monkeypatch):
    monkeypatch.setenv("GNEWS_API_KEY", "fake-key")
    requests_mock.get("https://gnews.io/api/v4/search", json=GNEWS_RESPONSE)

    news_sources.fetch_gnews("AI", 5)

    assert "from" not in requests_mock.last_request.qs


def test_fetch_gnews_adds_from_when_since_given(requests_mock, monkeypatch):
    monkeypatch.setenv("GNEWS_API_KEY", "fake-key")
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
    monkeypatch.setenv("PERIGON_API_KEY", "fake-key")
    requests_mock.get("https://api.perigon.io/v1/all", json=PERIGON_RESPONSE)

    articles = news_sources.fetch_perigon("AI", 5)

    assert len(articles) == 1
    assert articles[0]["title"] == "Fake Perigon Article"
    assert articles[0]["source"] == "example.com"


def test_enabled_sources_always_includes_free_sources(monkeypatch):
    for var in ("NEWSAPI_API_KEY", "GNEWS_API_KEY", "PERIGON_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    names = [name for name, _ in news_sources.enabled_sources()]

    for free_source in (
        "hackernews",
        "arxiv",
        "openai_blog",
        "huggingface_blog",
        "techcrunch_ai",
        "venturebeat_ai",
        "mit_tech_review",
        "bbc_business",
        "bbc_technology",
        "guardian_business",
        "guardian_technology",
        "marketwatch",
        "economist_business",
        "economist_tech",
        "nikkei_asia",
        "wired_business",
        "the_register",
        "computerworld",
        "zdnet",
        "engadget",
        "techradar",
    ):
        assert free_source in names
    for gated_source in ("newsapi", "gnews", "perigon"):
        assert gated_source not in names


def test_enabled_sources_gates_on_env_var(monkeypatch):
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" not in names

    monkeypatch.setenv("NEWSAPI_API_KEY", "fake-key")
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" in names


def test_enabled_sources_include_restricted_true_by_default(monkeypatch):
    monkeypatch.setenv("NEWSAPI_API_KEY", "fake-key")
    monkeypatch.setenv("PERIGON_API_KEY", "fake-key")
    names = [name for name, _ in news_sources.enabled_sources()]
    assert "newsapi" in names
    assert "perigon" in names


def test_enabled_sources_excludes_restricted_when_false(monkeypatch):
    monkeypatch.setenv("NEWSAPI_API_KEY", "fake-key")
    monkeypatch.setenv("PERIGON_API_KEY", "fake-key")
    monkeypatch.setenv("GNEWS_API_KEY", "fake-key")
    names = [name for name, _ in news_sources.enabled_sources(include_restricted=False)]
    assert "newsapi" not in names
    assert "perigon" not in names
    # gnews is a real api-class source but not in RESTRICTED_SOURCES -- its
    # budget has real headroom beyond what news_ingest.py alone uses
    assert "gnews" in names
    # unrestricted, always-on sources are unaffected
    assert "hackernews" in names
    assert "bbc_business" in names


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
    monkeypatch.setenv("GNEWS_API_KEY", "SUPERSECRETKEY123")
    requests_mock.get("https://gnews.io/api/v4/search", status_code=403, json={})

    try:
        news_sources.fetch_gnews("AI", 5)
        assert False, "expected the 403 to propagate"
    except Exception as exc:
        assert "SUPERSECRETKEY123" not in str(exc), f"key leaked into: {exc}"
        assert "403" in str(exc)


def test_traced_fetch_redacts_the_key_before_it_reaches_telemetry(monkeypatch):
    # traced_fetch's span attribute is shipped to Phoenix and retained 30 days,
    # so it is the last point the value could escape the process.
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


def test_general_tech_and_business_feeds_are_registered():
    """Added 2026-08-20 to widen a registry that measured 28.6% of its cache
    coming from feeds that structurally cannot produce anything but AI
    content (openai_blog, huggingface_blog, arxiv, techcrunch_ai,
    venturebeat_ai) -- see docs/analysis/cluster-measurements.md. RSS gives
    no history, so breadth has to come from more sources rather than from
    reaching further back."""
    names = {name for name, *_ in news_sources.SOURCE_REGISTRY}

    assert {"ars_technica", "techcrunch", "cnbc"} <= names


def test_techcrunch_general_feed_is_separate_from_the_ai_one():
    """Two different feeds, not a rename. The AI-only one stays; the point
    of the general one is that it isn't AI-only."""
    assert news_sources.fetch_techcrunch is not news_sources.fetch_techcrunch_ai


def test_new_sources_need_no_api_key():
    """Plain RSS, so no key, no quota, no budget tracking -- unlike Perigon,
    which is metered per request and burned a month's allowance in three
    days (docs/plans/security-plan.md finding 21)."""
    required = {name: env for name, _fn, env, _cls in news_sources.SOURCE_REGISTRY}

    for name in ("ars_technica", "techcrunch", "cnbc"):
        assert required[name] is None


def test_new_sources_are_not_restricted():
    """They go to subscribers, unlike newsapi/perigon. That's the whole
    reason these three were picked over the general-news feeds: they sit
    inside the product's stated technology-industry scope."""
    for name in ("ars_technica", "techcrunch", "cnbc"):
        assert name not in news_sources.RESTRICTED_SOURCES
