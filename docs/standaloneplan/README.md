# Standalone service plan

Reconstructed 2026-09-01 after the original discussion/draft
(`docs/standalone/`) was deleted when its settings piece got extracted
into its own project — the rest of the plan had no home for a few hours
before this doc. Written from the actual decisions made in that
discussion, not re-derived from scratch. Moved here from
`docs/plans/standalone-service-plan.md` the same day, once it became
clear this needed a sibling doc (`01-settings-migration.md`) rather than
staying a single file — same parent/numbered-child shape the original
`docs/standalone/` used.

## Why

Today the bot only runs one way: one Telegram bot, one DeepSeek key, one
Logfire project, deployed to one Oracle VM, config read via scattered
`os.environ[...]` calls. That blocks:

1. **A local INT environment** — a pre-prod copy runnable on a dev
   machine, verified before a change reaches PROD.
2. **Download-and-run** — someone who wants this bot but not this
   project's own cloud account should be able to clone it and run it
   with nothing but their own keys.
3. **Bring-your-own-provider** — their own AI provider (already solved,
   see "Explicitly not re-deciding" below), their own logging/alerting
   SaaS if they want one, or none at all.
4. **Bring-your-own-delivery-channel** — Telegram today; email at least
   as a real second option, pluggable enough that a third (LINE was
   named explicitly) is someone adding a class, not a redesign.
5. **Bring-your-own-management-surface** — the existing Telegram admin
   bot, a local web page, or both at once — not mutually exclusive.
   Someone could run with no admin surface, Telegram only, web only, or
   both simultaneously.

None of this changes what the bot *does*. It removes the assumption,
baked into the code today, that "this bot" and "this specific cloud
deployment" are the same thing.

## Status

| # | Seam | Status |
|---|---|---|
| 1 | **Settings** | **Design built and extracted.** Now its own project, [Trailsign](https://pypi.org/project/trailsign/) (`github.com/nankma/trailsign`) — the design turned out to be genuinely content-independent, not specific to this bot. **This repo's own adoption of it is separate ongoing work — see [`01-settings-migration.md`](01-settings-migration.md)** for the full env-var inventory, migration order, and current per-subsystem status (storage paths done 2026-09-01; models/news-source keys/Telegram-admin/telemetry not yet). |
| 2 | **Push / delivery target** | Not started |
| 3 | **Management surface** | Not started |
| 4 | **Telemetry / logging** | Not started |
| 5 | **Storage / persistence** | Not started |

Seams 2–5 below are rough — the level of detail this project's own
`writing-system-design-docs` skill calls "written only once that part's
turn actually comes." None of them has a dedicated child doc yet, unlike
seam 1.

## The four remaining seams

| # | Seam | Current state | Target |
|---|---|---|---|
| 2 | Push / delivery target | `bot.py`/`combined_bot.py`/`news_push.py` call the Telegram `Bot` API directly — no seam at all | A delivery-target interface (Telegram, Email, later LINE/...), each a factory-constructed adaptor off resolved settings, same registry shape as `news_sources.py` — one or more active at once |
| 3 | Management surface | `admin_bot.py` — Telegram-only | Telegram admin bot and/or a local (`localhost`-only) web page, each independently on/off — not a single either/or switch |
| 4 | Telemetry / logging | `logfire_logger.py`'s `LogfireLogger` conflates structured event logging (already behind a `Logger` Protocol) with owning OTel span-exporter setup (hardcoded to Logfire's OTLP endpoint) | Split into two independent settings: `telemetry.events` (the `Logger` Protocol — file-based default, Logfire, or anything else) and `telemetry.tracing` (a pluggable OTel `SpanExporter` — file-based default, Logfire/Phoenix/anywhere OTLP as options). A file `SpanExporter` isn't new territory: the OTel SDK already defines that interface and ships `ConsoleSpanExporter` as a reference |
| 5 | Storage / persistence | `users_db.py` (subscribers, interests, push settings), `message_archive.py`, `news_cache.py` — all hardcoded to SQLite/local filesystem | A storage-backend Protocol, SQLite as the zero-ops default, Postgres/MySQL/Mongo/etc. as pluggable alternatives |

