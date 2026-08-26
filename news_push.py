"""
Periodic news-push digest: read the shared local cache, filter to new +
relevant, write, send. See docs/plans/bot-features-plan.md item 5 and
docs/plans/local-news-cache-plan.md's "Interaction with news_push.py" section.

Reads from news_cache.py (populated on a schedule by news_ingest.py)
rather than calling news_sources.py live -- converged onto the cache
2026-08-15, per docs/plans/local-news-cache-plan.md's item 6. Before this, every
push cycle called every enabled source live for every subscriber's every
interest, with no relevance filter beyond "does the source's own query
support happen to work" -- most sources are query-blind RSS feeds that
return their latest N items regardless of query (see news_sources.py),
so a subscriber's digest could and did include whatever a broad
mainstream-press feed's front page happened to be that day (a real
incident: Nikkei Asia's general top-stories feed put an Indonesia
earthquake and a Japan-society piece into a push digest -- nothing tech-
related about either, and nothing in the old pipeline could have caught
it, since no relevance check existed before the digest-writing prompt).

Two-stage filtering, per docs/plans/local-news-cache-plan.md:
  Stage 1 (category filter, in code) -- select_candidate_articles narrows
  the shared cache to articles whose classifier-assigned categories
  overlap with a subscriber's own classified interests, before the model
  ever sees anything.
  Stage 2 (content filter) -- folded into the existing single
  write_push_digest call rather than a separate LLM call: the prompt now
  explicitly tells the model to still exercise judgment and omit
  candidates that survived the category filter but aren't genuinely
  about the topic, instead of forcing everything given into the report.

Deliberately still does NOT go through agent.py's tool-calling agent
(build_agent/run_agent): a single plain model.invoke (no tool loop) is
enough once the candidate list is already assembled in code -- there's
nothing left for tools to do.

"New" separates two things that were previously conflated, and the
distinction is load-bearing -- see select_candidate_articles:

- FILTERING is done by users_db's pushed_links alone. "Already seen" means
  "we actually sent this to this subscriber", nothing else.
- RANKING is done by published_dt, newest first, with a maximum-age gate.
  A publish date decides what's most worth showing, never what's eligible.

Filtering on the date was the earlier design and it was wrong: an article
that lost one cycle's cut was excluded permanently, without ever having
been seen by anyone.
"""

import re
from datetime import datetime, timedelta, timezone

import agent
import guardrails
import healthcheck
import news_cache
import news_classify
import news_embed
import news_sources
import users_db
from opentelemetry import trace

# How many messages one push cycle may send to one subscriber -- one per
# interest, so this is also "how many interests get served this cycle".
#
# Bounded because a subscriber at users_db.MAX_INTERESTS on a 8h interval
# would otherwise receive 30 messages a day. Interests that don't fit
# aren't dropped, they wait: interests_by_staleness puts the
# longest-un-pushed first, so the queue drains over successive cycles.
MAX_INTERESTS_PER_PUSH = 5

MAX_ARTICLES_PER_TOPIC = 5

# Of MAX_ARTICLES_PER_TOPIC, how many go to an article that's ON topic but
# UNLIKE the typical article in it, rather than always the newest. See
# select_candidate_articles's docstring and
# docs/analysis/cluster-measurements.md's "offbeat score" section for the
# measurement this implements. 0 disables the feature entirely and falls
# back to pure recency -- a safe rollback with no code change.
OFFBEAT_SLOTS_PER_TOPIC = 2

# _filter_by_relevance keeps at most this many of a topic's pool, ranked
# by similarity to the topic's retrieval query -- not a fixed fraction
# any more. Three real findings, all from the same 2026-08-25
# measurement session, forced this shape:
#
# 1. A fixed fraction cannot work across different pool sizes. This
#    session's measurement pulled the REAL production cache and found
#    999 live "AI"-category articles within one 48h cache TTL window at
#    once -- a fraction tuned for a small pool either starves a 999-item
#    one or barely filters it at all, depending which end you tune for.
# 2. A fixed fraction cannot work across different topic PHRASINGS
#    either, independent of pool size -- "AI Agent"'s genuinely-relevant
#    articles scored 0.53-0.72 cosine similarity on one corpus, while
#    "Large Language Model" against the SAME corpus topped out at 0.166
#    for its single best candidate. Absolute score, and therefore any
#    fraction-of-the-score-range framing, isn't comparable across
#    queries.
# 3. Recall past a certain point stops being worth chasing. Measured
#    with a carefully hand-verified 24-article ground truth on the real
#    999-article corpus (not the earlier, since-retracted 40-article
#    sample -- see docs/analysis/cluster-measurements.md): every fraction
#    from 60% to 90% recovers at least 22 of 24, and the last 2 --
#    "How Virgin Atlantic ships faster with Codex", "Asana cleared 5
#    years of engineering work in 2 weeks with Codex" -- only clear a
#    'business outcome' headline with none of the definition's technical
#    vocabulary in it, ranking near the very bottom of a 999-item pool
#    even though the content is genuinely on-topic. Chasing literally
#    every last one means keeping ~90% of the pool, which stops being a
#    filter. The project's own standard (this session, discussing where
#    to draw this exact line): missing a couple of genuine matches out
#    of a pool already offering 20+ good candidates is fine, since only
#    MAX_ARTICLES_PER_TOPIC=5 are ever pushed regardless -- a candidate
#    ranked near the bottom of a 20+ item pool was never going to be one
#    of the 5 sent anyway.
#
# So: an ABSOLUTE count, clamped between a floor and a ceiling rather
# than a percentage of a variable-sized, variable-scaled pool --
#
#   n_keep = min(RELEVANCE_KEEP_MAX, max(round(pool * RELEVANCE_KEEP_FRACTION), RELEVANCE_KEEP_MIN))
#
# RELEVANCE_KEEP_FRACTION=0.10 with a 20-50 clamp: a narrow topic's small
# pool gets the 20-item floor rather than being starved by 10% of a small
# number; a broad topic's pool (hundreds, per the 999-article measurement
# -- and uncapped by count going in, see the raw_pool loop below) gets
# capped at 50 rather than passing hundreds of candidates through to
# write_push_digest's single LLM judgment call. All three numbers are
# first-cut, not re-validated end to end -- adjust freely; they're
# independent named constants specifically so that's cheap.
RELEVANCE_KEEP_FRACTION = 0.10
RELEVANCE_KEEP_MIN = 20
RELEVANCE_KEEP_MAX = 50

