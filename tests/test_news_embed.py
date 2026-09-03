import news_embed
from telemetry_providers import Level
from tests.fakes import FakeEmbedder, FakeSpan


def _patch_events_span(monkeypatch):
    span = FakeSpan()
    monkeypatch.setattr(news_embed._events._tracer, "start_as_current_span", lambda name: span)
    return span


def test_embed_texts_returns_one_vector_per_input_in_order():
    result = news_embed.embed_texts(FakeEmbedder(), ["AI chips", "Bitcoin price"])
    assert len(result) == 2
    assert result[0] != result[1]


def test_embed_texts_with_no_embedder_returns_all_none():
    result = news_embed.embed_texts(None, ["AI chips", "Bitcoin price"])
    assert result == [None, None]


def test_embed_texts_with_empty_input_returns_empty_list():
    """The real model2vec raises ValueError on encode([]) -- callers of
    this wrapper are freed from knowing that, but only if empty input is
    actually short-circuited before reaching encode()."""
    assert news_embed.embed_texts(FakeEmbedder(), []) == []


def test_embed_texts_survives_an_encode_failure(monkeypatch):
    class Boom:
        def encode(self, texts):
            raise RuntimeError("model died")

    span = _patch_events_span(monkeypatch)
    result = news_embed.embed_texts(Boom(), ["a", "b", "c"])

    assert result == [None, None, None]
    assert span.attrs["logfire.level_num"] == Level.WARN
    assert span.attrs["batch_size"] == 3
    assert len(span.exceptions) == 1
    assert isinstance(span.exceptions[0], RuntimeError)


def test_embed_one_returns_a_single_vector():
    vec = news_embed.embed_one(FakeEmbedder(), "AI chips")
    assert vec is not None
    assert len(vec) == FakeEmbedder.DIM


def test_embed_one_with_no_embedder_returns_none():
    assert news_embed.embed_one(None, "AI chips") is None


def test_cosine_similarity_of_identical_text_is_near_one():
    embedder = FakeEmbedder()
    v = news_embed.embed_one(embedder, "Nvidia launches new GPU")
    assert news_embed.cosine_similarity(v, v) > 0.99


def test_cosine_similarity_of_overlapping_text_is_high():
    embedder = FakeEmbedder()
    a = news_embed.embed_one(embedder, "Nvidia launches new GPU")
    b = news_embed.embed_one(embedder, "Nvidia unveils new GPU")
    assert news_embed.cosine_similarity(a, b) > 0.5


def test_cosine_similarity_of_unrelated_text_is_low():
    embedder = FakeEmbedder()
    a = news_embed.embed_one(embedder, "Nvidia launches new GPU")
    b = news_embed.embed_one(embedder, "Bitcoin price surges")
    assert news_embed.cosine_similarity(a, b) < 0.1


def test_cosine_similarity_with_a_missing_embedding_is_minimal():
    """-1.0, not a crash and not a false "identical" (which 0.0 could be
    mistaken for, since two orthogonal real vectors also score 0.0) --
    the one value below every real cosine similarity, so a caller that
    forgets to guard against None still treats it as maximally
    dissimilar rather than accidentally matching or crashing."""
    v = news_embed.embed_one(FakeEmbedder(), "AI chips")
    assert news_embed.cosine_similarity(v, None) == -1.0
    assert news_embed.cosine_similarity(None, v) == -1.0
    assert news_embed.cosine_similarity(None, None) == -1.0


def test_build_embedder_returns_none_on_import_failure(monkeypatch):
    """The module-not-startup-fatal contract, exercised without needing
    the real model2vec package to actually be missing."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "model2vec":
            raise ImportError("no module named model2vec")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    span = _patch_events_span(monkeypatch)

    assert news_embed.build_embedder() is None
    assert span.attrs["logfire.level_num"] == Level.ERROR
    assert len(span.exceptions) == 1
    assert isinstance(span.exceptions[0], ImportError)


def test_mean_vector_of_identical_vectors_equals_that_vector():
    embedder = FakeEmbedder()
    v = news_embed.embed_one(embedder, "AI chips")
    centroid = news_embed.mean_vector([v, v, v])
    assert news_embed.cosine_similarity(centroid, v) > 0.999


def test_mean_vector_is_normalized():
    embedder = FakeEmbedder()
    a = news_embed.embed_one(embedder, "Nvidia launches new GPU")
    b = news_embed.embed_one(embedder, "Bitcoin price surges")
    centroid = news_embed.mean_vector([a, b])
    norm = sum(x * x for x in centroid) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_mean_vector_of_empty_list_is_none():
    assert news_embed.mean_vector([]) is None


def test_mean_vector_is_more_similar_to_its_own_members_than_an_outlier():
    embedder = FakeEmbedder()
    cluster = [news_embed.embed_one(embedder, t) for t in
              ["Nvidia launches new GPU", "Nvidia unveils new chip", "AMD releases new GPU"]]
    outlier = news_embed.embed_one(embedder, "Bitcoin price surges")
    centroid = news_embed.mean_vector(cluster)
    for v in cluster:
        assert news_embed.cosine_similarity(v, centroid) > news_embed.cosine_similarity(outlier, centroid)
