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
| 3 | Telemetry service install + hook (real backend for normal runs) | Not started |
| 4 | CI/CD setup | Not started |
| 5 | Test cases (actual scenarios) | Done — 14 tests, see below for what's covered vs. not |

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

Still deferred — test infrastructure (item 2) is now solid, this is the
natural next thing, but not started.

- **Recommendation from prior discussion: Arize Phoenix.** `pip install
  arize-phoenix`, local dashboard via `px.launch_app()`, OpenTelemetry-based
  auto-instrumentation (`openinference-instrumentation-langchain` +
  `phoenix.otel.register()`). No Docker, no external account.
- Considered and set aside: **LangSmith** (cloud-only — no fully local mode,
  requires an external account even on the free tier); **Langfuse**
  self-hosted (more full-featured — evals, prompt management — but requires
  running a Docker Compose stack with Postgres + ClickHouse, heavier than
  this project needs).
- When implemented, telemetry setup should be gated behind a config flag
  (e.g. `TELEMETRY_ENABLED`) so it's opt-in and never activates during tests
  or CI. `run_agent`'s `callbacks` param is already the injection point for
  this — `main()` would construct the real Phoenix/OTel callback handler (or
  none) and pass it through, same shape tests already exercise with
  `RecordingCallbackHandler`.

## 4. CI/CD

Still not started — same open questions as before:

- Platform not yet confirmed. GitHub Actions is the natural fit (repo is
  already on `github.com/nankma/myFirstAgent`), proposed but not agreed.
- Workflow would run `pytest` — no `DEEPSEEK_API_KEY` or real telemetry
  credentials needed, confirmed by the test suite actually running in 1.46s
  with zero real network/LLM calls.
- Dependencies are managed via `environment.yml` (conda-forge) — CI would
  need to either install via conda (`conda env create -f environment.yml`,
  slower) or maintain a compatible pip lockfile; not yet decided which. Given
  the permission issues hit locally with this conda install (`EnvironmentNotWritableError`,
  needed an elevated shell twice this session), conda-in-CI is untested
  territory and might have its own surprises.

## 5. Test cases

14 tests, all passing, all real network/LLM calls mocked out:

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

**Not covered yet** (candidates for later):
- No test compares captured events/output against a checked-in baseline file
  (snapshot testing) — current tests assert specific expected values inline
  instead. Revisit if that stops scaling.
- No multi-turn conversation test (two+ user turns in one `messages` list).
- No test of the real `ChatDeepSeek` + real API path (intentionally — that's
  what the fakes exist to avoid; covered instead by the live manual testing
  done earlier in this project's history, see `CLAUDE.md`).
