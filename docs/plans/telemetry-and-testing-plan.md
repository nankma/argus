# Telemetry & Testing Plan

Why this exists: we want CI-runnable tests for `agent.py` that hit no real LLM
and no real telemetry service — everything mocked, writing to memory or local
files only, with results compared against an expected baseline. That requires
the agent's model and its logging/telemetry to both be swappable at the point
they're constructed, rather than hardcoded.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Dependency injection (model + callbacks as parameters) | Done |
| 2 | Test infrastructure (folder, fixtures, fake LLM, fake logger) | Done |
| 3 | Telemetry service install + hook (real backend for normal runs) | Done — Arize Phoenix, via Docker. **2026-08-21: a second backend (Logfire) added alongside it** — `agent.setup_telemetry()` now wires Phoenix, Logfire, both or neither onto one shared `TracerProvider`; see `docs/plans/observability-platform-plan.md` for the platform decision and migration status (nothing deployed yet, Phoenix remains live in production). |
| 4 | CI setup (test automation) | Done — GitHub Actions; branch protection pending manual confirmation |
| 5 | Test cases (actual scenarios) | Done — 16 tests, see below for what's covered vs. not |
| 6 | LLM-judged end-to-end evaluation | **Built 2026-08-16** — `tools/run_eval.py`, 11/11 passing on first real run, see below |

## 1. Dependency injection

`build_agent(model)` builds the agent from an injected model; `run_agent(agent,
messages, callbacks=None)` threads an injected callbacks list through
`agent.invoke({"messages": ...}, config={"callbacks": callbacks})`. Neither is
hardcoded — production (`main()`) passes the real `ChatDeepSeek` and no
callbacks (until item 3 exists); tests pass fakes. `create_agent`/LangChain's
Runnable interface supports both injection points natively — no custom
provider abstraction needed (considered and rejected earlier as
over-engineering for a single-file agent).

## 2. Test infrastructure

Built under `tests/`:

- `tests/conftest.py` — `isolated_notes_file` fixture: monkeypatches
  `agent.NOTES_FILE` to a `tmp_path` file so `save_note` tests never touch the
  real `notes.jsonl`. (Confirmed necessary the hard way — an ad-hoc
  verification test before this fixture existed wrote a real "hello test"
  entry into the actual file; had to be cleaned up by hand.)
- `tests/fakes.py`:
  - `FakeToolCallingModel` — a **custom** fake, not a LangChain built-in.
    Both `GenericFakeChatModel` and `FakeMessagesListChatModel`
    (`langchain_core.language_models.fake_chat_models`) do **not** override
    `bind_tools()`, and `create_agent` calls
    `model.bind_tools(tools, tool_choice=...)` unconditionally whenever tools
    are present — confirmed live, this raises `NotImplementedError` with
    both built-ins. `FakeToolCallingModel` overrides `bind_tools()` to
    return `self` (ignoring the schema, since responses are pre-scripted)
    and cycles through a list of scripted `AIMessage` responses, stopping on
    the last one.
  - `RecordingCallbackHandler` — in-memory `BaseCallbackHandler` recording
    LLM/tool start/end events into `self.events`, standing in for a real
    telemetry backend in tests.
- `tests/fixtures.py` — mock response payloads (HN Algolia JSON, arXiv Atom
  XML, generic RSS XML, NewsAPI/GNews/Perigon JSON) shaped to match real
  responses captured during live source verification — except Perigon,
  which is **not** independently verified (no API key available; the
  fixture only matches what `fetch_perigon` is coded to expect).
- `pytest.ini` (`pythonpath = .`) — **required**, not optional boilerplate.
  Without it, pytest's default import mode doesn't add the project root to
  `sys.path`, so `tests/conftest.py`'s `import agent` fails with
  `ModuleNotFoundError` — hit this immediately on the first `pytest --fixtures`
  run.
- HTTP mocking: `requests_mock` (pytest fixture, auto-registered by the
  `requests-mock` package — confirmed via `pytest --collect-only` showing
  `plugins: ..., requests-mock-1.12.1`).
