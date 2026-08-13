# News Sources

`news_sources.py` powers the `search_news` tool with a pluggable source
registry — free/no-key sources are always enabled; key-gated sources turn
on automatically once their env var is set. This doc tracks what's wired up,
what's just documented for later, and how to add more.

Originally AI-industry-only. Broadened 2026-08-13 to general tech/business/
finance press, after a real gap: a subscriber asked about a specific
company (AAOI, a fiber-optic components maker) and no source in the
registry covered anything outside AI-industry blogs and community boards.

## Source classes

Every entry in `SOURCE_REGISTRY` is tagged with a class. This is
descriptive, not behavioral — `enabled_sources()` doesn't branch on it —
but it matters for one reason worth knowing before relying on a source's
result count: **most sources here don't actually filter by the search
query.**

| Class | Meaning | Filters by query? |
|---|---|---|
| **forum** | Community-curated discussion board (submissions + votes), not edited articles | Yes — real search |
| **api** | JSON REST API with real query-based search | Yes — real search |
| **rss** | Standard RSS/Atom feed | **No** — returns the feed's latest N items regardless of what was asked |

Of the 21 currently-enabled sources, only **5** (`hackernews`, `arxiv`, plus
the three key-gated `api`-class sources when a key is set) do real
filtering. The other 16+ are `rss`-class and always return their latest
items whether or not any of them are actually relevant — this is why
`search_news` returning a nonzero count is not proof the topic was
matched; the model reading the titles is currently the only thing that
catches this. See `docs/local-news-cache-plan.md` for where this is headed.

## Summary extraction (fixed 2026-08-13)

