# Local News Cache Plan

This doc captures the design, same pattern as the other `docs/*-plan.md`
files. **All open questions are resolved (see below) — implementation is
in progress.**

**The ask:** stop calling news sources live on every query. Instead, pull
from all enabled sources on a schedule, cache what's fetched locally with
a rough pre-classification, and have `search_news` query the cache —
filtering first by rough category, then by content — instead of hitting
the network per request.

## Status

| # | Item | Status |
|---|---|---|
| 1 | Periodic ingestion job (pull all sources on a schedule) | **Built** — `news_ingest.py`, per-source interval + daily budget respected, wired into both `bot.py` and `combined_bot.py`'s scheduler |
| 2 | Local cache (one file per article, 2-day TTL, auto-cleanup) | **Built** — `news_cache.py`, verified live: real fetch → real classification → real YAML file, contents match this doc's spec exactly |
| 3 | Article classification (rough category tags) | **Built** — `news_classify.py`, one batched structured-output call per cycle, verified live against the real DeepSeek model (not just mocked) |
| 4 | Two-stage query filtering (category, then content) | **Built for the push path (item 6), not yet for `search_news` (item 5).** |
| 5 | `search_news` rewritten to read the cache instead of live sources | **Not built — deliberately deferred.** Changes live, user-facing agent behavior (`agent.py`'s tool, and `guardrails.classify_message`'s output schema); items 1–3 are additive and don't touch anything currently running, so they shipped first. See "Next step" below. |
| 6 | `news_push.py` converging onto the same cache | **Built 2026-08-15** — see "Interaction with `news_push.py`" below, rewritten in place rather than left as a follow-on. |

**Why item 6 moved from "optional" to "built" ahead of item 5:** a real
incident forced it. `news_push.py` calling live, query-blind RSS sources
directly (Nikkei Asia's general top-stories feed, in this case) put an
Indonesia earthquake and a Japan-society piece into a subscriber's tech
digest — nothing in the old push pipeline could catch that, since no
relevance filter existed before the digest-writing prompt at all. This
was exactly the gap items 4/6 were meant to close; deferring it further
once it was visibly causing incorrect output wasn't defensible the way
deferring it pre-emptively was. `search_news` (item 5) keeps its original
deferral reasoning — it's synchronous and user-facing, needs the harness
(`tools/measure_guardrails.py`) extended to cover it before changing,
and doesn't have an equivalent live incident forcing the same urgency.

**Next step:** rewire `search_news` and extend the router's classification to emit query categories (item 4/5). Held back from this pass because it's the one piece that touches the live agent pipeline directly, and this project's own guardrail history (`docs/system-overview.md` Appendix B.1) is a direct lesson that changes to that pipeline need live measurement before shipping, not just review. Items 1–3 needed no such gate — nothing currently calls `news_ingest.py` from any user-facing path, so they could be built, tested, and verified live without any risk to what's actually running today.

## Why this is worth doing (not just "instead of on-demand")

Two things converged to motivate this, both surfaced this session:

**1. Most sources can't tell you "no match" — they just dump their latest
items regardless of the query.** Confirmed directly in `news_sources.py`:
of the 21 currently-enabled sources, only `hackernews` and `arxiv` (plus
the three dormant key-gated ones) actually filter by the search query. The
other 16+ are `rss`-class — they return their latest N items no matter
what was asked. When a subscriber asked about "AAOI" (a small fiber-optics
company), `search_news` reported "30 articles found across 7 sources," but
none were actually about AAOI — the count was padded by AI-blog sources
returning their latest posts regardless of relevance. **The only thing
that caught this was the model reading 30 titles and judging none of them
matched** — an unstructured, per-call, non-reproducible judgment, not a
code-level signal.

**2. A pre-classified local cache turns that into a code-level check.**
If every cached article already carries rough category tags, "does this
plausibly cover topic X" becomes a filter over structured data instead of
the model eyeballing a live dump on every single query. This is the same
move this project's guardrail history already learned to make the hard
way (`docs/system-overview.md` Appendix B.1): things that must be judged
correctly belong in code, not in a model's free-text reading of raw
content, wherever that's achievable.

**A secondary but real benefit:** 21 sources called live on every single
`search_news` invocation is real per-message latency and load on other
people's infrastructure. Pulling on a schedule instead means one round of
network calls per cycle, not one per user message.

## Proposed architecture

```
[APScheduler tick, every N hours]
        |
        v
  for each enabled source in news_sources.SOURCE_REGISTRY:
        fetch latest articles
        |
        v
  [one batched structured-output LLM call per cycle]
        classify all newly-fetched articles -> category tags per article
        |
        v
  write one YAML file per article to the cache directory
        |
        v
  [cleanup sweep] delete any cached file older than 2 days (by fetched_at)


[a chat message or push cycle needs articles]
        |
        v
  Stage 1 -- category filter (deterministic, in code)
        narrow the cache to files tagged with a plausibly-relevant category
        |
        v
  Stage 2 -- content filter (same mechanism as today)
        the model reads titles/summaries in the narrowed set and picks real matches
        |
        v
  synthesize a reply / digest from what's left
```

The key shift: **stage 1 happens before the model ever sees anything.**
Today the model reads through everything every source returned, including
whatever the 16 query-blind RSS sources dumped. After this change, it only
ever reads a pool that's already been narrowed by category — which is
smaller, faster, and doesn't require the model to silently discard
irrelevant AI-blog noise on every single call the way it does today.

## Cache format

**File naming:** `{source}-{id}.yaml`, where `id` is a short hash of the
article's `link` — e.g. `bbc_business-a3f9c21e.yaml`. Hashing the link
(rather than an incrementing counter or the source's own ID field, which
not every source provides) means re-fetching the same article in a later
cycle produces the same filename and just overwrites harmlessly, instead
of creating a duplicate — free deduplication, no separate tracking state
needed.

**File contents (YAML):**

```yaml
source: bbc_business
title: "Nvidia in talks to invest in CoreWeave amid cloud expansion"
link: "https://www.bbc.co.uk/news/articles/..."
summary: null              # most RSS sources don't provide one; null is fine
published: "Thu, 13 Aug 2026 10:00:00 GMT"   # raw string, as today
published_dt: "2026-08-13T10:00:00+00:00"    # parsed, as today
fetched_at: "2026-08-13T12:00:05+00:00"      # when THIS system pulled it
categories: [IT, Hardware, Finance, Stock]
```

**Retention is based on `fetched_at`, not `published_dt`.** Some sources'
dates don't parse (already a known gap — see `_parse_rss_published`
returning `None`), but `fetched_at` is always known and controlled by our
own code, so the 2-day cleanup sweep has a value it can always trust.