- **Bug found while wiring up mocking, fixed in `news_sources.py`:**
  `_fetch_rss` (used by 5 of the 7 free sources) called
  `feedparser.parse(url)` directly, letting feedparser do its own internal
  HTTP fetch via `urllib` — bypassing `requests` entirely. Two problems:
  no `timeout` at all (a slow/dead feed could hang the whole `search_news`
  call), and untestable with `requests_mock` (which only intercepts
  `requests`, not `urllib`). Fixed by routing through
  `requests.get(url, timeout=10)` first, then `feedparser.parse(resp.content)`
  — same pattern `fetch_arxiv` already used. Re-verified live against the
  real OpenAI/VentureBeat feeds after the change.

Test runner is pytest (was deferred earlier as "decide later" — just went
with it since nothing else was ever proposed).

## 3. Telemetry service (real backend, for normal/non-test runs)

**Done — Arize Phoenix, but not the way originally planned.** The original
idea was `pip install arize-phoenix` running in-process with no Docker
needed. That didn't survive contact with reality:

- **The full `arize-phoenix` package can't run in this Python environment
  at all.** It unconditionally imports `pandas` (for its bundled local
  UI/session code) at `import phoenix` time, and on this machine that DLL
  load is blocked by Windows 11 **Smart App Control** — confirmed as a real,
  widely-documented phenomenon (unsigned/no-reputation compiled extensions
  get blocked; see GitHub issues on mypy, ChimeraX, Julia hitting the exact
  same error text). Not fixable by switching install tools — conda, mamba,
  and pip all install the file fine; the block happens when the DLL tries to
  *load*, not at install time.
- **The fix: split client from server.** `arize-phoenix-otel` is a separate,
  much lighter PyPI/conda-forge package — confirmed via its PyPI dependency
  metadata to have **no pandas dependency at all** (just the OpenTelemetry/
  OpenInference stack). `agent.py` uses only this lightweight package to
  *send* traces. The actual Phoenix dashboard/collector runs as its own
  Docker container instead (`arizephoenix/phoenix:latest`), which sidesteps
  the Windows-native block entirely since it's an isolated Linux
  environment. This split turns out to be the right shape for the future
  Kubernetes deployment too, not just a workaround — see
  `docs/plans/deployment-plan.md`.
- Considered and set aside: **LangSmith** (cloud-only — no fully local mode,
  requires an external account even on the free tier); **Langfuse**
  self-hosted (more full-featured — evals, prompt management — but requires
  running a Docker Compose stack with Postgres + ClickHouse, heavier than
  this project needs).

**How it's wired up:** `agent.py`'s `setup_telemetry()` calls
`phoenix.otel.register(endpoint=PHOENIX_ENDPOINT, project_name="myfirstagent",
protocol="grpc", auto_instrument=True)`, gated behind the `PHOENIX_ENABLED`
env var — unset (the default, including in every test and CI run) means
it's a no-op. `auto_instrument=True` means LangChain calls are traced
automatically process-wide once registered; this is **not** the same
mechanism as `run_agent`'s `callbacks` parameter — Phoenix/OTel
instrumentation is global and set up once at startup, unlike the
per-invocation `callbacks` list. `run_agent`'s `callbacks` param remains
available for the local/in-memory case (`RecordingCallbackHandler` in
tests) but Phoenix doesn't go through it.

**Run it:**
```powershell
docker run -d --name phoenix -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest
$env:PHOENIX_ENABLED = "true"
python agent.py
```
Dashboard: `http://localhost:6006`.

**Verified end-to-end** (not just "the code runs without error") — queried
Phoenix's GraphQL API directly after a real run and confirmed 20 real spans
recorded under the `myfirstagent` project, with the expected structure:
`LangGraph` (chain) → `model` (chain) → `ChatDeepSeek`/`ChatCompletion`
(llm) → `search_news` (tool), called multiple times matching the agent's
actual multi-query behavior for that conversation.

**Tests:** `tests/test_telemetry.py` covers only the gating logic (register
not called when `PHOENIX_ENABLED` unset; called with expected args when
set) — deliberately not testing whether tracing "works," since that needs
the real Docker container and would break the test suite's zero-real-calls
guarantee. See item 6 below for how actual trace *content* gets verified.

### RESOLVED 2026-08-16 — restored and verified live

**Fixed the same day.** `docker run` redeployed with `PHOENIX_ENABLED=true`
and `PHOENIX_ENDPOINT=http://10.0.0.234:4317` restored. Verified for
real, not just "the container started without error" — `tools/
check_telemetry.py` (built the same day, see below) ran end-to-end and
confirmed **35 spans landed in Phoenix** within 90s of a real test
message through the live bot. `docker logs` also now shows the OTel
registration banner on startup, confirming the registered endpoint and
gRPC transport match what was intended.