`_fetch_rss` previously hardcoded `summary=None` for every RSS source,
discarding whatever the feed's own `<description>` provided — meaning the
model only ever saw a bare title, never the lede paragraph a publisher
already wrote. Confirmed live this was throwing away real content: BBC
gives a one-line description, Guardian a full ~1200-char editorial lede,
MarketWatch a sentence explicitly explaining *why* a stock moved (the
exact kind of detail a title alone won't carry). Fixed via `_clean_summary`
(strips embedded HTML, normalizes whitespace, caps at 300 chars — same
cap arXiv's summary already used).

**Verified live across all 21 enabled sources: 18 now return a real
summary. Three are structurally summary-less** — confirmed via
`feedparser`, not assumed:

| Source | Why no summary |
|---|---|
| `hackernews` | Algolia's search API returns metadata about a submission, not body text — most HN posts just link externally, there's nothing to summarize |
| `huggingface_blog` | Feed genuinely has no `summary`/`description`/`content` field at all |
| `nikkei_asia` | Same — the RDF feed provides title/link/date only, no excerpt |

For those three, and for anything a summary's lede doesn't happen to
mention (e.g., a consequence reported later in the article body, not the
opening paragraph), get closer to the truth than a summary offers,
retrieving full article content would be needed — see
`docs/local-news-cache-plan.md`'s open question on this, since it's a
materially bigger decision than this fix (scraping, paywalls, bot
defenses, and per-provider free-tier content truncation all apply).

## Enabled now (free, no key required)

### AI-industry press (original scope)

| Source | Class | Endpoint | Notes |
|---|---|---|---|
| **Hacker News** | forum | `https://hn.algolia.com/api/v1/search_by_date` (Algolia HN Search) | Use `search_by_date`, not the default `/search` — the latter ranks by relevance/points and surfaces old high-upvote posts instead of recent ones. Community-submitted, so quality/relevance is mixed (raw signal, not editorial). |
| **arXiv (cs.AI)** | api | `http://export.arxiv.org/api/query` | `search_query=cat:cs.AI`, `sortBy=submittedDate`, `sortOrder=descending`. Atom XML response, parsed with `feedparser`. Their API rate-limits under repeated calls in a short window (`429`) — transient, not a code issue. |
| **OpenAI Blog** | rss | `https://openai.com/news/rss.xml` | Standard RSS 2.0. |
| **Hugging Face Blog** | rss | `https://huggingface.co/blog/feed.xml` | Standard RSS 2.0. |
| **TechCrunch AI** | rss | `https://techcrunch.com/category/artificial-intelligence/feed/` | Standard RSS 2.0. |
| **VentureBeat AI** | rss | `https://venturebeat.com/category/ai/feed/` | Standard RSS 2.0. |
| **MIT Technology Review** | rss | `https://www.technologyreview.com/feed/` | Main feed, not AI-filtered — but heavily AI-weighted anyway. |

### Mainstream press — Business/Finance (added 2026-08-13)

| Source | Class | Endpoint | Notes |
|---|---|---|---|
| **BBC Business** | rss | `http://feeds.bbci.co.uk/news/business/rss.xml` | |
| **The Guardian Business** | rss | `https://www.theguardian.com/business/rss` | |
| **MarketWatch** | rss | `https://feeds.content.dowjones.io/public/rss/mw_topstories` | |
| **The Economist (Business)** | rss | `https://www.economist.com/business/rss.xml` | Full articles are paywalled; RSS gives headline + summary. Same tier as WSJ links this bot already cites. |
| **Nikkei Asia** | rss | `https://asia.nikkei.com/rss/feed/nar` | **RDF/RSS1.0, not RSS2.0** — feedparser normalizes it the same way as any other feed, but a naive string search for `<item>` (rather than parsing via feedparser) would wrongly read this feed as empty, since RDF uses `<rdf:li>` references instead. Confirmed live during verification. Only source in the registry with an Asia-market angle. |

### Mainstream press — Technology (added 2026-08-13)

| Source | Class | Endpoint | Notes |
|---|---|---|---|
| **BBC Technology** | rss | `http://feeds.bbci.co.uk/news/technology/rss.xml` | |
| **The Guardian Technology** | rss | `https://www.theguardian.com/technology/rss` | |
| **The Economist (Science & Technology)** | rss | `https://www.economist.com/science-and-technology/rss.xml` | Paywalled beyond RSS summary, same as Economist Business. |
| **Wired Business** | rss | `https://www.wired.com/feed/category/business/latest/rss` | |

### Enterprise/industry IT trade press (added 2026-08-13)

| Source | Class | Endpoint | Notes |
|---|---|---|---|
| **The Register** | rss | `https://www.theregister.com/headlines.atom` | Atom, not RSS2.0 — feedparser handles both transparently. 302 redirect, followed automatically. |
| **Computerworld** | rss | `https://www.computerworld.com/index.rss` | 301 redirect, followed automatically. |

### Consumer/gadget tech press (added 2026-08-13)

| Source | Class | Endpoint | Notes |
|---|---|---|---|
| **ZDNet** | rss | `https://www.zdnet.com/news/rss.xml` | Consumer reviews/how-tos, not industry news — different flavor from the rest of the registry. |
| **Engadget** | rss | `https://www.engadget.com/rss.xml` | Consumer gadgets/entertainment tech. |
| **TechRadar** | rss | `https://www.techradar.com/rss` (redirects to `/feeds.xml`) | **Blocks the default `python-requests` User-Agent with `403`** — confirmed live. Fixed by sending a self-identifying User-Agent (`_REQUEST_HEADERS` in `news_sources.py`), applied to every source for consistency rather than as a TechRadar-only special case. |

## User-Agent

Every fetch sends `Mozilla/5.0 (compatible; ArgusNewsBot/1.0; +https://github.com/nankma/argus)`
via `_REQUEST_HEADERS`. This is a **self-identifying** header, not browser
impersonation — added because TechRadar returns `403` to the bare
`python-requests/x.x` default, and a fake browser UA felt like the wrong
fix for an honest news aggregator. Applied everywhere so a future source
hitting the same block doesn't need its own special case.

## Documented, not yet enabled (need an API key)

Implemented in `news_sources.py` but skipped by `enabled_sources()` until
the corresponding env var is set — nothing breaks if it's absent, they're
just not called. **These are the only three sources in the registry that
would have covered the AAOI gap** (real query-based search across broad
press, including financial press) — getting a free-tier key for any one
of them is a smaller change than adding a new source, since these already
exist in code.

| Source | Class | Env var | Endpoint | Free tier | Notes |
|---|---|---|---|---|---|
| **NewsAPI.org** | api | `NEWSAPI_API_KEY` | `GET https://newsapi.org/v2/everything` | Exists, but "Developer" plan is for testing only, not production; exact rate limit/delay not confirmed on the docs page we checked. Has an explicit `business` category, confirming financial-press coverage. | `q`, `sortBy=publishedAt`, `pageSize`, `apiKey` (or `X-Api-Key` header). |
| **GNews** | api | `GNEWS_API_KEY` | `GET https://gnews.io/api/v4/search` | 100 requests/day, 10 articles/request, 1 req/sec, **12-hour delay on articles**, non-commercial only. Resets 00:00 UTC. | `q`, `lang`, `max`, `apikey`. |
| **Perigon** | api | `PERIGON_API_KEY` | `GET https://api.perigon.io/v1/all` | 150 requests/month, non-commercial only. | Response field names (`source.domain`, `summary`, `pubDate`) taken from general docs, **not verified live** — no key to test with. Double-check against a real response before relying on it. |

None of these were live-tested (no credentials available). Endpoint shapes
came from each provider's docs — verify against a real response the first
time a key is actually configured, in case something's drifted.

## Considered, tested live, and rejected

All verified live on 2026-08-13 before being ruled out — per this doc's
own standing rule (see "How to add a new source" below), nothing here is
rejected on a docs page's word alone.

| Source | Why not |
|---|---|
| **CNN** (`rss.cnn.com/rss/cnn_tech.rss`, `money_latest.rss`) | Feed responds `200`, but `lastBuildDate` is over a year stale — the infrastructure is abandoned, not actively publishing. Their newer `edition.cnn.com/business/rss` path returns `404`; no working replacement found. |
| **CNBC** (Technology and Finance sections) | `403 Forbidden` on both, even with a browser-like User-Agent. Blocked at a level beyond what a UA header fixes. |
| **Fortune** | `403 Forbidden`. |
| **Reuters** | `404` — discontinued public RSS in 2020 (previously documented; reconfirmed live this round). |
| **Business Insider** (default `/rss`) | Feed is live, but it's their general firehose (politics, military, world news) — not business/tech-specific despite the section name. Their `/tech/rss` and `/business/rss` paths both return `404`. |
| **InfoWorld** | `404`. |
| **Yahoo Finance** (per-ticker RSS, e.g. `finance.yahoo.com/rss/headline?s=AAOI`) | `429` — rate-limited/deprecated. |
| **Seeking Alpha** (per-symbol RSS, e.g. `seekingalpha.com/symbol/AAOI.xml`) | `403` — blocked. |
| **Nikkei Asia's plain feed URL grepped for `<item>`** | Not a rejection of the source (it's enabled — see above), but a methodology trap worth recording: naively grepping for `<item>` on this feed finds zero, because it's RDF/RSS1.0. Always verify via `feedparser`, matching how the code actually parses it, not a raw string search. |
| **Google News RSS search** (`news.google.com/rss/search?q=...`) | Works, and covers almost anything (validated: real AAOI coverage, and 100 results for a generic "NVIDIA chip" query from CNBC/Bloomberg/Financial Times/Benzinga). **Rejected anyway**: its `<link>` doesn't resolve to the actual article via a normal HTTP fetch — `curl -L` lands on a Google interstitial page that needs client-side JavaScript to redirect further to the real publisher URL. That breaks this project's requirement that `search_news` return real, citable URLs (see `CLAUDE.md`'s note on why `link` was added to `search_news`'s output in the first place). A source that regresses that isn't worth adding even though its coverage is excellent. |
| **Reddit** (e.g. r/MachineLearning, r/LocalLLaMA) | Reddit deprecated unauthenticated `.json` endpoint access around 2026-05-28 — a request with a proper custom `User-Agent` still returns `403 Forbidden`. Getting Reddit data now requires registering an OAuth app (`reddit.com/prefs/apps`) and using the official API — more setup than a simple API key, not done yet. |

## How to add a new source

1. Write a function matching the shape `fetch_x(query: str, max_results: int = 5) -> list[dict]`, returning a list of `{"title", "link", "source", "summary", "published"}` dicts (any field can be `None` if the source doesn't provide it). If it's a plain RSS/Atom feed, just call `_fetch_rss(url, "Source Name", max_results)` — see the existing sources for the pattern.
2. Add `("name", fetch_x, required_env_or_None, "class")` to `SOURCE_REGISTRY` in `news_sources.py`, where `class` is `"forum"`, `"api"`, or `"rss"` per the table above.
3. If it needs an API key, read it inside the function via `os.environ["YOUR_KEY_NAME"]` — `enabled_sources()` will skip it automatically until that env var is set.
4. Add a row to the appropriate table above.
5. **Test it live before trusting it** — several entries in this doc exist because a docs page or search summary turned out to be wrong (see `CLAUDE.md` for the DeepSeek-model-retirement false alarm and the OK Surf News API response-shape mismatch from earlier in this project's history). Check the HTTP status, the actual item count via `feedparser` (not a raw string search — see the Nikkei Asia note above), and how stale `lastBuildDate` is before adding it.
