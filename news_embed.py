"""
Article/topic embeddings -- the mechanism behind two things:

  - Near-duplicate collapse (news_push.select_candidate_articles): the
    same wire story cached under two links, or two outlets syndicating one
    piece, should count once in a digest and once in a "hot story" count.
  - Offbeat/novelty selection (news_push.select_candidate_articles): a
    handful of slots per topic go to articles that are still ON topic but
    UNLIKE the typical article in it, rather than always the newest N.

Both are described and measured in docs/analysis/cluster-measurements.md.
That document also settles the backend choice, which this module does not
re-litigate: model2vec's `minishlab/potion-base-8M` over fastembed/
sentence-transformers, because the deploy target
(VM.Standard.E2.1.Micro, 420 MB available at measurement time) can't
afford fastembed's 172 MB fixed cost or sentence-transformers' 488 MB,
and TF-IDF was measured to have NEGATIVE separation on this corpus (a
false-positive pair outscored a true match) -- not a weaker option, a
wrong one.

Injectable, same convention as agent.build_model: production code calls
build_embedder() once at startup and threads the result through as a
parameter, so tests substitute a fake with a matching `.encode()`
interface rather than loading the real ~30 MB model or hitting the
network. See tests/fakes.py's FakeEmbedder.

Degrades gracefully everywhere it's consumed. This is an enhancement to
push quality, not something the bot has ever depended on to function --
every caller must treat build_embedder() possibly returning None (import
failed, model files missing, out of memory) as a normal, expected case,
not a startup-fatal one, and every consumer must fall back to its pre-
embedding behavior (recency-only ranking, no near-duplicate check) when
either the embedder or a specific article's embedding is unavailable.
"""

import os

from telemetry import EventLogger, get_event_logger
from telemetry_providers import Level

_events: EventLogger = get_event_logger("argus.news_embed")

# model2vec's own encode() already L2-normalizes its output (confirmed by
# measurement: every vector's norm is 1.0), which is what makes a plain
# dot product the correct cosine similarity -- no renormalization needed
# here or by any caller storing/reading these vectors.
MODEL_NAME = "minishlab/potion-base-8M"

# Baked into the Docker image at build time (see Dockerfile) specifically
# so the container never reaches out to huggingface.co at runtime -- a
# 1-OCPU VM with no guaranteed outbound access to that host must not have
# ingestion silently degrade (or push startup) on a network hiccup for a
# model that's already sitting on disk. HF_HUB_OFFLINE makes a network
# attempt a loud ValueError instead of a slow, silent timeout.
os.environ.setdefault("HF_HUB_OFFLINE", "1")


def build_embedder():
    """Loads the embedding model once, for callers to hold onto and reuse
    -- matches agent.build_model's shape (built once at startup, passed
    as a parameter), not re-loaded per call. Loading takes ~1-2s warm
    (weights already on disk) even though nothing here is on a request's
    critical path.

    Returns None on any failure (import error, missing/corrupt model
    files, HF_HUB_OFFLINE rejecting an unexpected download) rather than
    raising -- see the module docstring on why this must never be
    startup-fatal. Logged at ERROR (not WARN): this degrades embeddings
    for the whole process, not just one call, so it's worth a human
    noticing rather than one more line in a batch of routine failures."""
    try:
        from model2vec import StaticModel
        return StaticModel.from_pretrained(MODEL_NAME)
    except Exception as exc:
        _events.log("embedder_load_failed",
                     f"could not load {MODEL_NAME}, embeddings disabled",
                     level=Level.ERROR, exc=exc)
        return None


def embed_texts(embedder, texts: list[str]) -> list[list[float] | None]:
    """Embeds `texts` in one batch call -- always call this once per batch
    of articles, never once per article, since the fixed per-call cost
    dwarfs the marginal per-item cost (measured: 0.2ms/article marginal
    against an 84 MB / ~1s fixed load, and that load already happened in
    build_embedder -- but even the encode() call itself batches far more
    efficiently than N calls of 1).

    Returns one entry per input text, in order, so a caller can zip() it
    straight back onto its article list. `embedder=None` (build_embedder
    failed) or an empty `texts` list both return an all-None list of the
    right length rather than raising -- model2vec's own encode([]) raises
    ValueError on empty input, which every caller of this function is
    freed from having to know or guard against."""
    if embedder is None or not texts:
        return [None] * len(texts)
    try:
        vectors = embedder.encode(texts)
    except Exception as exc:
        _events.log("embed_batch_failed",
                     {"message": f"encode() failed for a batch of {len(texts)}",
                      "batch_size": len(texts)},
                     level=Level.WARN, exc=exc)
        return [None] * len(texts)
    # Rounded before it ever reaches a caller -- these get stored as a
    # plain YAML float list (news_cache.write_article), where full
    # float32-to-repr precision roughly doubles the field's size for
    # differences (1e-7 range) far below what cosine similarity's
    # thresholds (0.95 for near-duplicates, a median split for the
    # offbeat gate) can even distinguish.
    return [[round(x, 6) for x in v.tolist()] for v in vectors]


def embed_one(embedder, text: str) -> list[float] | None:
    """A single short string -- for embedding a subscriber's interest/topic
    query, which is a few words, not a batch of article titles. Building
    on embed_texts rather than calling encode() directly keeps the same
    None-on-failure contract in exactly one place."""
    return embed_texts(embedder, [text])[0]


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Plain dot product, not `a @ b` via numpy -- these vectors are 256
    floats and callers compare a handful of pairs per push, so the numpy
    call overhead would cost more than the arithmetic it replaces. Both
    inputs are assumed pre-normalized (true for every vector this module
    produces; see the module docstring). Returns -1.0 (the minimum
    possible cosine similarity) when either side is None, so a caller
    that forgets to guard against a missing embedding gets "as dissimilar
    as possible" rather than a crash or a false "identical"."""
    if a is None or b is None:
        return -1.0
    return sum(x * y for x, y in zip(a, b))


def mean_vector(vectors: list[list[float]]) -> list[float] | None:
    """The centroid of `vectors`, L2-normalized so cosine_similarity
    against it behaves like cosine_similarity against any other vector
    this module produces. An un-normalized mean's norm shrinks as its
    inputs disagree with each other more, which would silently scale
    every comparison against it by an amount that has nothing to do with
    the comparison itself.

    None (not an empty list, not a zero vector) when `vectors` is empty,
    matching this module's None-for-"nothing to compute" convention
    elsewhere -- a caller that forgets to guard gets the same -1.0 from
    cosine_similarity as a missing article embedding would."""
    if not vectors:
        return None
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = sum(x * x for x in mean) ** 0.5
    if norm == 0:
        # Maximally disagreeing inputs cancelled out exactly -- nothing
        # meaningful to normalize by, so return the (zero) mean as-is
        # rather than dividing by zero.
        return mean
    return [x / norm for x in mean]
