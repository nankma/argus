"""
Pluggable AI-industry news sources for the search_news tool in agent.py.

Each source is a function `fetch(query, max_results) -> list[dict]` returning
a normalized article shape: {"title", "link", "source", "summary",
"published", "published_dt"}. "published" is the raw, source-specific date
string (for display); "published_dt" is that same date parsed into a
timezone-aware datetime (UTC), or None if parsing failed -- added so
callers (search_news, and news_push.py's periodic-digest dedup) can reason
about recency without each doing their own per-source date parsing. See
the incident this responds to: search_news wasn't surfacing "published" to
the model at all, so it had no way to judge freshness or avoid repeating
itself across calls -- see docs/bot-features-plan.md item 5.

SOURCE_REGISTRY lists sources with the env var (if any) required to enable
them; enabled_sources() skips key-gated sources whose key isn't set, so the
tool degrades gracefully instead of erroring. See docs/ai-news-sources.md
for what each source is and how to add a new one.
"""

import calendar
import os
from datetime import datetime, timezone

import feedparser
import requests


def _parse_iso_published(raw: str | None) -> datetime | None:
    """For sources that give an ISO-8601-ish string (HN's created_at,
    NewsAPI/GNews/Perigon's publishedAt/pubDate)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_rss_published(entry) -> datetime | None:
    """For feedparser entries (arXiv, RSS blogs) -- feedparser normalizes
    whatever date format the feed uses into published_parsed (a UTC
    struct_time), which is far more reliable than parsing the raw
    "published" string ourselves."""
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


# --- Free, no-key sources ------------------------------------------------


def fetch_hackernews(query: str, max_results: int = 5) -> list[dict]:
    resp = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": query, "tags": "story", "hitsPerPage": max_results},
        timeout=10,
    )
    resp.raise_for_status()
    return [
        {
            "title": hit.get("title"),
            "link": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "source": "Hacker News",
            "summary": None,
            "published": hit.get("created_at"),
            "published_dt": _parse_iso_published(hit.get("created_at")),
        }
        for hit in resp.json().get("hits", [])
    ]


def fetch_arxiv(query: str = "cat:cs.AI", max_results: int = 5) -> list[dict]:
    resp = requests.get(
        "http://export.arxiv.org/api/query",
        params={
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        },
        timeout=10,
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    return [
        {
            "title": entry.get("title", "").replace("\n", " ").strip(),
            "link": entry.get("link"),
            "source": "arXiv",
            "summary": entry.get("summary", "").replace("\n", " ").strip()[:300],
            "published": entry.get("published"),
            "published_dt": _parse_rss_published(entry),
        }
        for entry in feed.entries
    ]


def _fetch_rss(url: str, source_name: str, max_results: int = 5) -> list[dict]:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    return [
        {
            "title": entry.get("title"),
            "link": entry.get("link"),
            "source": source_name,
            "summary": None,
            "published": entry.get("published"),
            "published_dt": _parse_rss_published(entry),
        }
        for entry in feed.entries[:max_results]
    ]


def fetch_openai_blog(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://openai.com/news/rss.xml", "OpenAI Blog", max_results)


def fetch_huggingface_blog(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://huggingface.co/blog/feed.xml", "Hugging Face Blog", max_results)


def fetch_techcrunch_ai(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch AI", max_results)


def fetch_venturebeat_ai(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://venturebeat.com/category/ai/feed/", "VentureBeat AI", max_results)


def fetch_mit_tech_review(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.technologyreview.com/feed/", "MIT Technology Review", max_results)


# --- Key-gated sources (skipped unless the env var below is set) --------


def fetch_newsapi(query: str, max_results: int = 5) -> list[dict]:
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": query,
            "sortBy": "publishedAt",
            "pageSize": max_results,
            "apiKey": os.environ["NEWSAPI_API_KEY"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    return [
        {
            "title": a.get("title"),
            "link": a.get("url"),
            "source": (a.get("source") or {}).get("name", "NewsAPI"),
            "summary": a.get("description"),
            "published": a.get("publishedAt"),
            "published_dt": _parse_iso_published(a.get("publishedAt")),
        }
        for a in resp.json().get("articles", [])
    ]


def fetch_gnews(query: str, max_results: int = 5) -> list[dict]:
    resp = requests.get(
        "https://gnews.io/api/v4/search",
        params={"q": query, "lang": "en", "max": max_results, "apikey": os.environ["GNEWS_API_KEY"]},
        timeout=10,
    )
    resp.raise_for_status()
    return [
        {
            "title": a.get("title"),
            "link": a.get("url"),
            "source": (a.get("source") or {}).get("name", "GNews"),
            "summary": a.get("description"),
            "published": a.get("publishedAt"),
            "published_dt": _parse_iso_published(a.get("publishedAt")),
        }
        for a in resp.json().get("articles", [])
    ]


def fetch_perigon(query: str, max_results: int = 5) -> list[dict]:
    resp = requests.get(
        "https://api.perigon.io/v1/all",
        params={"q": query, "size": max_results, "apiKey": os.environ["PERIGON_API_KEY"]},
        timeout=10,
    )
    resp.raise_for_status()
    return [
        {
            "title": a.get("title"),
            "link": a.get("url"),
            "source": (a.get("source") or {}).get("domain", "Perigon"),
            "summary": a.get("summary"),
            "published": a.get("pubDate"),
            "published_dt": _parse_iso_published(a.get("pubDate")),
        }
        for a in resp.json().get("articles", [])
    ]


# --- Registry -------------------------------------------------------------

# (name, fetch_fn, required_env_var_or_None)
SOURCE_REGISTRY = [
    ("hackernews", fetch_hackernews, None),
    ("arxiv", fetch_arxiv, None),
    ("openai_blog", fetch_openai_blog, None),
    ("huggingface_blog", fetch_huggingface_blog, None),
    ("techcrunch_ai", fetch_techcrunch_ai, None),
    ("venturebeat_ai", fetch_venturebeat_ai, None),
    ("mit_tech_review", fetch_mit_tech_review, None),
    ("newsapi", fetch_newsapi, "NEWSAPI_API_KEY"),
    ("gnews", fetch_gnews, "GNEWS_API_KEY"),
    ("perigon", fetch_perigon, "PERIGON_API_KEY"),
]


def enabled_sources() -> list[tuple[str, callable]]:
    """(name, fetch_fn) pairs usable right now: always-on free sources, plus
    key-gated ones whose required env var is set."""
    return [
        (name, fn)
        for name, fn, required_env in SOURCE_REGISTRY
        if required_env is None or os.environ.get(required_env)
    ]
