"""
Article-vocabulary "foreignness" scoring for offbeat/novelty push
selection -- news_push._pick_for_topic's offbeat slots, replacing what
used to be an embedding-based gate + centroid-distance rank. See
docs/analysis/cluster-measurements.md's "Offbeat selection, take two" for
the full measurement history: five real-data iterations were needed
before landing on the design below, four of which failed for four
different, specific, measured reasons -- kept there, not repeated here.

Two independent signals, deliberately kept separate rather than merged
into one score:

  - A small constant list of novelty-signaling keywords, checked directly
    against title+summary text (NOVELTY_KEYWORDS/has_novelty_keyword).
    Validated across every iteration of the measurement above -- flagged
    articles consistently read as genuinely notable, not noise.
  - Per-noun "keyness" against the topic's own category pool -- a signed
    log-likelihood ratio (Dunning 1993 G2) comparing how often a word
    appears in this category's articles against its overall rate across
    the whole cache. Strongly negative = foreign to this category;
    strongly positive = category-defining vocabulary (measured: "openai"
    scores +286.95 for the AI category, correctly NOT flagged as
    foreign -- the user's own correction that fixed this: score a word
    against the TOPIC, not against another word in the same article).
    G2 rather than raw PMI specifically because PMI is known to be
    unstable at low counts (attempts 1-2 in the measurement doc); signing
    it by direction (present more or less than the word's own overall
    rate predicts) is what distinguishes "foreign" from "topic-typical",
    which unsigned G2 alone cannot.

Split into two phases because they run at different times, on different
data volumes, in different processes: `build_noun_index` +
`category_keyness` are the expensive, corpus-wide half, meant to run
ONCE per news_ingest.py cycle over the whole cache (all active
categories share the same POS-tagging pass -- the expensive part -- so
computing keyness for 13 categories costs barely more than computing it
for one) and persist via users_db.set_category_keyness. `article_nouns` +
`min_term_keyness` are the cheap, per-article half that news_push.py
calls at push time against a small (already relevance-filtered)
candidate pool, reading the precomputed table via
users_db.get_category_keyness -- a local DB read, never a live NLTK call.
This mirrors news_embed.py's shape exactly: precompute once at
ingestion, read cheaply at serving time, never a live dependency in the
push path itself.

Fails open everywhere, same convention as news_embed.py: a missing NLTK
data file, a tagging exception, or an empty/unavailable keyness table all
degrade to "no signal from this module" rather than raising --
news_push._pick_for_topic already has its own recency fallback for
exactly this case, so nothing here needs to be startup-fatal.
"""
import math
from collections import defaultdict

try:
    from nltk.tag import pos_tag
    from nltk.tokenize import word_tokenize
except Exception:
    pos_tag = None
    word_tokenize = None

# Novelty-signaling words/phrases, checked as a plain substring match
# against lowercased title+summary text. First-cut list (2026-08-26),
# explicitly meant to be tuned against real push data over time -- see
# the measurement doc's own note on this.
NOVELTY_KEYWORDS = [
    "leak", "leaked", "leaks", "breakthrough", "unveil", "unveils", "unveiled",
    "first-ever", "exclusive", "surprising", "unexpected", "controversial",
    "backlash", "shuts down", "shutdown", "banned", "lawsuit", "warns", "warning",
]

NOUN_TAGS = {"NN", "NNS", "NNP", "NNPS"}

# A term needs to be a real, recurring word across the whole cache, not a
# typo or one-off, before its keyness score is trusted at all.
MIN_GLOBAL_DF = 5

# Below this expected joint count, there isn't enough data to trust a
# direction (positive or negative) -- the actual fix for PMI/G2's
# small-count instability: don't score what the data can't support,
# rather than scoring everything and hoping normalization saves you.
MIN_EXPECTED_COUNT = 1.0


def has_novelty_keyword(title: str | None, summary: str | None) -> bool:
    text = f"{title or ''} {summary or ''}".lower()
    return any(kw in text for kw in NOVELTY_KEYWORDS)