# The relevance-filtered pool is capped here again before recency/offbeat
# selection runs -- matches RELEVANCE_KEEP_MAX exactly, not some smaller
# number, specifically so THIS cap is never the tighter, silently-binding
# one. It was 30 until 2026-08-25 (a guess made before RELEVANCE_KEEP_MAX
# existed), which would have made RELEVANCE_KEEP_MAX=50 a dead ceiling --
# the exact bug this whole revision exists to stop repeating.
OFFBEAT_POOL_SIZE = RELEVANCE_KEEP_MAX

# Above this cosine similarity, two articles collapse to one -- the same
# wire story cached under two links, or two outlets syndicating one
# piece. Not a title-match: docs/analysis/cluster-measurements.md found
# genuine duplicates (9 gnews copies of one syndicated story) and
# same-titled non-duplicates (BBC's "Tech Now"/"Tech Life" programme
# titles, a different episode every time) indistinguishable by title
# alone -- only content similarity tells them apart.
NEAR_DUPLICATE_SIMILARITY = 0.95

# Ceiling on how old an article's own publication date may be to still be
# pushed, regardless of when we downloaded it. Needed as of 2026-08-19,
# when candidate selection switched from published_dt to fetched_at (see
# select_candidate_articles): "new to us" is the right test for a source
# that publishes on a delay, but on its own it would also happily push
# genuinely ancient content. Real case that forced this: Perigon's one
# successful fetch returned 50 articles whose NEWEST was over a year old
# (docs/plans/security-plan.md finding 21) -- under a fetched_at-only rule
# every one of them would have been eligible.
#
# 7 days is deliberately generous rather than tight. It exists to exclude
# absurdly stale content, not to enforce freshness -- the fetched_at check
# already does that. arXiv's own indexing runs ~3 days behind
# (docs/current/ai-news-sources.md), and those papers are legitimately worth
# sending, so a tighter bound would silently drop a real source.
MAX_ARTICLE_AGE_HOURS = 168

_PUSH_DIGEST_PROMPT = (
    "You are a technology industry analyst writing a periodic news digest "
    "for a Telegram subscriber, covering AI and the broader tech industry. "
    "Below is a list of candidate articles that are new since their last "
    "digest, grouped by the topic they matched during a coarse category "
    "filter. That filter is not perfect -- some candidates may not "
    "actually be about the subscriber's topic, or may not be genuinely "
    "tech-industry content at all (e.g. general news that happened to "
    "come from a source that also covers tech). Use your own judgment: "
    "write a short trend report covering ONLY the candidates that are "
    "genuinely relevant, silently omitting any that aren't -- do not "
    "force an irrelevant candidate into the report just because it was "
    "in the list, and do not invent or reference anything not in the "
    "list either. If NONE of the candidates are genuinely relevant, "
    "write nothing (an empty reply) rather than reporting on off-topic "
    "content. Do not mention that this is an automated or periodic "
    "message, and do not mention the filtering process itself.\n\n"
    + agent.HTML_FORMATTING_RULES
    + "\n\n"
    + agent.TREND_REPORT_STRUCTURE
)


def resolve_interest_categories(model, interests: list[str]) -> dict[str, list[str]]:
    """Stage-1 setup: maps each interest to its category tags, using
    users_db's persistent cache and classifying only what's missing --
    interest text is stable vocabulary (unlike article content), so this
    should be a cache hit for any interest that's been pushed before.

    An interest the classifier FAILED on is left out of the cache entirely,
    so the next cycle retries it. Only a real answer gets cached -- including
    a genuinely empty one, which is a valid classification and should not be
    re-paid for every cycle.

    Caching failures as [] is what poisoned the live cache during the
    2026-08-17 classification outage: "AI", "Bitcoin", "機器人科技" and
    others were all stored as [], permanently, with no retry. An empty
    mapping matches every article (see select_candidate_articles), so those
    subscribers were receiving entirely unfiltered news -- the exact failure
    the category filter exists to prevent."""
    resolved = users_db.get_cached_interest_categories(interests)
    missing = [i for i in interests if i not in resolved]
    if missing:
        taxonomy = news_classify.Taxonomy.from_rows(users_db.get_active_categories())
        newly_classified = news_classify.classify_interests(model, missing, taxonomy)
        failed = [i for i in missing if i not in newly_classified]
        for interest, categories in newly_classified.items():
            users_db.set_interest_categories(interest, categories)
            resolved[interest] = categories
        if failed:
            print(f"[news_push] could not classify {len(failed)} interest(s), "
                  f"will retry next cycle: {', '.join(failed)}")
    return resolved


