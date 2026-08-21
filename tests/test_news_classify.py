from unittest.mock import MagicMock

import news_classify
import users_db

# The same 13 categories users_db seeds, so these tests exercise the
# real taxonomy without needing a database -- Taxonomy is a parameter
# precisely so this is possible (see its docstring).
TAXONOMY = news_classify.Taxonomy.from_rows(users_db.SEED_CATEGORIES)


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

    result = news_classify.classify_articles(model, articles, TAXONOMY)

    assert result == {0: ["IT", "Hardware", "Finance", "Stock"], 1: ["AI"]}
    model.with_structured_output.assert_called_once_with(news_classify.ClassificationBatch)


def test_classify_articles_empty_input_returns_empty_without_calling_model():
    model = MagicMock()
    assert news_classify.classify_articles(model, [], TAXONOMY) == {}
    model.with_structured_output.assert_not_called()


def test_classify_articles_fails_open_on_model_error():
    model = MagicMock()
    model.with_structured_output.side_effect = RuntimeError("boom")
    result = news_classify.classify_articles(model, [{"title": "x", "summary": None}], TAXONOMY)
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
    result = news_classify.classify_articles(model, articles, TAXONOMY)
    assert result == {1: ["Crypto"]}


def test_seed_taxonomy_covers_the_expected_names():
    """The taxonomy moved from a Literal in this module to the categories
    table (docs/plans/taxonomy-and-admin-plan.md A1/A2). This pins the names
    rather than a count -- the count is now expected to change as categories
    are added, and a test that fails on "14 != 13" reports nothing useful."""
    assert {"Stock", "Robotics", "Antitrust", "Government"} <= set(TAXONOMY.names)


def test_taxonomy_prompt_fragment_lists_every_category_with_its_description():
    """The fragment goes into the classifier prompt verbatim, so a category
    silently missing from it would be one the model is never offered."""
    fragment = TAXONOMY.prompt_fragment()

    for name, description in users_db.SEED_CATEGORIES:
        assert f"- {name}: {description}" in fragment


def test_classify_interests_maps_interest_text_to_categories():
    model = _fake_structured_model(
        news_classify.ClassificationBatch(
            items=[
                news_classify.ArticleCategories(index=0, categories=["Robotics"]),
                news_classify.ArticleCategories(index=1, categories=["Stock", "Hardware"]),
            ]
        )
    )

    result = news_classify.classify_interests(model, ["機器人科技", "AAOI"], TAXONOMY)

    assert result == {"機器人科技": ["Robotics"], "AAOI": ["Stock", "Hardware"]}


def test_classify_interests_omits_an_interest_the_classifier_failed_on():
    """This previously asserted the failed interest came back as [], which
    pinned a real bug: the caller caches whatever it gets, permanently, so a
    failure became an answer that never got retried. An empty mapping also
    matches every article, so the subscriber silently received unfiltered
    news. Omission is what lets the caller tell the two apart."""
    model = _fake_structured_model(news_classify.ClassificationBatch(items=[]))

    result = news_classify.classify_interests(model, ["some obscure ticker"], TAXONOMY)

    assert result == {}


def test_classify_interests_keeps_a_genuinely_empty_classification():
    """The other half of the distinction: the model DID answer, and its
    answer is "no category applies". That is a real result, not a failure,
    and the caller should cache it rather than re-paying for it forever."""
    model = _fake_structured_model(news_classify.ClassificationBatch(
        items=[news_classify.ArticleCategories(index=0, categories=[])]
    ))

    result = news_classify.classify_interests(model, ["some obscure ticker"], TAXONOMY)

    assert result == {"some obscure ticker": []}


def test_classify_interests_empty_input_returns_empty_without_calling_model():
    model = MagicMock()
    assert news_classify.classify_interests(model, [], TAXONOMY) == {}
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

    result = news_classify.classify_articles(model, articles, TAXONOMY)

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

    result = news_classify.classify_articles(model, articles, TAXONOMY)

    # first 50 lost, second 50 kept -- and they carry the caller's indexes
    assert set(result) == set(range(50, 100))


def test_classify_articles_chunk_failure_is_not_silent(capsys):
    """The silent version hid a three-day outage."""
    articles = [{"title": "A", "summary": None}]
    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("boom")
    model = MagicMock()
    model.with_structured_output.return_value = structured

    news_classify.classify_articles(model, articles, TAXONOMY)

    out = capsys.readouterr().out
    assert "news_classify" in out and "failed" in out