**Storage:** flat files in a directory, matching the user's stated
preference for now. Following the same configurability convention as
`users_db.py`'s `SUBSCRIBERS_DB_FILE` — a `NEWS_CACHE_DIR` env var,
defaulting to a local relative path, pointed at the mounted `/data`
volume in the deployed container so it survives restarts the same way
`subscribers.db` does.

**Real storage estimate**, so this isn't a guess: 21 sources × ~5 articles
per pull × 6 pulls/day (if the interval is 4h) ≈ 630 files/day, ~1,260 at
steady state under a 2-day TTL. At roughly 1–2 KB per YAML file, that's
**under 3 MB total.** Checked the deployed VM directly: 35 GB free of 45
GB. This is not a real constraint at this scale, and isn't worth
optimizing around.

## Category taxonomy (proposed)

Multi-label, not single-label — the worked example from this request
("Nvidia investigates CoreWeave and its stock goes up") should plausibly
carry several tags at once, not be forced into one bucket.

| Category | Covers |
|---|---|
| **AI** | AI models, research, agents, LLMs |
| **Software** | Software products, dev tools, programming |
| **Hardware** | Chips, semiconductors, devices, infrastructure hardware |
| **IT** | Enterprise IT, cloud, infrastructure, enterprise software |
| **Startups** | Funding rounds, new companies, venture capital |
| **Finance** | Business/financial industry news, economics, corporate deals |
| **Stock** | Stock price moves, market reactions — distinct from Finance: this is specifically about market/price impact, not business news generally |
| **Policy** | Regulation, government, legal, antitrust |
| **Security** | Cybersecurity, breaches, vulnerabilities |
| **Research** | Academic papers, science (arXiv-flavored) |
| **Consumer** | Consumer gadgets, reviews, product launches for individual users |
| **Robotics** | Robotics specifically — called out separately because it's already a real subscriber interest (`機器人科技`), not something "AI" or "Hardware" alone captures well |
| **Crypto** | Cryptocurrency/blockchain — also already a real subscriber interest (`bitcoin`) |