def select_candidate_articles(
    cached_articles: list[dict],
    topics: list[str],
    topic_categories: dict[str, list[str]],
    since: datetime | None,
    already_pushed_links: set[str],
    include_restricted: bool = False,
    max_per_topic: int = MAX_ARTICLES_PER_TOPIC,
    now: datetime | None = None,
    embedder=None,
    offbeat_slots: int = OFFBEAT_SLOTS_PER_TOPIC,
) -> list[dict]:
    """Stage 1 (category filter): narrows the shared cache to one
    subscriber's candidate articles, before the digest-writing model
    (stage 2, in write_push_digest's prompt) ever sees anything.

    An article is a candidate for `topic` when its own categories (set at
    ingestion time by news_classify.py) overlap with that topic's mapped
    categories from `topic_categories`. A topic that mapped to NO
    categories at all (a classifier miss) is treated as unrestricted --
    matches any article -- rather than matching nothing: a subscriber's
    own stated interest is presumably tech/industry-relevant by
    construction (they set it on a tech-industry bot), and a
    classification miss on the INTEREST shouldn't silently starve them.
    An article with no categories (the classifier found nothing that
    plausibly applies, e.g. a general-news piece with no tech angle at
    all) is excluded whenever the topic itself has real categories to
    match against -- this is the exact mechanism that would have kept
    the Nikkei Asia earthquake/society articles out of a digest, since
    neither classifies into any of the 13 tech-industry categories.

    Since 2026-08-20 the cache distinguishes "the classifier found nothing
    applicable" (users_db.UNCLASSIFIABLE) from "never classified" (None).
    This function deliberately still treats them alike -- neither has a
    category that can match a topic, so both are excluded the same way --
    but the distinction now exists in the data for anything that needs it,
    which is how a silent classification outage becomes detectable.
    Known, accepted overlap with a separate case this can't distinguish:
    an article left uncategorized because news_classify.classify_articles
    failed for its whole ingestion batch (fails open, see that
    function's docstring) looks identical to a genuine "nothing applies"
    result here -- both get excluded. Not solved here; a batch
    classification failure already means that cycle's articles are
    "harder to find... until the next cycle re-fetches and reclassifies
    it," per that function's own docstring, so this is consistent with
    an existing accepted limitation, not a new one.

    **A date is a ranking signal, never a "have they seen it" filter.**
    Restructured 2026-08-19 around that separation:

    - **Filter** -- `already_pushed_links`, and nothing else. That set is
      the direct record of what this subscriber was actually sent, so it
      answers the question exactly rather than approximating it.
    - **Rank** -- `published_dt`, newest first. What a date is genuinely
      good for.
    - **Quality gate** -- `MAX_ARTICLE_AGE_HOURS`, which drops absurdly
      stale articles regardless of when we downloaded them. A gate on
      worth-sending, not on already-seen.

    Why this matters, from two real failures. The original code filtered on
    `published_dt <= since`, which structurally excluded every source with
    a publication delay: GNews publishes ~12h behind, so for any subscriber
    pushed more recently than that, its articles were skipped outright --
    227 of them sat in the cache, correctly fetched and classified, and not
    one could ever reach a digest. Switching that filter to `fetched_at`
    fixed the delay case but kept the deeper flaw: an article that was a
    candidate and simply lost the `max_per_topic` cut would have its
    timestamp fall behind the next `since` and be excluded forever, unsent
    and unrecorded.

    Both failures come from the same mistake -- using a timestamp as a
    proxy for "already seen" when the actual record exists. Anything not
    yet sent stays a candidate until it is sent or ages out of the cache,
    so the pool drains in publication order and nothing starves.

    Restricted-source gating is unchanged: NewsAPI/Perigon articles are
    skipped entirely unless `include_restricted`.

    `since` is retained in the signature but no longer filters -- it stays
    only because callers already pass it and removing it would be a
    breaking change for no gain. `now` is a parameter rather than read from
    the clock so the age guard is deterministic in tests, same convention
    as run_push_cycle's own `now`.

    Three more steps run per topic before the `max_per_topic` cut, all new
    2026-08-25 and all optional (a missing embedder, or an article
    missing its own embedding, degrades each one independently rather
    than failing the whole call -- see news_embed's module docstring):

    - **Near-duplicate collapse** -- the same wire story cached under two
      links, or two outlets syndicating one piece, counts once. See
      NEAR_DUPLICATE_SIMILARITY and docs/analysis/cluster-measurements.md
      on why this can't be title matching (BBC's "Tech Now"/"Tech Life"
      programme titles repeat with different content every episode; 9
      gnews copies of one syndicated story were genuine duplicates under
      DIFFERENT titles).
    - **Relevance filter** -- a fine pass after the coarse category one,
      using the TOPIC STRING itself (not its category) as a retrieval
      query. Found necessary from a live user report: "AI", "AI Agent",
      "AI coding" and "Large Language Model" all map to category `AI`,
      so all four drew from the same undifferentiated pool of generic
      AI-industry news -- the category filter alone cannot tell them
      apart. See _filter_by_relevance for why this keeps an absolute,
      clamped count of the pool's top scorers (RELEVANCE_KEEP_MIN to
      RELEVANCE_KEEP_MAX), not a fixed threshold or a fraction of the
      pool -- both were tried and measured not to generalize.
    - **Offbeat selection** -- of the final `max_per_topic`,
      `offbeat_slots` go to an article that's still on-topic but unlike
      the typical article in this topic's own pool, rather than always
      the newest. See _pick_for_topic."""
    now = now or datetime.now(timezone.utc)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(
        cached_articles,
        key=lambda a: a.get("published_dt") or epoch,
        reverse=True,
    )
    max_age = timedelta(hours=MAX_ARTICLE_AGE_HOURS)
    seen_links = set()
    candidates = []
    for topic in topics:
        topic_cats = set(topic_categories.get(topic, []))
        # The embedding query text -- NOT necessarily `topic` itself. See
        # _resolve_query_text: a cached, LLM-generated definition of the
        # topic outperforms the bare topic string as a retrieval query,
        # measurably (docs/analysis/cluster-measurements.md). `topic`
        # itself is still what's used for the category-match check right
        # below and for tagging candidates -- only the embedding calls
        # use `query_text`.
        query_text = _resolve_query_text(topic)
        # Newest-first, deduplicated, gathered BEFORE the max_per_topic cut
        # (unlike the old single-pass loop), because offbeat selection
        # needs a pool bigger than the final cut to have anything to
        # choose from beyond what recency already picked, and the
        # relevance filter right below needs a big-enough sample to
        # compute a meaningful median over.
        #
        # Not truncated by count here (no RELEVANCE_SAMPLE_SIZE-style cap,
        # removed 2026-08-25): per-article embeddings are precomputed at
        # ingestion time (news_ingest.py), so scoring a larger pool here
        # costs a few hundred more 256-dim dot products, not more model
        # calls -- negligible. A recency pre-cut ahead of the relevance
        # filter risked discarding a genuinely more-relevant but slightly
        # older article before the filter ever saw it. The only bound on
        # this loop's input is however much `cached_articles` the caller
        # passed in -- in production, whatever news_cache.read_all()
        # returns, itself bounded by the 48h TTL (DEFAULT_TTL_HOURS) that
        # cleanup_expired enforces, not by a count.
        raw_pool = []
        for article in ordered:
            link = article.get("link")
            if not link or link in seen_links:
                continue
            if not include_restricted and article.get("source_key") in news_sources.RESTRICTED_SOURCES:
                continue
            article_cats = set(article.get("categories") or [])
            if topic_cats and not (article_cats & topic_cats):
                continue
            # The one and only "has this subscriber seen it" test.
            if link in already_pushed_links:
                continue
            # Not already-seen, but not worth sending either if it's ancient.
            # Articles whose published_dt didn't parse pass this guard rather
            # than being dropped -- same fail-open instinct as the rest of
            # the pipeline.
            published_dt = article.get("published_dt")
            if published_dt is not None and now - published_dt > max_age:
                continue
            # Near-duplicate collapse. An article with no embedding never
            # matches anything here -- cosine_similarity(None, x) is -1.0
            # by construction -- so it's always kept, same fail-open shape
            # as every other check in this loop. Deliberately does NOT
            # add `link` to seen_links: being a near-duplicate within
            # THIS topic's pool doesn't mean it should be unavailable to
            # a DIFFERENT topic later in this same call, which may have
            # an entirely different pool to be a duplicate within.
            embedding = article.get("embedding")
            if embedding is not None and any(
                news_embed.cosine_similarity(embedding, p.get("embedding")) >= NEAR_DUPLICATE_SIMILARITY
                for p in raw_pool
            ):
                continue
            seen_links.add(link)
            raw_pool.append(article)

        # The fine filter after the coarse one -- see _filter_by_relevance.
        # Capped at OFFBEAT_POOL_SIZE afterward, same reasoning as before
        # this existed: bigger than the final max_per_topic cut, so
        # offbeat selection has survivors to choose from.
        pool = _filter_by_relevance(raw_pool, embedder, query_text)[:OFFBEAT_POOL_SIZE]

        for article in _pick_for_topic(pool, embedder, query_text, max_per_topic, offbeat_slots):
            candidates.append({**article, "topic": topic})
    return candidates