def test_classify_articles_handles_a_model_returning_none():
    """A structured-output call can come back None rather than raising --
    tools/claude_cli_model.py's shim documents doing exactly that when a
    reply fails schema validation. classify_articles must treat it the same
    as any other chunk failure: fail open, leave those articles
    uncategorized, and say so rather than going silent. The silent version
    of this hid a three-day outage."""
    class NoneReturningModel:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            return None

    result = news_classify.classify_articles(
        NoneReturningModel(), [{"title": "Some article", "summary": None}], TAXONOMY
    )

    assert result == {}


def test_one_invented_label_does_not_discard_the_rest_of_the_batch():
    """Regression test for a real production failure. The model answered
    "Education" for one article in a 50-article batch; ArticleCategories
    used list[Category], so pydantic rejected the whole ClassificationBatch
    and all 50 articles lost their categories -- including 49 classified
    correctly.

    An LLM occasionally inventing a plausible label is ordinary behaviour,
    not an error. The unknown label is dropped and everything else
    survives."""
    class InventsALabel:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def invoke(self, messages):
            return self.schema.model_validate({
                "items": [
                    {"index": 0, "categories": ["AI", "Software"]},
                    {"index": 1, "categories": ["Research", "Education"]},
                    {"index": 2, "categories": ["Crypto"]},
                ]
            })

    articles = [{"title": f"Article {i}", "summary": None} for i in range(3)]
    result = news_classify.classify_articles(InventsALabel(), articles, TAXONOMY)

    assert result[0] == ["AI", "Software"]
    assert result[1] == ["Research"], "the valid label survives, Education is dropped"
    assert result[2] == ["Crypto"]


def test_an_article_whose_labels_are_all_invalid_gets_an_empty_list():
    """Not a failure -- indistinguishable from the model saying nothing
    applies, which the prompt explicitly allows."""
    class AllInvalid:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def invoke(self, messages):
            return self.schema.model_validate(
                {"items": [{"index": 0, "categories": ["Education", "Sports"]}]}
            )

    result = news_classify.classify_articles(
        AllInvalid(), [{"title": "A", "summary": None}], TAXONOMY
    )

    assert result == {0: []}


def test_generated_prompt_preserves_the_curated_category_order():
    """The seed order is curated, not incidental, and this pins it.

    Ordering the taxonomy by name instead alphabetizes the list, which
    separates Stock from Finance -- and Stock's own description reads
    "distinct from Finance, which covers business news generally", a
    cross-reference that only makes sense when they are adjacent. Moving
    the taxonomy into the database silently introduced exactly that
    reordering (all 13 categories present, identical text, wrong order),
    and it was caught by diffing the generated prompt against the constant
    it replaced rather than by any test passing.
    """
    fragment = TAXONOMY.prompt_fragment()
    lines = [line.split(":")[0].removeprefix("- ") for line in fragment.split("\n")]

    assert lines == [name for name, _ in users_db.SEED_CATEGORIES]
    assert lines.index("Stock") == lines.index("Finance") + 1, (
        "Stock's description cross-references Finance and must follow it"
    )


# --- A3: reporting labels outside the taxonomy ----------------------------


def _model_returning(items):
    class _M:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def invoke(self, messages):
            return self.schema.model_validate({"items": items})

    return _M()


def test_an_unknown_label_is_reported_with_its_example_article():
    """The admin prompt needs an example -- a bare label with no article to
    look at is much harder to decide about. This is why the callback carries
    the article rather than just the name."""
    seen = []
    articles = [{"title": "Stanford launches free AI curriculum", "link": "https://s.edu/a",
                 "summary": None}]

    news_classify.classify_articles(
        _model_returning([{"index": 0, "categories": ["AI", "Education"]}]),
        articles, TAXONOMY, on_unknown_label=lambda label, art: seen.append((label, art)),
    )

    assert len(seen) == 1
    label, article = seen[0]
    assert label == "Education"
    assert article["link"] == "https://s.edu/a"


def test_a_repeated_unknown_label_is_reported_once_per_batch():
    """Once per batch, not once per article. The same gap appearing five
    times in one chunk is one observation of that gap, and counting it five
    times would make a single batch look like a trend."""
    seen = []
    articles = [{"title": f"A{i}", "summary": None} for i in range(3)]

    news_classify.classify_articles(
        _model_returning([{"index": i, "categories": ["Education"]} for i in range(3)]),
        articles, TAXONOMY, on_unknown_label=lambda label, art: seen.append(label),
    )

    assert seen == ["Education"]