Worked example, checked against this table: "Nvidia in talks to invest in
CoreWeave, stock reacts" → `IT` (cloud infrastructure), `Hardware` (chips),
`Finance` (the deal itself), `Stock` (the price reaction). All four
present, matching the request's own example.

**Resolved as a v1, not a final answer.** Confirmed this table is coarser
than it will eventually need to be — but the fix is refining it later
against real classified data, not blocking implementation on getting it
right up front. A category taxonomy is cheap to revise (relabel/resplit
existing cache entries, no schema migration, no code path depends on the
exact category names beyond string matching) — better to ship this
version, see what real articles actually get tagged, and refine from
observed gaps than to keep guessing categories against no data at all.

## Classification mechanism

**Proposed: one batched structured-output LLM call per ingestion cycle**,
not one call per article. Send the cycle's full batch of newly-fetched
articles (title + summary + source) in a single call, get back a list of
`{article_id, categories: [...]}`.

**Why batched, not per-article:** at ~630 articles/day, per-article calls
would mean 630 LLM calls/day just for classification — real, avoidable
cost. Batching by cycle means one call per pull (4–6 calls/day at a 4–6
hour interval), each classifying everything that cycle fetched at once.

**Why a cheap model fits here:** this is exactly the shape
`docs/model-portability-plan.md`'s "Level 2 — per-stage model routing"
already anticipated — a small/fast model is sufficient as long as it
supports structured output, same as the router and the output-check
already use. No new infrastructure decision, just another consumer of a
seam that already exists.

**What this doc is NOT proposing:** keyword/heuristic tagging instead of
an LLM call. It was considered — zero marginal cost, no model dependency
— but rejected as the primary mechanism: this project's own guardrail
history is a direct lesson that hand-rolled text-matching heuristics are
exactly the kind of thing that quietly breaks in ways that are hard to
notice (`docs/system-overview.md` Appendix B.1). A cheap structured-output
model call is more reliable for the same reason it already won for the
router and the output check. Per **P4** (accuracy raised by
post-deployment testing, not assumption), classification accuracy should
still be spot-checked against real articles before being trusted at
scale — that's an implementation-phase task, not a planning-phase one.

## Two-stage query filtering, in more detail

**Stage 1 (category filter) needs to know which categories to look at.**
Two different call sites, two different mechanisms — reusing
infrastructure that already exists in both cases, rather than adding a
new classification pass:

- **Push digests** (`news_push.py`): a subscriber's `interests` are
  already stored and stable. Classify each interest into categories once,
  when it's set (`update_interests`/`/interests`), and cache that mapping
  — cheap, since it only runs on change, not per push cycle.
