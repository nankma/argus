import news_keyness


def _article(title="", summary=None, categories=None):
    return {"title": title, "summary": summary, "categories": categories or []}


# --- has_novelty_keyword -----------------------------------------------


def test_has_novelty_keyword_true_on_a_match():
    assert news_keyness.has_novelty_keyword("Major AI model leaks early", None) is True


def test_has_novelty_keyword_is_case_insensitive():
    assert news_keyness.has_novelty_keyword("COMPANY FACES LAWSUIT over data use", None) is True


def test_has_novelty_keyword_checks_summary_too():
    assert news_keyness.has_novelty_keyword("Quarterly update", "Executives warns of headwinds") is True


def test_has_novelty_keyword_false_with_no_match():
    assert news_keyness.has_novelty_keyword("Company ships new feature", "A routine release") is False


def test_has_novelty_keyword_false_on_empty_input():
    assert news_keyness.has_novelty_keyword(None, None) is False


# --- article_nouns (fake tagger -- see conftest.fake_nltk) --------------


def test_article_nouns_keeps_only_noun_tagged_tokens(fake_nltk, monkeypatch):
    # fake_pos_tag tags every token NN, so restrict the fake tagger's
    # output here to prove article_nouns actually filters by tag rather
    # than accepting everything the tokenizer produces.
    monkeypatch.setattr(
        news_keyness, "pos_tag",
        lambda tokens: [(t, "NN" if t in ("robot", "chip") else "VB") for t in tokens],
    )
    result = news_keyness.article_nouns("the robot uses a chip to run", None)
    assert result == {"robot", "chip"}


def test_article_nouns_combines_title_and_summary(fake_nltk):
    result = news_keyness.article_nouns("robot news", "about a chip")
    assert result == {"robot", "news", "about", "chip"}


def test_article_nouns_drops_short_tokens(fake_nltk):
    result = news_keyness.article_nouns("an ai robot", None)
    assert "an" not in result
    assert "ai" not in result  # len 2, dropped by the len(w) > 2 filter
    assert "robot" in result


def test_article_nouns_empty_on_empty_input(fake_nltk):
    assert news_keyness.article_nouns(None, None) == set()
    assert news_keyness.article_nouns("", "") == set()


def test_article_nouns_fails_open_when_tagger_unavailable(monkeypatch):
    monkeypatch.setattr(news_keyness, "pos_tag", None)
    monkeypatch.setattr(news_keyness, "word_tokenize", None)
    assert news_keyness.article_nouns("robot news today", "a chip story") == set()


def test_article_nouns_fails_open_on_tagger_exception(fake_nltk, monkeypatch):
    def boom(tokens):
        raise RuntimeError("tagger died")

    monkeypatch.setattr(news_keyness, "pos_tag", boom)
    assert news_keyness.article_nouns("robot news today", None) == set()


# --- build_noun_index ----------------------------------------------------


def test_build_noun_index_aligns_by_list_index(fake_nltk):
    articles = [_article("robot news"), _article("chip story")]
    doc_terms, global_df = news_keyness.build_noun_index(articles)
    assert doc_terms[0] == {"robot", "news"}
    assert doc_terms[1] == {"chip", "story"}


def test_build_noun_index_counts_document_frequency_not_raw_occurrences(fake_nltk):
    articles = [_article("robot robot robot"), _article("robot chip")]
    _doc_terms, global_df = news_keyness.build_noun_index(articles)
    # "robot" appears in both articles (2 documents), not 4 raw occurrences.
    assert global_df["robot"] == 2
    assert global_df["chip"] == 1


# --- category_keyness ----------------------------------------------------


def _built(articles):
    return news_keyness.build_noun_index(articles)


# --- _signed_g2 (direct unit tests -- category_keyness's callers all use
# pool sizes large enough that the MIN_EXPECTED_COUNT floor never fires,
# so it had no coverage until these) --------------------------------------


