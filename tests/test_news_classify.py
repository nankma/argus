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
