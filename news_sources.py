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
itself across calls -- see docs/plans/bot-features-plan.md item 5.

SOURCE_REGISTRY lists sources with the env var (if any) required to enable
them; enabled_sources() skips key-gated sources whose key isn't set, so the
tool degrades gracefully instead of erroring. See docs/current/ai-news-sources.md
for what each source is and how to add a new one.
"""

import calendar
import os
import re
from datetime import datetime, timezone

import feedparser
import requests
from opentelemetry import trace

# Self-identifying, not a fake browser -- some feeds (TechRadar, confirmed
# live) return 403 to the bare `python-requests/x.x` default User-Agent but
# accept a real one; the honest fix is to say who we are, not to impersonate
# a browser. Applied to every source for consistency, not just the one that
# needed it -- a future source hitting the same block shouldn't need its own
# special case.
_USER_AGENT = "Mozilla/5.0 (compatible; ArgusNewsBot/1.0; +https://github.com/nankma/argus)"
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}


def _parse_iso_published(raw: str | None) -> datetime | None:
    """For sources that give an ISO-8601-ish string (HN's created_at,
    NewsAPI/GNews/Perigon's publishedAt/pubDate).

    Real incident, 2026-08-14: a source returning a timestamp with no UTC
    offset at all (e.g. "2026-08-13T22:00:00", no "Z", no "+00:00") made
    `datetime.fromisoformat` return a naive datetime, silently breaking
    this function's documented "always timezone-aware" contract. That
    naive value then crashed every news_push.py cycle for two real
    subscribers with `TypeError: can't compare offset-naive and
    offset-aware datetimes` (published_dt <= since, where since is
    always aware) -- not an occasional glitch, a deterministic failure on
    every single tick once that source had a new article. Fixed by
    assuming UTC for any parse that comes back naive, same as this
    function already does explicitly for "Z"."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_rss_published(entry) -> datetime | None:
    """For feedparser entries (arXiv, RSS blogs) -- feedparser normalizes
    whatever date format the feed uses into published_parsed (a UTC
    struct_time), which is far more reliable than parsing the raw
    "published" string ourselves."""
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_summary(raw: str | None, max_len: int = 300) -> str | None:
    """feedparser's entry.summary carries the feed's own <description>,
    which several sources give as a real lede paragraph (confirmed live:
    Guardian gives ~1200 chars of actual editorial context, MarketWatch a
    full sentence explaining *why* a stock moved) -- previously discarded
    outright by _fetch_rss hardcoding summary=None, so the model only ever
    saw a bare title. Some feeds embed raw HTML (<p>, <a href>) in the
    description; stripped here since it's read by the model, not rendered.
    Returns None (not "") when the feed genuinely has nothing, so downstream
    code can still tell "no summary" from "empty after cleaning" apart."""
    if not raw:
        return None
    text = _HTML_TAG_RE.sub("", raw).replace("\n", " ").strip()
    return text[:max_len] if text else None


# --- Free, no-key sources ------------------------------------------------


def fetch_hackernews(query: str, max_results: int = 5, since: datetime | None = None) -> list[dict]:
    """`since`, when given, adds Algolia's `numericFilters=created_at_i>X`
    -- confirmed live 2026-08-16 (45 hits in a 6h window for one query, all
    strictly after the cutoff) -- so news_ingest.py can ask for everything
    new since its last pull instead of a flat top-N regardless of how much
    is genuinely new. Omitted (server returns its default top-N) when
    `since` is None, e.g. the first-ever pull or agent.py's search_news,
    which has no "last pull" concept."""
    params = {"query": query, "tags": "story", "hitsPerPage": max_results}
    if since is not None:
        params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"
    resp = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params=params,
        timeout=10,
        headers=_REQUEST_HEADERS,
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


def fetch_arxiv(query: str = "cat:cs.AI", max_results: int = 5, since: datetime | None = None) -> list[dict]:
    """`since`, when given, appends a `submittedDate:[X TO 9999...]` range
    to the query -- confirmed live 2026-08-16 the syntax works. Note:
    arXiv's own indexing has a real multi-day lag (a plain, unfiltered
    query on 2026-08-16 returned nothing newer than 2026-08-13), so a
    short since-last-pull window (news_ingest's default interval is 4h)
    will often legitimately return nothing -- that's arXiv's real update
    cadence, not a bug, and no worse than before (today's flat top-N cap
    mostly re-fetches the same few papers on a source this slow)."""
    search_query = query
    if since is not None:
        search_query = f"{query} AND submittedDate:[{since.strftime('%Y%m%d%H%M')} TO 99991231235959]"
    resp = requests.get(
        "http://export.arxiv.org/api/query",
        params={
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        },
        timeout=10,
        headers=_REQUEST_HEADERS,
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
    resp = requests.get(url, timeout=10, headers=_REQUEST_HEADERS)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    return [
        {
            "title": entry.get("title"),
            "link": entry.get("link"),
            "source": source_name,
            "summary": _clean_summary(entry.get("summary")),
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


# --- Mainstream press: Business/Finance sections -------------------------
# All query-less (see _fetch_rss note above) -- added 2026-08-13 per a real
# gap: a subscriber asked about a specific company (AAOI) and no source in
# the registry covered anything outside AI-industry press. See
# docs/current/ai-news-sources.md for the sources tested and rejected (CNN's feeds
# are abandoned -- lastBuildDate over a year stale; CNBC and Fortune block
# with 403; Reuters discontinued public RSS in 2020).


def fetch_bbc_business(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("http://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business", max_results)


def fetch_guardian_business(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.theguardian.com/business/rss", "The Guardian Business", max_results)


def fetch_marketwatch(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss(
        "https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch", max_results
    )


def fetch_economist_business(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.economist.com/business/rss.xml", "The Economist (Business)", max_results)


def fetch_nikkei_asia(query: str = None, max_results: int = 5) -> list[dict]:
    """RDF/RSS1.0, not RSS2.0 -- feedparser normalizes it the same way, but
    worth noting since a naive '<item>' string search (rather than
    feedparser) would wrongly read this feed as empty."""
    return _fetch_rss("https://asia.nikkei.com/rss/feed/nar", "Nikkei Asia", max_results)


# --- Mainstream press: Technology sections --------------------------------


def fetch_bbc_technology(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("http://feeds.bbci.co.uk/news/technology/rss.xml", "BBC Technology", max_results)


def fetch_guardian_technology(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.theguardian.com/technology/rss", "The Guardian Technology", max_results)


def fetch_economist_tech(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss(
        "https://www.economist.com/science-and-technology/rss.xml",
        "The Economist (Science & Technology)",
        max_results,
    )


def fetch_wired_business(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.wired.com/feed/category/business/latest/rss", "Wired Business", max_results)


# --- Enterprise/industry IT trade press -----------------------------------


def fetch_the_register(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.theregister.com/headlines.atom", "The Register", max_results)


def fetch_computerworld(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.computerworld.com/index.rss", "Computerworld", max_results)


# --- Consumer/gadget tech press -------------------------------------------


def fetch_zdnet(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.zdnet.com/news/rss.xml", "ZDNet", max_results)


def fetch_engadget(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.engadget.com/rss.xml", "Engadget", max_results)


def fetch_techradar(query: str = None, max_results: int = 5) -> list[dict]:
    return _fetch_rss("https://www.techradar.com/rss", "TechRadar", max_results)


# --- Key-gated sources (skipped unless the env var below is set) --------
# Deliberately NO `since` param on fetch_newsapi/fetch_perigon, unlike
# fetch_hackernews/fetch_arxiv/fetch_gnews above -- news_ingest.py still
# gets "everything since last pull" for these via a client-side filter on
# published_dt instead (see its module docstring), which sidesteps two
# real findings from live-testing this 2026-08-16:
#   - NewsAPI's free "Developer" tier has an undocumented ~24-36h article
#     delay -- `from=<24h ago>` returned 0 results live, `from=<36h ago>`
#     returned 380. Since news_ingest.py pulls NewsAPI once every 24h
#     (_SOURCE_INTERVAL_HOURS), a server-side `from=last_pulled_at` would
#     frequently return nothing at all -- worse than today's flat top-N,
#     not better. Client-side filtering has no such failure mode: it just
#     takes whatever NewsAPI's own delayed index currently has and keeps
#     what's new, so a delayed article surfaces on whichever later cycle
#     it becomes available rather than being asked for and missed.
#   - Perigon's date-filter behavior is simply unverified (no API key
#     available to test against the live service, same caveat as its
#     response-shape mapping below) -- not worth trusting an unconfirmed
#     server-side param when the client-side filter works regardless.


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


def fetch_gnews(query: str, max_results: int = 5, since: datetime | None = None) -> list[dict]:
    """`since`, when given, adds GNews's documented `from` (ISO 8601) date
    filter -- confirmed live 2026-08-16 (30 articles in a 24h window for
    one query, all recent). Note: `max` is capped at 10/request by GNews's
    own free tier regardless of what's asked (docs/current/ai-news-sources.md), so
    news_ingest.py's generous safety cap for time-filterable sources just
    gets silently clamped here, not an error."""
    params = {"q": query, "lang": "en", "max": max_results, "apikey": os.environ["GNEWS_API_KEY"]}
    if since is not None:
        params["from"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        "https://gnews.io/api/v4/search",
        params=params,
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

# (name, fetch_fn, required_env_var_or_None, source_class)
#
# source_class is descriptive, not behavioral -- enabled_sources() ignores
# it today. It exists because the registry stopped being uniform once
# mainstream press was added: most sources here are "forum" (community
# board) or "api" (real query-based search) are the exception, not the
# rule -- of 21 sources below, only hackernews/arxiv/newsapi/gnews/perigon
# actually filter by `query`; every "rss" source below returns its latest
# N items regardless of what was asked (see _fetch_rss). That distinction
# matters for anything downstream that assumes a nonzero result means a
# real topic match -- see docs/plans/local-news-cache-plan.md.
#
#   forum -- community-curated discussion board, not edited articles
#   api   -- real query-based search, JSON REST
#   rss   -- standard RSS/Atom feed, query-less (latest N regardless)
SOURCE_REGISTRY = [
    ("hackernews", fetch_hackernews, None, "forum"),
    ("arxiv", fetch_arxiv, None, "api"),
    ("openai_blog", fetch_openai_blog, None, "rss"),
    ("huggingface_blog", fetch_huggingface_blog, None, "rss"),
    ("techcrunch_ai", fetch_techcrunch_ai, None, "rss"),
    ("venturebeat_ai", fetch_venturebeat_ai, None, "rss"),
    ("mit_tech_review", fetch_mit_tech_review, None, "rss"),
    ("bbc_business", fetch_bbc_business, None, "rss"),
    ("bbc_technology", fetch_bbc_technology, None, "rss"),
    ("guardian_business", fetch_guardian_business, None, "rss"),
    ("guardian_technology", fetch_guardian_technology, None, "rss"),
    ("marketwatch", fetch_marketwatch, None, "rss"),
    ("economist_business", fetch_economist_business, None, "rss"),
    ("economist_tech", fetch_economist_tech, None, "rss"),
    ("nikkei_asia", fetch_nikkei_asia, None, "rss"),
    ("wired_business", fetch_wired_business, None, "rss"),
    ("the_register", fetch_the_register, None, "rss"),
    ("computerworld", fetch_computerworld, None, "rss"),
    ("zdnet", fetch_zdnet, None, "rss"),
    ("engadget", fetch_engadget, None, "rss"),
    ("techradar", fetch_techradar, None, "rss"),
    ("newsapi", fetch_newsapi, "NEWSAPI_API_KEY", "api"),
    ("gnews", fetch_gnews, "GNEWS_API_KEY", "api"),
    ("perigon", fetch_perigon, "PERIGON_API_KEY", "api"),
]


# Sources gated behind per-user access, on top of the env-var gate above --
# not because they're technically different (they're plain "api"-class
# sources like GNews), but because their real-world usage is constrained
# in ways that don't scale to every caller of search_news: NewsAPI's free
# tier is documented as development/testing only, not production
# (docs/current/ai-news-sources.md), and Perigon's 150/month budget is already
# fully spoken for by news_ingest.py's own scheduled pulls (3/day, see
# docs/plans/local-news-cache-plan.md) -- search_news calling them too, on every
# matching on-demand query from every user, would exhaust both almost
# immediately. GNews is deliberately not here: its 100/day budget has
# real headroom beyond what news_ingest.py alone uses.
RESTRICTED_SOURCES = {"newsapi", "perigon"}


def enabled_sources(include_restricted: bool = True) -> list[tuple[str, callable]]:
    """(name, fetch_fn) pairs usable right now: always-on free sources, plus
    key-gated ones whose required env var is set. `include_restricted`
    additionally excludes RESTRICTED_SOURCES when False -- see
    agent.py's search_news, the only caller that ever passes False; every
    other caller (news_ingest.py) keeps the default so it's unaffected.
    Same 2-tuple shape as before source_class was added -- callers and
    tests all unpack exactly (name, fn)."""
    return [
        (name, fn)
        for name, fn, required_env, _source_class in SOURCE_REGISTRY
        if (required_env is None or os.environ.get(required_env))
        and (include_restricted or name not in RESTRICTED_SOURCES)
    ]


_tracer = trace.get_tracer(__name__)


def traced_fetch(source_key: str, fetch: callable, query: str, max_results: int) -> list[dict]:
    """Wraps one source's fetch call in an OpenTelemetry span, so it shows
    up in Phoenix's unified trace view alongside LLM calls -- this is the
    layer openinference-instrumentation-langchain's auto-instrumentation
    can't reach on its own. A plain fetch() here is a bare requests.get()
    invoked from news_ingest.py's scheduled pull loop or agent.py's
    search_news, entirely outside LangChain's tool-calling loop; auto-
    instrumentation only sees LangChain-mediated calls (LLM invocations,
    @tool-decorated tool calls), so without this wrapper these fetches are
    invisible to Phoenix regardless of whether tracing is enabled. See
    docs/plans/local-news-cache-plan.md's "API call visibility" section.

    trace.get_tracer() returns a real tracer once agent.py's
    setup_telemetry() has called phoenix.otel.register(), or a safe no-op
    tracer if it hasn't (PHOENIX_ENABLED unset, or register() never
    called at all, e.g. in tests) -- this call has no effect either way
    when tracing isn't configured, same "no-op by default" shape as
    everywhere else telemetry touches this project.

    Re-raises on fetch failure after recording it on the span -- callers
    already have their own per-source try/except (news_ingest.py,
    search_news), so error isolation is unchanged; this only adds
    visibility, it doesn't change control flow."""
    with _tracer.start_as_current_span("fetch_source") as span:
        span.set_attribute("source_key", source_key)
        span.set_attribute("query", query)
        span.set_attribute("restricted", source_key in RESTRICTED_SOURCES)
        try:
            articles = fetch(query, max_results)
        except Exception as exc:
            span.set_attribute("error", str(exc))
            span.set_attribute("article_count", 0)
            raise
        span.set_attribute("article_count", len(articles))
        return articles
