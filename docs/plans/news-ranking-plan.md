# News Ranking & Digest Sizing Plan

Research and design for replacing the current fixed-cap digest with one
sized by how much genuinely good news there actually is. **Nothing here
is built** — this doc is the survey and the recommendation, same pattern
as `model-portability-plan.md` was before implementation.

**The ask:** stop treating 5 as a ceiling. If today has more important or
interesting news, push more; if it's a quiet day, push less. That needs a
working definition of "important" and "interesting" — which is the hard
part, and what most of this doc is about.

A Chinese translation of this document is kept at
`news-ranking-plan.zh.md`. Both are maintained together — if one changes,
the other must too, or they stop being the same document.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Survey of importance/newsworthiness scoring methods | **Done — this doc** |
| 2 | Survey of user-preference matching methods | **Done — this doc** |
| 3 | Survey of digest-sizing (how many to send) methods | **Done — this doc** |
| 4 | Decide which to adopt | **Not decided** — options in "Options on the table" below, awaiting review |
| 5 | Implementation | Not started |
| 6 | Source-collapse regression (found live 2026-08-18) | **Diagnosed, deliberately not fixed yet** — observation window open until ~2026-08-20, see below |

---

## The incident that reframed this doc (2026-08-18)

Written a day after the survey below, and it changes what the
recommendation should be — so it goes first.

**Symptom, reported by the operator**: the last push digest was
effectively all from one source.

**Root cause**: `news_push.select_candidate_articles` sorts the entire
cache by `published_dt` descending and takes the top `max_per_topic` per
topic. There is **no source-diversity constraint anywhere in the
selection path**. Whichever source publishes most frequently wins the
recent slice outright.

**Live evidence** (1262 cached articles, measured on the production VM):

| Source | Share of whole cache | Share of **newest 50** |
|---|---|---|
| `hackernews` | 33% (422) | **64% (32)** |
| `engadget` | 3.8% | 12% |
| `marketwatch` | 3.5% | 10% |
| The other 17 sources combined | ~55% | 14% |

Hacker News's `search_by_date` returns a continuous stream of brand-new
submissions (dozens per hour); an RSS feed publishes maybe 10-30 per day.
Under a purely recency-ordered selection, HN wins by publish rate alone.

**Why it only appeared now.** The flaw is pre-existing — selection has
always been recency-only. But until 2026-08-16 every source was capped at
5 articles per ingestion cycle, and *that low cap was flattening the
source distribution by accident*, hiding the flaw. Raising the caps
(RSS 5→200, query-capable 5→50, `local-news-cache-plan.md` item 7) removed
that accidental protection: the pool now honestly reflects each source's
real publish rate, and HN dominates. The cap raise did work on its own
terms — the last digest had 26 candidate articles, up from ~5 — but it
bought quantity at the cost of source variety.

**A second, compounding problem in the same finding.** `fetch_hackernews`
uses `search_by_date` deliberately (see `ai-news-sources.md`), so what it
returns is the *newest* submissions — the ones nobody has voted on yet
(live sample: 2, 2, and 1 point). So the feed isn't merely
HN-dominated, it's dominated by **the least community-vetted slice of
HN**. This is the same underlying gap as the discarded
`points`/`num_comments` finding in Part 1b below, seen from the other
side.

**Status: deliberately left running.** The operator chose to keep current
behavior for ~2 days to observe the real impact before changing anything,
rather than reflexively patching. Nothing about the diagnosis is
uncertain; the open question is how much it actually degrades the digests
in practice, which is worth knowing before picking between the options
below.

**What it teaches about the design**, and why it belongs in this doc
rather than being filed as a standalone bug: it is direct evidence that
**recency alone is not a ranking function**. Any scoring worth adopting
has to encode both "many outlets independently covered this" (Part 1b's
cross-source corroboration) and "one outlet should not fill the digest"
(diversity as an explicit constraint). Those are two different things and
both are missing today.

---

## Part 1 — How "importance" gets defined

Three distinct traditions, which get conflated in casual discussion but
answer different questions.

### 1a. Journalism theory — what editors have always meant by "news value"

The foundational taxonomy is **Galtung & Ruge (1965)**, revised by
**Harcup & O'Neill (2001, and again 2017)** — the most-cited framework in
journalism studies, and the vocabulary most "importance" heuristics are
implicitly reaching for. Harcup & O'Neill's revised list:

