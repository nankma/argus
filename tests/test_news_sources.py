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


def test_parse_iso_published_handles_missing_and_malformed():
    assert news_sources._parse_iso_published(None) is None
    assert news_sources._parse_iso_published("") is None
    assert news_sources._parse_iso_published("not a date") is None


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


def test_fetch_perigon(requests_mock, monkeypatch):
    # NOTE: Perigon's real response shape is unverified (no API key available
    # to test against the live service — see docs/ai-news-sources.md). This
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