Diagnosing this also surfaced that the Phoenix VM was never the
problem — see the section below, corrected in place: it turned out to
be a native systemd + venv install (not Docker, contrary to older docs),
confirmed healthy and reachable on port 4317 the whole time. The actual
defect really was just the two missing env vars on the bot's `docker
run`, exactly as suspected before the fix.

**A real, separate finding while verifying this**: Phoenix's own startup
banner shows `Span Processor: SimpleSpanProcessor`, with its own
explicit warning — *"strongly advised to use a BatchSpanProcessor in
production environments"*. `SimpleSpanProcessor` exports every span
synchronously the moment it ends, meaning every LLM call / tool call now
blocks on a network round-trip to the Phoenix collector before
continuing -- real latency added to every agent step, on a
`VM.Standard.E2.1.Micro` (1/8 OCPU) shape where that's not free. Not
fixed as part of this pass -- flagged here as a worthwhile follow-up
(switch to a `BatchSpanProcessor` via `register()`'s options) rather than
guessed at and changed without measuring the actual latency impact
first, same discipline as everything else in this doc.

**Original finding, retained below for the investigation trail:**

### Currently NOT connected on the live deployment — found 2026-08-16 (historical)

**The deployed `myfirstagent-bot` container is missing `PHOENIX_ENABLED`
and `PHOENIX_ENDPOINT` entirely.** `docker inspect`ing the running
container's env shows only `PHOENIX_API_KEY_SECRET_OCID` — fetching the
API key secret happens in `docker-entrypoint.sh` regardless, but that
alone doesn't turn tracing on; `setup_telemetry()` checks `PHOENIX_ENABLED`
specifically. This is a regression from the state `docs/plans/deployment-plan.md`
records as "verified end-to-end" (`PHOENIX_ENABLED=true`,
`PHOENIX_ENDPOINT=http://10.0.0.234:4317`, a real trace confirmed in
Phoenix's UI) — somewhere between that verification and the current
deployment, a `docker run` stopped including those two flags and nobody
re-added them on a later redeploy. **Also found**: the Phoenix collector
VM itself (private IP `10.0.0.234`) didn't respond to a `curl` from the
bot VM at all (connection timeout) — separate from the missing env vars,
worth checking whether that VM is still running before just restoring the
flags. **Not fixed as part of this session's work** — flagged here so it
isn't lost; restoring it means (a) confirming/reviving the Phoenix VM, (b)
re-adding both env vars to the bot's `docker run` command in
`docs/plans/deployment-plan.md` and using them on the next actual redeploy.

**Practical consequence while this stayed broken**: zero traces reached
Phoenix — not just the source-fetch spans below, but every LLM call,
router classification, and guardrail check too. **Resolved above** — see
the "RESOLVED 2026-08-16" section at the top of this finding.

### Why didn't anything alert on this? — the precise answer, found 2026-08-16

There already **is** an alerting mechanism for exactly this —
`telemetry_monitor.py`, wired into `combined_bot.py` — and it's not
broken. It never started. `combined_bot.py`'s `_start_telemetry_monitor`:

```python
if not os.environ.get("PHOENIX_ENABLED"):
    return None
```

The monitor that checks "is Phoenix's OTLP port reachable" and pages the
admin (via `admin_bot.py`'s token) on a state change is itself gated
behind the exact same env var that went missing. So the one
misconfiguration — `PHOENIX_ENABLED` absent from the deployed
container — simultaneously (a) disabled tracing and (b) prevented the
monitor built to detect (a) from ever spinning up. Not a bug in
`telemetry_monitor.py`'s logic (edge-triggered TCP reachability checks,
alert on up→down and down→up, correctly designed) — the monitor was
never given the chance to run at all. This is the single-point-of-failure
shape worth naming for next time: a self-monitoring mechanism that's
gated behind the same flag as the thing it monitors can't be relied on to
catch that flag going missing.

