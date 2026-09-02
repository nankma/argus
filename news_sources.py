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

RSS sources are configured via Settings (news_source.rss in settings.yml),
not hardcoded here -- see _rss_sources_from_settings. Only the sources with
real per-source logic beyond "a URL and a display name" (hackernews, arxiv,
and the three API-key-gated sources) are still plain Python functions.
"""

import calendar
import re
from datetime import datetime, timezone

import feedparser
import requests
from opentelemetry import trace

from app_settings import get_settings
from trailsign import SettingsError

# Self-identifying, not a fake browser -- some feeds (TechRadar, confirmed
# live) return 403 to the bare `python-requests/x.x` default User-Agent but
# accept a real one; the honest fix is to say who we are, not to impersonate
# a browser. Applied to every source for consistency, not just the one that
# needed it -- a future source hitting the same block shouldn't need its own
# special case.
_USER_AGENT = "Mozilla/5.0 (compatible; ArgusNewsBot/1.0; +https://github.com/nankma/argus)"
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}


# Query-string parameters whose value is a credential. requests puts the full
# request URL into an HTTPError's message, and news_ingest.py logs that
# exception straight to stdout -- i.e. into `docker logs`, unredacted, on
# every failed fetch.
#
# Real incident, 2026-08-19: a routine check of the ingestion logs surfaced
# GNews's and Perigon's live API keys in plaintext, from a 400 and a 403
# respectively. Not a one-off mistake -- systematic, and it had been
# happening on every error since these sources were added. Both keys were
# rotated. traced_fetch's OpenTelemetry span carried the same value into
# the telemetry backend (Phoenix at the time; Logfire now).
_SECRET_QUERY_PARAM_RE = re.compile(r"((?:api[-_]?key|apikey|token)=)[^&\s]+", re.IGNORECASE)


def _redact(text: object) -> str:
    """Strips credential values out of anything about to be logged."""
    return _SECRET_QUERY_PARAM_RE.sub(r"\1<redacted>", str(text))


def _raise_for_status(resp: requests.Response) -> None:
    """requests.raise_for_status() with the credential stripped from the
    error message. Use this instead of resp.raise_for_status() for any
    source whose auth travels in the query string."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise requests.HTTPError(_redact(exc)) from None


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