## Rough phasing (confirmed 2026-08-31, seam 1 design completed 2026-09-01)

1. ~~Settle the Settings design and build it.~~ **Done** — see Status
   above.
2. **Gradually migrate connected components onto Settings, behavior
   unchanged.** In progress — tracked in `01-settings-migration.md`, not
   here. Each of the ~18 existing `os.environ[...]` call sites moves one
   at a time, same value, same default, no functional change, with an
   explicit production-cutover check before each phase reaches PROD
   (see that doc's "Before deploying" section — a real deployment risk,
   not boilerplate caution).
3. **Finish the telemetry seam and whatever else is needed to actually
   run standalone.**
   - File-based `Logger` implementation (the no-account default for
     `telemetry.events`).
   - File-based `SpanExporter` for `telemetry.tracing` — a small
     implementation of the OTel SDK's own extension point, writing
     JSON-line spans instead of exporting OTLP.
   - Standalone-readiness audit: a grep sweep for anything else that
     silently assumes the current cloud deployment — same shape as this
     project's own Phoenix-retirement sweep. Candidates already visible:
     `docker-entrypoint.sh`'s Vault-fetch assumptions (fine — becomes a
     no-op once a settings file has no `oracleKeyVault`-resolved nodes,
     since Trailsign's `OracleKeyVaultResolver` lazy-imports `oci`),
     whether `combined_bot.py` can run directly with `python
     combined_bot.py` with no Docker at all (should already be true,
     worth actually verifying when this phase starts).
4. **Add an email delivery client**, registry shape copied from
   `news_sources.py`, third channel (LINE) left genuinely addable later
   without touching this project's own code.
   - **The real work is the subscriber schema, not the interface.**
     `users_db.py` today models a subscriber as a Telegram `chat_id` —
     adding email needs a channel-agnostic identity plus one-or-more
     per-channel addresses. Decided direction (2026-08-31, after
     reconsidering a document/KV-store switch — see below): **stays
     SQLite**, multi-channel identity is an ordinary `subscribers` +
     `subscriber_channels` join table (a normal one-to-many relational
     shape, nothing document-store-shaped about it).
   - **The storage seam (5) gets exercised here for the first time.**
     Considered and rejected switching the *default* backend to a
     document/KV store (e.g. Mongo): the trigger (multi-channel
     subscribers) is an ordinary relational shape, and a document DB
     needs a running server process, which cuts against the "standalone,
     run on a laptop with nothing" goal that started this whole plan.
     TinyDB (embedded, no server) was also considered and rejected — no
     real transaction/crash-safety guarantees, a real risk given this
     project's own history with silent data loss (`NEWS_CACHE_DIR` being
     unset once reset the entire article cache on every redeploy,
     `docs/plans/deployment-plan.md`, 2026-08-19/20 — the exact same
     failure mode `01-settings-migration.md`'s "Before deploying" section
     flags for the settings migration itself) — and it would swap the
     storage engine wholesale rather than an incremental step. The
     broader "schema changes are a headache" worry (not just the
     multi-channel case) gets solved differently: stable, frequently
     queried fields stay real typed columns; anything likely to grow or
     change shape goes in a SQLite JSON column (`json_extract`, already
     available through the stdlib `sqlite3` module this project already
     uses, no new dependency) — adding a new loosely-structured
     preference later means writing a new JSON key, not an `ALTER TABLE`
     + migration.
   - Settings vs. `users_db.py` stay separate concerns — Settings is
     deployment-level config; per-subscriber preferences (interests,
     push interval, reply language, which channel(s)) stay in
     `users_db.py`'s SQLite, runtime data, untouched by the Settings
     refactor itself.
   - HTML formatting is channel-specific — `telegram_html.py`'s
     Telegram-HTML quirks don't transfer to email; email needs its own
     formatter.
