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
| `DEEPSEEK_API_KEY` | ~~`tools/measure_guardrails.py`, `tools/run_eval.py`, `agent.py` (implicitly via `ChatDeepSeek`)~~ | **`docker-entrypoint.sh` vault-fetch** (via `DEEPSEEK_API_KEY_SECRET_OCID`, unchanged) | **yes** | `models.main.api-key` / `models.guardrail.api-key` | **Migrated** 2026-09-02 — see methodology rule 3, this was the worked example |
| `LLM_MODEL` / `LLM_MODEL_CLASSIFIER` | ~~`agent.py` (`build_model`)~~ | plain `os.environ` | no | `models.main.model` / `models.guardrail.model` | **Migrated** 2026-09-02 — `build_model`/`init_chat_model` removed outright, not kept alongside; the new `agent.build_model_from_config` is provider-generic (any OpenAI-wire-compatible endpoint via `ChatOpenAI`), superseding the LangChain-provider-registry approach `model-portability-plan.md` originally described, not just feeding it |
| `LLM_REASONING_EFFORT` / `LLM_REQUEST_TIMEOUT_SECONDS` | ~~`agent.py`~~ | plain `os.environ` | no | `models.main.reasoning_effort` / `models.main.request_timeout_seconds` (and the `models.guardrail.*` mirrors) | **Migrated** 2026-09-02, `default=<literal>` in `build_model_from_config` (reasoning_effort defaults to absent, not `"none"` — that's DeepSeek-specific, set explicitly in settings.yml's own `models.main`/`models.guardrail`, not code) |
| `LOGFIRE_ENABLED` | ~~`agent.py`~~ | plain `os.environ` | no | ~~`telemetry.enabled`~~, now `telemetry.providers[]` (see below) | **Migrated** 2026-09-03 to flat `telemetry.enabled`/`.logfire-api-key`, `resolved_optional` (fails open). **Reshaped** 2026-09-03, same day, into the pluggable-provider refactor below (take two: `telemetry.general[]`/`telemetry.llm[]`), then **reshaped again** the same day (take three: ONE list, `telemetry.providers[]`, routed by each entry's own `KIND` — see below for why take two's two-list schema was itself a real bug). There is no more separate `enabled` boolean at all now — see below for why an empty provider list replaces it. |
| `LOGFIRE_API_KEY` | ~~`agent.py`, `tools/check_logfire.py`~~ | **`docker-entrypoint.sh` vault-fetch** | **yes** | ~~`telemetry.logfire-api-key`~~, now nested inside a `telemetry.providers[]` entry's `headers.Authorization` (see below) | **Migrated** 2026-09-03, then **reshaped twice** 2026-09-03 (same day) into the pluggable-provider refactor -- see "Telemetry providers, take two" and "take three" below |
| `NEWSAPI_API_KEY`, `GNEWS_API_KEY`, `PERIGON_API_KEY` | ~~`news_sources.py`~~, now `news_adapters/{newsapi,gnews,perigon}.py` | **`docker-entrypoint.sh` vault-fetch** (unchanged) | **yes** | `news_source.api[].api-key` (see below -- reshaped from `news_source.<name>.api-key` the same day) | **Migrated** 2026-09-03, then **reshaped** 2026-09-03 into the `NewsSourceAdapter`/`news_adapters/` refactor -- see "News sources, take two" below. A source with no key configured (or an unresolvable one) is still skipped, same as an unset env var always was; the trailsign gotcha behind that (a `trailsign-resolve` node that's *present* but points at an unset env var raises `SettingsError` even with `default=None`) is now handled per-entry, not per-source-dict-key -- see `news_sources._raw_api_entries`/`_resolved_api_key`'s own docstrings for why resolving `news_source.api` as one list would have reintroduced a worse version of the same problem (one bad entry blowing up every other configured source, not just itself). |
| `TELEGRAM_BOT_TOKEN` | ~~`bot.py`, `combined_bot.py`, `admin_bot.py`~~ | **`docker-entrypoint.sh` vault-fetch** | **yes** | `delivery.telegram.bot-token` | **Migrated** 2026-09-03, `required=True` (unchanged bracket-access-equivalent semantics — a bot that can't authenticate to Telegram should fail loudly at startup) |
| `ADMIN_BOT_TOKEN` | ~~`bot.py`, `combined_bot.py`, `admin_bot.py`~~ | **`docker-entrypoint.sh` vault-fetch** | **yes** | `delivery.telegram.admin-bot-token` | **Migrated** 2026-09-03, `required=True`. Path uses a hyphen (`admin-bot-token`), not the underscore this table originally sketched (`admin_bot_token`) — normalized to match `bot-token`'s own hyphen and this codebase's broader `api-key`-style hyphenated-field convention. |
| `ADMIN_CHAT_ID` | ~~`bot.py`, `combined_bot.py`, `admin_bot.py`~~ | **`docker-entrypoint.sh` vault-fetch** | no (an id, not a credential — still fetched the same way) | `delivery.telegram.admin-chat-id` | **Migrated** 2026-09-03, `required=True` (same hyphen normalization as `admin-bot-token` above) |
| `TEST_API_PORT` / `ENABLE_TEST_API` | ~~`test_api.py`~~ | plain `os.environ` | no | `test_api.port` / `test_api.enabled` | **Migrated** 2026-09-03, `resolved_optional` (both fail open — `port` keeps its old 8765 default, `enabled` keeps its old "server never starts" default) |

**A deliberately SHALLOWER migration than `DEEPSEEK_API_KEY`'s rule-3
"worked example" for the four entrypoint-vault-fetched rows above
(`TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_CHAT_ID`,
`LOGFIRE_API_KEY`) — flagging so a future session doesn't assume every
vault-fetched secret finished the same way.** These bridge to the
*already-vault-fetched plain env var* (`trailsign-resolve:
environment-variable`, same as any plain `os.environ` migration) rather
than doing rule 3's full 3-step pattern (Python reading directly via a
`trailsign-resolve: oracleKeyVault` node, `docker-entrypoint.sh`'s
now-redundant fetch block for that variable removed). `docker-entrypoint.sh`
is completely untouched by this batch — still fetches all four from
Vault and exports them as plain env vars, exactly as before. This was a
scope choice, not an oversight: going all the way to direct
`oracleKeyVault` resolution for these four is real, separate follow-up
work (touching `docker-entrypoint.sh` and re-verified on a real deploy,
per rule 3's own requirements), not something to fold into a
same-shape-as-plain-env-vars batch.

Deliberately **not** migrating (stay raw `os.environ`, out of scope):
`SETTINGS_FILE` itself (the bootstrap exception, see above),
`OTEL_SERVICE_NAME` (`tests/test_telemetry.py` — that test is
specifically about env-var ordering before any Settings/provider exists,
migrating it would defeat the test's own point), `OMP_NUM_THREADS`
(`docs/analysis/tools/build_taxonomy.py`, an analysis tool outside the
image, not part of the running service).

### Telemetry providers, take two: `telemetry.enabled` reshaped into pluggable `telemetry.general[]`/`telemetry.llm[]` — SUPERSEDED, see "take three" below

**This subsection describes the FIRST reshape, kept as history.** Code
review caught a real bug in it hours later — double-exporting every
span to Logfire, and an llm-only provider silently receiving general
app-event spans — and it was corrected the same day into the one-list
`telemetry.providers[]` schema. See "Telemetry providers, take three"
further down for the corrected, currently-live design; the rest of
this subsection is left as originally written, so the reasoning that
seemed right at the time (and why it turned out wrong) isn't lost.

Same day as the migration above, and same reasoning class as `news_source.api`'s
own reshape (see `NEWSAPI_API_KEY`'s row above) — a single-backend,
boolean-gated design (`telemetry.enabled` + `telemetry.logfire-api-key`,
hardcoded to Logfire) got replaced with a `NewsSourceAdapter`-style
pluggable-provider mechanism once it became clear more than one telemetry
backend, and two DIFFERENT capability concerns (general app-event logging
vs. LLM-call tracing specifically), needed to be independently
configurable — Phoenix, Grafana Cloud, SigNoz, OpenObserve, a local
file all being real candidates, not just Logfire.

New package `telemetry_providers/` (structurally identical to
`news_adapters/`: a `TelemetryProvider` Protocol, `pkgutil`/`importlib`
auto-discovery, `validate_configured_types` failing the whole process at
startup if a configured `type` has no matching class). Three providers
shipped this batch: `otlp` (generic — endpoint/headers only, covers
Logfire/Grafana Cloud/SigNoz/OpenObserve/Phoenix's own OTLP ingestion,
`KIND={"general","llm"}`), `file` (writes JSON lines to a local file, no
OTel involved, `KIND={"general"}` only), `phoenix` (`KIND={"llm"}` only —
see below for why it needs no `arize-phoenix-otel` dependency at all,
deliberately deviating from an earlier plan to use that package).
`langfuse`/`openlit` were discussed as future candidates but not built —
no stub files, just an open extension point.

New top-level `telemetry.py` replaces `logfire_logger.py` (deleted
outright, no compat shim — same "clean delete" precedent this project
already set for `telemetry_monitor.py`'s Phoenix retirement) — its
`setup_telemetry()` builds ONE shared `TracerProvider` per process and
attaches every configured provider's own processor/instrumentation to
it via `add_span_processor` (additive), and `get_event_logger(scope)`
replaces `LogfireLogger(scope)` at all eleven `_events = ...` call
sites, fanning out each `.log(...)` call to every configured
non-span-based general provider while emitting exactly one shared span
regardless of how many span-based ones (otlp entries) are configured --
OTel's own multi-processor delivery already fans that one span out to
all of them; calling per-provider would double-export.

**No more `telemetry.enabled` boolean.** Two independent lists,
`telemetry.general`/`telemetry.llm`, each `default=[]` — an empty list
IS "off" for that category, same contract `news_source.rss`/
`news_source.api` already have. `settings.oracle.yml` (PROD,
`LOGFIRE_ENABLED=true` today) got live entries in both lists, pointed at
Logfire; `settings.int.yml` (INT, `LOGFIRE_ENABLED` deliberately never
set — see `local-infra/infrastructure.yaml`'s own note on why sharing
PROD's Logfire project would defeat the dead-man's-switch alerts) got no
`telemetry:` section at all, same "absent means off" shape as an
unconfigured `news_source.api`; `settings.yml` (local dev) got a
commented preview — **NOT live**, unlike the old
`telemetry.enabled`/`.logfire-api-key` shape, which safely relied on the
separate `enabled` flag to stay off even though `LOGFIRE_API_KEY` is
present in this project's dev environment. Without that flag, an
uncommented live entry here would start every local script/test run
exporting real spans the moment that env var resolves — the exact
failure the old design's `enabled` gate existed to prevent. This is the
one real, deliberate behavior-shape change from the reshape: the
"off by default even with a credential present" safety net moved from
an explicit flag to "don't put a live entry in the file," same as every
other optional Settings section in this project already works.

`endpoint` is now a literal value in Settings, not derived from the
token's region at runtime the way `agent.setup_telemetry()` used to
(`agent.logfire_traces_endpoint`) — `telemetry_providers/otlp.py` takes
whatever `endpoint` config says, no vendor-specific logic. That function
still exists in `agent.py`, kept as a standalone deployer's tool (run it
once against a real token to get the value to paste into settings), just
no longer called automatically.

**Deviation from the original plan, backed by evidence not assumption:**
`telemetry_providers/phoenix.py` does NOT use `arize-phoenix-otel` at
all, despite that being the original intent when this batch started.
Directly inspecting `phoenix.otel.register()`'s actual implementation
(installed into a scratch directory outside this project's environment,
purely to read the source) showed it unconditionally builds and returns
its OWN `TracerProvider` subclass — there is no parameter to hand it an
existing provider to attach to instead, and that subclass is the same
one whose "silently discards its own default processor unless told not
to" behavior caused the real dual-write incident this whole redesign
exists to avoid (see `docs/plans/observability-platform-plan.md`).
Sidestepping `register()` avoids that failure class at the root: Phoenix
ingests plain OTLP under the hood, so `phoenix.py` builds a plain
`OTLPSpanExporter` pointed at Phoenix's endpoint and attaches it to the
same shared provider every other provider uses — identical shape to
`otlp.py`, needing nothing beyond `opentelemetry-sdk`/
`opentelemetry-exporter-otlp-proto-http`, both already required
dependencies. Nothing new went into `environment.yml` for this.

### Telemetry providers, take three: ONE list, `telemetry.providers[]`, routed by KIND — the currently-live design

Found by code review, same day (2026-09-03), hours after "take two"
shipped: **two separate settings lists for two capability categories
was itself a bug, not just a design preference.** `otlp`'s `KIND` is
`{"general","llm"}` — a deployer wanting one Logfire project to receive
both general app-events and LLM-call traces had to write the SAME
`type: otlp` / `endpoint` / `headers` entry out TWICE, once under
`telemetry.general` and once under `telemetry.llm` (exactly what
`settings.oracle.yml`'s live config did). Combined with the take-two
implementation building one SHARED `TracerProvider` for everything,
this meant every span got exported to Logfire twice, and a genuinely
llm-only provider (Phoenix) silently received every general-category
span too, since nothing scoped a processor to the list its entry came
from. Confirmed with a real, not mocked, OTel repro
(`TracerProvider`/`InMemorySpanExporter`): two processors registered
the way a general entry and an llm entry each would, one span emitted
the way `EventLogger.log()` does — both processors received it.

**The fix collapses to ONE settings list, `telemetry.providers[]`.**
Each entry is just `{type, ...config}` — no `general:`/`llm:` split at
all. `telemetry.py`'s coordinator determines what an entry does purely
from its discovered class's own `KIND`: `"general"` in `KIND` routes it
to an internal general `TracerProvider`; `"llm"` in `KIND` routes it to
an internal llm `TracerProvider`; a dual-KIND entry (otlp) gets routed
to BOTH from the one config line — `initialize()` is called once per
applicable internal provider, on a FRESH provider-class instance each
time (never the same instance twice), so a dual-KIND entry ends up with
two genuinely independent processors, not one shared unsafely between
categories. `TelemetryProvider.initialize()`'s signature grew a third
parameter, `kind: str` ("general" or "llm"), so a provider like otlp can
tell which call this is -- load-bearing for `instrument_langchain`
specifically: without checking `kind == "llm"`, a dual-KIND entry with
`instrument_langchain: true` would also instrument LangChain onto the
GENERAL provider on its general-side call, which makes no sense (LLM
spans have no business there).

The two internal `TracerProvider` objects themselves are unchanged from
take two's intent (that part was correct) -- genuinely separate
providers is still what makes the general/llm categories actually
isolated at the OTel level; only the SETTINGS SHAPE (one list vs. two)
and the routing mechanism (by KIND, not by which list a deployer chose)
changed. `validate_configured_types` simplified alongside this -- it
only checks a configured `type` exists at all now; there's no more
"configured under the wrong list" case to check, since there's no
longer a list a deployer can put an entry under incorrectly.

`settings.oracle.yml` shrank to ONE `otlp` entry (`instrument_langchain:
true`), replacing the take-two duplicate-entry-under-both-lists shape --
this alone is direct proof the new schema removes the double-export bug
at the config level, not just internally. `settings.yml`'s commented
preview and `settings.int.yml`'s "no telemetry section" state both
updated the same way. Three new/rewritten empirical tests in
`tests/test_telemetry.py` (real `TracerProvider`/`SimpleSpanProcessor`/
`InMemorySpanExporter`, not mocks) prove: a dual-KIND `otlp` entry
genuinely receives both categories from one config entry (two
independent exporters, not a shared one); an llm-only provider
(`phoenix`) never receives a general-category event even when
configured in the same list as a general-only provider (`file`). See
`telemetry.py`'s own module docstring for the full mechanism -- this
paragraph is the abbreviated tracking-doc version.

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

| Constant | File | Controls | Status |
|---|---|---|---|
| ~~`PUSH_TICK_SECONDS = 900`~~ | `bot.py` | Push heartbeat cadence | **Migrated** 2026-09-03 → `push.tick_seconds` — first real entry in `push.*`, still mostly reserved for `news_push.py`'s own batch |
| ~~`INGEST_TICK_SECONDS = 900`~~ | `bot.py` | Ingest periodic-check cadence | **Migrated** 2026-09-03 → `news_source.tick_seconds` |
| ~~`DEFAULT_INTERVAL_HOURS = 4`~~ | `news_ingest.py` | Default per-source pull interval | **Migrated** → `news_source.default_interval_hours` |
| ~~`_SOURCE_INTERVAL_HOURS`~~ | `news_ingest.py` | Per-source interval overrides (dict) | **Migrated, redesigned** — no longer a dict; each source's override is `news_source.<name>.interval_hours`, nested in that source's own block (alongside its `api-key`), not a separate global mapping. `_interval_hours()` does a live per-source lookup. |
| ~~`_DAILY_CAPS`~~ | `news_ingest.py` | Per-source daily API-call budget (dict) | **Migrated, same redesign** → `news_source.<name>.daily_cap` |
| ~~`REQUEST_DELAY_SECONDS = 1.1`~~ | `news_ingest.py` | Rate-limit delay between source requests | **Migrated** → `news_source.request_delay_seconds` |
| ~~`DEFAULT_TTL_HOURS = 48`~~ | `news_cache.py` | Article cache retention | **Migrated, redesigned same day** → `storage.news_cache_dir.ttl_hours` — first landed as a flat sibling key (`storage.news_cache_ttl_hours`), then renested inside `news_cache_dir` itself (`{path, ttl_hours}`) to co-locate the retention setting with the directory it prunes, same pattern as `news_source.<name>.interval_hours`. Real gotcha hit doing this: a `trailsign-resolve` node collapses ENTIRELY to the resolver's return value, so `ttl_hours` can't be a sibling of `trailsign-resolve` inside the same dict (confirmed live — it silently vanishes) — it has to nest under `path` specifically, one level below where `trailsign-resolve` lives. This is *why* `settings.oracle.yml`/`settings.int.yml` need the extra `path:` layer that `settings.yml` doesn't strictly require (no `trailsign-resolve` there) but uses anyway for consistency across all three files. |
| ~~`DEFAULT_TTL_DAYS = 7`~~ | `message_archive.py` | Message archive retention | **Migrated, same redesign** → `storage.message_archive_dir.ttl_days` |
| ~~`DEFAULT_PUSH_INTERVAL_HOURS = 24` / `MIN_PUSH_INTERVAL_HOURS = 1`~~ | `users_db.py` | Subscriber push-frequency default/floor | **Migrated** → `subscription.default_interval_hours` / `subscription.min_interval_hours` — a subscriber-preference concern, not push mechanics, hence `subscription.*` not `push.*` |
| ~~`PUSHED_LINK_RETENTION_HOURS = 72`~~ | `users_db.py` | How long "already seen" dedup memory lasts | **Migrated** → `subscription.pushed_link_retention_hours` |
| ~~`CATEGORY_PROPOSAL_THRESHOLD = 5`~~ | `users_db.py` | Sightings needed before proposing a new category | **Migrated** → `categories.proposal_threshold` — the category-taxonomy-proposal feature got its own top-level section, kept isolated from `subscription.*` |
| ~~`MAX_INTERESTS = 10`~~ | `users_db.py` | Per-subscriber interest cap | **Migrated** → `subscription.max_interests` |
| ~~`MAX_HTML_ATTEMPTS = 3`~~ | `news_push.py` | HTML-validation retry budget | **Migrated** 2026-09-03 → `push.max_html_attempts` |
| ~~`NEAR_DUPLICATE_SIMILARITY = 0.95`~~ | `news_push.py` | Near-dup collapse threshold | **Migrated** → `push.near_duplicate_similarity` |
| ~~`MAX_ARTICLE_AGE_HOURS = 168`~~ | `news_push.py` | How stale an article can be and still get pushed | **Migrated** → `push.max_article_age_hours` |
| ~~`UNREACHABLE_STRIKES = 3`~~ | `news_push.py` | Retries before giving up on a subscriber | **Migrated** → `push.unreachable_strikes` |
| `CALL_TIMEOUT_SECONDS = 60` | `test_api.py` | Dev-tool only, low priority, same reasoning as the others | Not started |
| ~~`MAX_ARTICLES_PER_TOPIC = 5`~~ | `news_push.py` | Tied to this deployment's own cloud sizing — see below | **Migrated** → `push.max_articles_per_topic` |
| ~~`MAX_INTERESTS_PER_PUSH = 5`~~ | `news_push.py` | same | **Migrated** → `push.max_interests_per_push` |
| ~~`RELEVANCE_KEEP_FRACTION/MIN/MAX`, `NOVELTY_RELEVANCE_KEEP_FRACTION/MIN/MAX`~~ | `news_push.py` | same | **Migrated** → `push.relevance_keep.{fraction,min,max}` / `push.novelty_relevance_keep.{fraction,min,max}` |
| ~~`CATEGORY_SIGHTING_RETENTION_DAYS = 30`~~ | `users_db.py` | same | **Migrated** → `categories.sighting_retention_days` (moved out of the "tied to cloud sizing" framing -- it's the category-proposal feature's own retention window, not a sizing knob) |
| ~~`PUSH_OUTCOME_RETENTION_DAYS = 90`~~ | `users_db.py` | same | **Migrated, redesigned** → `storage.push_outcomes_ttl_days` — reframed as a storage/retention concern (alongside `news_cache_dir` etc.), not a push-behavior or subscription setting, and renamed to match the `_ttl_` convention planned for `news_cache.py`/`message_archive.py`'s retention values above |

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
| `RESTRICTED_SOURCES` | `news_sources.py` | The news-source factory pattern landed (migration order step 3) but this stayed a separate global set, not a per-`news_source.api[]` flag as originally guessed here — it's a per-user-access policy (`docs/current/ai-news-sources.md`'s "Restricted sources" section), a different axis from "is this source configured/enabled" that `news_source.api` answers. Revisit only if that policy itself needs to become settings-driven, not as a side effect of the adapter refactor. |
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
2. ~~**Models**~~ — **done** 2026-09-02. First real secret migrated. Scope
   grew beyond the original "feeds `model-portability-plan.md`, no new
   design" plan: the provider-string `build_model()`/`init_chat_model`
   construction path was removed outright (not kept alongside), replaced
   by `agent.build_model_from_config`/`build_model_from_settings` --
   generic across any OpenAI-wire-compatible provider via `ChatOpenAI`,
   so a deployment points at a different AI provider by editing its own
   settings.yml alone, no code change and no new LangChain provider
   package needed. `tools/measure_guardrails.py`'s hardcoded
   `deepseek-chat` baseline was folded in too -- it now reads
   `models.guardrail` the same way, so measuring a candidate model/
   provider's baseline is "point `SETTINGS_FILE` at a settings.yml naming
   it, re-run, compare reports" rather than editing the harness itself.
3. **News sources.** Split into two, done separately (turned out not to
   need to be one pass):
   - ~~**RSS sources**~~ — **done** 2026-09-03. All 22 query-less
     `_fetch_rss`-wrapper sources moved to `news_source.rss` (a list of
     `{key, display_name, url}`) in settings.yml/settings.oracle.yml/
     settings.int.yml — `news_sources._rss_sources_from_settings()`
     builds their `SOURCE_REGISTRY` entries at import time, same pattern
     as `models.*`. No API keys involved (RSS needs none), so this
     needed no factory-pattern/credential design at all — a deployer
     adds/removes/edits a feed by editing their own settings.yml.
     `hackernews`/`arxiv` (real query/date-range logic) stayed hardcoded
     Python, alongside the three still-pending API-gated sources below.
   - ~~`NEWSAPI_API_KEY`, `GNEWS_API_KEY`, `PERIGON_API_KEY`~~ — **done**
     2026-09-03, in two passes the same day:
     - **First pass**: credential-only. Turned out NOT to need the
       factory pattern -- each source's real per-source query/auth logic
       (URL, param shapes, response parsing) stayed exactly where it was
       in `news_sources.py`'s `fetch_newsapi`/`fetch_gnews`/`fetch_perigon`
       functions; only the credential (`os.environ["X_API_KEY"]` →
       `news_source.<name>.api-key` via Settings, one dict key per
       source) moved. `_NON_RSS_SOURCES` stored a settings-path fragment
       per gated source instead of a raw env-var name.
     - **Second pass, same day: the factory pattern after all.** A
       direct follow-up request asked for exactly the plugin
       architecture the first pass had judged unnecessary: a
       `NewsSourceAdapter` interface (`initialize(config)`/`pull(...)`,
       a `typing.Protocol`, structurally typed like `logfire_logger.py`'s
       `Logger`/`LogfireLogger` -- no explicit subclassing), one class
       per source under a new `news_adapters/` package, discovered at
       process startup via `pkgutil.iter_modules` +
       `inspect.getmembers` (`news_sources.discover_adapter_types`) so a
       new source is "write the adapter file, add a settings entry," no
       registry edit. Settings reshaped again, from separate
       `news_source.newsapi`/`.gnews`/`.perigon` dict keys to one
       `news_source.api: [{key, type, api-key, interval_hours,
       daily_cap}, ...]` list -- `type` selects the adapter class,
       `key` is the source's own registry identity (kept distinct so a
       future second account could reuse one adapter type). A
       misconfigured/absent `type` fails the whole process at startup
       (`validate_configured_types`, `SettingsError`), per an explicit
       requirement that this be loud, not silent, unlike a merely-
       unresolvable credential (still dropped quietly, matching the
       always-standing "optional source degrades, doesn't crash"
       contract). `hackernews`/`arxiv` also became adapter classes for
       consistency, but stayed OUTSIDE `news_source.api` entirely --
       free, no credential, no override, wired in directly by
       `news_sources._always_on_sources()` -- forcing them through the
       same settings-driven list as the credentialed sources would only
       have added ceremony with no real behavior to justify it.
       **A real trailsign finding drove the settings design**: resolving
       `news_source.api` as one list in one `Settings.resolved()` call
       recursively resolves every entry's `api-key` too, so ONE entry
       with an unresolvable credential (an unset env var for an
       intentionally-off optional source) raised for the WHOLE list,
       reintroducing a worse version of the exact problem the first
       pass's per-source-dict-key design had avoided (see
       `news_sources._raw_api_entries`'s own docstring). Fixed by
       reading the list raw (unresolved) and resolving each entry's own
       `api-key` independently, catching `SettingsError` per entry --
       verified live before settling on this shape, not assumed.
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

## Settings file comment cleanup (2026-09-03) — where the detail went

The three settings files (`settings.yml`, `settings.oracle.yml`,
`settings.int.yml`) had accumulated years-of-history-style comments —
useful once, but not what someone configuring or deploying the file
actually needs to read every time. Trimmed per file by audience:
`settings.yml`'s comments are now just "what this value is for" + an
example (its audience is someone setting up their own local copy);
`settings.oracle.yml`/`settings.int.yml`'s comments are now just "why
this specific value is set this way" (their audience is someone
deciding whether it's safe to change something, not learning the
subsystem). The design rationale, incident histories, and rejected
alternatives that used to live inline moved here. Sections below not
already covered elsewhere in this doc (telemetry's two-iteration
redesign is in "Telemetry providers, take two/three" above; the
`news_source.api` factory-pattern reshape is in "Migration order" §3;
the `ttl_hours` nesting gotcha is in the "Worth exposing" table's
`DEFAULT_TTL_HOURS` row) — this section only carries what was ONLY in
the YAML comments before now.

### `storage.*`

`news_archive_dir` has no TTL of its own, deliberately — articles move
here INSTEAD of being deleted from `news_cache_dir` once their cache
TTL expires, so nothing prunes this directory by age; it stays a bare
path (no `path:`/`ttl_hours:` nesting) since there's nothing to nest.
Omitted entirely from `settings.yml` (archiving off is `news_cache.py`'s
own `default=None`, a legitimate intentional state, not a missing
required key) but present and live in `settings.oracle.yml`/
`settings.int.yml`.

`push_outcomes_ttl_days` deliberately outlives `categories.
sighting_retention_days` (90 vs 30 days) — answering "was this normal?"
after an incident means comparing against the weeks before it, and
`push_outcomes` rows are tiny (`users_db.py`'s own comment), so there's
no storage-cost reason to match the shorter window.

### `news_source.*`

**Real incident, 2026-09-02 to 2026-09-03: `settings.oracle.yml` and
`settings.int.yml` each briefly had a SECOND top-level `news_source:`
mapping** (the `newsapi`/`gnews`/`perigon` block was added as its own
`news_source:` key further down the file, instead of nesting inside the
existing one). YAML duplicate-key handling silently keeps only the LAST
mapping — production would have deployed with ZERO RSS sources, the
whole `rss:` list shadowed and gone. Caught before it ever reached a
real deploy, confirmed live via `yaml.safe_load` showing only
`{newsapi, gnews, perigon}` as keys, `rss` silently absent. Fixed by
merging back into one mapping. This is why both files' comments say
"ONE `news_source:` key for the whole file" — it's not stylistic, a
second one is a silent, undetected-by-YAML data-loss bug.

RSS sources (`news_source.rss`) are free, no API key, query-less — each
returns its latest N items regardless of what's asked
(`news_sources._fetch_rss`), so the whole list is pure data with zero
credential/factory-pattern design needed, unlike the `api` list.
`hackernews`/`arxiv` (free, real query/date-range logic, always-on) are
wired in directly by `news_sources._always_on_sources()` rather than
living in either `rss` or `api` — forcing them through a settings-driven
list would add ceremony with no behavior to justify it (see "Migration
order" §3 for the `api`-list adapter-pattern reasoning that does NOT
apply to these two).

`interval_hours`/`daily_cap` per `news_source.api[]` entry are tuned to
each source's real budget, not arbitrary: `perigon`'s 8h/3-per-day
matches its 150/month plan; `newsapi`'s 24h/1-per-day matches an
individual-use judgment call recorded in
`docs/plans/local-news-cache-plan.md`. `newsapi`/`perigon` are also both
in `news_sources.RESTRICTED_SOURCES` (excluded from `agent.py`'s live
`search_news`, ingest-only) — `newsapi`'s free tier is documented
dev/test-only, not production (`docs/current/ai-news-sources.md`);
`perigon`'s budget is already fully spoken for by the scheduled ingest
job's own capped pulls. `gnews` is the one source with real headroom
(100/day) and isn't restricted from live search either.

The "an entry with an unresolvable `api-key` is silently dropped, not a
crash" contract, and why resolving `news_source.api` as one list would
have broken that, is covered under "Migration order" §3's second pass —
`news_sources._raw_api_entries`/`_resolved_api_key` are the functions
that implement the fix.

### `models.*` — INT's guardrail model choice

INT's `models.main` matches PROD exactly (DeepSeek direct) — it's
`models.guardrail` that deliberately differs, routed through Together.ai
instead: `deepseek-ai/DeepSeek-V4-Flash-0731` via Together, same model
as `main`, tied on price ($0.14/$0.28 combined $0.42, identical to
DeepSeek's own pricing for this model — not actually cheaper). The
point isn't price: guardrail is where INT's real call volume lives
(layer 2/4 + ingest classification, far more calls than `main` ever
makes), and routing that through a SEPARATE Together account/budget
means it can't drain or rate-limit-starve the main DeepSeek API key.

**Two genuinely cheaper Together candidates were tried and rejected,
2026-09-02 — don't re-try either without addressing what broke them:**
- `openai/gpt-oss-20b` ($0.25 combined): passed a single-trial
  `tools/measure_guardrails.py` sanity check (layer2 18/21, multi-intent
  6/6, layer4 8/8) but failed hard under real INT load once deployed —
  sustained `OpenAITimeoutError` on both ingest `classify_articles`
  batches and live `test_api` messages (well past the initial
  restart-ingest burst window, not explained by it), plus one live call
  that extracted the wrong interest text entirely ("robotics" →
  "final"). Read as a reasoning-style model generating more than a
  router call needs — a single-trial pass wasn't enough signal; real
  load caught what it didn't.
- `arize-ai/qwen-2-1.5b-instruct` ($0.20 combined): failed the isolated
  sanity check outright — layer2 2/21 (10%), multi-intent 1/6 (17%).
  1.5B is too small for this task's nuance (bilingual, multi-field
  extraction).

**Also confirmed**: Together's `/v1/models` listing has NO reliable
field for "actually callable serverless, not dedicated-endpoint-only" —
nonzero pricing does NOT mean available. `meta-llama/Meta-Llama-3.1-8B-
Instruct-Turbo` and 7 other cheap/"Turbo"-branded candidates
(Qwen2.5-7B/72B-Turbo, Mistral-Small-24B, Llama-3.2-3B,
Llama-3-8b-chat-hf, Meta-Llama-3-8B-Instruct, Ministral-3-14B,
arcee-ai/trinity-mini) all returned "Unable to access non-serverless
model ... create a dedicated endpoint" on a real call. Two more ARE
callable but weren't viable either: `Llama-3.3-70B-Instruct-Turbo` works
but costs $2.08 combined (5x main); `Qwen/Qwen3.5-9B` is callable and
ties main's price, but its reply leaked a raw `</think>` tag into
content — a reasoning-model tell, same risk class as gpt-oss-20b's
failure, not tested further. **A single `ChatOpenAI(...).invoke()` probe
call is enough to rule a candidate in/out on availability before
spending on the full `measure_guardrails.py` suite** — do that first for
any future candidate.

### `push.*`

`max_articles_per_topic`/`max_interests_per_push` bound how much one
subscriber gets in one cycle — interests that don't fit wait for a
later cycle, they aren't dropped (`news_push.py`'s own comments).
`relevance_keep`/`novelty_relevance_keep` are absolute-count clamps
(fraction of pool, floor, ceiling), not a fixed fraction, because a
fixed fraction measurably breaks across different pool sizes and topic
phrasings, and recall past a certain point stops being worth chasing —
see `news_push.py`'s own comment for the three measurements that forced
this shape. `near_duplicate_similarity` is the cosine-similarity
threshold two articles collapse at (same wire story under two links, or
syndication). `max_article_age_hours` is deliberately generous (168h) —
a ceiling against absurdly stale content, not a freshness requirement.
`unreachable_strikes` is consecutive undeliverable digests before push
turns off for that subscriber.

### `subscription.*` vs `push.*`

Two different axes, kept in separate top-level sections on purpose:
`subscription.*` is the subscriber's OWN cadence preference
(`default_interval_hours`/`min_interval_hours`) and per-subscriber state
(`pushed_link_retention_hours`, `max_interests`) — user-facing account
settings. `push.*` is how the push JOB itself builds and paces one
digest once a subscriber is due — mechanics, not preference.
`pushed_link_retention_hours` is deliberately longer than
`news_cache_dir`'s own cache TTL (`users_db.py`'s own comment) — the
"already sent this link" dedup memory only needs to outlast whatever the
cache TTL already bounds.

### `categories.*`

Kept as its own top-level section, isolated from `subscription.*`/
`push.*`, because the category-taxonomy-proposal feature
(`users_db.py`'s `categories`/`category_sightings` tables) is a
genuinely separate concern from either subscriber preferences or push
mechanics. `proposal_threshold` (sightings of an out-of-taxonomy label,
inside the retention window, before an admin is asked to
activate/merge/reject it) is a placeholder judgment call, not a measured
value — revisit once there's a real sighting distribution to read
(`users_db.py`'s own comment).

### `delivery.telegram.*`

Three keys, one bot's worth of identity plus the operator's own id:
`bot-token` is the info bot's own token (what subscribers talk to);
`admin-bot-token` is the SEPARATE second bot `admin_bot.py` runs
(category proposal review, access-request approval) — two different
Telegram bot identities by design, not one bot wearing two hats;
`admin-chat-id` is the operator's own numeric Telegram user id, always
allowed past the access-request gate (`bot.py`'s own docstring), and
where `admin_bot.py`'s notifications land. All three `required=True` —
a bot that can't authenticate to Telegram at all should fail loudly at
startup, matching the old bracket-access (`os.environ["X"]`,
KeyError-on-missing) semantics these replaced.

### `test_api.*`

`test_api.py`'s local HTTP debug endpoint (`POST /test_message`) ships
in the image (Dockerfile's `COPY` list) and is enabled on both INT and
PROD today (`local-infra/infrastructure.yaml`'s `docker_run.command`) —
used by `tools/check_logfire.py` and manual live verification, not just
local dev. `enabled` fails closed (server never starts) by default, same
as the old `if not os.environ.get("ENABLE_TEST_API")`; `port` keeps its
old 8765 default.

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
