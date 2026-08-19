from unittest.mock import MagicMock

import news_classify


def _fake_structured_model(return_value) -> MagicMock:
    fake_structured = MagicMock()
    fake_structured.invoke.return_value = return_value
    model = MagicMock()
    model.with_structured_output.return_value = fake_structured
    return model


def test_classify_articles_maps_index_to_categories():
    model = _fake_structured_model(
        news_classify.ClassificationBatch(
            items=[
                news_classify.ArticleCategories(index=0, categories=["IT", "Hardware", "Finance", "Stock"]),
                news_classify.ArticleCategories(index=1, categories=["AI"]),
            ]
        )
    )
    articles = [
        {"title": "Nvidia in talks to invest in CoreWeave", "summary": "Cloud deal"},
        {"title": "New LLM released", "summary": None},
    ]

    result = news_classify.classify_articles(model, articles)

    assert result == {0: ["IT", "Hardware", "Finance", "Stock"], 1: ["AI"]}
    model.with_structured_output.assert_called_once_with(news_classify.ClassificationBatch)


def test_classify_articles_empty_input_returns_empty_without_calling_model():
    model = MagicMock()
    assert news_classify.classify_articles(model, []) == {}
    model.with_structured_output.assert_not_called()


def test_classify_articles_fails_open_on_model_error():
    model = MagicMock()
    model.with_structured_output.side_effect = RuntimeError("boom")
    result = news_classify.classify_articles(model, [{"title": "x", "summary": None}])
    assert result == {}


def test_classify_articles_handles_missing_index_gracefully():
    """If the model only returns some of the articles, the caller gets
    back only what was classified -- not padded with empty lists."""
    model = _fake_structured_model(
        news_classify.ClassificationBatch(
            items=[news_classify.ArticleCategories(index=1, categories=["Crypto"])]
        )
    )
    articles = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    result = news_classify.classify_articles(model, articles)
    assert result == {1: ["Crypto"]}


def test_categories_list_matches_the_literal_type():
    assert "Stock" in news_classify.CATEGORIES
    assert "Robotics" in news_classify.CATEGORIES
    assert len(news_classify.CATEGORIES) == 13


def test_classify_interests_maps_interest_text_to_categories():
    model = _fake_structured_model(
        news_classify.ClassificationBatch(
            items=[
                news_classify.ArticleCategories(index=0, categories=["Robotics"]),
                news_classify.ArticleCategories(index=1, categories=["Stock", "Hardware"]),
            ]
        )
    )

    result = news_classify.classify_interests(model, ["機器人科技", "AAOI"])

    assert result == {"機器人科技": ["Robotics"], "AAOI": ["Stock", "Hardware"]}


def test_classify_interests_missing_index_defaults_to_empty_list():
    model = _fake_structured_model(news_classify.ClassificationBatch(items=[]))

    result = news_classify.classify_interests(model, ["some obscure ticker"])

    assert result == {"some obscure ticker": []}


def test_classify_interests_empty_input_returns_empty_without_calling_model():
    model = MagicMock()
    assert news_classify.classify_interests(model, []) == {}
    model.with_structured_output.assert_not_called()


def test_classify_articles_chunks_large_batches():
    """Real outage, 2026-08-19: one call per ingestion cycle failed
    all-or-nothing above ~110 articles, leaving 92.8% of the production
    cache uncategorized for three days. Batches are chunked now."""
    articles = [{"title": f"Article {i}", "summary": None} for i in range(120)]
    calls = []

    def fake_invoke(messages):
        # count how many numbered lines this call was asked to classify
        listing = messages[1]["content"]
        n = len(listing.strip().splitlines())
        calls.append(n)
        return news_classify.ClassificationBatch(
            items=[news_classify.ArticleCategories(index=i, categories=["AI"]) for i in range(n)]
        )

    structured = MagicMock()
    structured.invoke.side_effect = fake_invoke
    model = MagicMock()
    model.with_structured_output.return_value = structured

    result = news_classify.classify_articles(model, articles)

    assert len(calls) == 3, f"120 articles at 50/call should be 3 calls, got {calls}"
    assert max(calls) <= news_classify.MAX_ARTICLES_PER_CALL
    # every article classified, and indexes map back to the caller's list
    assert len(result) == 120
    assert set(result) == set(range(120))


def test_classify_articles_one_failed_chunk_does_not_lose_the_others():
    """The blast radius of a failure is one chunk, not the whole cycle."""
    articles = [{"title": f"Article {i}", "summary": None} for i in range(100)]
    call_count = {"n": 0}

    def fake_invoke(messages):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("output token limit")
        n = len(messages[1]["content"].strip().splitlines())
        return news_classify.ClassificationBatch(
            items=[news_classify.ArticleCategories(index=i, categories=["AI"]) for i in range(n)]
        )

    structured = MagicMock()
    structured.invoke.side_effect = fake_invoke
    model = MagicMock()
    model.with_structured_output.return_value = structured

    result = news_classify.classify_articles(model, articles)

    # first 50 lost, second 50 kept -- and they carry the caller's indexes
    assert set(result) == set(range(50, 100))


def test_classify_articles_chunk_failure_is_not_silent(capsys):
    """The silent version hid a three-day outage."""
    articles = [{"title": "A", "summary": None}]
    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("boom")
    model = MagicMock()
    model.with_structured_output.return_value = structured

    news_classify.classify_articles(model, articles)

    out = capsys.readouterr().out
    assert "news_classify" in out and "failed" in out
