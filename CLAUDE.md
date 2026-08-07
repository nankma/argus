# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

An AI-industry news-trend agent built on **LangChain**, using DeepSeek as the LLM (`langchain-deepseek`'s `ChatDeepSeek`). It calls `search_news` to pull recent items from multiple AI-focused sources (Hacker News, arXiv, company blogs, tech press — see `docs/ai-news-sources.md`), then writes a short trend-report article from what it finds, citing sources. It also has `save_note` for the model to persist arbitrary short notes.

This was originally a raw `openai`-SDK script (DeepSeek is OpenAI Chat Completions–compatible) with a hand-written tool-calling loop. It was migrated to LangChain's `create_agent` to get a framework-managed agent loop, and — per `docs/telemetry-and-testing-plan.md` — to make the model and telemetry swappable for CI testing later (fake LLM + fake logger, no real API calls in tests). See that doc for what's built vs. still planned.

## Setup & Run

Uses a dedicated Miniforge/conda environment named `myfirstagent` (not the system Python or `base`). Dependencies are conda-forge packages, declared in `environment.yml` — there is no `requirements.txt`/pip path for this project.

```powershell
conda env create -f environment.yml   # first time only, creates myfirstagent
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
python agent.py
```

If `conda` isn't recognized in a given shell (not initialized for that shell), call it via its full path instead, e.g. `& "C:\ProgramData\miniforge3\condabin\conda.bat" activate myfirstagent`, or invoke the env's interpreter directly: `& "C:\ProgramData\miniforge3\envs\myfirstagent\python.exe" agent.py`. To add a dependency later, **use `mamba`, not `conda`** — conda's classic solver has repeatedly taken 10+ minutes or hung outright on this project's dependency tree; see the `use-mamba-not-conda` skill: `mamba install -n myfirstagent -c conda-forge <package>` (full path: `& "C:\ProgramData\miniforge3\condabin\mamba.bat" install ...`) — then add it to `environment.yml` to keep the env reproducible.

There is no lint config or build step. There is a test suite (`tests/`, pytest) — see the **Testing** section below.

## Architecture

Two files: `agent.py` (agent/tools/CLI) and `news_sources.py` (the pluggable source registry `search_news` draws on — see below).

`agent.py`:

- `save_note` / `search_news` — LangChain tools via the `@tool` decorator (`langchain_core.tools`), not hand-written JSON schemas. `TOOLS` is just `[save_note, search_news]`.
- `build_agent(model)` — constructs the agent via `langchain.agents.create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)`. **Takes `model` as a parameter rather than importing/constructing a hardcoded `ChatDeepSeek` internally** — this is the dependency-injection point that lets tests substitute a fake chat model instead of hitting the real DeepSeek API. Use `tests.fakes.FakeToolCallingModel`, **not** LangChain's built-in `GenericFakeChatModel`/`FakeMessagesListChatModel` — neither overrides `bind_tools()`, and `create_agent` calls it unconditionally whenever tools are present, so both raise `NotImplementedError` (confirmed live). See `docs/telemetry-and-testing-plan.md`.
- `run_agent(agent, messages, callbacks=None)` — thin wrapper around `agent.invoke({"messages": messages}, config={"callbacks": callbacks})`. The full tool-calling loop (deciding to call a tool, executing it, feeding the result back, looping until a final answer) is handled internally by `create_agent` — there's no manual `while` loop here anymore, unlike the pre-LangChain version. `callbacks` is the second DI point: tests pass an in-memory/local-file callback handler instead of a real telemetry backend.
- `main()` — CLI REPL. Calls `setup_telemetry()`, constructs the real `ChatDeepSeek(model=MODEL)`, builds the agent once, then loops calling `run_agent`. `result["messages"]` from `agent.invoke()` are LangChain message objects (`.content`, not dict-style `["content"]`) — mixing them with plain `{"role": ..., "content": ...}` dicts in the same running `messages` list across turns works fine; LangChain normalizes both.
- `setup_telemetry()` — see **Telemetry** section below.

Notes saved via `save_note` are appended as JSONL to `notes.jsonl` (created at runtime, not checked in).

`search_news` no longer hits a single generic news API — as of this project's pivot to AI-industry focus, it aggregates across a **pluggable source registry** in `news_sources.py`. Each source is a `fetch(query, max_results) -> list[dict]` function normalized to `{"title", "link", "source", "summary", "published"}`; `SOURCE_REGISTRY` pairs each with an optional required env var, and `enabled_sources()` skips any source whose key isn't set — so `search_news` degrades gracefully instead of erroring when e.g. `NEWSAPI_API_KEY` is absent. Per-source fetch errors are caught individually in the tool so one broken source doesn't take down the whole call. See `docs/ai-news-sources.md` for the full source list (what's live, what needs a key, what was considered and rejected — e.g. Reddit's unauthenticated JSON access is now blocked) and how to add a new one.

The old OK Surf News API integration (general Google News sections, no AI filtering) was removed in this pivot — if you see references to `NEWS_API_BASE` or `by_section` in old context, that's stale; it's gone from the codebase.

`MODEL` is set to `"deepseek-chat"` — confirmed live and working via direct API test (a search result claimed it was retired 2026-07-24; that was wrong or premature). Don't switch to `"deepseek-reasoner"` without changing the tools setup: per LangChain's own docs, DeepSeek-R1 (`deepseek-reasoner`) does not support tool calling, which this agent depends on for both `save_note` and `search_news`.

## Telemetry

Tracing via [Arize Phoenix](https://arize.com/docs/phoenix), gated behind the `PHOENIX_ENABLED` env var (unset = no-op, which is the case for every test/CI run). **Important:** `agent.py` depends on the lightweight `arize-phoenix-otel` package, not the full `arize-phoenix` package — the full package unconditionally imports `pandas` at `import phoenix` time, and on Windows with Smart App Control active, loading pandas's compiled DLL gets blocked outright (`ImportError: DLL load failed ... An Application Control policy has blocked this file` — this is a real, widely-documented Smart App Control behavior, not specific to this machine). `arize-phoenix-otel` has no pandas dependency at all and covers everything `agent.py` needs (`from phoenix.otel import register`). Do not add `arize-phoenix` back as a dependency for this reason.

Because the client is split from the server, the Phoenix dashboard/collector runs as its own Docker container rather than in-process:

```powershell
docker run -d --name phoenix -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest
$env:PHOENIX_ENABLED = "true"
python agent.py
```

Dashboard: `http://localhost:6006`. `setup_telemetry()` calls `register(endpoint=PHOENIX_ENDPOINT, project_name="myfirstagent", protocol="grpc", auto_instrument=True)` — `auto_instrument=True` traces LangChain calls process-wide once registered; this is a separate mechanism from `run_agent`'s `callbacks` parameter, not routed through it.

See `docs/telemetry-and-testing-plan.md` item 3 for the full story (why the original plan changed) and item 6 for the planned LLM-judged end-to-end evaluation (not built yet).

## Testing

```powershell
conda activate myfirstagent
pytest
```

No `DEEPSEEK_API_KEY` or network access needed — the whole suite runs in ~1.5s against fakes/mocks only. `pytest.ini` (`pythonpath = .`) is required for `tests/` to be able to `import agent`/`news_sources` at all; without it pytest's default import mode doesn't add the project root to `sys.path`.

- `tests/fakes.py` — `FakeToolCallingModel` (see the `build_agent` note above for why it's custom, not a LangChain built-in) and `RecordingCallbackHandler` (in-memory stand-in for a real telemetry backend).
- `tests/fixtures.py` — mock response payloads for `news_sources.py`'s fetchers, matching real responses captured during live verification (Perigon excepted — unverified, no API key available).
- `tests/conftest.py` — `isolated_notes_file` fixture, monkeypatches `agent.NOTES_FILE` so `save_note` tests never touch the real `notes.jsonl`.
- `tests/test_telemetry.py` — only the `PHOENIX_ENABLED` gating logic (register not called when unset, called with expected args when set). Does not test actual tracing — that needs the real Docker container and is verified manually instead (see **Telemetry** above).
- HTTP mocking via the `requests_mock` pytest fixture (auto-registered by the `requests-mock` package).

See `docs/telemetry-and-testing-plan.md` for what's covered, what's explicitly not, and what's still planned (CI/CD, LLM-judged end-to-end evaluation). See `docs/deployment-plan.md` for the plan to containerize `agent.py` itself and deploy to Kubernetes/cloud (separate from, but related to, the Docker usage here).