def _resolve_query_text(topic: str) -> str:
    """The bare topic string is a weak embedding query -- measured,
    2026-08-25: for "AI coding", three genuinely relevant real articles
    ("Claude Cowork...") scored BELOW an unrelated stock-picking article,
    because model2vec's static embeddings can't connect a product name to
    a category word without shared vocabulary. A generated definition,
    rich in the concrete tool/technique vocabulary a real article on the
    subject would use, measurably ranks genuine matches much higher (the
    same worst case dropped from needing the top 83% of a pool kept down
    to 44%). See news_classify.expand_interest_for_retrieval for what's
    generated and cached, and agent.py's _add_one_interest for when.

    Falls back to the bare `topic` when nothing is cached -- an interest
    added before this feature existed, or one whose generation call
    failed and was never retried. Never calls the LLM itself: this runs
    on every push cycle, and generation is deliberately a write-time,
    cached, once-per-interest cost, not a read-time one."""
    return users_db.get_interest_query_expansion(topic) or topic


def _filter_by_relevance(pool: list[dict], embedder, query_text: str) -> list[dict]:
    """The fine filter after the coarse category one: cuts `pool` (already
    category-matched, newest-first) down to the top-scoring articles by
    similarity to `query_text`, where "top" means RELEVANCE_KEEP_MIN to
    RELEVANCE_KEEP_MAX articles depending on pool size -- see that
    constant's own comment for the full reasoning (an absolute,
    clamped count, not a fraction; a fraction was tried first and measured
    not to generalize, across both pool size and topic phrasing).
    `query_text` is resolved by the caller (see _resolve_query_text) -- a
    cached generated definition of the topic when one exists, the bare
    topic string otherwise; this function doesn't know or care which.

    Found necessary live, 2026-08-25: "AI", "AI Agent", "AI coding" and
    "Large Language Model" all map to category ['AI'], so a subscriber
    following all four got four near-identical digests drawn from the
    same generic AI-industry pool -- the category filter has no way to
    tell them apart, because it was never asked to; only the topic
    STRING carries that distinction, and until this filter existed
    nothing downstream used it before write_push_digest's single LLM
    judgment call, which was measured (same session) to keep every
    off-target candidate for both "AI Agent" and "AI coding" given the
    identical input.

    This makes the filter a blunt tool, not the fine-grained relevance
    decision an earlier version of this docstring implied it could make
    alone: RELEVANCE_KEEP_MAX's own comment documents two articles that
    are genuinely on-topic but rank near the bottom of a 999-article real
    corpus, because their headlines use business-outcome language with
    none of the definition's technical vocabulary in it -- no clamp
    shape fixes that specific case on its own, only embedding richer
    source text does (title+summary rather than title alone -- see
    news_ingest.py's run_ingestion_cycle, which is what this filter's
    embeddings actually come from). The precision work still rests on
    write_push_digest's own topic-framed judgment (see that function's
    docstring) -- this filter's honest job is trimming the pool down to
    a workable size that's disproportionately on-topic, not guaranteeing
    every genuine match survives.

    Falls back to `pool` unchanged -- no filtering at all -- when there's
    no embedder, no topic vector, or fewer than 2 embedded articles to
    compute a gate over (same fail-open shape as everywhere else
    embeddings touch this pipeline). An article with no embedding of its
    own always survives this filter too -- there is nothing to judge it
    by, and the pipeline's convention throughout is that a missing
    embedding excludes an article from an embedding-based FEATURE, never
    from being a candidate at all."""
    if not pool:
        return pool
    query_vector = news_embed.embed_one(embedder, query_text)
    if query_vector is None:
        return pool
    scored = [(a, news_embed.cosine_similarity(a["embedding"], query_vector))
             for a in pool if a.get("embedding") is not None]
    if len(scored) < 2:
        return pool
    # Computed from the KEEP side and rounded, not `int(n * (1 - frac))`
    # -- that subtraction hits float imprecision (1 - 0.9 == 0.09999999999999998
    # in Python) that silently changes cut_index for specific pool sizes.
    # Caught by a real test: n=10 computed cut_index=0 instead of 1,
    # keeping a zero-relevance article that should have been the single
    # excluded outlier.
    #
    # Clamped to len(scored) at the end -- RELEVANCE_KEEP_MIN=20 is bigger
    # than plenty of real pools (a narrow interest's whole category-matched
    # pool can be smaller than 20 outright), and without this clamp
    # n_kept > len(scored) drives cut_index negative, which Python indexes
    # from the END of the sorted list -- the single highest score becomes
    # the gate, and the filter would exclude nearly everything instead of
    # keeping everything.
    n_kept = min(RELEVANCE_KEEP_MAX, max(round(len(scored) * RELEVANCE_KEEP_FRACTION), RELEVANCE_KEEP_MIN))
    n_kept = min(n_kept, len(scored))
    cut_index = len(scored) - n_kept
    gate = sorted(sim for _, sim in scored)[cut_index]
    relevant_links = {a["link"] for a, sim in scored if sim >= gate}
    return [a for a in pool if a.get("embedding") is None or a["link"] in relevant_links]