5. **Add a simple web management service** that does what the admin bot
   does today, able to run alongside the Telegram admin bot or standing
   alone — not exclusive with it.
   - **Admin logic extracted into its own module, deployment topology
     kept separately configurable** (decided 2026-08-31): pull
     `admin_bot.py`'s business logic (category-proposal review,
     subscriber management, ...) into a channel-agnostic layer both the
     Telegram admin bot and the web page call into. Deployment topology
     stays a choice, not forced: same-container/same-process
     (`combined_bot.py` already proves this pattern — public bot + admin
     bot + push scheduler as concurrent asyncio tasks in one process
     today, no supervisor involved) for the smallest footprint, or
     separate containers via docker-compose for real isolation.
     Explicitly **not** recommended: one container running two OS-level
     processes via a supervisor (s6-overlay/supervisord/etc.) — against
     Docker's own one-process-per-container convention, no benefit this
     project doesn't already get more cleanly from its existing
     asyncio-tasks approach. This question ("can one Docker container
     run two services?") came up explicitly — yes, technically, via a
     supervisor, but the existing asyncio-tasks precedent is strictly
     better for this project's own shape.
   - **Good precedent already in the codebase**: `users_db.py`'s
     subscriber-preference functions (`set_interests`, `set_push_enabled`,
     `set_push_interval_hours`, `set_language`, ...) are already
     channel-agnostic — plain `chat_id`/value arguments, no Telegram
     objects involved; a web route can call these directly. `admin_bot.py`'s
     own handlers are less clean — Telegram-callback-shaped, likely with
     logic worth extracting first, mirroring what Phase 4 does for
     delivery. Check exactly how entangled these are once this phase
     starts, don't assume.
   - **Framework leaning: stay on the stdlib.** `test_api.py` — this
     project's own existing local HTTP surface — is built on
     `http.server`/`ThreadingHTTPServer`, no Flask/FastAPI dependency at
     all. Matches `system-overview.md`'s P5 (minimal infrastructure
     budget) and is a real precedent already in this codebase.
   - Auth (even `localhost`-only) is still an open question.

## Explicitly not re-deciding

**AI provider swapping is already built.** `docs/plans/model-portability-plan.md`
shipped config-driven model selection (`LLM_MODEL`/`LLM_MODEL_CLASSIFIER`,
`agent.build_model()`) 2026-08-16. This plan's only job there is to have
those two env vars come from `app_settings.get_settings()` instead of
raw `os.environ.get(...)` once Phase 2 of `01-settings-migration.md`
reaches the models subsystem — no new design needed.

**`news_sources.py`'s registry is the existing precedent.** It already
does "pluggable implementations, selected by config, each one a small
class registered under a key." The delivery-target registry (Phase 4)
should copy that shape rather than invent a new one.

## Open questions not yet settled

- **Web management page auth story** — even `localhost`-only, an admin
  page shouldn't be wide open to anything on the same machine/network.
  Not discussed yet.
- **Storage Protocol's exact shape** — the direction (SQLite default,
  Protocol-abstracted, JSON columns for loosely-structured fields) is
  decided, but the actual interface (what methods, sync vs. async, how a
  non-SQLite implementation would represent "a JSON column") isn't
  designed — due when seam 5 gets its own doc.
- **Public release mechanics** — license choice, a secrets/PII audit of
  history before anything is made public, whether "download and run"
  means a GitHub repo, a PyPI package (Trailsign itself already answers
  this question for the settings piece specifically), or documented
  clone instructions. Separate from the technical refactor above.

## Relationship to existing docs

- `docs/standaloneplan/01-settings-migration.md` — this repo's own
  settings-migration tracking (inventory, order, deploy-safety), split
  out from this file because it's a faster-moving, more mechanical
  document than the rest of this plan.
- `docs/plans/model-portability-plan.md` — AI provider swapping, already
  built, reused as-is.
- `docs/plans/observability-platform-plan.md` / `docs/current/telemetry-catalog.md`
  — describe the current Logfire-specific state that Phase 3 changes;
  update both once a telemetry backend seam actually exists.
- `docs/plans/deployment-plan.md` / `docs/current/infrastructure.md` —
  describe the current single-cloud-deployment topology; Phase 3 is what
  eventually makes those "the PROD topology" rather than "the only
  topology."