def article_nouns(title: str | None, summary: str | None) -> set[str]:
    """Nouns only (NN/NNS/NNP/NNPS) -- verbs/adjectives/adverbs were
    measured to be exactly what made an earlier all-content-words version
    of this pick up meaningless pairs like "using"/"based"/"across" (see
    the measurement doc's attempt 1). Fails open to an empty set on any
    NLTK error -- missing tagger data, a tokenizer exception, whatever --
    since a caller treating "no nouns found" as "no signal" is already
    the correct fallback shape."""
    if pos_tag is None or word_tokenize is None:
        return set()
    text = f"{title or ''} {summary or ''}"
    if not text.strip():
        return set()
    try:
        tokens = word_tokenize(text)
        tagged = pos_tag(tokens)
    except Exception:
        return set()
    return {w.lower() for w, tag in tagged if tag in NOUN_TAGS and w.isalpha() and len(w) > 2}


def build_noun_index(articles: list[dict]) -> tuple[list[set[str]], dict[str, int]]:
    """One pass over `articles` (title+summary each): the nouns per
    article (aligned by list index, same convention as news_embed.embed_
    texts) and each noun's corpus-wide document frequency. This is the
    expensive step -- everything else in this module is cheap arithmetic
    over these two structures, which is why it's factored out to run
    once per news_ingest.py cycle rather than once per category."""
    doc_terms = []
    global_df = defaultdict(int)
    for a in articles:
        nouns = article_nouns(a.get("title"), a.get("summary"))
        doc_terms.append(nouns)
        for t in nouns:
            global_df[t] += 1
    return doc_terms, dict(global_df)


def _signed_g2(topic_df: int, global_df_count: int, n_topic: int, n_rest: int) -> float | None:
    """Dunning (1993) log-likelihood ratio over the 2x2 contingency table
    "does this word appear" x "in the topic pool or the rest of the
    corpus", signed by direction. None when the expected joint count is
    too small to trust either direction -- see MIN_EXPECTED_COUNT."""
    a = topic_df
    b = global_df_count - a
    c = n_topic - a
    d = n_rest - b
    n_total = n_topic + n_rest
    p = global_df_count / n_total
    e_a = p * n_topic
    if e_a < MIN_EXPECTED_COUNT:
        return None
    e_b = p * n_rest
    e_c = n_topic - e_a
    e_d = n_rest - e_b

    def term(o, e):
        return o * math.log(o / e) if o > 0 and e > 0 else 0.0

    g2 = 2 * (term(a, e_a) + term(b, e_b) + term(c, e_c) + term(d, e_d))
    return -g2 if a < e_a else g2


def category_keyness(
    articles: list[dict], doc_terms: list[set[str]], global_df: dict[str, int], category: str
) -> dict[str, float]:
    """All keyness scores for `category`'s vocabulary. `articles` and
    `doc_terms` must be the same lists (and same order) passed to/
    returned by build_noun_index -- this function does no NLTK work of
    its own, just arithmetic over what that pass already computed, so
    it's cheap enough to call once per active category without repeating
    the expensive tagging pass."""
    valid_terms = {t for t, c in global_df.items() if c >= MIN_GLOBAL_DF}
    n_total = len(articles)
    pool_idx = [i for i, a in enumerate(articles) if category in (a.get("categories") or [])]
    n_topic = len(pool_idx)
    n_rest = n_total - n_topic
    if n_topic == 0 or n_rest == 0:
        return {}

    topic_df = defaultdict(int)
    for idx in pool_idx:
        for t in doc_terms[idx]:
            if t in valid_terms:
                topic_df[t] += 1

    scores = {}
    for t, count in topic_df.items():
        s = _signed_g2(count, global_df[t], n_topic, n_rest)
        if s is not None:
            scores[t] = s
    return scores


def min_term_keyness(title: str | None, summary: str | None, keyness: dict[str, float]) -> tuple[float, str] | None:
    """An article's offbeat score is its single most topic-foreign noun
    (lowest keyness) -- per-word, not per-pair against another word in
    the same article, so an article with k nouns has k chances to be
    flagged, not C(k,2) -- see the module docstring on why the per-pair
    approach (attempts 1-4 in the measurement doc) didn't work. None when
    the article has no nouns scorable against `keyness` (empty/missing
    table, or every noun in the article fell below MIN_GLOBAL_DF) --
    callers treat that as "no signal", same fail-open shape as
    everywhere else."""
    nouns = article_nouns(title, summary)
    candidates = [(keyness[t], t) for t in nouns if t in keyness]
    if not candidates:
        return None
    return min(candidates, key=lambda p: p[0])
