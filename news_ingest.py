"""
Periodic news ingestion for the local cache -- see
docs/local-news-cache-plan.md. Pulls from every enabled source on its own
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
"""

import time
from datetime import datetime, timezone

import news_cache
import news_classify
import news_sources
import users_db

MAX_RESULTS_PER_SOURCE = 5
DEFAULT_INTERVAL_HOURS = 4
# 1 req/sec is GNews's own documented free-tier limit (docs/ai-news-sources.md);
# used as the general delay between consecutive same-source calls since
# other sources' limits aren't always documented, and this is cheap
# regardless (cycles run every 4h+).
REQUEST_DELAY_SECONDS = 1.1
_DEFAULT_QUERY = "technology"

# Per-source pull interval, in hours -- docs/local-news-cache-plan.md's
# resolved "pull interval" question. Sources absent here use
# DEFAULT_INTERVAL_HOURS (unrestricted sources, and GNews -- its 100/day
# budget comfortably covers 6 pulls/day at the default interval).
_SOURCE_INTERVAL_HOURS = {
    "perigon": 8,  # 3x/day, matching its 150/month budget
    "newsapi": 24,  # 1x/day, matching the individual-use judgment recorded in the plan doc
}

# Daily call caps for budget-tracked sources -- docs/local-news-cache-plan.md's
# Perigon/NewsAPI worked examples. Absent = no cap.
_DAILY_CAPS = {
    "perigon": 3,
    "newsapi": 1,
}

_SOURCE_CLASS = {name: source_class for name, _fn, _env, source_class in news_sources.SOURCE_REGISTRY}


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


def run_ingestion_cycle(model, now: datetime | None = None) -> None:
    """One scheduler tick. Every outcome is printed -- same reasoning as
    news_push.py's run_push_cycle: a silent per-source/per-cycle failure
    was a real incident there (docs/observability-and-debugging.md),
    worth not repeating here."""
    now = now or datetime.now(timezone.utc)

    deleted = news_cache.cleanup_expired(now)
    print(f"[news_ingest] tick at {now.isoformat()}: cleaned up {deleted} expired cache entr{'y' if deleted == 1 else 'ies'}")

    interests = users_db.list_all_interests()
    fetched: list[tuple[str, dict]] = []

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

        queries = _queries_for_source(source_key, now, interests)
        source_articles = 0
        for i, query in enumerate(queries):
            if i > 0:
                # Real incident, first deploy of this job: GNews's
                # documented 1-request/second limit (docs/ai-news-sources.md)
                # returned 429 on 5 of 7 back-to-back queries for the same
                # source in one cycle. A flat delay between consecutive
                # calls to the SAME source is cheap here (cycles run every
                # 4h+, a few extra seconds is nothing) and avoids needing a
                # per-source rate table for limits that aren't always
                # documented up front.
                time.sleep(REQUEST_DELAY_SECONDS)
            try:
                articles = news_sources.traced_fetch(source_key, fetch, query, MAX_RESULTS_PER_SOURCE)
            except Exception as exc:
                print(f"[news_ingest] {source_key}: fetch({query!r}) failed with {exc!r}")
                continue
            for article in articles:
                if article.get("link"):
                    fetched.append((source_key, article))
            source_articles += len(articles)

        users_db.set_source_last_pulled_at(source_key, now)
        print(f"[news_ingest] {source_key}: fetched {source_articles} article(s) across {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}")

    if not fetched:
        print(f"[news_ingest] tick at {now.isoformat()}: nothing new to classify")
        return

    categories_by_index = news_classify.classify_articles(model, [a for _, a in fetched])
    for i, (source_key, article) in enumerate(fetched):
        news_cache.write_article(source_key, article, categories_by_index.get(i, []), now)

    print(f"[news_ingest] tick at {now.isoformat()}: cached {len(fetched)} article(s)")
