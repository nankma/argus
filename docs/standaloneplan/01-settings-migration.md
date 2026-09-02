# Settings migration plan (this repo's own adoption of Trailsign)

Not the same document as Trailsign's own design
(`github.com/nankma/trailsign/blob/main/docs/design.md`) — that's the
library. This is **this repo's** plan for actually moving its ~18
`os.environ` call sites onto it: what exists, in what order, what has to
happen before each phase can safely reach production, and current
status. Living document — update the status column as each phase lands,
don't let it drift from the code.

## Migration methodology — read this before touching any call site

This section went through two real corrections on 2026-09-01 (both
below, marked with the date) — re-read the whole thing before starting
models, news sources, Telegram/admin, or telemetry, not just the parts
that seem relevant.

1. **No code-level fallback to the old source, ever. Untouched settings
   stay 100% untouched until their own turn; touched settings read only
   from `Settings`, full stop.** (Corrected 2026-09-01 — the first
   version of this rule said the opposite: wrap the old env var as
   `default=`. That was wrong. The actual discipline is simpler and
   stricter.)
   ```python
   # Before touching NEWS_CACHE_DIR at all: leave it exactly as it was.
   # Don't pre-wrap it in Settings "just in case" while working on
   # something else -- an untouched setting is not this migration's
   # business yet.
   CACHE_DIR = os.environ.get("NEWS_CACHE_DIR", "news_cache")

   # The moment NEWS_CACHE_DIR is actually being migrated: read Settings
   # only. No os.environ fallback baked into the call site -- that would
   # just recreate the old hidden-dual-source problem in a new place.
   CACHE_DIR = get_settings().resolved("storage.news_cache_dir", required=True)
   ```
   `required=True` (no `default=`) for anything the service always needs
   a real value for — an absent key is a deployment mistake and should
   fail loudly at import time, not silently paper over with a maybe-wrong
   guess. `default=<a plain literal>` only for a value where "unset" is
   a genuinely legitimate, intentional state (e.g. `LLM_REQUEST_TIMEOUT_SECONDS`
   defaulting to a sensible timeout, or `news_archive_dir` defaulting to
   `None` — archiving off is a real, expected state, not a
   misconfiguration). Never `default=os.environ.get(...)` — once a
   setting is migrated, the old env var is dead for it, not a permanent
   safety net.
