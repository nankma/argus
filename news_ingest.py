"""
Periodic news ingestion for the local cache -- see
docs/plans/local-news-cache-plan.md. Pulls from every enabled source on its own
schedule, classifies each cycle's newly-fetched articles in one batched
call, writes them to news_cache.py, and sweeps expired entries -- folded
into the same cycle rather than a separate job, per the plan doc's
resolved "cleanup mechanism" question.

Deliberately separate from news_push.py: that module fetches per-
subscriber, per-interest, to build one subscriber's digest. This module
fetches once, generally, to keep the shared cache stocked -- one pull can
then satisfy every subscriber's queries against it for the next 2 days
(see the plan doc's "Refinement" section on why a scarce source's calls
are worth sharing, not spending per-query).

Query-capable sources (forum/api class -- hackernews, arxiv, newsapi,
gnews, perigon) fetch "everything since this source's last successful
pull" instead of a flat top-N: a flat 5 was a real bottleneck on an
active source, discarding genuinely new articles beyond the first 5
regardless of how much was actually new. RSS-class sources (16 of the 21
registered) keep a flat top-N cap instead -- a plain RSS/Atom feed has no
query or date-range parameter to ask for "since X" at all, it always
returns whatever the publisher currently has in the feed (see
news_sources.py's SOURCE_REGISTRY comment), so there's nothing to switch
to. That cap was raised from 5 to MAX_RESULTS_PER_SOURCE_RSS (200,
2026-08-16) -- 5 was arbitrary and cut real digests down to a handful of
items; 200 comfortably exceeds what any of these feeds actually carry
(most run 20-50 entries), so this is now "take everything the feed has",
not a real limit.

Deduplication against what's already cached (`_existing_cache_links`,
loaded once per cycle from news_cache.read_all()) skips re-classifying a
link this cycle already has a live cache entry for -- necessary once the
RSS cap stopped being small: at 200/feed, most of a cycle's fetch is
usually the *same* items as last cycle (feeds don't turn over that fast),
and without this check every one of them would get re-sent through a real
paid DeepSeek classification call every 4 hours for no reason --
news_cache.write_article's own overwrite-by-link-hash already makes a
redundant write harmless, but a redundant *classification call* isn't
free the way an overwrite is.

Two complementary mechanisms, not one, per source-specific findings from
live-testing this 2026-08-16 (see news_sources.py's fetch_hackernews/
fetch_arxiv/fetch_gnews/fetch_newsapi docstrings and comments for the
per-source detail):
  - Server-side date filter (_SERVER_SIDE_SINCE_SOURCES) -- passed as the
    `since` kwarg, for the 3 sources confirmed live to support it
    correctly. Reduces payload size/wasted budget on rate-limited sources.
  - Client-side filter (applied here, after every query-capable source's
    fetch) -- articles with published_dt at or before the cutoff are
    dropped regardless of what the server did. This is the actually-
    authoritative "new" boundary, and the only mechanism at all for
    newsapi/perigon, whose server-side date param is either
    counterproductive (NewsAPI's free-tier delay) or unverified (Perigon,
    no key to test against).

**The cutoff is the newest article's own published_dt actually seen from
that source (`users_db.get_source_last_article_dt`/
`set_source_last_article_dt`), not when the job last ran
(`last_pulled_at`).** A real design correction, 2026-08-16: using
`last_pulled_at` (wall-clock job time) for this meant a source that
indexes an article with a delay (confirmed live for NewsAPI's free tier --
up to ~36h, see docs/current/ai-news-sources.md) could have that article
silently skipped forever -- `last_pulled_at` keeps advancing every cycle
whether or not anything new was actually found, so a delayed article's
published_dt can fall behind a since-cutoff that already moved past it by
the time the source finally surfaces it. `last_article_dt` only advances
when a newer article is actually observed (the max published_dt among
that cycle's genuinely-new articles), so it can never outrun what's truly
been seen the way a wall-clock timestamp can. See
get_source_last_article_dt's docstring in users_db.py for the full
reasoning.
"""

import functools
import time
from datetime import datetime, timezone

import healthcheck
import news_cache
import news_classify
import news_sources
import users_db