**Why the gating exists at all, and why it's not simply wrong**: running
a Phoenix-reachability monitor when telemetry is deliberately off (local
dev, `PHOENIX_ENABLED` intentionally unset) would be pointless noise
against `PHOENIX_ENDPOINT`'s `localhost` default. The gate is a
reasonable design choice for "don't monitor a thing that's supposed to be
off" — the actual defect is purely operational (a `docker run` silently
dropped two flags at some point), not a code bug to fix in
`telemetry_monitor.py` itself.

**What actually closes this gap, added the same day, two complementary
pieces — deliberately not one**, since each catches a different half of
"how would we have known":

1. **`tools/check_telemetry.py`** (see below the smoke-test checklist
   entry it's tied to) — an EXTERNAL, post-deploy check that doesn't run
   inside the bot process at all, so it can't be silently disabled by the
   bot's own missing env var the way `telemetry_monitor.py` was. This is
   the layer that would have caught the exact regression described
   above, on the very next deploy, regardless of what `docker run` did
   or didn't include.
2. **`healthcheck.py`** — a NEW, separate liveness check for whether
   `news_ingest.py`/`news_push.py`'s periodic jobs are still ticking at
   all, alerting the admin (reusing the same `admin_bot.py` channel
   `telemetry_monitor.py` already uses) on a change in problem state.
   Deliberately not gated behind `PHOENIX_ENABLED` or anything else
   optional — it always runs once the bot starts, so it can't fail the
   same way. Answers a different question than either `telemetry_monitor.py`
   (is Phoenix reachable) or `check_telemetry.py` (are traces actually
   landing): are the periodic jobs themselves still alive, independent of
   whether Phoenix is even in the picture. See its own module docstring
   for the full design (why it's deliberately not a full incident-
   management system — no severity levels, no escalation, just reusing
   the one paging channel this project already has for a one-admin
   deployment).

### Raw source fetches — a gap auto-instrumentation can't close, added 2026-08-16

`auto_instrument=True` only wires up `openinference-instrumentation-
langchain` (the only OpenInference instrumentor in `environment.yml`) —
it traces LangChain-mediated calls (LLM invocations, `@tool`-decorated
tool calls made through `create_agent`'s loop) automatically, but has no
visibility at all into a plain `requests.get()` called from outside that
framework. `news_ingest.py`'s scheduled per-source pulls and `agent.py`'s
`search_news` both call `news_sources.py`'s fetch functions directly —
neither goes through anything LangChain instruments, so even with Phoenix
fully connected, these calls would never appear as spans.

Prompted by a real gap: diagnosing how many calls had been made against
NewsAPI/Perigon (the two budget-constrained restricted sources, see
`docs/current/ai-news-sources.md`) turned up that `search_news`'s on-demand usage
of them was invisible everywhere — not logged, not counted against any
budget, and (per the section above) not even reaching Phoenix since it
was disconnected.

**Fixed with two independent, complementary mechanisms** — deliberately
not just one, since Phoenix availability and the local DB are different
failure domains:

1. **`news_sources.traced_fetch(source_key, fetch, query, max_results)`**
   wraps every source fetch call (from both `news_ingest.py` and
   `search_news`) in a manual OpenTelemetry span (`trace.get_tracer(...)`),
   tagged with `source_key`, `query`, `restricted`, `article_count`, and
   `error` on failure. This is real OpenTelemetry API usage, not
   LangChain-specific — it works the moment Phoenix is connected again,
   with no further code change, and is a safe no-op right now (and in
   tests) since `get_tracer()` returns a no-op tracer when no provider is
   registered.
2. **`users_db.api_budget`** (used independently of whether Phoenix is
   up): migrated from one row per source (today's count only, overwritten
   on every date rollover — no history at all) to one row per
   `(source, date)`, so `get_api_budget_history`/`get_total_api_calls`
   give a real, persistent, queryable count regardless of tracing
   infrastructure. `search_news` now calls the new non-enforcing
   `users_db.record_api_call` for any restricted source it actually hits
   — recorded in the same table `news_ingest.py`'s budget-enforced
   `try_consume_api_budget` writes to, so a query against either source
   reflects combined usage from both call paths, not just the scheduled
   ingestion job's.

**Why both, not just Phoenix once it's reconnected**: the DB-backed count
is what `try_consume_api_budget` already needs for cap enforcement
regardless of tracing state — extending it to also serve as a queryable
log was cheap and doesn't depend on the Phoenix VM being reachable, which
it currently isn't. Phoenix (once reconnected) adds richer per-call
detail (timing, the actual query string, error text) that a plain counter
can't — the two are complementary, not redundant.

## 4. CI (test automation)

**Done — GitHub Actions.** `.github/workflows/ci.yml` runs on every push to
`main` and every PR against it: `mamba-org/setup-micromamba@v3` builds the
environment straight from `environment.yml` (no separate pip lockfile —
avoids the drift risk of maintaining two dependency files), then `pytest`.
No secrets needed — same zero-real-calls guarantee as running locally.

**Not yet confirmed:**
- Whether the workflow run actually went green on GitHub (asked to check,
  no confirmation received yet).
- Branch protection on `main` — walked through the exact settings to apply
  via GitHub's web UI (require PR, require the `test` status check, no
  admin bypass, no approval requirement, force-push/deletion disabled), but
  applying it is a manual step on GitHub's side, not something committable
  to this repo. Not confirmed done.

This is CI (test automation) only — not CD. See `docs/plans/deployment-plan.md`
for what actual deployment automation needs first (it needs the whole
deployment chain resolved, not just this).

## 5. Test cases

16 tests, all passing, all real network/LLM calls mocked out:

**`tests/test_agent.py`** (the agentic loop, via `FakeToolCallingModel`):
- `save_note` writes the expected JSON line to an isolated temp file and
  returns the expected confirmation — real `notes.jsonl` untouched.
- `search_news` aggregation with one working + one failing mocked source:
  the working source's result and an `ERROR: ...` line for the failing one
  both appear in the tool output, and the agent still reaches a final
  answer — confirms per-source error isolation actually works end-to-end,
  not just in isolated unit logic.
- A turn with no tool calls at all — direct answer, no `ToolMessage` in the
  result.
- `run_agent`'s `callbacks` param actually reaches the model —
  `RecordingCallbackHandler` sees `llm_start`/`llm_end` events.

**`tests/test_news_sources.py`** (individual fetchers, via `requests_mock`):
- `fetch_hackernews` — parses hits, including the `url: None` →
  `news.ycombinator.com/item?id=...` fallback link.
- `fetch_arxiv` — parses Atom entries, strips embedded newlines from title.
- `_fetch_rss` (generic — covers the shared code path behind 5 of the 7 free
  sources) — parsing and `max_results` truncation.
- `fetch_openai_blog` — confirms the wrapper hits the right URL with the
  right source name (representative spot-check; `huggingface_blog`/
  `techcrunch_ai`/`venturebeat_ai`/`mit_tech_review` share the same
  `_fetch_rss` code path and aren't each re-tested individually).
- `fetch_newsapi`, `fetch_gnews`, `fetch_perigon` — parsing plus the
  `os.environ[...]` key lookup (via `monkeypatch.setenv`).
- `enabled_sources()` — all 7 free sources always present; gated sources
  absent/present based on whether their env var is set.

**`tests/test_telemetry.py`** — the `PHOENIX_ENABLED` gating logic (see item
3), plus, added 2026-08-21: `LOGFIRE_ENABLED`/`LOGFIRE_API_KEY` gating (a
key present without the flag must not export — load-bearing, since
`LOGFIRE_API_KEY` sits in the dev shell env), the raise-when-enabled-
without-a-key contract, one shared `TracerProvider` when both backends are
on, and the token-prefix → region derivation (`logfire_traces_endpoint`).

**This "Test cases" section and its counts are stale project-wide** (the
suite is now 486 tests, not 16 — `qa-engineer` tracks the current total,
this doc doesn't attempt to stay in sync test-by-test). Two additions
specifically from the 2026-08-21 push-outcomes/telemetry work, since
those are new *categories* of behaviour rather than more of an existing
one:

- **`tests/test_news_push.py`/`tests/test_users_db.py`** — `push_outcomes`
  recording (`news_push._record`, `users_db.record_push_outcome` and its
  queries), the cost-bug fix (failure paths advancing `last_push_at` only
  once generation has happened, three-strikes-and-disable for an
  unreachable chat), and the push-tick heartbeat span. See
  `docs/plans/incident-monitoring-plan.md`'s "Status: step 1 built" for
  the design this covers.
- **`tests/test_news_push.py::test_emit_heartbeat_is_a_noop_without_a_tracer_provider`**
  and the Logfire gating tests above are this project's hermeticity check
  for telemetry specifically: nothing in `tests/` or `tests/conftest.py`
  calls `setup_telemetry()` implicitly or at import time, so a real
  `LOGFIRE_API_KEY` sitting in the dev environment cannot cause a real
  export during any test or CI run.

**Not covered yet** (candidates for later):
- ~~`agent._logfire_processor`'s actual body, and the Logfire-only wiring
  branch of `setup_telemetry`~~ — flagged 2026-08-21 by `qa-engineer`,
  closed the same day: `test_logfire_processor_targets_the_regional_endpoint_with_the_token`
  exercises the real `OTLPSpanExporter`/`BatchSpanProcessor` construction
  (mocking only those two classes, not the whole function), and
  `test_setup_telemetry_without_phoenix_builds_and_installs_its_own_provider`
  exercises the `provider is None` branch and asserts the provider is
  actually installed as the process-global one, not just constructed.
- No test compares captured events/output against a checked-in baseline file
  (snapshot testing) — current tests assert specific expected values inline
  instead. Revisit if that stops scaling.
- No multi-turn conversation test (two+ user turns in one `messages` list).
- No test of the real `ChatDeepSeek` + real API path (intentionally — that's
  what the fakes exist to avoid; covered instead by the live manual testing
  done earlier in this project's history, see `CLAUDE.md`).

## 6. LLM-judged end-to-end evaluation

**Built 2026-08-16** — `tools/run_eval.py`. A fundamentally different kind
of test than items 2/5 above, so it got its own item rather than folding
into "more test cases."

**The problem it solves:** items 2/5 test the agent's *mechanics* (does the
tool-calling loop work, does error isolation work) using a fake LLM with
scripted responses — deliberately not testing whether the *real* DeepSeek
model's actual output is any good, since that's non-deterministic and can't
be asserted against with `==`. There's currently no automated check that the
real end-to-end system (real LLM, real tools, real sources) produces
reasonable output — only the manual spot-checks done throughout this
project's history (see `CLAUDE.md`).

**What got built.** `tools/run_eval.py` runs 11 representative questions
(4 `news_query`, 6 settings categories, 1 multi-intent) through the REAL
pipeline (`bot.process_message`, real `ChatDeepSeek` models built via
`agent.build_model` — automatically evaluating whatever
`LLM_MODEL`/`LLM_MODEL_CLASSIFIER` are currently configured to, per
`docs/plans/model-portability-plan.md`), against an isolated
`SUBSCRIBERS_DB_FILE` so it never touches production subscriber data.
Judge is DeepSeek itself, reusing the classifier-tier model (no second
provider key available — same call as `docs/plans/model-portability-plan.md`'s
Level 2 plumbing), via structured output with a `reasoning`-before-
`meets_criteria` field order (same fix that raised
`guardrails.OutputCheck`'s reliability). The rubric is derived from
`agent.LAYER1_IDENTITY`/`TREND_REPORT_STRUCTURE`'s actual stated rules —
three separate rubrics (news_query / settings / multi), not one generic
"is this good" prompt.

**First real run, 2026-08-16: 11/11 passed** (4/4 news_query, 6/6
settings, 1/1 multi-intent) — a genuine first confirmation that the real
end-to-end pipeline (real DeepSeek, real news sources, real router,
real Route A/B dispatch) produces reasonable output across every category
this project supports, not just the mechanics tests items 2/5 already
covered with fakes.

**Deterministic sub-checks built** (cheaper and more reliable than the
judge where they apply), reusing `bot.py`'s own existing safety-net
helpers rather than reimplementing them:
- **Source links present** — every 🔗 line in a trend report must carry a
  real `<a href="...">`. Built without needing a `TREND_REPORT_STRUCTURE`
  change after all: it already requires "only include sources actually
  provided... never invent a URL", which is a meaningful check as-is.
- **Output format compliance** — defined concretely as: HTML tags balanced
  (reuses `bot._is_html_balanced`), no stray Markdown bold leaked through
  (reuses `bot._MARKDOWN_BOLD_RE`), and `news_query` replies start with
  the 📰 report marker (reuses `bot._TREND_REPORT_MARKER`) — the same three
  safety nets `bot.py` already applies to real traffic, now also checked
  against real model output in this harness.

**Deliberately still not built: writing pattern/style consistency.** No
rubric exists for what "style" means here — that's a product decision,
not something to invent while building the harness. Stays an open item.