def _pick_for_topic(
    pool: list[dict], embedder, query_text: str, max_per_topic: int, offbeat_slots: int,
) -> list[dict]:
    """`pool` is already newest-first and deduplicated (select_candidate_
    articles' job). `query_text` is resolved by the caller -- see
    _resolve_query_text -- and may be a cached generated definition of
    the topic rather than the bare topic string. Splits the pool into a
    recency cut and an offbeat cut:

        score = sim(article, query_text) with a floor at
                the REMAINDER's own median (still genuinely about the
                topic, not a stray outlier), ranked by LOWEST similarity
                to the pool's centroid (unlike a typical article in it).

    A single-term "farthest from what's already selected" was measured
    and rejected in docs/analysis/cluster-measurements.md: it surfaces
    parsing garbage and language outliers, which are maximally distant
    from EVERYTHING by construction, before it surfaces genuine novelty.
    The gate is what fixes that -- both garbage exhibits from that
    measurement failed it (neither was a good match for its own topic
    query to begin with).

    Falls back to pure recency -- the pre-2026-08-25 behavior -- for
    whichever slots don't have a usable signal: no embedder, no topic
    vector, or fewer than 2 embedded candidates left over after the
    recency cut (not enough to make a median split meaningful). This is
    also how `offbeat_slots=0` cleanly disables the feature.

    The gate threshold (the remainder's own median) is a provisional
    choice, not a fitted one -- see cluster-measurements.md's "Still
    open" section. It hasn't been re-validated against a corpus pulled
    with the fixed (non-title-truncating) snapshot collector."""
    recency_n = max(max_per_topic - offbeat_slots, 0)
    recency_pick = pool[:recency_n]
    remainder = pool[recency_n:]

    def fallback():
        filler_needed = max_per_topic - len(recency_pick)
        return recency_pick + remainder[:filler_needed]

    if not remainder or offbeat_slots <= 0:
        return fallback()

    query_vector = news_embed.embed_one(embedder, query_text)
    embedded_remainder = [a for a in remainder if a.get("embedding") is not None]
    if query_vector is None or len(embedded_remainder) < 2:
        return fallback()

    query_sims = [news_embed.cosine_similarity(a["embedding"], query_vector) for a in embedded_remainder]
    gate = sorted(query_sims)[len(query_sims) // 2]
    # The centroid represents a typical article FOR THIS TOPIC, so it's
    # computed over the fuller `pool`, not just the narrower `remainder`
    # left over after the recency cut.
    centroid = news_embed.mean_vector([a["embedding"] for a in pool if a.get("embedding") is not None])

    survivors = [(a, sim) for a, sim in zip(embedded_remainder, query_sims) if sim >= gate]
    survivors.sort(key=lambda pair: news_embed.cosine_similarity(pair[0]["embedding"], centroid))
    offbeat_pick = [a for a, _sim in survivors[:offbeat_slots]]

    filler_needed = max_per_topic - len(recency_pick) - len(offbeat_pick)
    if filler_needed > 0:
        # Not enough offbeat survivors to fill every slot -- top up with
        # the next most recent article not already picked, rather than
        # sending a short digest.
        chosen_links = {a["link"] for a in recency_pick + offbeat_pick}
        offbeat_pick += [a for a in remainder if a["link"] not in chosen_links][:filler_needed]
    return recency_pick + offbeat_pick


def write_push_digest(model, articles: list[dict], topic: str | None = None,
                      language: str | None = None) -> str:
    """A single direct model call (no tool loop -- see module docstring)
    that turns a stage-1-filtered candidate list into a Telegram HTML
    digest, applying stage-2 (content) filtering itself per the prompt
    above. `language`, when set, is the subscriber's stored reply-language
    preference (users_db.get_language) -- pushes go through this module's
    own prompt rather than agent.py's dynamic_prompt middleware, so the
    preference has to be threaded in here too, not just in _compose_prompt.

    `topic` is an explicit parameter, passed by the caller (which already
    has it in scope -- see run_push_cycle's per-interest loop), NOT read
    off `articles`. Until 2026-08-25 it wasn't a parameter at all: each
    candidate carried its own `"topic"` field (stamped on by
    select_candidate_articles) and the listing repeated `[{topic}]` as a
    prefix on every single line. Two things wrong with that, found via a
    live user report of near-identical digests across different
    interests:

    1. It was a fossil. Before push became one-message-per-interest
       (2026-08-24), one call covered a subscriber's WHOLE interest list,
       and the per-line tag genuinely distinguished which of several
       DIFFERENT interests each candidate belonged to. Once every call
       covers exactly one interest, every line in one listing carries the
       identical value -- zero information content, confirmed by grep:
       no code anywhere reads `article["topic"]` except this f-string.
    2. It was actively misleading, not just useless. `_PUSH_DIGEST_PROMPT`
       explicitly tells the model to be skeptical -- "some candidates may
       not actually be about the subscriber's topic... use your own
       judgment" -- but the listing FORMAT presented `[AI Agent]` as a
       per-article, seemingly-confirmed classification sitting right next
       to each headline. The instruction said "doubt this," the
       presentation said "this is verified." Reproduced live: given 5
       generic AI-industry articles (an earnings-reaction piece, a
       funding round, a job-market study -- none actually about agents or
       coding), write_push_digest kept every single one under BOTH
       topic="AI Agent" and topic="AI coding", producing near-identical
       reports. Stage 1's category filter is coarse by design; stage 2
       (this function) is supposed to be the fine filter, and the old
       format gave it nothing to be fine WITH.

    The fix here is necessary but not sufficient: stating the topic once,
    plainly, with an explicit instruction to judge specificity rather
    than category membership, gives the model a chance to filter
    correctly -- it does not guarantee it will, since the model still has
    no signal stronger than its own semantic judgment of a headline
    against a topic word. A stricter fix (embedding-based relevance
    filtering ahead of this call, using the topic string as a retrieval
    query the way the offbeat gate in select_candidate_articles already
    does) is discussed but not yet built -- see
    docs/analysis/cluster-measurements.md."""
    listing = "\n".join(
        f"- {a['title']} ({a.get('source')}, "
        f"published {a.get('published') or 'date unknown'}) — {a.get('link')}"
        for a in articles
    )
    system_prompt = _PUSH_DIGEST_PROMPT
    if topic:
        system_prompt += (
            f"\n\nThe subscriber's interest is: {topic}. The candidates "
            "below already passed a coarse category filter, which only "
            "confirms they share a broad category with this interest -- "
            "it does NOT confirm they are actually, specifically about "
            f"{topic}. Generic industry news that merely mentions the "
            "same broad field (earnings, funding, market moves, general "
            f"research) does not count as being about {topic} unless it "
            "genuinely is. Apply this before applying your usual "
            "relevance judgment from the instructions above."
        )
    if language:
        system_prompt += (
            f"\n\nWrite your ENTIRE reply in {language}, regardless of what "
            "language the article titles/summaries above are in. If this "
            "is a specific script/variant (e.g. Traditional vs Simplified "
            "Chinese, Brazilian vs European Portuguese), use exactly that "
            "variant's script and spelling conventions throughout."
        )
    response = model.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": listing},
        ]
    )
    return response.content