# Raised from 5 to 200 on 2026-08-16 -- see module docstring. Still a real
# cap, not "unlimited": RSS feeds are the publisher's own choice of how
# much to include, but this comfortably exceeds it for every source
# currently registered (checked against docs/current/ai-news-sources.md's
# per-source notes, all well under 200 items/feed).
MAX_RESULTS_PER_SOURCE_RSS = 200
# Safety cap for query-capable sources once they're fetching "since last
# pull" rather than a flat top-N -- the real limit is now the since-filter,
# not this count, but an unbounded ask is still worth guarding against a
# pathological burst after a long outage. Generous on purpose (hackernews
# alone returned 45 hits in a 6h window for one query, live-tested
# 2026-08-16); sources with their own lower hard cap (e.g. GNews's
# 10/request free-tier limit) just clamp silently, no error.
MAX_RESULTS_PER_SOURCE_SINCE_LAST_PULL = 50
DEFAULT_INTERVAL_HOURS = 4
# 1 req/sec is GNews's own documented free-tier limit (docs/current/ai-news-sources.md);
# used as the general delay between consecutive same-source calls since
# other sources' limits aren't always documented, and this is cheap
# regardless (cycles run every 4h+).
REQUEST_DELAY_SECONDS = 1.1
_DEFAULT_QUERY = "technology"

# Per-source pull interval, in hours -- docs/plans/local-news-cache-plan.md's
# resolved "pull interval" question. Sources absent here use
# DEFAULT_INTERVAL_HOURS (unrestricted sources, and GNews -- its 100/day
# budget comfortably covers 6 pulls/day at the default interval).
_SOURCE_INTERVAL_HOURS = {
    "perigon": 8,  # 3x/day, matching its 150/month budget
    "newsapi": 24,  # 1x/day, matching the individual-use judgment recorded in the plan doc
}

# Daily call caps for budget-tracked sources -- docs/plans/local-news-cache-plan.md's
# Perigon/NewsAPI worked examples. Absent = no cap.
_DAILY_CAPS = {
    "perigon": 3,
    "newsapi": 1,
}

_SOURCE_CLASS = {name: source_class for name, _fn, _env, source_class in news_sources.SOURCE_REGISTRY}

# "forum"/"api" classes are the query-capable sources per news_sources.py's
# SOURCE_REGISTRY comment -- "rss" sources have no query or date-range
# parameter at all, so there's nothing to switch to since-based fetching
# for them.
_TIME_FILTERABLE_CLASSES = {"forum", "api"}

# Of the 5 time-filterable sources, only these 3 get the `since` kwarg
# passed through to their fetch function (a server-side date filter) --
# see news_sources.py's fetch_newsapi/fetch_perigon comment for why the
# other two (newsapi, perigon) deliberately don't: a free-tier delay and
# an unverified param, respectively. All 5 still get the client-side
# published_dt filter below regardless -- that's the mechanism actually
# doing the filtering for those two.
_SERVER_SIDE_SINCE_SOURCES = {"hackernews", "arxiv", "gnews"}


def _interval_hours(source_key: str) -> int:
    return _SOURCE_INTERVAL_HOURS.get(source_key, DEFAULT_INTERVAL_HOURS)


def _is_source_due(source_key: str, last_pulled_at: datetime | None, now: datetime) -> bool:
    if last_pulled_at is None:
        return True
    elapsed_hours = (now - last_pulled_at).total_seconds() / 3600
    return elapsed_hours >= _interval_hours(source_key)


