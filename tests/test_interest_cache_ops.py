import interest_cache_ops


def test_get_cached_interest_categories_empty_when_nothing_cached(isolated_subscribers_db):
    assert interest_cache_ops.get_cached_interest_categories(["AI"]) == {}


def test_get_cached_interest_categories_empty_input_returns_empty(isolated_subscribers_db):
    assert interest_cache_ops.get_cached_interest_categories([]) == {}


def test_set_and_get_cached_interest_categories(isolated_subscribers_db):
    interest_cache_ops.set_interest_categories("AI", ["AI", "Research"])
    assert interest_cache_ops.get_cached_interest_categories(["AI"]) == {"AI": ["AI", "Research"]}


def test_get_cached_interest_categories_only_returns_known_interests(isolated_subscribers_db):
    interest_cache_ops.set_interest_categories("AI", ["AI"])
    result = interest_cache_ops.get_cached_interest_categories(["AI", "AAOI"])
    assert result == {"AI": ["AI"]}
    assert "AAOI" not in result


def test_set_interest_categories_can_store_empty_list(isolated_subscribers_db):
    # A classifier miss (interest doesn't map to any category) is a real,
    # cacheable result -- distinct from "not yet classified at all".
    interest_cache_ops.set_interest_categories("some obscure ticker", [])
    assert interest_cache_ops.get_cached_interest_categories(["some obscure ticker"]) == {"some obscure ticker": []}


def test_set_interest_categories_upserts(isolated_subscribers_db):
    interest_cache_ops.set_interest_categories("AI", ["AI"])
    interest_cache_ops.set_interest_categories("AI", ["AI", "Research"])
    assert interest_cache_ops.get_cached_interest_categories(["AI"]) == {"AI": ["AI", "Research"]}


def test_get_interest_query_expansion_none_when_never_generated(isolated_subscribers_db):
    assert interest_cache_ops.get_interest_query_expansion("AI coding") is None


def test_set_interest_query_expansion_round_trips(isolated_subscribers_db):
    interest_cache_ops.set_interest_query_expansion("AI coding", "AI systems that assist developers...")
    assert interest_cache_ops.get_interest_query_expansion("AI coding") == "AI systems that assist developers..."


def test_set_interest_query_expansion_upserts(isolated_subscribers_db):
    interest_cache_ops.set_interest_query_expansion("AI coding", "first version")
    interest_cache_ops.set_interest_query_expansion("AI coding", "second version")
    assert interest_cache_ops.get_interest_query_expansion("AI coding") == "second version"


def test_get_category_keyness_empty_when_never_computed(isolated_subscribers_db):
    assert interest_cache_ops.get_category_keyness("AI") == {}


def test_set_category_keyness_round_trips(isolated_subscribers_db):
    interest_cache_ops.set_category_keyness("AI", {"openai": 286.95, "quantum": -31.45})
    assert interest_cache_ops.get_category_keyness("AI") == {"openai": 286.95, "quantum": -31.45}


def test_set_category_keyness_replaces_the_whole_category_not_an_upsert(isolated_subscribers_db):
    """Unlike interest_query_expansions' single-key upsert, a category's
    entire row set is meant to be replaced together each news_ingest.py
    cycle -- a term that scored last cycle but not this one (dropped
    below the df floor, or the category pool changed) must not linger."""
    interest_cache_ops.set_category_keyness("AI", {"openai": 286.95, "stale_term": -5.0})
    interest_cache_ops.set_category_keyness("AI", {"openai": 320.62, "quantum": -31.45})
    assert interest_cache_ops.get_category_keyness("AI") == {"openai": 320.62, "quantum": -31.45}


def test_set_category_keyness_is_scoped_per_category(isolated_subscribers_db):
    interest_cache_ops.set_category_keyness("AI", {"openai": 286.95})
    interest_cache_ops.set_category_keyness("Finance", {"nasdaq": 150.0})
    assert interest_cache_ops.get_category_keyness("AI") == {"openai": 286.95}
    assert interest_cache_ops.get_category_keyness("Finance") == {"nasdaq": 150.0}