_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


def links_actually_sent(digest: str, candidates: list[dict]) -> list[str]:
    """Which candidate articles genuinely appear in the delivered digest.

    The digest is free-form prose the model writes from the candidate list,
    and _PUSH_DIGEST_PROMPT explicitly tells it to omit candidates that
    aren't relevant -- so "what we offered" and "what the subscriber saw"
    are different sets. Only the latter should count as seen: marking an
    omitted candidate as sent would retire an article nobody ever read.

    Recovered by matching the digest's own <a href> targets against the
    candidate links, since TREND_REPORT_STRUCTURE requires every cited item
    to carry its source link and forbids inventing URLs."""
    hrefs = {h.strip() for h in _HREF_RE.findall(digest or "")}
    return [a["link"] for a in candidates if a.get("link") in hrefs]


# Liveness for the dead man's switch in
# docs/plans/observability-platform-plan.md.
#
# The alarm asks "has ANY span arrived in the last 30 minutes". Without
# this, the answer is legitimately "no" on a healthy system: every LLM call
# in a push cycle sits inside the per-subscriber loop AFTER the due check,
# so a tick where nobody is due emits nothing at all. Two subscribers on a
# 6-hour interval leave most of the 96 daily ticks silent, and an alarm
# that cries wolf on an idle Sunday is an alarm nobody reads.
#
# A no-op when no tracer provider is configured -- which is every test and
# CI run -- because OpenTelemetry's default is a no-op tracer. No env check
# needed, and nothing to remember to stub.
_tracer = trace.get_tracer("argus.news_push")


def _emit_heartbeat(subscriber_count: int) -> None:
    """One span per tick, whether or not there was any work to do.

    Carries the subscriber count as an attribute rather than just existing:
    the same span then answers "is it running" and "how many subscribers
    did it see", and the second is the number whose sudden growth was the
    2026-08-21 incident."""
    with _tracer.start_as_current_span("argus_heartbeat") as span:
        span.set_attribute("heartbeat.job", "push_tick")
        span.set_attribute("heartbeat.push_enabled_subscribers", subscriber_count)


class _ModelStageError(Exception):
    """Marks a failure that came out of an LLM call rather than out of
    local filtering or the database.

    The alternative -- deciding after the fact whether an exception "looks
    like" a model error by matching its message -- breaks the first time a
    provider rewords one, and breaks silently, in the direction of
    under-counting. Classifying by WHERE the call was made cannot drift:
    `_model_call` wraps exactly the LLM calls and nothing else."""


def _model_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise _ModelStageError(exc) from exc


# Telegram's wording for "this chat can no longer receive anything."
# Both are permanent for practical purposes and both cost a full digest
# generation per cycle, so criterion 1 treats them identically.
_UNREACHABLE_CHAT_MARKERS = (
    "chat not found",       # BadRequest -- the chat never existed or was deleted
    "bot was blocked",      # Forbidden -- the user blocked the bot
    "user is deactivated",  # Forbidden -- the account is gone
)


def _classify_send_failure(exc: Exception) -> str:
    """Narrows a delivery failure to chat_not_found where the message says
    so, and leaves everything else as a generic cycle failure.

    Matching on the message rather than the exception type because the
    type does not separate these: `BadRequest` covers both a dead chat and
    malformed HTML, and only the text tells them apart. The failure mode
    of a reworded message is that a genuinely unreachable chat records as
    `cycle_failed` -- criterion 1 goes quiet, rather than every failure
    being mislabelled as unreachable and subscribers being disabled
    wrongly. Wrong in the safe direction, deliberately."""
    text = str(exc).lower()
    if any(marker in text for marker in _UNREACHABLE_CHAT_MARKERS):
        return users_db.PUSH_CHAT_NOT_FOUND
    return users_db.PUSH_CYCLE_FAILED


# Consecutive undeliverable digests before push is turned off for that
# subscriber -- criterion 1 in docs/plans/incident-monitoring-plan.md.
#
# Three rather than one because delivery can fail for reasons that are not
# about the chat being gone, and turning a real subscriber off is the more
# expensive mistake of the two: they simply stop receiving news, with no
# error to notice. Three consecutive failures with no successful delivery
# between them is not a blip.
UNREACHABLE_STRIKES = 3


def _strike_unreachable_subscriber(chat_id: int, now: datetime) -> None:
    """Turns push off once a chat has been undeliverable UNREACHABLE_STRIKES
    times running.

    This is the bound on the 2026-08-21 failure mode. An unreachable chat
    still costs a full digest generation -- three LLM calls -- every cycle,
    because generation happens before delivery is attempted and is billed
    whether or not the send lands. Without a stop, that recurs for as long
    as the subscriber row exists: the leak was open for eight days.

    Reversible on purpose: only `push_enabled` is cleared. The subscriber,
    their interests and their language survive, so a user who blocked the
    bot and later unblocks it turns push back on and continues, rather than
    discovering their settings were deleted.

    Re-enabling does NOT reset the strike count -- only a delivered digest
    does (see users_db.consecutive_chat_not_found). So a subscriber who
    turns push back on while still unreachable is disabled again after a
    single further failure rather than after three. That is deliberate: a
    transient outage records `cycle_failed` and accrues no strikes at all,
    so one more `chat_not_found` really does mean still unreachable, and
    granting a fresh allowance would just pay for three more undeliverable
    digests."""
    strikes = users_db.consecutive_chat_not_found(chat_id)
    if strikes < UNREACHABLE_STRIKES:
        return
    users_db.set_push_enabled(chat_id, False)
    detail = f"undeliverable {strikes} cycles running"
    _record(chat_id, users_db.PUSH_DISABLED,
            f"{detail} -- push turned off (they can turn it back on)",
            now, detail=detail)


