"""
Data Access Layer for the daily call budget on rate-limited news sources
-- see docs/plans/local-news-cache-plan.md's Perigon/NewsAPI worked
examples.
"""

from storage import get_storage


def try_consume_api_budget(source: str, daily_cap: int, today: str) -> bool:
    """Returns True and records the call if `source` is still under
    `daily_cap` for `today`; False (without incrementing) if the cap is
    already reached. `today` is passed in so callers/tests control the
    date deterministically."""
    return get_storage().try_consume_api_budget(source, daily_cap, today)


def record_api_call(source: str, today: str) -> None:
    """Records one call WITHOUT checking or enforcing any cap -- for
    callers with usage visibility but no scheduled-pull budget (e.g.
    agent.py's search_news). Shares the same rows as
    try_consume_api_budget."""
    get_storage().record_api_call(source, today)


def get_api_budget_history(source: str, limit: int = 30) -> list[dict]:
    rows = get_storage().get_api_budget_history(source, limit)
    return [{"date": date, "count": count} for date, count in rows]


def get_total_api_calls(source: str) -> int:
    return get_storage().get_total_api_calls(source)
