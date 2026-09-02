# TODO

Follow-ups noted during work, not yet scheduled. See CLAUDE.md's
"Design before code" -- none of these get implemented without a design
pass first, per that rule.

- [ ] **Confirm `TREND_REPORT_STRUCTURE` wording change** (`agent.py`).
  When a user's specific request (a ticker/company/narrow topic) has no
  direct coverage, the report should title itself after what the user
  actually asked for (not the substituted broader topic), and lead with
  a short "no direct coverage, here's what's related and why" note
  *before* the story sections, not after. Proposed exact wording was
  given in chat 2026-09-02 -- confirm it (or a revision) before editing.
  Found via a real INT test: asking about AAOI returned a report titled
  "AI Datacenter Networking Trend Report" with the AAOI-specific gap note
  buried at the bottom.

- [ ] **Add `GNEWS_API_KEY` to INT's `docker_run.command`**
  (`local-infra/infrastructure.yaml`). INT currently has none of
  PROD's three API-gated news sources (GNews/NewsAPI/Perigon) configured
  -- found while comparing an AAOI query's results on INT vs PROD.
  GNews specifically is the one live `search_news` calls can actually
  use (NewsAPI/Perigon are excluded from live search by
  `RESTRICTED_SOURCES`, only used by the background ingest job) and its
  100/day budget has headroom beyond what ingest alone uses (see
  `news_sources.py`'s own comment) -- shouldn't compete with PROD's usage.

- [ ] **Design: switch `search_news` from live source fetch to searching
  downloaded/ingested content.** Currently `search_news` fetches live,
  per query, from `news_sources.enabled_sources()` (excludes
  NewsAPI/Perigon by default) -- a real source-coverage gap against what
  `news_ingest.py`'s background cache already has (unrestricted, all
  sources). Idea: search the local cache/index that ingest already
  builds, instead of re-fetching from sources on every query. Needs a
  real design pass (how the cache gets indexed/searched, whether it's
  still "live" enough) before any code -- explicitly deferred until
  after the current settings work lands.