def fetch_hackernews(query: str, max_results: int = 5, since: datetime | None = None,
                     section: str | None = None) -> list[dict]:
    """`since`, when given, adds Algolia's `numericFilters=created_at_i>X`
    -- confirmed live 2026-08-16 (45 hits in a 6h window for one query, all
    strictly after the cutoff) -- so news_ingest.py can ask for everything
    new since its last pull instead of a flat top-N regardless of how much
    is genuinely new. Omitted (server returns its default top-N) when
    `since` is None, e.g. the first-ever pull or agent.py's search_news,
    which has no "last pull" concept."""
    # `section` replaces the query for scheduled ingestion. HN's own
    # front_page ranking is a better relevance signal than anything a query
    # could express, and it carries no sampling bias toward what subscribers
    # already named.
    if section:
        params = {"tags": section, "hitsPerPage": max_results}
    else:
        params = {"query": query, "tags": "story", "hitsPerPage": max_results}
    if since is not None:
        params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"
    # front_page is a RANKING; search_by_date would re-sort it into
    # chronological order and throw away the only thing it was for.
    endpoint = "search" if section == "front_page" else "search_by_date"
    resp = requests.get(
        f"https://hn.algolia.com/api/v1/{endpoint}",
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


def fetch_arxiv(query: str = "cat:cs.AI", max_results: int = 5, since: datetime | None = None,
                section: str | None = None) -> list[dict]:
    """`since`, when given, appends a `submittedDate:[X TO 9999...]` range
    to the query -- confirmed live 2026-08-16 the syntax works. Note:
    arXiv's own indexing has a real multi-day lag (a plain, unfiltered
    query on 2026-08-16 returned nothing newer than 2026-08-13), so a
    short since-last-pull window (news_ingest's default interval is 4h)
    will often legitimately return nothing -- that's arXiv's real update
    cadence, not a bug, and no worse than before (today's flat top-N cap
    mostly re-fetches the same few papers on a source this slow)."""
    # A section is an arXiv subject class, which is what this archive
    # actually indexes by -- far better than free-text search, which found
    # only 36 quantum and 6 optics articles for subscribers who follow
    # exactly those topics.
    search_query = f"cat:{section}" if section else query
    if since is not None:
        search_query = f"{search_query} AND submittedDate:[{since.strftime('%Y%m%d%H%M')} TO 99991231235959]"
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


def _news_source_api_key(name: str, required: bool = False) -> str | None:
    """Resolves news_source.<name>.api-key. Two call shapes:

    required=False (the gating check in enabled_sources()) -- None
    whether the block is omitted entirely from settings.yml OR present
    with an unresolvable credential (e.g. its env var unset). Both mean
    "this optional source isn't configured right now," never a crash --
    unlike models.*/storage.*'s required=True keys, os.environ.get()
    never raised for these three either, and that's a real behavior this
    migration keeps: a deployer without a NewsAPI/GNews/Perigon key gets
    that source silently skipped, not a startup failure.

    required=True (inside each fetch function, e.g. fetch_perigon) --
    raises SettingsError if actually called without a configured key.
    enabled_sources() should have already gated this out; if it's called
    anyway, that's a real bug worth failing loudly on, matching the old
    os.environ["KEY"] bracket-access behavior (KeyError, not a silent
    None passed to the request).

    Resolved fresh on every call, not cached at import time -- same
    "live" semantics os.environ.get()/os.environ[...] always had, and
    what lets tests swap Settings without needing to know this module
    imported at some point in the past."""
    path = f"news_source.{name}.api-key"
    if required:
        return get_settings().resolved(path, required=True)
    try:
        return get_settings().resolved(path, default=None)
    except SettingsError:
        return None


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


# RSS sources are pure data (a URL and a display name -- see _fetch_rss
# above, every one of them is query-less and returns the same latest-N
# regardless of what was asked), so they live in Settings
# (news_source.rss, a list of {key, display_name, url}) instead of one
# hardcoded function per feed. A deployer adds/removes/edits feeds by
# editing their own settings.yml, no code change -- see settings.yml's
# own news_source.rss section for the current list, grouped the same way
# this file used to group the individual fetch_ functions (AI/ML
# publications, mainstream press business/technology, enterprise IT
# trade press, consumer/gadget press). See docs/current/ai-news-sources.md for
# sources tested and rejected (CNN's feeds are abandoned -- lastBuildDate
# over a year stale; Fortune blocks with 403; Reuters discontinued public
# RSS in 2020).


def _make_rss_fetcher(url: str, display_name: str) -> callable:
    """One closure per configured feed, matching the (query, max_results)
    -> list[dict] shape every other source's fetch function has -- `query`
    is accepted and ignored, same as the old hardcoded fetch_ functions
    (RSS has no query parameter to filter by, see _fetch_rss)."""
    def fetch(query: str = None, max_results: int = 5) -> list[dict]:
        return _fetch_rss(url, display_name, max_results)
    return fetch


def _rss_sources_from_settings() -> list[tuple[str, callable, None, str]]:
    """Builds the RSS portion of SOURCE_REGISTRY from news_source.rss --
    default=[] rather than required=True, since a deployer running with
    zero configured RSS feeds is a legitimate (if sparse) state, not a
    misconfiguration -- same "fails open, doesn't crash" shape as every
    other optional subsystem in this project (e.g. news_embed's embedder).
    Each entry becomes a 4-tuple matching every other SOURCE_REGISTRY row:
    (key, fetch_fn, required_env=None -- RSS never needs a key, source_class="rss")."""
    entries = get_settings().resolved("news_source.rss", default=[])
    return [
        (entry["key"], _make_rss_fetcher(entry["url"], entry["display_name"]), None, "rss")
        for entry in entries
    ]


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


def _newsapi_articles(resp) -> list[dict]:
    """Shared by both NewsAPI endpoints -- /v2/everything for a real search
    and /v2/top-headlines for a section pull. Same response shape."""
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


def fetch_newsapi(query: str, max_results: int = 5, section: str | None = None) -> list[dict]:
    """A `section` switches to /v2/top-headlines, which needs no query at
    all. That matters more here than anywhere else: this source is a
    multilingual aggregator, so an unconstrained query returns whatever
    matches globally. Measured 2026-08-21 -- "AOI" came back half Chinese
    (AOI is heavily covered by the Taiwanese electronics press) plus
    Japanese anime (AOI is also a name), and "Bitcoin" returned
    Spanish-language finance. All 65 cached articles from this source were
    Chinese, against 1 from every other source combined."""
    if section:
        params = {
            "category": section,
            "language": "en",
            "pageSize": max_results,
            "apiKey": _news_source_api_key("newsapi", required=True),
        }
        resp = requests.get("https://newsapi.org/v2/top-headlines",
                            params=params, timeout=10)
        _raise_for_status(resp)
        return _newsapi_articles(resp)
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": query,
            # Pinned to English, matching fetch_gnews. Without it this
            # source returned 65 of 65 articles in Chinese, because
            # news_ingest._queries_for_source rotates through subscriber
            # interest text as the query and several subscribers store
            # theirs in Chinese (機器人科技, 科技財經, 光通訊). NewsAPI
            # obliged; GNews didn't, purely because it had this parameter.
            #
            # Not a cosmetic difference. A monolingual block inside a
            # mostly-English corpus clusters by LANGUAGE rather than
            # subject: those 65 articles formed a 28-strong "hot topic"
            # spanning Taiwanese stocks, optical networking, a Pixel phone
            # review and robot touch sensors, with mean pairwise similarity
            # 0.71 -- and they simultaneously dominated the
            # farthest-from-everything novelty pick, since anything in
            # another script is maximally distant from an English pool.
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": max_results,
            "apiKey": _news_source_api_key("newsapi", required=True),
        },
        timeout=10,
    )
    _raise_for_status(resp)
    return _newsapi_articles(resp)