2. **Touching the code and updating *both* real settings.yml files
   (local dev's and the Oracle-target one) is one atomic unit of
   change, not two.** You cannot migrate a call site to
   `required=True`/no-default and leave either settings.yml without that
   key — the app won't start. Deploying the code and deploying its
   settings.yml value happen together, in the same batch, verified
   together — never "ship the code now, add the config value later" (see
   "Deploying past storage paths" below for what this actually looks
   like when it went wrong).
3. **Secrets fetched via `docker-entrypoint.sh` are a different, wider
   shape than a plain `os.environ` read — most of what's left in this
   migration is this shape, not the simple one.** (Added 2026-09-01,
   after `DEEPSEEK_API_KEY` was used as a concrete counter-example to
   rule 1's first, wrong version.) `docker-entrypoint.sh` conditionally
   fetches `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`,
   `ADMIN_CHAT_ID`, `LOGFIRE_API_KEY`, `GNEWS_API_KEY`, `PERIGON_API_KEY`,
   and `NEWSAPI_API_KEY` from OCI Vault *before Python even starts*,
   exporting each as a plain env var only if its `*_SECRET_OCID`
   companion var is set. For these, "the old place" is the whole chain
   — the shell fetch block *and* however Python consumes the resulting
   env var (sometimes implicit, e.g. `ChatDeepSeek` reading
   `DEEPSEEK_API_KEY` internally, no explicit `os.environ.get` line in
   `agent.py` at all). Migrating one of these means touching **three**
   places together, in the same batch:
   1. `settings.yml` — add a `trailsign-resolve: oracleKeyVault` node
      using the *same* secret OCID the entrypoint script already fetches
   2. the Python construction site — read from `Settings` explicitly
      instead of relying on the env var
   3. `docker-entrypoint.sh` — remove that variable's now-redundant fetch
      block (and its `*_SECRET_OCID` env var from `docker_run.command`),
      since Python now fetches it directly via the same instance-principal
      auth Trailsign's `OracleKeyVaultResolver` already proved works

   Until a given secret's turn actually comes, none of these three get
   touched — same "untouched stays untouched" principle as rule 1, just
   correctly scoped to what the old mechanism actually is for a secret
   versus a plain value.
4. **One setting at a time, one small change at a time.** Move exactly
   one variable (plain or entrypoint-fetched) per change — don't batch
   multiple variables together just because they share a file, a
   subsystem, or the same entrypoint script.
5. **Verify against something as close to real as possible after every
   single change, before moving to the next ("test to INT").** Unit
   tests with fakes aren't sufficient on their own for this class of
   change — the risk is specifically in the wiring between settings.yml,
   the real filesystem, and (for entrypoint-fetched secrets) the real
   OCI Vault, none of which a mocked `Settings` exercises. **A real INT
   environment now exists** (built and verified 2026-09-01 —
   `local-infra/infrastructure.yaml`'s `local-int-machine`, own Docker
   host on the local network, own isolated Telegram bot identity, own
   `/data` volume, `LOGFIRE_ENABLED` deliberately unset) — future phases
   should deploy and smoke-test there before touching production, the
   same way Phase 1 (storage) and this session's `models.main.api-key`
   groundwork already did. The real Oracle instance
   (`instance-mnk-phoenix-20260808-1012`) remains the right target
   specifically for verifying `oracleKeyVault`/instance-principal secret
   resolution, since that auth shape only works from inside an actual
   OCI compute instance — the local INT machine can't stand in for that
   one case.
6. **The discipline doesn't relax as stakes go up.** If anything it
   matters more once real secrets (models, Telegram tokens) are
   involved, not less.

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
  production actually gets a real settings file (see "Deploying storage
  paths" below — hasn't happened yet).
- **If no file exists at the resolved path**, `app_settings.get_settings()`
  falls back to an empty `Settings({})`. What that means depends on
  whether a given call site has been migrated yet — see "Migration
  methodology" below: an **unmigrated** setting doesn't care (it's still
  a plain `os.environ.get(...)` line, Settings being empty is
  irrelevant); a **migrated, `required=True`** setting throws
  `SettingsError` immediately at import time. That's intentional, not a
  gap — a deployment missing a required setting should fail loudly, not
  silently guess. This is exactly why rule 2 below makes "update the
  code" and "update both real settings.yml files" one atomic change, not
  two.

## Three real environments, not one file

**`settings.yml` and `settings.oracle.yml` (both repo root) are
committed on purpose, as of 2026-09-01** — reconsidered from an earlier
gitignored-by-default design. Neither has ever contained a real secret,
IP, or this-deployment-specific OCID, and a committed, working example
is genuinely useful (clone the repo, run it, works — one of this whole
plan's own "Why" goals). The boundary that makes this safe:

> **Criterion — also in CLAUDE.md's Landmines**: `settings.yml` and
> `settings.oracle.yml` may only ever contain env-var *names*,
> placeholder/example identifiers, relative paths, and structure —
> never a real, this-deployment-specific value (a real OCID, IP,
> hostname, or literal secret). The moment Phase 2+ needs one of this
> project's actual vault/secret/compartment OCIDs, that value goes into
> a **new** gitignored file under `local-infra/` instead (matching
> `local-infra/infrastructure.yaml`'s existing convention) — these two
> files keep placeholder versions, forever, not the real one.

- **`settings.yml`** — local dev's real, working file, what
  `SETTINGS_FILE` defaults to. `storage.*` fully populated (required, so
  it has to be), plus a commented-out preview of sections not built yet
  (folded in from a since-removed separate `settings.example.yml` — one
  file, not two that could drift apart).
- **`settings.oracle.yml`** — the production-target template. Bridges
  `storage.*` to the exact same env vars `docker_run.command` already
  sets, via `trailsign-resolve: environment-variable` nodes — genuinely
  deployable as-is today, not just illustrative. Once models/Telegram
  secrets are migrated (Phase 2+), *this file* gains the
  `trailsign-credential-sources.oci-vault-main` + `trailsign-resolve:
  oracleKeyVault` shape using placeholder OCIDs (key renamed from the
  bare `credential_sources` in Trailsign v0.2.0, 2026-09-02 -- same
  collision-proofing reasoning as `trailsign-resolve` itself, applied to
  the one remaining generic top-level word), while the real deploy uses
  a sibling file under `local-infra/` with this project's actual OCIDs
  filled in.
- **The test environment** — not a yaml file at all. `tests/conftest.py`
  injects a `Settings({"storage": {...}})` dict directly (via
  `app_settings.reset_settings_for_tests(...)`) before importing any
  migrated module, since module-level constants are computed once at
  first import — before any pytest fixture could run. Forgetting to add
  a newly-`required=True` key here breaks the *entire* test suite
  immediately and loudly (every test that imports the affected module
  fails at collection) — a real, useful forcing function, not just an
  inconvenience.

## Full inventory — every raw `os.environ` read, current status

Re-grepped 2026-09-01, not carried over from memory:

| Variable | Read in | Old mechanism | Secret? | Target settings path | Status |
|---|---|---|---|---|---|
| `NEWS_CACHE_DIR` | ~~`news_cache.py`~~ | plain `os.environ` | no | `storage.news_cache_dir` | **Migrated** 2026-09-01, `required=True` |
| `NEWS_ARCHIVE_DIR` | ~~`news_cache.py`~~ | plain `os.environ` | no | `storage.news_archive_dir` | **Migrated** 2026-09-01, `default=None` (intentional "off" state) |
| `MESSAGE_ARCHIVE_DIR` | ~~`message_archive.py`~~ | plain `os.environ` | no | `storage.message_archive_dir` | **Migrated** 2026-09-01, `required=True` |
| `SUBSCRIBERS_DB_FILE` | ~~`users_db.py`~~ | plain `os.environ` | no | `storage.subscribers_db_file` | **Migrated** 2026-09-01, `required=True` |
| `DEEPSEEK_API_KEY` | `tools/measure_guardrails.py`, `tools/run_eval.py` (`agent.py` reads it implicitly via `ChatDeepSeek`) | **`docker-entrypoint.sh` vault-fetch** (via `DEEPSEEK_API_KEY_SECRET_OCID`) | **yes** | `models.main.api-key` | Not started — see methodology rule 3, this is the worked example |
| `LLM_MODEL` / `LLM_MODEL_CLASSIFIER` | `agent.py` (`build_model`) | plain `os.environ` | no | `models.main.model` / `models.guardrail.model` | Not started — feeds the already-built `model-portability-plan.md` mechanism, no new design |
| `LLM_REASONING_EFFORT` / `LLM_REQUEST_TIMEOUT_SECONDS` | `agent.py` | plain `os.environ` | no | `models.main.reasoning_effort` / `models.main.request_timeout_seconds` | Not started — legitimate `default=<literal>` candidates, not `required=True` |
| `LOGFIRE_ENABLED` | `agent.py` | plain `os.environ` | no | `telemetry.tracing.*` (folds into the seam-4 split, see `docs/standaloneplan/README.md` Phase 3) | Not started |
| `LOGFIRE_API_KEY` | `agent.py`, `tools/check_logfire.py` | **`docker-entrypoint.sh` vault-fetch** | **yes** | `telemetry.tracing.*` | Not started |
| `NEWSAPI_API_KEY`, `GNEWS_API_KEY`, `PERIGON_API_KEY` | `news_sources.py` | **`docker-entrypoint.sh` vault-fetch** | **yes** | `news_source.<name>.api-key` | Not started |
| `TELEGRAM_BOT_TOKEN` | `bot.py`, `combined_bot.py`, `admin_bot.py` | **`docker-entrypoint.sh` vault-fetch** | **yes** | `delivery.telegram.bot-token` | Not started — highest blast radius of the "current" set, bot startup itself depends on it |
| `ADMIN_BOT_TOKEN` | `bot.py`, `combined_bot.py`, `admin_bot.py` | **`docker-entrypoint.sh` vault-fetch** | **yes** | `delivery.telegram.admin_bot_token` | Not started |
| `ADMIN_CHAT_ID` | `bot.py`, `combined_bot.py`, `admin_bot.py` | **`docker-entrypoint.sh` vault-fetch** | no (an id, not a credential — still fetched the same way) | `delivery.telegram.admin_chat_id` | Not started |
| `TEST_API_PORT` / `ENABLE_TEST_API` | `test_api.py` | plain `os.environ` | no | `test_api.port` / `test_api.enabled` | Not started — lowest priority, dev-only tool |

Deliberately **not** migrating (stay raw `os.environ`, out of scope):
`SETTINGS_FILE` itself (the bootstrap exception, see above),
`OTEL_SERVICE_NAME` (`tests/test_telemetry.py` — that test is
specifically about env-var ordering before any Settings/provider exists,
migrating it would defeat the test's own point), `OMP_NUM_THREADS`
(`docs/analysis/tools/build_taxonomy.py`, an analysis tool outside the
image, not part of the running service).

## Hardcoded constants (beyond `os.environ`) — surveyed 2026-09-01, not migrated

A second, separate inventory. Every constant above already had an env
var; these never did — they're plain `ALL_CAPS = <literal>` module-level
constants, found by grepping every top-level file for
`^[A-Z_][A-Z0-9_]*\s*=\s*[0-9{]`. **Not being touched by any phase
above** — documented here purely so they aren't lost before their own
turn comes, per explicit instruction. Add rows as more are found; don't
assume this grep caught everything forever.

### Worth exposing — real operational tuning knobs

All are `default=<literal>` candidates matching their current value
(never `required=True` — every one of these already has a sensible,
working default; the point is letting it be *overridden*, not demanding
it be set).

| Constant | File | Controls |
|---|---|---|
| `PUSH_TICK_SECONDS = 900` | `bot.py` | Push heartbeat cadence |
| `INGEST_TICK_SECONDS = 900` | `bot.py` | Ingest periodic-check cadence |
| `DEFAULT_INTERVAL_HOURS = 4` | `news_ingest.py` | Default per-source pull interval |
| `_SOURCE_INTERVAL_HOURS` | `news_ingest.py` | Per-source interval overrides (dict) |
| `_DAILY_CAPS` | `news_ingest.py` | Per-source daily API-call budget (dict) |
| `REQUEST_DELAY_SECONDS = 1.1` | `news_ingest.py` | Rate-limit delay between source requests |
| `DEFAULT_TTL_HOURS = 48` | `news_cache.py` | Article cache retention |
| `DEFAULT_TTL_DAYS = 7` | `message_archive.py` | Message archive retention |
| `DEFAULT_PUSH_INTERVAL_HOURS = 24` / `MIN_PUSH_INTERVAL_HOURS = 1` | `users_db.py` | Subscriber push-frequency default/floor |
| `PUSHED_LINK_RETENTION_HOURS = 72` | `users_db.py` | How long "already seen" dedup memory lasts |
| `CATEGORY_PROPOSAL_THRESHOLD = 5` | `users_db.py` | Sightings needed before proposing a new category |
| `MAX_INTERESTS = 10` | `users_db.py` | Per-subscriber interest cap |
| `MAX_HTML_ATTEMPTS = 3` | `news_push.py` | HTML-validation retry budget |
| `NEAR_DUPLICATE_SIMILARITY = 0.95` | `news_push.py` | Near-dup collapse threshold |
| `MAX_ARTICLE_AGE_HOURS = 168` | `news_push.py` | How stale an article can be and still get pushed |
| `UNREACHABLE_STRIKES = 3` | `news_push.py` | Retries before giving up on a subscriber |
| `CALL_TIMEOUT_SECONDS = 60` | `test_api.py` | Dev-tool only, low priority, same reasoning as the others |
| **`MAX_ARTICLES_PER_TOPIC = 5`** | `news_push.py` | **Tied to this deployment's own cloud sizing** — see below |
| **`MAX_INTERESTS_PER_PUSH = 5`** | `news_push.py` | same |
| **`RELEVANCE_KEEP_FRACTION/MIN/MAX`, `NOVELTY_RELEVANCE_KEEP_FRACTION/MIN/MAX`** | `news_push.py` | same |
| **`CATEGORY_SIGHTING_RETENTION_DAYS = 30`** | `users_db.py` | same |
| **`PUSH_OUTCOME_RETENTION_DAYS = 90`** | `users_db.py` | same |

**Why the bolded five aren't just "borderline"** (corrected 2026-09-01
— originally filed as "nobody's ever asked to change these"): their
specific values exist *because of this deployment's own cloud
constraints* (the free-tier VM's RAM/CPU, article volume at current
subscriber count, what fits comfortably per push cycle) — not because
5 or 30 or 90 is intrinsically correct. A different deployment (bigger
machine, far more subscribers, a corpus-heavy self-hosted use case)
would legitimately want different numbers, which is exactly what makes
these settings rather than constants — the same reasoning that applies
to the plain tuning knobs above, just easier to miss because nothing
about them looks environment-specific at a glance.

**Also newly noticed while grounding this section**: `news_sources.py`'s
`_USER_AGENT = "Mozilla/5.0 (compatible; ArgusNewsBot/1.0;
+https://github.com/nankma/argus)"` — still says the pre-rename project
name and repo URL (missed during the Argus→Auguring rename, since it
reads as a "live functional constant," not a doc mention — see that
rename's own commit). Belongs in this same "worth exposing" bucket for
a different reason than the others: a self-hoster running this under
their *own* project name needs their own identifying User-Agent, not
this project's.

### Needs design thought, not simply "no"

Flagged as tricky rather than dismissed — each for a different reason,
not one blanket "internal, skip it":

| Constant | File | Why it's not a simple config-knob add |
|---|---|---|
| **`LOGFIRE_HOSTS`** | `agent.py` | **Architecturally misplaced, not just "maybe configurable."** It's a Logfire-specific region→host map, used only by `logfire_traces_endpoint()` — Logfire-vendor knowledge that has nothing to do with `agent.py`'s actual job (building the LangChain agent). It ended up there because `setup_telemetry()` currently lives in `agent.py` too. This should move as part of `docs/standaloneplan/README.md`'s Phase 3 (the `telemetry.events`/`telemetry.tracing` split) — once a Logfire-specific backend implementation exists behind that seam, `LOGFIRE_HOSTS`/`logfire_traces_endpoint()` belong inside *it*, not as a bare constant a deployer pokes at directly. Don't just add a setting here without doing that move first — it would cement the wrong location. |
| `_ALLOWED_TAGS` | `telegram_html.py` | A security allowlist, and channel-specific — Telegram HTML's safe tag set isn't the same question as email's would be. Naturally belongs inside each delivery-channel's own formatter (Phase 4, the email-client work) rather than one global setting; loosening it casually is a real injection-risk, not a convenience knob. |
| `RESTRICTED_SOURCES` | `news_sources.py` | Already tied to per-source config (`news_source.<name>.*`) once the news-source factory pattern lands (migration order step 3) — likely becomes a per-source flag there rather than a separate global set. |
| `NOUN_TAGS`, `MIN_GLOBAL_DF`, `MIN_EXPECTED_COUNT` | `news_keyness.py` | Algorithm hyperparameters for the novelty-detection statistic, not deployment config — closer to a research/tuning knob than something a self-hoster would reach for. Could theoretically be exposed later if someone's actually experimenting with the algorithm, different category of "configurable" than the rest of this doc. |
| `ROUTE_B_CATEGORIES` | `agent.py` | Coupled to the guardrail/routing design itself (`docs/plans/guardrails-plan.md`) — changing this changes routing/security behavior, not a sizing/cadence knob. A setting here needs guardrail-reliability re-measurement (`tools/measure_guardrails.py`) before it's safe, not just a config wire-up. |
| `_NARROW_CHECK_CATEGORIES` | `guardrails.py` | Same reasoning as `ROUTE_B_CATEGORIES` — the two are each other's mirror across the router/output-check layers. |

### Categorically not settings (derived, not independent)

Not "maybe someday" — these are *computed from other data*, so making
them independently settable wouldn't even be coherent:

- `_TIMESTAMP_LEN = 21` (`message_archive.py`) — fixed width of a
  `strftime` format string, a math fact about that format, not a
  tunable.
- `_SOURCE_CLASS`, `_TIME_FILTERABLE_CLASSES`, `_SERVER_SIDE_SINCE_SOURCES`
  (`news_ingest.py`) — derived from `news_sources.SOURCE_REGISTRY`
  itself; they change when the registry changes, not independently.

## Migration order and reasoning

1. ~~**Storage paths**~~ — **done** 2026-09-01. No secrets, lowest
   blast radius, plain `os.environ` reads (no `docker-entrypoint.sh`
   involved) — proved the `app_settings.py` bootstrap pattern and the
   `required=True`/"code and both settings.yml files move together"
   discipline before touching anything with real secrets or the
   entrypoint-fetch shape.
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

## Deploying storage paths: what "code and settings.yml move together" actually requires

`news_cache.py`/`message_archive.py`/`users_db.py` now use `required=True`
for `news_cache_dir`/`message_archive_dir`/`subscribers_db_file` (only
`news_archive_dir` keeps a plain `default=None`, since "off" is a real,
intentional state — see methodology rule 1). **This means the current
code cannot start at all without a settings.yml that has those three
keys** — confirmed directly: importing `news_cache` with no settings.yml
anywhere throws `SettingsError` immediately, not a silent fallback.
That's the point (fail loud on a real misconfiguration), but it makes
this phase's actual deploy prerequisite a hard requirement, not an
optional nicety:

**Before this code can ever run on the Oracle VM, `settings.oracle.yml`
(prepared, verified locally, not yet deployed) has to ship to the
container in the same deploy.** It bridges `storage.*` to the exact same
env vars `docker_run.command` already sets
(`-e NEWS_CACHE_DIR=/data/news_cache` etc.), via
`trailsign-resolve: environment-variable` nodes — so once deployed,
behavior is unchanged from today, but the failure mode if this step gets
skipped is now a crash at container startup, not a silent
`docs/plans/deployment-plan.md`-style cache reset. That's a real
improvement over the original design (a *silent* wrong-path bug) even
though it means the deploy has one more hard prerequisite than before.

To actually deploy: add
`-e SETTINGS_FILE=/config/settings.yml -v <path-on-vm>/settings.yml:/config/settings.yml:ro`
to `docker_run.command`, scp `settings.oracle.yml` to the
VM, then verify with `tools/check_data_persistence.py` (already exists,
already built for this exact failure mode) — don't just trust the
deploy succeeded because the container started; a crash-on-missing-key
is loud, but confirm the *values* are actually right too.

## Production secrets (Phase 2+): the pattern is proven, not theoretical

`OracleKeyVaultResolver` (Trailsign) was verified 2026-09-01 against a
real OCI Vault secret from a real OCI compute instance, using instance
principal auth — the same auth shape this project's own
`docker-entrypoint.sh` already uses for every other secret today. See
Trailsign's own repo history for the fix that made this work (the
resolver's original config-dict shape didn't match instance-principal
auth's `signer=`-based construction). When Phase 2 (models) or Phase 4
(Telegram/admin) needs a real vault-backed secret in PROD's
`settings.yml`, the `trailsign-credential-sources.oci-vault-main` +
`trailsign-resolve: oracleKeyVault` shape from Trailsign's own design
doc is ready to use, not speculative (key namespaced to
`trailsign-credential-sources` as of Trailsign v0.2.0).

## Testing

Already built, not just planned. Two layers:

- **`app_settings.reset_settings_for_tests()`** (no-arg resets to force
  a reload; pass a `Settings` instance to inject a fake) plus
  `trailsign.Settings.__init__` accepting a plain dict directly
  (`Settings({"storage": {"news_cache_dir": "/tmp/x"}})`) — no file
  needed. `tests/test_app_settings.py` covers `app_settings.py` itself.
- **`tests/conftest.py` injects a real `Settings` dict** (the `storage.*`
  section, matching what's `required=True` today) **before** importing
  `news_cache`/`message_archive`/`users_db`/etc. — has to happen at
  conftest's own module level, not inside a fixture, since the migrated
  modules' constants are computed once at first import (collection
  time), before any fixture runs. **This is a real forcing function, not
  just plumbing**: forgetting to add a newly-`required=True` key here
  when migrating a new setting breaks the entire test suite immediately
  and loudly (every test importing the affected module fails to even
  collect) — treat that failure as "you forgot a step," not a bug to
  route around.

Existing tests for migrated modules needed **zero** further changes
across Phase 1 beyond the conftest.py injection above, because they
already monkeypatched the module-level constant
(`monkeypatch.setattr(news_cache, "CACHE_DIR", ...)`) rather than the
env var or `Settings` itself — expect the same to hold for Phases 2–4,
worth confirming per-phase rather than assuming.