def _queries_for_source(source_key: str, now: datetime, interests: list[str]) -> list[str]:
    """RSS-class sources ignore the query entirely (see news_sources.py) --
    one call is enough regardless of what's passed. Capped sources (a
    scarce daily budget) get exactly one call, using a query that rotates
    deterministically through real subscriber interests over time rather
    than a fixed default -- otherwise a source added specifically to cover
    low-profile topics like the AAOI case would never actually search for
    anything specific. Uncapped, query-capable sources (hackernews, arxiv,
    gnews) query once per distinct interest, since their budgets can
    absorb it."""
    if not interests:
        return [_DEFAULT_QUERY]

    source_class = _SOURCE_CLASS.get(source_key)
    if source_class == "rss":
        return [_DEFAULT_QUERY]

    if source_key in _DAILY_CAPS:
        interval_seconds = _interval_hours(source_key) * 3600
        tick_number = int(now.timestamp() // interval_seconds)
        return [interests[tick_number % len(interests)]]

    return interests


def _report_category_proposals(now: datetime) -> None:
    """Prints what the classifier has been asking for that the taxonomy
    doesn't have.

    Deliberately only a log line for now. The threshold that decides when
    this becomes an admin prompt (docs/plans/taxonomy-and-admin-plan.md A4)
    is currently a placeholder of 5-in-30-days picked from a single
    observation of "Education", and choosing it properly needs the real
    distribution first. Accumulating visibly and deciding later beats
    guessing a threshold now and then tuning it against alerts nobody
    trusts."""
    proposals = users_db.count_recent_sightings(now)
    if not proposals:
        return
    ranked = sorted(proposals.items(), key=lambda kv: -kv[1])
    # Truncated because this prints every cycle and the tail is noise: a
    # label seen once is a model slip, and the decision this feeds is about
    # what keeps recurring. The full list is in the table.
    summary = ", ".join(f"{name} x{count}" for name, count in ranked[:8])
    print(f"[news_ingest] taxonomy gaps proposed by the classifier "
          f"(last {users_db.CATEGORY_SIGHTING_RETENTION_DAYS}d): {summary}")


def run_ingestion_cycle(model, now: datetime | None = None) -> None:
    """One scheduler tick. Every outcome is printed -- same reasoning as
    news_push.py's run_push_cycle: a silent per-source/per-cycle failure
    was a real incident there (docs/reference/observability-and-debugging.md),
    worth not repeating here."""
    now = now or datetime.now(timezone.utc)
    # Recorded unconditionally, before any per-source due-check -- this is
    # what healthcheck.py's liveness check reads to answer "is this job
    # still ticking at all", decoupled from whether any individual source
    # happened to be due this tick (every source can legitimately be
    # "not due yet" for hours at a time; that's not the same as the job
    # itself having stopped running).
    users_db.set_source_last_pulled_at(healthcheck.INGEST_TICK_KEY, now)

    deleted = news_cache.cleanup_expired(now)
    print(f"[news_ingest] tick at {now.isoformat()}: cleaned up {deleted} expired cache entr{'y' if deleted == 1 else 'ies'}")
    # Sightings age out on the same tick as the cache, so the threshold
    # question stays "how often recently" rather than "how often ever" --
    # see users_db.CATEGORY_SIGHTING_RETENTION_DAYS.
    users_db.prune_category_sightings(now)
    # Reported here, next to the prune, rather than after classification:
    # both are about accumulated evidence and neither depends on whether
    # this cycle fetched anything. Placing it after the `if not fetched`
    # return meant a quiet cycle pruned the evidence but never showed it.
    _report_category_proposals(now)

    interests = users_db.list_all_interests()
    fetched: list[tuple[str, dict]] = []
    # Loaded once per cycle (after cleanup_expired, so already-expired
    # links are gone and eligible to be re-added) -- skips re-classifying
    # a link this cycle already has a live cache entry for. Matters most
    # for RSS-class sources now that their cap is 200, not 5 (see module
    # docstring): most of a 200-item RSS pull is typically unchanged from
    # last cycle, and without this check every one of them would cost a
    # real, paid DeepSeek classification call every cycle for no reason.
    existing_links = {a["link"] for a in news_cache.read_all() if a.get("link")}
    total_duplicates_skipped = 0

    for source_key, fetch in news_sources.enabled_sources():
        last_pulled_at = users_db.get_source_last_pulled_at(source_key)
        if not _is_source_due(source_key, last_pulled_at, now):
            print(f"[news_ingest] {source_key}: not due yet")
            continue

        daily_cap = _DAILY_CAPS.get(source_key)
        if daily_cap is not None and not users_db.try_consume_api_budget(
            source_key, daily_cap, now.date().isoformat()
        ):
            print(f"[news_ingest] {source_key}: daily budget of {daily_cap} reached, skipping")
            continue

        time_filterable = _SOURCE_CLASS.get(source_key) in _TIME_FILTERABLE_CLASSES
        max_results = MAX_RESULTS_PER_SOURCE_SINCE_LAST_PULL if time_filterable else MAX_RESULTS_PER_SOURCE_RSS
        # The newest article's own published_dt actually seen from this
        # source so far -- NOT last_pulled_at (see module docstring for
        # why that was the wrong cutoff: it advances on wall-clock time
        # regardless of whether anything new was found, which can
        # permanently skip an article a source indexes with a delay).
        last_article_dt = users_db.get_source_last_article_dt(source_key)
        # Server-side date filter for the 3 sources confirmed to support it
        # correctly (see _SERVER_SIDE_SINCE_SOURCES above) -- an efficiency
        # optimization only; the client-side filter below is what's
        # actually authoritative for "new", regardless of whether this
        # fires. None on the first-ever pull (last_article_dt is None) --
        # nothing to filter against yet, so just take the unfiltered top-N
        # up to the safety cap.
        if time_filterable and source_key in _SERVER_SIDE_SINCE_SOURCES and last_article_dt is not None:
            fetch_call = functools.partial(fetch, since=last_article_dt)
        else:
            fetch_call = fetch

        queries = _queries_for_source(source_key, now, interests)
        source_articles = 0
        source_new = 0
        newest_seen_this_cycle = last_article_dt
        for i, query in enumerate(queries):
            if i > 0:
                # Real incident, first deploy of this job: GNews's
                # documented 1-request/second limit (docs/current/ai-news-sources.md)
                # returned 429 on 5 of 7 back-to-back queries for the same
                # source in one cycle. A flat delay between consecutive
                # calls to the SAME source is cheap here (cycles run every
                # 4h+, a few extra seconds is nothing) and avoids needing a
                # per-source rate table for limits that aren't always
                # documented up front.
                time.sleep(REQUEST_DELAY_SECONDS)
            try:
                articles = news_sources.traced_fetch(source_key, fetch_call, query, max_results)
            except Exception as exc:
                print(f"[news_ingest] {source_key}: fetch({query!r}) failed with {exc!r}")
                continue
            if time_filterable and last_article_dt is not None:
                # The actually-authoritative "new" check -- applies
                # regardless of whether a server-side filter ran above, so
                # it's correct even for newsapi/perigon (no server-side
                # filter at all) and even if a server-side filter silently
                # didn't behave as expected. An article with no parseable
                # published_dt is kept rather than dropped -- can't tell if
                # it's new, and news_cache's dedup-by-link-hash makes
                # re-caching a harmless overwrite either way (see
                # news_cache.write_article).
                articles = [a for a in articles if a.get("published_dt") is None or a["published_dt"] > last_article_dt]
            source_articles += len(articles)
            for article in articles:
                link = article.get("link")
                if not link:
                    continue
                if link in existing_links:
                    total_duplicates_skipped += 1
                    continue
                existing_links.add(link)
                fetched.append((source_key, article))
                source_new += 1
                published_dt = article.get("published_dt")
                if published_dt is not None and (newest_seen_this_cycle is None or published_dt > newest_seen_this_cycle):
                    newest_seen_this_cycle = published_dt

        users_db.set_source_last_pulled_at(source_key, now)
        if time_filterable and newest_seen_this_cycle is not None and newest_seen_this_cycle != last_article_dt:
            users_db.set_source_last_article_dt(source_key, newest_seen_this_cycle)
        print(
            f"[news_ingest] {source_key}: fetched {source_articles} article(s) across "
            f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'} -- "
            f"{source_new} new, {source_articles - source_new} already cached"
        )

    if not fetched:
        print(
            f"[news_ingest] tick at {now.isoformat()}: nothing new to classify "
            f"({total_duplicates_skipped} already-cached article(s) skipped)"
        )
        return

    # Loaded once per cycle, not per chunk: the taxonomy must not change
    # underneath a batch, or the prompt and the validation set would
    # disagree about what a valid answer is.
    taxonomy = news_classify.Taxonomy.from_rows(users_db.get_active_categories())
    categories_by_index = news_classify.classify_articles(
        model, [a for _, a in fetched], taxonomy,
        on_unknown_label=lambda label, article: users_db.record_category_sighting(
            label, now, article.get("link"), article.get("title")
        ),
    )
    for i, (source_key, article) in enumerate(fetched):
        news_cache.write_article(source_key, article, categories_by_index.get(i, []), now)

    # The new/duplicate split here is logged specifically so the RSS cap
    # (MAX_RESULTS_PER_SOURCE_RSS) can be tuned later from real data rather
    # than guessed at -- see the module docstring for why 200 was picked.
    print(
        f"[news_ingest] tick at {now.isoformat()}: cached {len(fetched)} new article(s), "
        f"{total_duplicates_skipped} already-cached article(s) skipped"
    )