def _record(chat_id: int, outcome: str, message: str, now: datetime,
            detail: str | None = None) -> None:
    """Reports a push outcome three ways, from one call site so they cannot
    disagree: a log line, a database row, and a span.

    Each reaches a different reader and none replaces the others.

    The PRINT goes to `docker logs`, where someone looks when debugging a
    specific cycle. It is free text with no timestamp of its own, it cannot
    be read from inside the container, and a deploy destroys it -- fine for
    a human reading a single cycle, useless as an alarm's input.

    The ROW is what `/status` and any local query read. It survives with no
    network at all, which is what makes it the floor: if telemetry export
    breaks, this still answers "what happened".

    The SPAN is what an ALARM can query. Criteria 2 and 3 of
    docs/plans/incident-monitoring-plan.md live in Logfire, and Logfire can
    only alert on what it has received -- the SQLite rows sit on the bot VM
    where the alerting engine cannot see them. A no-op when no tracer
    provider is configured, which is every test and CI run."""
    print(f"[news_push] chat_id={chat_id}: {message}")
    users_db.record_push_outcome(chat_id, outcome, now, detail=detail)
    with _tracer.start_as_current_span("push_outcome") as span:
        # Flat, low-cardinality attributes: an alert query filters on
        # `outcome` and counts, so it must not have to parse prose.
        span.set_attribute("push.outcome", outcome)
        # The opaque id, not chat_id: the row and the log line keep the
        # real Telegram identifier (they never leave the VM), but a span
        # does leave. See users_db.external_id.
        span.set_attribute("push.subscriber", users_db.external_id(chat_id))
        # Whether an LLM was paid for this cycle -- criterion 3's
        # denominator, computed here rather than by re-deriving the outcome
        # set inside every alert query.
        span.set_attribute("push.generated", outcome in users_db.PUSH_GENERATED_OUTCOMES)
        if detail:
            span.set_attribute("push.detail", detail)


def is_subscriber_due(last_push_at: datetime | None, interval_hours: int, now: datetime) -> bool:
    if last_push_at is None:
        return True
    elapsed_hours = (now - last_push_at).total_seconds() / 3600
    return elapsed_hours >= interval_hours