def test_signed_g2_none_when_expected_count_too_small():
    """A term at exactly the MIN_GLOBAL_DF floor (so it would otherwise
    be considered "real, not a typo"), in a large corpus with a small
    topic pool -- global_df=5, n_total=1000, n_topic=100 gives
    e_a = (5/1000)*100 = 0.5, below MIN_EXPECTED_COUNT=1.0. Not enough
    expected presence within THIS topic specifically to trust a
    direction, even though the term clears the global floor -- this is
    the exact "don't score what the data can't support" case the
    module docstring describes."""
    result = news_keyness._signed_g2(topic_df=1, global_df_count=5, n_topic=100, n_rest=900)
    assert result is None


def test_signed_g2_real_score_when_expected_count_is_sufficient():
    """Same shape as the module's own measured sanity check (openai
    strongly positive for AI) at unit-test scale: a term present in
    every topic-pool article and never outside it, with enough topic-
    pool size that e_a clears MIN_EXPECTED_COUNT easily."""
    result = news_keyness._signed_g2(topic_df=20, global_df_count=20, n_topic=20, n_rest=20)
    assert result is not None
    assert result > 0


def test_signed_g2_negative_for_a_term_rarer_in_topic_than_overall():
    result = news_keyness._signed_g2(topic_df=1, global_df_count=20, n_topic=20, n_rest=20)
    assert result is not None
    assert result < 0


def test_category_keyness_positive_for_a_topic_defining_term(fake_nltk):
    """A noun that appears in EVERY AI-category article and never
    outside it should score strongly positive -- present far more than
    its overall rate would predict."""
    articles = (
        [_article("openai news update", categories=["AI"]) for _ in range(20)]
        + [_article("finance market report", categories=["Finance"]) for _ in range(20)]
    )
    doc_terms, global_df = _built(articles)
    scores = news_keyness.category_keyness(articles, doc_terms, global_df, "AI")
    assert scores["openai"] > 0


def test_category_keyness_negative_for_a_foreign_term(fake_nltk):
    """A noun that's common globally but essentially absent from the AI
    pool specifically should score strongly negative."""
    articles = (
        [_article("openai news update", categories=["AI"]) for _ in range(20)]
        + [_article("quantum physics finding", categories=["Physics"]) for _ in range(20)]
        # One straggler quantum article that also happens to be AI-tagged.
        + [_article("openai and quantum research", categories=["AI", "Physics"])]
    )
    doc_terms, global_df = _built(articles)
    scores = news_keyness.category_keyness(articles, doc_terms, global_df, "AI")
    assert scores["quantum"] < 0


def test_category_keyness_excludes_terms_below_the_global_df_floor(fake_nltk):
    articles = (
        [_article("openai news", categories=["AI"])] * news_keyness.MIN_GLOBAL_DF
        + [_article("raretypo term", categories=["AI"])]
        + [_article("finance market report", categories=["Finance"])] * 5
    )
    doc_terms, global_df = _built(articles)
    scores = news_keyness.category_keyness(articles, doc_terms, global_df, "AI")
    assert "raretypo" not in scores
    assert "openai" in scores


def test_category_keyness_empty_for_a_category_with_no_articles(fake_nltk):
    articles = [_article("openai news", categories=["AI"])] * 10
    doc_terms, global_df = _built(articles)
    assert news_keyness.category_keyness(articles, doc_terms, global_df, "Robotics") == {}


def test_category_keyness_empty_when_category_is_the_whole_corpus(fake_nltk):
    """n_rest == 0 -- no "outside the topic" half to compare against, so
    nothing is scorable, not a division-by-zero crash."""
    articles = [_article("openai news", categories=["AI"])] * 10
    doc_terms, global_df = _built(articles)
    assert news_keyness.category_keyness(articles, doc_terms, global_df, "AI") == {}


# --- min_term_keyness ----------------------------------------------------


def test_min_term_keyness_picks_the_lowest_scoring_noun(fake_nltk):
    keyness = {"openai": 300.0, "quantum": -30.0, "robot": 5.0}
    result = news_keyness.min_term_keyness("openai robot article", "about quantum too", keyness)
    assert result == (-30.0, "quantum")


def test_min_term_keyness_none_when_no_noun_is_scorable(fake_nltk):
    keyness = {"openai": 300.0}
    assert news_keyness.min_term_keyness("totally unrelated headline", None, keyness) is None


def test_min_term_keyness_none_on_empty_keyness_table(fake_nltk):
    assert news_keyness.min_term_keyness("openai robot article", None, {}) is None