def test_known_labels_are_never_reported():
    seen = []
    news_classify.classify_articles(
        _model_returning([{"index": 0, "categories": ["AI", "Robotics"]}]),
        [{"title": "A", "summary": None}], TAXONOMY,
        on_unknown_label=lambda label, art: seen.append(label),
    )
    assert seen == []


def test_an_out_of_range_index_does_not_lose_the_batch():
    """The index comes from the model, so a malformed reply can point past
    the end of the chunk. That must not take down classification for the
    articles that were fine."""
    seen = []
    result = news_classify.classify_articles(
        _model_returning([
            {"index": 0, "categories": ["AI"]},
            {"index": 99, "categories": ["Education"]},
        ]),
        [{"title": "A", "summary": None}], TAXONOMY,
        on_unknown_label=lambda label, art: seen.append((label, art)),
    )

    assert result[0] == ["AI"]
    assert seen == [("Education", {})], "reported with no example rather than crashing"


def test_classification_works_without_a_callback():
    """on_unknown_label is optional -- classify_interests doesn't pass one."""
    result = news_classify.classify_articles(
        _model_returning([{"index": 0, "categories": ["AI", "Education"]}]),
        [{"title": "A", "summary": None}], TAXONOMY,
    )
    assert result == {0: ["AI"]}


def test_a_malformed_entry_does_not_block_a_later_good_example():
    """Regression test. `rejected` used to gate both the log-line dedup and
    the callback, so whichever entry mentioned a label FIRST won. A
    malformed one (index past the end, no article) arriving before a valid
    one meant the admin got the label with no example, even though a
    perfectly good example was in the same batch.

    The suite was green with that bug: the existing out-of-range test
    happened to use the other ordering."""
    seen = []
    articles = [{"title": "Stanford AI curriculum", "link": "https://s.edu/a", "summary": None}]

    news_classify.classify_articles(
        _model_returning([
            {"index": 99, "categories": ["Education"]},   # malformed, comes first
            {"index": 0, "categories": ["Education"]},    # the real example
        ]),
        articles, TAXONOMY, on_unknown_label=lambda label, art: seen.append((label, art)),
    )

    assert len(seen) == 1, "still reported once per batch"
    assert seen[0][1].get("link") == "https://s.edu/a"


def test_a_label_seen_only_on_a_malformed_entry_is_still_reported():
    """A sighting with no example is still evidence. Dropping it would be
    worse than showing the admin a label with nothing attached."""
    seen = []
    news_classify.classify_articles(
        _model_returning([{"index": 99, "categories": ["Education"]}]),
        [{"title": "A", "summary": None}], TAXONOMY,
        on_unknown_label=lambda label, art: seen.append((label, art)),
    )
    assert seen == [("Education", {})]


# --- interest normalization -----------------------------------------------


def _echo_model(english):
    class _M:
        seen = None
        def with_structured_output(self, schema):
            self.schema = schema
            return self
        def invoke(self, messages):
            _M.seen = messages[-1]["content"]
            return self.schema.model_validate({"reasoning": "r", "english": english})
    return _M()


def test_normalize_interest_returns_the_english_label():
    assert news_classify.normalize_interest(_echo_model("Optical Communications"),
                                            "光通訊") == "Optical Communications"


def test_peer_interests_are_sent_as_disambiguation_context():
    """An ambiguous ticker expanded blind picks the wrong company, and is
    then worse than not expanding: "AOI" came back as "Africa Oil Corp" on
    one run and "Applied Optoelectronics" on the next -- two different
    wrong answers to the same input. The subscriber's other interests
    resolve it, and they cost nothing to include."""
    model = _echo_model("Automated Optical Inspection")

    news_classify.normalize_interest(model, "AOI", alongside=["AAOI", "semiconductors"])

    assert "AAOI" in type(model).seen and "semiconductors" in type(model).seen


def test_no_peers_means_no_context_paragraph():
    model = _echo_model("Robotics")
    news_classify.normalize_interest(model, "機器人科技")
    assert "also follows" not in type(model).seen


def test_a_failed_normalization_returns_none_so_the_caller_keeps_the_original():
    """A stored interest that searches badly beats one that silently
    wasn't saved."""
    class Boom:
        def with_structured_output(self, schema):
            return self
        def invoke(self, messages):
            raise RuntimeError("model down")

    assert news_classify.normalize_interest(Boom(), "光通訊") is None


def test_empty_interest_text_is_rejected_without_calling_the_model():
    class NeverCalled:
        def with_structured_output(self, schema):
            raise AssertionError("should not reach the model")

    assert news_classify.normalize_interest(NeverCalled(), "   ") is None