- **On-demand chat queries** (`agent.py`'s `search_news` tool): most real
  queries are one-off natural language, not tied to a stored interest at
  all (the AAOI question itself never touched `/interests`). Extend the
  router's existing structured-output classification
  (`guardrails.classify_message`) to also emit likely categories for the
  query — one more field on a call that already happens for every
  message, not a new call.

**Stage 2 (content filter) is unchanged from today** — the model reads
titles/summaries within the now-much-smaller, already-fresh candidate
pool and picks real matches, exactly like it does now. The difference is
what it's reading: a category-narrowed slice of a 2-day cache, not a live
dump padded by 16 sources that ignored the query entirely.

## What this doesn't solve, and the resulting recommendation

**Important limitation to be honest about:** a periodic pull of "latest N
from each source's homepage/category feed" does not guarantee coverage of
a low-profile, specific-company query. If AAOI simply never appears in
any of the 21 sources' latest items during a given 2-day window, the cache
won't have it either — no different from today. The cache makes relevance
*filtering* correct and cheap; it doesn't manufacture coverage that
doesn't exist.

**Resolved design: per-source pull frequency, one mechanism, not two.**
Earlier drafts of this doc proposed reserving budget-limited sources
(Perigon) for reactive on-demand fallback only, excluded from the
periodic pull entirely. Revised: **every source participates in the same
periodic ingestion job, each at its own safe frequency** — simpler than
maintaining two separate mechanisms (scheduled pull for free sources,
reactive-only fallback for capped ones), and it means a capped source's
scarce calls proactively broaden the cache instead of only reacting to a
miss.

| Source | Frequency | Why |
|---|---|---|
| Unrestricted (RSS, `hackernews`, `arxiv`) | Every 4h | No real budget constraint |
| GNews | Every 4h | 100/day budget comfortably covers 6 pulls/day |
| Perigon | 3x/day | 150/month budget — see below |
| NewsAPI | 1x/day | Individual-use judgment — see below |

A live, reactive fallback for a specific on-demand query that still comes
up empty after all of the above (an AAOI-style edge case) remains a
possible later addition, but isn't required for v1 — proactively pulling
from Perigon and NewsAPI several times a day already broadens the
cache's real-search coverage well beyond what the free-tier RSS sources
alone provide, which should make a total miss meaningfully rarer than it
is today.

**Every pull — scheduled, any source — writes into the same shared
cache**, not a private result for whoever triggered it. This is the
actual reason a budget-limited source is worth having in the cache
architecture at all: **one Perigon call can satisfy every subscriber
whose interests match it for the next two days**, not just one query.
Without a shared cache, a source with a 150/month budget could really
only ever answer 150 individual questions/month across every user
combined — with it, those same 150 calls seed content that any number of
matching queries can draw from within each call's 2-day cache window.

**Worked example: Perigon's budget, concretely.** 150 requests/month,
free/non-commercial tier (see `docs/ai-news-sources.md`'s key-acquisition
section). 3 pulls/day ≈ 93/month, leaving ≈57/month headroom for testing.
When the daily cap is hit, that cycle's pull is skipped (not attempted)
and logged, same shape as `news_push.py`'s existing per-cycle outcome
logging. Tracking the daily count is a small, global piece of state
(source name, date, count — reset when the date rolls over), natural to
add to `users_db.py` alongside everything else it already tracks.

**NewsAPI — added, with its own real constraint documented so it isn't
forgotten.** NewsAPI's free "Developer" plan terms, quoted directly from
their own criteria: *"You should only use the Developer plan if your
project is in development. If your project is being used in production,
please upgrade to a paid plan."* This project's own judgment (recorded
here, not re-litigated further): a personal, unpaid, invite-gated pilot
run by an individual is a reasonable read of "in development," not
"production" in the sense that clause is aimed at — but that judgment
is worth being able to revisit, not silently forgotten. **1 pull/day**,
chosen deliberately conservative relative to whatever NewsAPI's actual
technical rate limit turns out to be (not confirmed live — see
`docs/ai-news-sources.md`). **Trigger to revisit:** if this project is
ever monetized, opened beyond an invite-only pilot, or otherwise starts
looking like the "production" NewsAPI's own terms describe, upgrade to a
paid plan or drop NewsAPI — don't keep running on the Developer plan past
that point.

**Sequencing risk — partially mitigated 2026-08-14, not eliminated.**
The original concern: the Perigon and NewsAPI keys exist in Vault and
`docker-entrypoint.sh` fetches both, but before this plan was built,
nothing stopped `search_news` from calling them unthrottled on every
matching on-demand query from every user. Since resolved for
*unauthorized* usage: `news_sources.RESTRICTED_SOURCES` gates both behind
a per-user DB flag (`docs/ai-news-sources.md`'s "Restricted sources"
section), defaulting to off for everyone except the admin. **What's still
true:** `try_consume_api_budget` is only ever called from
`news_ingest.py` — the admin's own `search_news` usage of Perigon/NewsAPI
is not rate-limited against what the ingestion job already spent that
day/month. For a single admin user this is a much smaller exposure than
"anyone can trigger it," but it's not the same as "protected," and is
worth closing once `search_news` is rewired to read the cache (item 5)
rather than calling these sources live at all. GNews remains unrestricted
by design — its 100/day budget has real headroom beyond what
`news_ingest.py` alone uses.

## Interaction with `news_push.py` — built 2026-08-15

`news_push.py` now reads `news_cache.read_all()` once per push cycle
(shared across every due subscriber in that tick, not re-read per
subscriber) instead of calling `news_sources.enabled_sources()` live.
Two-stage filtering, per the architecture above:

