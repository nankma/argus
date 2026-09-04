"""
Data Access Layer for global (not per-subscriber) classification caches --
an interest's category mapping, retrieval-query expansion, and a
category's keyness scores.
"""

import json

from storage import get_storage


def get_cached_interest_categories(interests: list[str]) -> dict[str, list[str]]:
    """{interest: categories} for whichever of `interests` already have a
    cached classification -- an interest with no cached mapping is absent
    from the result, not present with an empty list."""
    if not interests:
        return {}
    rows = get_storage().get_cached_interest_categories(interests)
    return {interest: json.loads(categories_json) for interest, categories_json in rows}


def set_interest_categories(interest: str, categories: list[str]) -> None:
    get_storage().set_interest_categories(interest, json.dumps(categories))


def get_interest_query_expansion(interest: str) -> str | None:
    """None means never generated -- callers fall back to the bare
    interest string in that case."""
    return get_storage().get_interest_query_expansion(interest)


def set_interest_query_expansion(interest: str, expansion: str) -> None:
    get_storage().set_interest_query_expansion(interest, expansion)


def set_category_keyness(category: str, scores: dict[str, float]) -> None:
    """Replaces `category`'s entire row set atomically -- news_keyness.py
    recomputes this fresh every ingest cycle."""
    get_storage().set_category_keyness(category, list(scores.items()))


def get_category_keyness(category: str) -> dict[str, float]:
    """{} when nothing has been computed for this category yet -- callers
    treat "no keyness signal" as a normal, expected case."""
    rows = get_storage().get_category_keyness(category)
    return {term: score for term, score in rows}