async def run_push_cycle(model, send: "callable", now: datetime | None = None, embedder=None) -> None:
    """One scheduler tick: for every push-enabled, due subscriber with at
    least one interest, select candidate articles from the shared cache,
    and if there are any, write and send a digest. `send` is
    `async def send(chat_id, html_text)` (bound to the real bot's
    send_message in production, faked in tests) -- kept generic so this
    module doesn't need a live Bot/Application to be tested. One
    subscriber's failure doesn't stop the others, same isolation pattern
    as search_news's per-source error handling -- but unlike that
    isolation, every outcome here is both printed (docker logs captures
    stdout) and recorded in push_outcomes rather than swallowed silently,
    including ticks where nobody was due -- not just when something
    actually sends. See _record for why both, and
    docs/plans/incident-monitoring-plan.md for what reads the rows.

    `embedder=None` (the default) threads straight through to every
    select_candidate_articles call, which degrades near-duplicate
    collapse and offbeat selection to their pre-2026-08-25 behavior
    rather than failing -- see news_embed's module docstring.

    The cache is read ONCE per cycle and reused across every subscriber
    -- matches docs/plans/local-news-cache-plan.md's stated efficiency argument
    for a shared cache ("one Perigon call can satisfy every subscriber
    whose interests match it"), and avoids N redundant directory scans
    for N due subscribers in the same tick.

    Real incident, 2026-08-09, two parts: (1) a subscriber reported never
    receiving a push despite users_db showing a completed cycle -- there
    was no way to confirm from logs alone whether `send` actually ran,
    since nothing was ever printed either way; (2) fixing that alone
    turned out insufficient -- the container's stdout was block-buffered
    (Python's default when stdout isn't a TTY, true for any `docker run
    -d` container), so prints were never reaching `docker logs` at all
    regardless of what they said (fixed separately: PYTHONUNBUFFERED=1
    in the Dockerfile). Once both were fixed, a THIRD gap remained: the
    same subscriber asked why a nominal 30-minute interval sometimes took
    close to 45 -- unanswerable because only *due* subscribers were ever
    logged, not the tick itself or why a not-yet-due subscriber was
    skipped. This function now logs every tick's summary and every
    subscriber's due-check outcome, not just successful sends."""
    now = now or datetime.now(timezone.utc)
    # Recorded unconditionally, before any per-subscriber due-check -- see
    # healthcheck.py's docstring for why: every subscriber can legitimately
    # be "not due yet" for hours (push_interval_hours), which isn't the
    # same as this job having stopped running entirely.
    users_db.set_source_last_pulled_at(healthcheck.PUSH_TICK_KEY, now)
    # Bounded here rather than on a timer of its own: this is the only job
    # that writes the table, so it is the only one that can grow it.
    users_db.prune_push_outcomes(now)
    subscribers = users_db.list_push_enabled_subscribers()
    print(f"[news_push] tick at {now.isoformat()}: {len(subscribers)} push-enabled subscriber(s)")
    _emit_heartbeat(len(subscribers))
    cached_articles = news_cache.read_all()
    for subscriber in subscribers:
        chat_id = subscriber["chat_id"]
        interests = subscriber["interests"]
        if not interests:
            _record(chat_id, users_db.PUSH_NO_INTERESTS,
                    "push enabled but no interests set -- skipping", now)
            continue
        last_push_at = subscriber["last_push_at"]
        interval_hours = subscriber["push_interval_hours"]
        if not is_subscriber_due(last_push_at, interval_hours, now):
            elapsed = (now - last_push_at).total_seconds() / 3600 if last_push_at else None
            print(
                f"[news_push] chat_id={chat_id}: not due yet "
                f"(interval={interval_hours}h, elapsed={elapsed:.2f}h)"
            )
            continue
        print(f"[news_push] chat_id={chat_id}: due -- checking for new articles")

        # What record_push must be told, and whether it must be told at
        # all. None until an LLM has been paid to write a digest; a list
        # from then on. Load-bearing twice over:
        #
        # Not-None means the money is spent, so the next attempt must be a
        # full interval away even though this cycle failed. Leaving
        # last_push_at untouched instead -- which is what every failure
        # path used to do -- makes the subscriber due again on the very
        # next tick, and the tick is PUSH_TICK_SECONDS (15 minutes), not
        # their interval.
        #
        # Its CONTENTS matter for a narrower case: a send can succeed and a
        # later step still raise (the outcome insert, or record_push
        # itself). Recording [] there would leave articles the subscriber
        # genuinely received still eligible, and they would be sent again.
        delivered: list[str] | None = None
        try:
            topic_categories = _model_call(resolve_interest_categories, model, interests)
            # One message per interest, longest-un-pushed first. Two
            # reasons this replaced a single combined digest:
            #
            # Retrieval. The interest string IS the query, and merging
            # five of them into one candidate pool then asking one model
            # call to write about all of it discards the specificity that
            # made each one findable -- the same "any category layer
            # between the interest and the articles costs recall" result
            # measured in docs/analysis/cluster-measurements.md.
            #
            # Rotation. MAX_INTERESTS_PER_PUSH bounds how noisy a cycle
            # can be, and staleness ordering is what stops that bound from
            # permanently starving whatever sorts last. An interest with no
            # new articles does NOT consume one of the cycle's slots --
            # only a message that was actually sent does.
            sent = 0
            blocked = 0
            send_failure: tuple[str, str] | None = None
            for topic in users_db.interests_by_staleness(chat_id, interests):
                if sent >= MAX_INTERESTS_PER_PUSH:
                    break
                new_articles = select_candidate_articles(
                    cached_articles,
                    [topic],
                    topic_categories,
                    subscriber["last_push_at"],
                    # What this cycle has already delivered counts as seen
                    # too. Without the union, an article that matches two
                    # of a subscriber's interests goes out twice in the
                    # same push -- record_push only runs at the end, so
                    # pushed_links cannot know about it yet.
                    set(subscriber["pushed_links"]) | set(delivered or []),
                    include_restricted=subscriber["restricted_sources_enabled"],
                    now=now,
                    embedder=embedder,
                )
                if not new_articles:
                    continue

                digest = _model_call(write_push_digest, model, new_articles,
                                     topic, subscriber["language"])
                # Generated: the money is spent from here on, so the next
                # attempt must be a full interval away even if the rest of
                # this fails. See the module's `delivered` reasoning.
                if delivered is None:
                    delivered = []

                # Checked by presence of a real source link, not just
                # "is the string empty" -- _PUSH_DIGEST_PROMPT's "write
                # nothing" instruction asks for a literal empty reply
                # when nothing is relevant, but the model doesn't always
                # comply with that literally; it can instead write a
                # non-empty sentence explaining that nothing was relevant
                # ("No genuinely relevant AI coding stories emerged..."),
                # which `not digest.strip()` would miss entirely and send
                # to the subscriber as if it were real content.
                # TREND_REPORT_STRUCTURE requires every genuine item to
                # carry a real <a href> link, so a digest with none is
                # equivalent to an empty one regardless of what prose
                # surrounds that fact. Reproduced live, 2026-08-25, right
                # after tightening the "be skeptical of the topic label"
                # instruction below -- a stricter prompt makes the model
                # MORE likely to reject everything, which makes this
                # exact gap more likely to fire, not less.
                if not digest or not _HREF_RE.search(digest):
                    # Stage 2 judged none of this interest's candidates
                    # genuinely relevant -- see _PUSH_DIGEST_PROMPT's
                    # explicit "write nothing" instruction. Not an error,
                    # and nothing is retired: an article that merely lost a
                    # relevance judgment has not been seen.
                    continue

                # A stored interest is user-supplied, unsanitized text that
                # ends up embedded in the digest prompt, so the same output
                # guardrail bot.py runs on chat replies applies here.
                if not _model_call(guardrails.is_output_on_topic, model, digest):
                    blocked += 1
                    continue

                try:
                    await send(chat_id, digest)
                except Exception as exc:
                    # Delivery failed, not generation. Recorded as its own
                    # outcome so "we paid to write digests nobody can
                    # receive" stays answerable -- the shape of the
                    # 2026-08-21 incident.
                    #
                    # Stops this subscriber's remaining interests rather
                    # than trying them: whatever makes one send fail
                    # (blocked bot, deleted chat, Telegram down) applies to
                    # the next one too, and retrying would multiply the
                    # failure by MAX_INTERESTS_PER_PUSH.
                    send_failure = (_classify_send_failure(exc), repr(exc))
                    break

                # Only what the subscriber actually saw is retired. A
                # candidate the model left out stays eligible for a later
                # digest -- see links_actually_sent.
                delivered.extend(links_actually_sent(digest, new_articles))
                users_db.mark_interest_pushed(chat_id, topic, now)
                sent += 1

            if send_failure is not None:
                outcome, detail = send_failure
                _record(chat_id, outcome,
                        f"{sent} message(s) sent, then delivery failed with {detail}",
                        now, detail=detail)
                if outcome == users_db.PUSH_CHAT_NOT_FOUND:
                    _strike_unreachable_subscriber(chat_id, now)
            elif sent:
                # One outcome per subscriber per cycle, NOT one per message.
                # The three live alert criteria in
                # docs/plans/incident-monitoring-plan.md are thresholds over
                # this table; making a cycle emit N rows instead of 1 would
                # silently rescale every one of them.
                #
                # `blocked` folded into the detail rather than dropped: this
                # branch wins over the `elif blocked` one below whenever
                # ANY interest got through, so a guardrail misfiring on one
                # specific interest while the subscriber's others keep
                # succeeding would otherwise leave no trace anywhere -- not
                # in this detail, not printed, not in the span. Still
                # correctly not marked pushed and still retried next cycle;
                # this only fixes whether a human investigating later has
                # something to go on.
                detail = f"{sent} interest(s)" + (f", {blocked} blocked" if blocked else "")
                _record(chat_id, users_db.PUSH_DELIVERED,
                        f"sent {sent} message(s), one per interest", now, detail=detail)
            elif blocked:
                _record(chat_id, users_db.PUSH_BLOCKED,
                        f"{blocked} digest(s) blocked by output guardrail, none sent", now)
            elif delivered is not None:
                _record(chat_id, users_db.PUSH_NOT_RELEVANT,
                        "candidates found but none judged relevant -- not sending", now)
            else:
                # Nothing anywhere had new articles. last_push_at still
                # advances so the next check is a full interval away
                # instead of re-checking every tick.
                _record(chat_id, users_db.PUSH_NOTHING_NEW,
                        "due, but no new articles -- advancing last_push_at only", now)

            users_db.record_push(chat_id, delivered or [], now)
        except _ModelStageError as exc:
            # A 402, a rate limit, a provider outage. Unlike everything
            # else here this is never about one subscriber -- if the model
            # is down for this chat it is down for all of them -- which is
            # why criterion 2 alerts on a single occurrence rather than on
            # a threshold.
            detail = repr(exc.__cause__ or exc)
            _record(chat_id, users_db.PUSH_MODEL_ERROR,
                    f"model call failed with {detail}", now, detail=detail)
            if delivered is not None:
                users_db.record_push(chat_id, delivered, now)
            continue
        except Exception as exc:
            detail = repr(exc)
            _record(chat_id, users_db.PUSH_CYCLE_FAILED,
                    f"cycle failed with {detail}", now, detail=detail)
            if delivered is not None:
                users_db.record_push(chat_id, delivered, now)
            continue
