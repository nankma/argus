# AI News Sources

`news_sources.py` powers the `search_news` tool with a pluggable source
registry — free/no-key sources are always enabled; key-gated sources turn
on automatically once their env var is set. This doc tracks what's wired up,
what's just documented for later, and how to add more.

All endpoints below were verified live on 2026-08-05 (or noted as
not-yet-verified for key-gated ones we don't have credentials for).

## Enabled now (free, no key required)

| Source | What it is | Endpoint | Notes |
|---|---|---|---|
| **Hacker News** | Tech community board — submissions + discussion | `https://hn.algolia.com/api/v1/search_by_date` (Algolia HN Search) | Use `search_by_date`, not the default `/search` — the latter ranks by relevance/points and surfaces old high-upvote posts instead of recent ones. Community-submitted, so quality/relevance is mixed (raw signal, not editorial). |
| **arXiv (cs.AI)** | Preprint papers | `http://export.arxiv.org/api/query` | `search_query=cat:cs.AI`, `sortBy=submittedDate`, `sortOrder=descending`. Atom XML response, parsed with `feedparser`. ~193k papers in the category as of verification — plenty of volume. |
| **OpenAI Blog** | Official company blog/news | `https://openai.com/news/rss.xml` | Standard RSS 2.0. The older `openai.com/blog/rss.xml` also still redirects/works. |
| **Hugging Face Blog** | Official company blog | `https://huggingface.co/blog/feed.xml` | Standard RSS 2.0. |
| **TechCrunch AI** | Tech journalism, AI category | `https://techcrunch.com/category/artificial-intelligence/feed/` | Standard RSS 2.0. |
| **VentureBeat AI** | Tech journalism, AI category | `https://venturebeat.com/category/ai/feed/` | Standard RSS 2.0. |
| **MIT Technology Review** | Tech journalism (general feed, not AI-filtered) | `https://www.technologyreview.com/feed/` | No separate AI-category feed found; this is their main feed, which is heavily AI-weighted anyway. |

The RSS-based sources (OpenAI/HF/TechCrunch/VentureBeat/MIT) ignore the
`query` parameter — they return the latest N posts unfiltered, since these
feeds are already scoped to AI/tech. Hacker News and arXiv use `query` for
real search.

## Documented, not yet enabled (need an API key)

Implemented in `news_sources.py` but skipped by `enabled_sources()` until
the corresponding env var is set — nothing breaks if it's absent, they're
just not called.

| Source | Env var | Endpoint | Free tier | Notes |
|---|---|---|---|---|
| **NewsAPI.org** | `NEWSAPI_API_KEY` | `GET https://newsapi.org/v2/everything` | Exists, but "Developer" plan is for testing only, not production; exact rate limit/delay not confirmed on the docs page we checked. | `q`, `sortBy=publishedAt`, `pageSize`, `apiKey` (or `X-Api-Key` header). |
| **GNews** | `GNEWS_API_KEY` | `GET https://gnews.io/api/v4/search` | 100 requests/day, 10 articles/request, 1 req/sec, **12-hour delay on articles**, non-commercial only. Resets 00:00 UTC. | `q`, `lang`, `max`, `apikey`. |
| **Perigon** | `PERIGON_API_KEY` | `GET https://api.perigon.io/v1/all` | 150 requests/month, non-commercial only. | Response field names (`source.domain`, `summary`, `pubDate`) taken from general docs, **not verified live** — no key to test with. Double-check against a real response before relying on it. |

None of these were live-tested (no credentials available). Endpoint shapes
came from each provider's docs — verify against a real response the first
time a key is actually configured, in case something's drifted.

## Considered, not implemented

| Source | Why not |
|---|---|
| **Reddit** (e.g. r/MachineLearning, r/LocalLLaMA) | Reddit deprecated unauthenticated `.json` endpoint access around 2026-05-28 — confirmed live on 2026-08-05, a request with a proper custom `User-Agent` still returns `403 Forbidden`. The old "just hit `/r/x/.json` with a User-Agent" trick no longer works. Getting Reddit data now requires registering an OAuth app (`reddit.com/prefs/apps`) and using the official API — more setup than a simple API key, not done yet. |

## How to add a new source

1. Write a function matching the shape `fetch_x(query: str, max_results: int = 5) -> list[dict]`, returning a list of `{"title", "link", "source", "summary", "published"}` dicts (any field can be `None` if the source doesn't provide it).
2. If it's free/no-key, add `("name", fetch_x, None)` to `SOURCE_REGISTRY` in `news_sources.py`.
3. If it needs an API key, read it inside the function via `os.environ["YOUR_KEY_NAME"]`, and add `("name", fetch_x, "YOUR_KEY_NAME")` to the registry — `enabled_sources()` will skip it automatically until that env var is set.
4. Add a row to the appropriate table above.
5. Test it live before trusting it — several entries in this doc exist because a docs page or search summary turned out to be wrong (see `CLAUDE.md` for the DeepSeek-model-retirement false alarm and the OK Surf News API response-shape mismatch from earlier in this project's history).