def fetch_gnews(query: str, max_results: int = 5, since: datetime | None = None,
                section: str | None = None) -> list[dict]:
    """`since`, when given, adds GNews's documented `from` (ISO 8601) date
    filter -- confirmed live 2026-08-16 (30 articles in a 24h window for
    one query, all recent). Note: `max` is capped at 10/request by GNews's
    own free tier regardless of what's asked (docs/current/ai-news-sources.md), so
    news_ingest.py's generous safety cap for time-filterable sources just
    gets silently clamped here, not an error."""
    params = {"lang": "en", "max": max_results, "apikey": _news_source_api_key("gnews", required=True)}
    if section:
        params["topic"] = section
    else:
        params["q"] = query
    if since is not None:
        params["from"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        "https://gnews.io/api/v4/top-headlines" if section else "https://gnews.io/api/v4/search",
        params=params,
        timeout=10,
    )
    _raise_for_status(resp)
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
        params={"q": query, "size": max_results, "apiKey": _news_source_api_key("perigon", required=True)},
        timeout=10,
    )
    _raise_for_status(resp)
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


# The section vocabulary each query-capable source accepts, as an
# alternative to a search query. The values are dictated by each API and
# live here alongside the fetch functions that pass them; the reasoning for
# pulling by section at all is ingestion policy and lives with it, in
# news_ingest._sections_for_source.
#
# Used only by news_ingest's scheduled pulls. agent.py's search_news still
# passes a real user question, which is a legitimate query.
SOURCE_SECTIONS: dict[str, list[str]] = {
    # https://newsapi.org/docs/endpoints/top-headlines -- fixed vocabulary
    "newsapi": ["technology", "business", "science", "health"],
    # https://gnews.io/docs/v4 -- `topic` on /top-headlines
    "gnews": ["technology", "business", "science", "world"],
    # arXiv subject classes. Deliberately includes quant-ph and
    # physics.optics: subscribers follow quantum computing/sensing and
    # optical communications, and free-text search found 36 and 6 articles
    # for those -- too few to cluster -- while the subject classes are
    # exactly the right handle. These are papers, not industry news, so
    # they widen the corpus more than they feed digests.
    "arxiv": ["cs.AI", "cs.LG", "cs.RO", "cs.CR", "quant-ph", "physics.optics"],
    # Algolia's front_page tag is HN's own ranking, which is a better
    # relevance signal than anything a query could express here.
    "hackernews": ["front_page"],
}


# --- Registry -------------------------------------------------------------

