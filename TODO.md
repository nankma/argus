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

- [ ] **Move `push_outcomes` from a DB table to a log, not a DB record.**
  Currently a SQLite table in `subscribers.db` (`users_db.py`) -- one row
  per push attempt (chat_id, outcome, recorded_at, detail), pruned by
  `storage.push_outcomes_ttl_days`. Doesn't make sense to "remember"
  this relationally: it's an event stream (what happened, once), not
  state a subscriber has -- a better fit for this project's existing
  `LogfireLogger`/`logfire_logger.py` pattern (already used throughout
  for exactly this kind of "record that X happened" event), queried via
  Logfire instead of SQL.
  Before implementing: find and account for every current reader --
  at least the "ratio alarm" (failure rate in the last 24h across all
  subscribers) and the "consecutive-failure alarm" (a subscriber's last
  N outcomes) both query this table directly today; moving to a log
  means redesigning how those two queries work against Logfire (or
  whatever the log backend ends up being), not just swapping the write
  side. Also: if the table goes away, `storage.push_outcomes_ttl_days`
  (just added, 2026-09-03) goes away too or gets repointed at whatever
  the new retention mechanism is -- don't leave it orphaned.

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