| News value | What it means |
|---|---|
| **Magnitude** | Scale of impact — how many people affected, how much money |
| **Relevance** | To the audience's own life/interests |
| **Surprise** | Unexpectedness, departure from the routine |
| **The power elite** | Involves influential organizations or people |
| **Celebrity** | Involves already-famous individuals |
| **Bad news / Good news** | Conflict, failure, disaster / rescue, breakthrough |
| **Follow-up** | Advances a story already in the audience's head |
| **Entertainment** | Human interest, oddity |
| **Audibility/agenda** | Fits the outlet's own priorities |

**Why this matters here**: these are the criteria a human editor uses,
which makes them exactly what an LLM judge should be asked about — they
are *describable in a prompt* in a way that a click-through model is not.
Several map directly onto this project's domain (Magnitude, Surprise,
Power elite, Follow-up) and several clearly don't (Celebrity,
Entertainment).

### 1b. Computational signals — importance inferred from data, no model needed

These are what production news aggregators actually run on. Ranked here by
how applicable each is to *this* project specifically.

| Signal | How it works | Applicable here? |
|---|---|---|
| **Cross-source corroboration** | Count how many *independent* outlets covered the same story. The single strongest importance proxy in practice — editors independently deciding something matters is a real, hard-to-fake signal. | **Yes, and it's free.** 21 registered sources already pulled into one cache every cycle. Requires story clustering (below), not new data. |
| **Burst detection** | A term/entity's frequency spikes sharply above its own recent baseline. Classic event-detection method, usually paired with clustering. | **Yes, cheaply.** 48h of cached articles is enough for a crude baseline; a proper one needs longer retention. |
| **Source authority** | Weight by publisher reputation. Google News names this explicitly ("authoritativeness") alongside prominence and freshness. | **Yes, trivially** — but only as a hand-assigned per-source weight (Reuters > a gadget blog). No link graph available to compute it properly. |
| **Freshness / recency decay** | Score decays with age; breaking news outranks the same story a day later. | **Yes** — `published_dt` is already parsed and stored. |
| **Engagement / social signals** | Upvotes, comments, shares. | **Partially — and there's free data being discarded today.** See the finding below. |
| **Entity salience** | Is a major company/person *central* to the story or merely mentioned? | Possible, but needs NER or an extra LLM pass. Lower value per unit of work. |
| **Link/citation graph** | PageRank-style over which outlets cite which. | **No.** RSS gives no link graph. |

**Concrete finding, verified live while writing this**: Hacker News's
Algolia API already returns `points` and `num_comments` on every hit, and
`news_sources.fetch_hackernews` **discards both** — it maps only title,
link, source, summary, and dates. That's a real engagement signal already
being fetched and thrown away, at zero additional API cost.

**Important caveat on that signal, also verified live**: `fetch_hackernews`
uses `search_by_date` (deliberately — see `ai-news-sources.md`), so it
returns the *newest* stories, which have barely accumulated any votes yet.
A live sample returned points of 2, 2, and 1. Points are only meaningful
after a story has had hours to accumulate them, so this is useful for
*re-scoring cached articles on later cycles*, not for scoring at first
fetch. Worth knowing before treating it as a drop-in importance number.

### 1c. LLM-as-judge — ask a model to score it

Now a well-established evaluation paradigm, with three standard shapes:

| Shape | How | Trade-off |
|---|---|---|
| **Pointwise** | Score each article independently (e.g. 1-5 Likert). | Cheapest and most scalable; scores drift between batches, so absolute thresholds are unstable. |
| **Pairwise** | "Which of these two matters more?" | Most reliable per judgment; O(n²) comparisons, too expensive at any real volume. |
| **Listwise** | Hand the model the whole candidate list, get back a ranked order. | One call for the whole batch; quality degrades as the list gets long. |

Research on absolute (pointwise) LLM relevance judgments on fine-grained
ordinal scales finds them workable but sensitive to scale design — which
matches this project's own hard-won lesson that **structured output with
one field per question beats asking for a single composite score**
(`system-overview.md` Appendix B.1: the structured-output guardrail scored
15/15 where a compact text-prompt variant scored 1/15).

---

## Part 2 — How "what this user wants" gets found

| Method | How it works | Applicable here? |
|---|---|---|
| **Content-based filtering** | Match article content against a profile built from what the user liked/stated. | **Yes — this is what's built today** (`news_classify.py`'s 13 categories + stated interests). |
| **Collaborative filtering** | "Users like you also read X." | **No.** Structurally impossible — needs many users with overlapping histories; this deployment has a handful. Also has a documented cold-start problem for new items, which is *every* item in a news feed. |
| **Hybrid** | CF + content-based, the standard production architecture. | **No** — the CF half isn't available. |
| **Semantic embeddings** | Embed articles and the user's interest profile in the same vector space, rank by cosine similarity. Sentence-BERT-style. The standard answer to cold-start, precisely because it needs no interaction history. | **Yes — the strongest available upgrade** to today's coarse 13-category matching. "AAOI" or "fiber-optic components" can't be expressed as one of 13 categories, but is expressible as an embedding. |
| **Implicit feedback** | Clicks, dwell time, opens. | **No, and this is the real structural gap** — a Telegram push has no click or read signal coming back. Nothing in the current architecture can observe whether a digest was read, let alone liked. |
| **Explicit feedback** | Thumbs up/down, per-item ratings. | **Yes, and cheap to add** — Telegram inline buttons on each digest. This is the only realistic way this project ever gets a preference signal, since implicit feedback is unavailable. |