- **Stage 1 (category filter, in code)** —
  `news_push.select_candidate_articles`. Each subscriber's `interests`
  are mapped to categories via `news_push.resolve_interest_categories`,
  which reads/writes a new `users_db.interest_categories` table (global,
  not per-subscriber — the same interest text means the same categories
  for anyone) and only calls `news_classify.classify_interests` for
  interests not already cached. An interest with NO cached category
  (classifier miss) is treated as unrestricted, matching any article,
  rather than matching nothing — the alternative would silently starve a
  subscriber over a classification gap on their own stated interest. An
  article with no categories IS excluded whenever the topic has real
  categories to match against — this is the exact mechanism that keeps a
  general-news RSS item (no tech angle at all) out of a digest.
- **Stage 2 (content filter)** — folded into the existing single
  `write_push_digest` model call rather than a separate LLM call:
  `_PUSH_DIGEST_PROMPT` now explicitly tells the model the category
  filter is coarse and to omit any candidate that survived it but isn't
  genuinely relevant, up to writing nothing at all if none qualify
  (`run_push_cycle` treats an empty/whitespace-only digest the same as
  "no new articles" — advances dedup state, doesn't send).

**Real incident that forced this**, not a speculative improvement: a
subscriber's push digest included an Indonesia earthquake and a Japan-
society piece, sourced from Nikkei Asia's general top-stories RSS feed
(query-blind, like 16 of the 21 registered sources — see "Why this is
worth doing" above). The old push pipeline had zero relevance filtering
between "source returned it" and "model was told to report on it" —
exactly the gap this section's two-stage design was meant to close, just
not yet wired up for the push path specifically. Diagnosed by pulling the
feed directly (`curl https://asia.nikkei.com/rss/feed/nar`) and confirming
it's the site's whole front page, not a tech-scoped vertical.

**Restricted-source gating (NewsAPI/Perigon) is unchanged in spirit,
adapted to the cache**: `select_candidate_articles` checks each cached
article's `source_key` against `news_sources.RESTRICTED_SOURCES` and
excludes it unless the subscriber's own `restricted_sources_enabled` flag
is set — same gate as the short-lived live-fetch version shipped one day
earlier (2026-08-14), now applied to cache reads instead of live calls.

**What stayed the same**: `pushed_links` dedup (the fallback for articles
whose `published_dt` didn't parse), the `since`/`last_push_at` recency
check, and per-subscriber isolation (one subscriber's exception doesn't
stop the cycle) are all unchanged from the live-fetch version — only
*where* the candidate articles come from changed.

## Open questions — all resolved, implementation cleared to start

1. ~~**Pull interval.**~~ **Resolved**: per-source, not one global
   number. See the frequency table above — unrestricted sources and
   GNews every 4h, Perigon 3x/day, NewsAPI 1x/day.
2. ~~**Category taxonomy**~~ **Resolved as a v1**: ship the 13-category
   table as-is, refine later against real classified data rather than
   block on getting it perfect up front.
3. ~~**Live fallback**~~ **Resolved, then superseded**: the original
   "reserve capped sources for reactive fallback" idea was replaced by
   giving every source its own periodic-pull frequency instead (see
   above) — simpler, one mechanism instead of two. A true on-demand
   fallback for a still-empty query remains a possible later addition,
   not required for v1.
4. ~~**Classification model**~~ **Resolved**: the current DeepSeek
   instance, now. `docs/model-portability-plan.md`'s Level 2 routing can
   swap in a cheaper model later without a redesign — independent
   decision, not a blocker.
5. ~~**Cleanup mechanism**~~ **Resolved**: folded into the ingestion
   tick — delete expired, then fetch new, same cycle, one job.
6. **Storage engine, later.** Still the one open item, and deliberately
   left open rather than resolved: files for now, per the original
   request. Same reasoning pattern as `docs/data-layer-plan.md`'s SQLite
   deferral — revisit only if a real trigger shows up (e.g., needing to
   query across cached articles in ways flat files make awkward), not
   preemptively.

## Non-goals for this version

- No embeddings/vector search — stage 2 stays LLM-reads-the-shortlist,
  same mechanism as today. Worth revisiting only if the category filter
  alone proves too coarse in practice.
- No move off SQLite or onto a shared cache store — this is a
  single-process, single-host cache, consistent with
  `docs/data-layer-plan.md`'s current stance on the rest of this
  project's persistence.
- No retroactive backfill of historical articles — the cache starts
  empty and fills from the first ingestion cycle forward.
