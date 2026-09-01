# Settings migration plan (this repo's own adoption of Trailsign)

Not the same document as Trailsign's own design
(`github.com/nankma/trailsign/blob/main/docs/design.md`) — that's the
library. This is **this repo's** plan for actually moving its ~18
`os.environ` call sites onto it: what exists, in what order, what has to
happen before each phase can safely reach production, and current
status. Living document — update the status column as each phase lands,
don't let it drift from the code.

## Bootstrap: how `app_settings.py` finds its config

`app_settings.py` (repo root) is the one process-wide `Settings`
instance every migrated module imports. Its own bootstrap is the one
config value that can never live inside the settings file itself — the
file's *path*:

- **`SETTINGS_FILE` env var**, defaulting to `settings.yml` in the
  working directory if unset.
- **Docker** passes this the same way it passes any other env var today
  — `-e SETTINGS_FILE=/config/settings.yml -v /host/settings.yml:/config/settings.yml:ro`
  — no new deployment mechanism needed, just one more `-e`/`-v` pair in
  `local-infra/infrastructure.yaml`'s `docker_run.command` when
  production actually gets a real settings file (see "Before deploying
  any phase" below — hasn't happened yet).
- **If no file exists at the resolved path**, `app_settings.get_settings()`
  falls back to an empty `Settings({})`. Every migrated call site was
  written with its own `default=` argument specifically so this doesn't
  break anything — it just means every setting resolves to its
  hardcoded default, same as before migration. This is deliberate: it's
  what let the storage-path migration (Phase 1 below) land and pass
  tests with zero settings.yml deployed anywhere yet.

## Two templates, not one file

- **`settings.example.yml`** (committed, repo root) — the shape,
  showing every currently-migrated section plus a commented-out preview
  of sections not built yet. No real values.
- **The real `settings.yml`** — gitignored (`.gitignore` entry added
  alongside `local-infra/`), never committed. **Local dev's copy and
  PROD's copy are not the same file and don't have the same shape**:
  local dev can use plain `trailsign-resolve: plaintext` or
  `environment-variable` nodes for everything; PROD's real secrets
  (once Phase 3+ migrates them) will reference
  `credential_sources.oci-vault-main` and `trailsign-resolve: oracleKeyVault`
  nodes, matching `local-infra/infrastructure.yaml`'s existing vault
  OCIDs. This split is why the real file lives outside git entirely
  rather than being one checked-in file with placeholder secrets.

## Full inventory — every raw `os.environ` read, current status

Re-grepped 2026-09-01, not carried over from memory:

| Variable | Read in | Secret? | Target settings path | Status |
|---|---|---|---|---|
| `NEWS_CACHE_DIR` | ~~`news_cache.py`~~ | no | `storage.news_cache_dir` | **Migrated** 2026-09-01 |
| `NEWS_ARCHIVE_DIR` | ~~`news_cache.py`~~ | no | `storage.news_archive_dir` | **Migrated** 2026-09-01 |
| `MESSAGE_ARCHIVE_DIR` | ~~`message_archive.py`~~ | no | `storage.message_archive_dir` | **Migrated** 2026-09-01 |
| `SUBSCRIBERS_DB_FILE` | ~~`users_db.py`~~ | no | `storage.subscribers_db_file` | **Migrated** 2026-09-01 |
| `DEEPSEEK_API_KEY` | `tools/measure_guardrails.py`, `tools/run_eval.py` (`agent.py` reads it implicitly via `ChatDeepSeek`) | **yes** | `models.main.api-key` | Not started |
| `LLM_MODEL` / `LLM_MODEL_CLASSIFIER` | `agent.py` (`build_model`) | no | `models.main.model` / `models.guardrail.model` | Not started — feeds the already-built `model-portability-plan.md` mechanism, no new design |
| `LLM_REASONING_EFFORT` / `LLM_REQUEST_TIMEOUT_SECONDS` | `agent.py` | no | `models.main.reasoning_effort` / `models.main.request_timeout_seconds` | Not started |
| `LOGFIRE_ENABLED` / `LOGFIRE_API_KEY` | `agent.py`, `tools/check_logfire.py` | key: **yes** | `telemetry.tracing.*` (folds into the seam-4 split, see `docs/standaloneplan/README.md` Phase 3) | Not started |
| `NEWSAPI_API_KEY`, `GNEWS_API_KEY`, `PERIGON_API_KEY` | `news_sources.py` | **yes** | `news_source.<name>.api-key` | Not started |
| `TELEGRAM_BOT_TOKEN` | `bot.py`, `combined_bot.py`, `admin_bot.py` | **yes** | `delivery.telegram.bot-token` | Not started — highest blast radius of the "current" set, bot startup itself depends on it |
| `ADMIN_BOT_TOKEN` | `bot.py`, `combined_bot.py`, `admin_bot.py` | **yes** | `delivery.telegram.admin_bot_token` | Not started |
| `ADMIN_CHAT_ID` | `bot.py`, `combined_bot.py`, `admin_bot.py` | no | `delivery.telegram.admin_chat_id` | Not started |
| `TEST_API_PORT` / `ENABLE_TEST_API` | `test_api.py` | no | `test_api.port` / `test_api.enabled` | Not started — lowest priority, dev-only tool |

Deliberately **not** migrating (stay raw `os.environ`, out of scope):
`SETTINGS_FILE` itself (the bootstrap exception, see above),
`OTEL_SERVICE_NAME` (`tests/test_telemetry.py` — that test is
specifically about env-var ordering before any Settings/provider exists,
migrating it would defeat the test's own point), `OMP_NUM_THREADS`
(`docs/analysis/tools/build_taxonomy.py`, an analysis tool outside the
image, not part of the running service).

## Migration order and reasoning

1. ~~**Storage paths**~~ — **done** 2026-09-01. No secrets, lowest
   blast radius, proved the `app_settings.py` bootstrap pattern and the
   "empty-Settings fallback means nothing breaks pre-deployment" design
   before touching anything riskier.
2. **Models** (`DEEPSEEK_API_KEY`, `LLM_MODEL`, `LLM_MODEL_CLASSIFIER`,
   `LLM_REASONING_EFFORT`, `LLM_REQUEST_TIMEOUT_SECONDS`) — next up.
   First real secret migrated, but low-traffic-surface (one construction
   point, `agent.build_model()`), and directly feeds the already-built
   `model-portability-plan.md` mechanism rather than needing new design.
3. **News sources** (`NEWSAPI_API_KEY`, `GNEWS_API_KEY`,
   `PERIGON_API_KEY`) — pairs naturally with moving `news_sources.py`
   onto the factory pattern described in Trailsign's own design doc
   (`queryadoptor` dispatch), so this phase is really "adopt the factory
   pattern *and* migrate its keys" together, not two separate passes.
4. **Telegram / admin** (`TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`,
   `ADMIN_CHAT_ID`) — deliberately last of the "current" set. Bot
   startup itself depends on these; get comfortable with the pattern on
   lower-stakes subsystems first.
5. **Telemetry** (`LOGFIRE_ENABLED`, `LOGFIRE_API_KEY`) — folds into
   `docs/standaloneplan/README.md`'s Phase 3 (the `telemetry.events`/
   `telemetry.tracing` split), not purely a mechanical migration like
   1–4 — real design work (the file-based `Logger`/`SpanExporter`)
   happens at the same time.

## Before deploying ANY phase past storage paths: a real production cutover step is required, not optional

**Real risk found while building this inventory, not hypothetical.**
`local-infra/infrastructure.yaml`'s live `docker_run.command` still sets
`-e NEWS_CACHE_DIR=/data/news_cache -e NEWS_ARCHIVE_DIR=/data/news_archive
-e MESSAGE_ARCHIVE_DIR=/data/message_archive -e SUBSCRIBERS_DB_FILE=/data/subscribers.db`.
The migrated code (Phase 1, done) no longer reads any of these four env
vars directly — it reads `storage.*` from `app_settings.get_settings()`
instead, which falls back to relative-path defaults
(`news_cache`, `message_archive`, `subscribers.db`, unset archive) when
no `settings.yml` exists on the container. **If this code were deployed
today with no settings.yml shipped alongside it, the container's
`/data`-pointed env vars would go silently unread, and the cache/db
paths would revert to the container's own filesystem** — exactly the
class of bug this project already has a full incident write-up for
(`docs/plans/deployment-plan.md`, 2026-08-19/20: `NEWS_CACHE_DIR` unset
→ every redeploy silently reset the article cache).

**The fix, before this phase's code ever reaches PROD**: ship a real
`settings.yml` on the container that bridges to the exact same env vars
docker_run already sets, so behavior is provably unchanged:

```yaml
storage:
  news_cache_dir:
    trailsign-resolve: environment-variable
    name: NEWS_CACHE_DIR
  news_archive_dir:
    trailsign-resolve: environment-variable
    name: NEWS_ARCHIVE_DIR
  message_archive_dir:
    trailsign-resolve: environment-variable
    name: MESSAGE_ARCHIVE_DIR
  subscribers_db_file:
    trailsign-resolve: environment-variable
    name: SUBSCRIBERS_DB_FILE
```

This is zero-risk (same env vars, same values, just read through one
more layer) and should be the **first** real settings.yml PROD ever
gets, landing in the same deploy as (or before) this phase's code. Add
`-e SETTINGS_FILE=... -v .../settings.yml:...` to
`local-infra/infrastructure.yaml`'s `docker_run.command` at the same
time, and verify with `tools/check_data_persistence.py` (already exists,
already built for this exact failure mode) immediately after that
deploy — don't just trust the code compiled and tests passed. This same
"bridge to the existing env var first, don't jump straight to a vault
reference" approach should be the default move for every later phase
too, not just this one.

## Production secrets (Phase 2+): the pattern is proven, not theoretical

`OracleKeyVaultResolver` (Trailsign) was verified 2026-09-01 against a
real OCI Vault secret from a real OCI compute instance, using instance
principal auth — the same auth shape this project's own
`docker-entrypoint.sh` already uses for every other secret today. See
Trailsign's own repo history for the fix that made this work (the
resolver's original config-dict shape didn't match instance-principal
auth's `signer=`-based construction). When Phase 2 (models) or Phase 4
(Telegram/admin) needs a real vault-backed secret in PROD's
`settings.yml`, the `credential_sources.oci-vault-main` +
`trailsign-resolve: oracleKeyVault` shape from Trailsign's own design
doc is ready to use, not speculative.

## Testing

Already built, not just planned: `app_settings.reset_settings_for_tests()`
(no-arg resets to force a reload; pass a `Settings` instance to inject a
fake) plus the fact that `trailsign.Settings.__init__` accepts a plain
dict directly (`Settings({"storage": {"news_cache_dir": "/tmp/x"}})`) —
no file needed for tests. `tests/test_app_settings.py` covers the
bootstrap itself. Existing tests for migrated modules needed **zero**
changes across Phase 1, because they already monkeypatched the
module-level constant (`monkeypatch.setattr(news_cache, "CACHE_DIR", ...)`)
rather than the env var — expect the same to hold for Phases 2–4, worth
confirming per-phase rather than assuming.