# (name, fetch_fn, gate_or_None, source_class)
#
# source_class is descriptive, not behavioral -- enabled_sources() ignores
# it today. It exists because the registry stopped being uniform once
# mainstream press was added: most sources here are "forum" (community
# board) or "api" (real query-based search) are the exception, not the
# rule -- only hackernews/arxiv/newsapi/gnews/perigon actually filter by
# `query`; every "rss" source (news_source.rss in Settings, see
# _rss_sources_from_settings above) returns its latest N items regardless
# of what was asked (see _fetch_rss). That distinction matters for
# anything downstream that assumes a nonzero result means a real topic
# match -- see docs/plans/local-news-cache-plan.md.
#
#   forum -- community-curated discussion board, not edited articles
#   api   -- real query-based search, JSON REST
#   rss   -- standard RSS/Atom feed, query-less (latest N regardless)
#
# Only the non-RSS sources are hardcoded here -- they have real per-source
# logic (hackernews's numeric-id filter, arxiv's date-range param, the
# three api-class sources' auth/query shapes) that doesn't reduce to
# "a URL and a display name" the way every RSS source does. `gate` for
# the three api-key sources is the news_source.<name> key used to build
# the settings path (see _news_source_api_key) -- NOT a resolved value.
# Resolving eagerly and baking the result into this tuple would freeze it
# at import time; keeping the bare name here means enabled_sources()
# re-checks Settings fresh on every call, same "live" semantics
# os.environ.get() always had.
_NON_RSS_SOURCES = [
    ("hackernews", fetch_hackernews, None, "forum"),
    ("arxiv", fetch_arxiv, None, "api"),
    ("newsapi", fetch_newsapi, "newsapi", "api"),
    ("gnews", fetch_gnews, "gnews", "api"),
    ("perigon", fetch_perigon, "perigon", "api"),
]

SOURCE_REGISTRY = _NON_RSS_SOURCES + _rss_sources_from_settings()


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
    key-gated ones whose news_source.<name>.api-key is configured (see
    _news_source_api_key). `include_restricted` additionally excludes
    RESTRICTED_SOURCES when False -- see agent.py's search_news, the only
    caller that ever passes False; every other caller (news_ingest.py)
    keeps the default so it's unaffected. Same 2-tuple shape as before
    source_class was added -- callers and tests all unpack exactly
    (name, fn)."""
    return [
        (name, fn)
        for name, fn, gate, _source_class in SOURCE_REGISTRY
        if (gate is None or _news_source_api_key(gate))
        and (include_restricted or name not in RESTRICTED_SOURCES)
    ]


_tracer = trace.get_tracer(__name__)


def traced_fetch(source_key: str, fetch: callable, query: str, max_results: int,
                 section: str | None = None) -> list[dict]:
    """Wraps one source's fetch call in an OpenTelemetry span, so it shows
    up in the telemetry backend's unified trace view alongside LLM calls
    -- this is the layer openinference-instrumentation-langchain's
    auto-instrumentation can't reach on its own. A plain fetch() here is a
    bare requests.get() invoked from news_ingest.py's scheduled pull loop
    or agent.py's search_news, entirely outside LangChain's tool-calling
    loop; auto-instrumentation only sees LangChain-mediated calls (LLM
    invocations, @tool-decorated tool calls), so without this wrapper
    these fetches are invisible to telemetry regardless of whether it's
    enabled. See docs/plans/local-news-cache-plan.md's "API call
    visibility" section.

    trace.get_tracer() returns a real tracer once agent.py's
    setup_telemetry() has called LogfireLogger.setup() (see
    logfire_logger.py), or a safe no-op tracer if it hasn't
    (LOGFIRE_ENABLED unset, or setup_telemetry() never called at all,
    e.g. in tests) -- this call has no effect either way when tracing
    isn't configured, same "no-op by default" shape as everywhere else
    telemetry touches this project.

    Re-raises on fetch failure after recording it on the span -- callers
    already have their own per-source try/except (news_ingest.py,
    search_news), so error isolation is unchanged; this only adds
    visibility, it doesn't change control flow."""
    with _tracer.start_as_current_span("fetch_source") as span:
        span.set_attribute("source_key", source_key)
        # A section-based pull ignores the query, so recording it would put
        # the same placeholder on every ingestion span and hide which
        # section was actually fetched -- the one thing worth knowing when
        # diagnosing one of these.
        if section is not None:
            span.set_attribute("section", section)
        else:
            span.set_attribute("query", query)
        span.set_attribute("restricted", source_key in RESTRICTED_SOURCES)
        try:
            articles = fetch(query, max_results)
        except Exception as exc:
            # Redacted for the same reason as _raise_for_status: a fetch
            # error's text can carry the request URL, and this attribute is
            # shipped to the telemetry backend and retained there (Logfire's
            # own retention policy, not configured here -- see
            # docs/system-overview.md §C4). Belt-and-braces --
            # _raise_for_status already strips it at the source for the
            # key-gated fetchers, but this is the last point before the value
            # leaves the process.
            span.set_attribute("error", _redact(exc))
            span.set_attribute("article_count", 0)
            raise
        span.set_attribute("article_count", len(articles))
        return articles