**The one-sentence version**: nearly all modern news-recommendation
research assumes a click stream this project cannot produce. What's left —
and what's actually appropriate at this scale — is content-based matching,
made much sharper by embeddings, plus explicit feedback if it's worth
adding a button for.

---

## Part 3 — How many to send (the actual question asked)

| Method | How it works | Trade-off |
|---|---|---|
| **Fixed top-N** | Always send N. | What's built today. Simple; sends filler on quiet days and truncates on busy ones — exactly the complaint. |
| **Absolute threshold** | Send everything scoring above a fixed bar. | Naturally variable-length. Fragile with LLM scores, whose absolute calibration drifts. |
| **Relative / adaptive threshold** | Bar set from the *recent distribution* — e.g. "top decile of the last 7 days." Standard practice in alerting, precisely to avoid alert fatigue from a static cutoff. | Self-calibrating; needs score history retained (this project's cache TTL is 48h, so this needs a small persistent score table). |
| **Threshold + floor/ceiling** | Adaptive threshold, clamped to min 2 / max ~12. | Guards both failure modes: never an empty-feeling digest, never a wall of text. Telegram's 4096-char limit makes a ceiling load-bearing, not cosmetic. |
| **Marginal-utility cutoff** | Stop when the next item adds little beyond what's already included (redundancy-aware, MMR-style). | Handles the "eight outlets covered the same story" case natively. More complex; partially achieved already by the digest prompt's instruction to synthesize across sources. |

---

---

## Options on the table

Five concrete, independently-decidable options. They are **not** a
sequence to be worked through in order — A is a stopgap, B/C/D are real
designs that could each be adopted alone, E is a prerequisite decision
that changes how B and D get built.

### Option A — Per-source cap in selection (stopgap for the incident)

Limit how many articles any one source can contribute to a single digest
(e.g. 2-3), inside `select_candidate_articles`.

| | |
|---|---|
| **Effort** | ~10 lines, one afternoon including tests |
| **Cost** | Zero — no API calls, fully deterministic |
| **Fixes** | The source-collapse symptom, immediately |
| **Doesn't fix** | Anything about importance. Recency is still the only ranking signal; this just spreads the same unranked selection across more sources |
| **Risk** | Low. Worst case it's later superseded by B/D and deleted |

Honest framing: this is a **symptom patch**, not a ranking design. Its
value is that it's cheap enough to be worth doing even if it gets thrown
away later.

### Option B — Deterministic scoring: corroboration + authority + freshness

Cluster the cache by story, then score each cluster by (number of
distinct sources covering it) × (hand-assigned source authority weight),
decayed by age. Pick the top-scoring *clusters*, one representative
article each.

| | |
|---|---|
| **Effort** | Days, not hours. Needs story clustering (see Option E) |
| **Cost** | Zero recurring — no LLM calls in the scoring path |
| **Fixes** | Both problems at once: it encodes the strongest real-world importance proxy, *and* selecting per-cluster rather than per-article makes single-source domination structurally impossible |
| **Doesn't fix** | "Interesting to *this specific user*" — it's a global importance score, personalization still comes from the existing category filter |
| **Risk** | Medium. Clustering quality is the whole ballgame; bad clustering makes the corroboration count meaningless |

Also folds in the two free signals currently discarded: HN's
`points`/`num_comments` (on re-scoring passes, not at first fetch, per
Part 1b's caveat).

### Option C — Adaptive digest size (the original ask, literally)

Replace the fixed `MAX_ARTICLES_PER_TOPIC` with a threshold set from the
recent score distribution, clamped by a floor and ceiling.

| | |
|---|---|
| **Effort** | Small *in itself* — but meaningless without B or D, since it needs a score to threshold on |
| **Cost** | Zero recurring; needs a small persistent score-history table (the 48h cache TTL is too short to calibrate against) |
| **Fixes** | Digest size genuinely tracking how much good news there is |
| **Risk** | Low, *provided* floor/ceiling clamps exist — Telegram's 4096-char limit makes the ceiling load-bearing, not cosmetic |

**Dependency worth being explicit about**: this is the option that
actually answers "don't let 5 be a ceiling," but it cannot be built
first. It's a consumer of whatever scoring B or D produces.

### Option D — LLM judge on importance

Pointwise scoring with structured output, **one field per criterion**
(magnitude / surprise / power-elite / relevance-to-this-user), using the
Harcup & O'Neill vocabulary from Part 1a rather than one vague
"importance 1-10".

| | |
|---|---|
| **Effort** | Moderate — the call itself is easy; getting the rubric right is the work |
| **Cost** | **Real and recurring.** One extra call per ingestion cycle at minimum |
| **Fixes** | The only option that can judge "interesting", not just "corroborated". Handles the single-source scoop that B would score low precisely *because* only one outlet has it |
| **Risk** | Highest. LLM absolute scores drift between batches, which interacts badly with Option C's thresholding. Must be measured, not assumed better |

The structured-output-per-criterion shape is not a stylistic preference —
it's this project's own measured finding (`system-overview.md` Appendix
B.1: 15/15 vs 1/15 for a compact single-score prompt variant).

### Option E — Embeddings (a prerequisite decision, not a feature)

Whether to introduce sentence embeddings at all.

| | |
|---|---|
| **Unlocks** | Story clustering for Option B (title-overlap heuristics are the free-but-crude alternative), *and* semantic interest matching from Part 2 — "AAOI" or "fiber-optic components" can't be expressed in 13 categories but is expressible as a vector |
| **Cost** | Depends entirely on local vs API. A local sentence-transformer model on a 1 GB VM is a real memory question (principle P5); an embedding API is a new recurring cost and a new dependency |
| **Why it's listed separately** | Doing it once, properly, serves both clustering and personalization. Deciding it silently inside Option B's implementation would be the wrong way to make a decision this size |

### What is deliberately not recommended

- **Collaborative filtering** — structurally impossible at this user count.
- **Implicit-feedback modeling** — no click signal exists to model, and
  the Telegram push architecture cannot produce one.
- **Explicit feedback buttons** — genuinely useful and cheap, but a
  *product* decision (does the operator want subscribers asked to rate
  things?), not a ranking one. Worth deciding separately from all of the
  above.

### If a single path has to be picked

**A now, E decided next, then B, then C.** D last and only if B+C prove
insufficient — it's the only option with recurring cost, and this
project's own history (Appendix B.1) is a direct lesson that the
plausible-sounding upgrade has to be measured before it's trusted.

But the 2-day observation window is the right immediate move regardless:
it costs nothing and it tells us whether source collapse is actually
degrading the digests or merely looks wrong on paper.

## Sources

- Harcup & O'Neill, [*What is News? News values revisited (again)*](https://www.tandfonline.com/doi/full/10.1080/1461670X.2016.1150193) (2017) — and the [2001 Galtung & Ruge revisit](https://www.tandfonline.com/doi/abs/10.1080/14616700118449)
- Google, [Ranking within Google News](https://support.google.com/news/publisher-center/answer/9606702?hl=en) and [Understanding How News Works on Google](https://www.google.com/intl/en_us/search/howsearchworks/how-news-works/) — prominence / authoritativeness / freshness
- [*A Survey of Personalized News Recommendation*](https://link.springer.com/article/10.1007/s41019-023-00228-5) (Data Science and Engineering, 2023)
- Wu et al., [*Personalized News Recommendation: Methods and Challenges*](https://dl.acm.org/doi/10.1145/3530257) (ACM TOIS)
- [*Likert or Not: LLM Absolute Relevance Judgments on Fine-Grained Ordinal Scales*](https://arxiv.org/html/2505.19334)
- [*Efficient Pointwise-Pairwise Learning-to-Rank for News Recommendation*](https://arxiv.org/html/2409.17711v1)
- [*LLMs-as-Judges: A Comprehensive Survey on LLM-based Evaluation Methods*](https://arxiv.org/html/2412.05579v2)
- [*Key News Event Detection and Event Context Using Graphic Convolution, Clustering, and Summarizing Methods*](https://www.mdpi.com/2076-3417/13/9/5510) — clustering + burst detection
- Reuters Tracer, [*Toward Automated News Production Using Large Scale Social Media Data*](https://arxiv.org/pdf/1711.04068) — three-grade newsworthiness criteria
- [*General Item Representation Learning for Cold-start Content Recommendations*](https://arxiv.org/pdf/2404.13808) and [*Language-Model Prior Overcomes Cold-Start Items*](https://arxiv.org/pdf/2411.09065)
