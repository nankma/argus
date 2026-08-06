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

If `conda` isn't recognized in a given shell (not initialized for that shell), call it via its full path instead, e.g. `& "C:\ProgramData\miniforge3\condabin\conda.bat" activate myfirstagent`, or invoke the env's interpreter directly: `& "C:\ProgramData\miniforge3\envs\myfirstagent\python.exe" agent.py`. To add a dependency later: `conda install -n myfirstagent -c conda-forge <package>` and add it to `environment.yml` to keep the env reproducible.

There is no lint config or build step. There is a test suite (`tests/`, pytest) — see the **Testing** section below.

## Architecture

Two files: `agent.py` (agent/tools/CLI) and `news_sources.py` (the pluggable source registry `search_news` draws on — see below).

`agent.py`:

- `save_note` / `search_news` — LangChain tools via the `@tool` decorator (`langchain_core.tools`), not hand-written JSON schemas. `TOOLS` is just `[save_note, search_news]`.
- `build_agent(model)` — constructs the agent via `langchain.agents.create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)`. **Takes `model` as a parameter rather than importing/constructing a hardcoded `ChatDeepSeek` internally** — this is the dependency-injection point that lets tests substitute a fake chat model instead of hitting the real DeepSeek API. Use `tests.fakes.FakeToolCallingModel`, **not** LangChain's built-in `GenericFakeChatModel`/`FakeMessagesListChatModel` — neither overrides `bind_tools()`, and `create_agent` calls it unconditionally whenever tools are present, so both raise `NotImplementedError` (confirmed live). See `docs/telemetry-and-testing-plan.md`.
- `run_agent(agent, messages, callbacks=None)` — thin wrapper around `agent.invoke({"messages": messages}, config={"callbacks": callbacks})`. The full tool-calling loop (deciding to call a tool, executing it, feeding the result back, looping until a final answer) is handled internally by `create_agent` — there's no manual `while` loop here anymore, unlike the pre-LangChain version. `callbacks` is the second DI point: tests pass an in-memory/local-file callback handler instead of a real telemetry backend.
- `main()` — CLI REPL. Constructs the real `ChatDeepSeek(model=MODEL)`, builds the agent once, then loops calling `run_agent`. `result["messages"]` from `agent.invoke()` are LangChain message objects (`.content`, not dict-style `["content"]`) — mixing them with plain `{"role": ..., "content": ...}` dicts in the same running `messages` list across turns works fine; LangChain normalizes both.

Notes saved via `save_note` are appended as JSONL to `notes.jsonl` (created at runtime, not checked in).

`search_news` no longer hits a single generic news API — as of this project's pivot to AI-industry focus, it aggregates across a **pluggable source registry** in `news_sources.py`. Each source is a `fetch(query, max_results) -> list[dict]` function normalized to `{"title", "link", "source", "summary", "published"}`; `SOURCE_REGISTRY` pairs each with an optional required env var, and `enabled_sources()` skips any source whose key isn't set — so `search_news` degrades gracefully instead of erroring when e.g. `NEWSAPI_API_KEY` is absent. Per-source fetch errors are caught individually in the tool so one broken source doesn't take down the whole call. See `docs/ai-news-sources.md` for the full source list (what's live, what needs a key, what was considered and rejected — e.g. Reddit's unauthenticated JSON access is now blocked) and how to add a new one.

The old OK Surf News API integration (general Google News sections, no AI filtering) was removed in this pivot — if you see references to `NEWS_API_BASE` or `by_section` in old context, that's stale; it's gone from the codebase.

`MODEL` is set to `"deepseek-chat"` — confirmed live and working via direct API test (a search result claimed it was retired 2026-07-24; that was wrong or premature). Don't switch to `"deepseek-reasoner"` without changing the tools setup: per LangChain's own docs, DeepSeek-R1 (`deepseek-reasoner`) does not support tool calling, which this agent depends on for both `save_note` and `search_news`.

## Testing

```powershell
conda activate myfirstagent
pytest
```

No `DEEPSEEK_API_KEY` or network access needed — the whole suite runs in ~1.5s against fakes/mocks only. `pytest.ini` (`pythonpath = .`) is required for `tests/` to be able to `import agent`/`news_sources` at all; without it pytest's default import mode doesn't add the project root to `sys.path`.

- `tests/fakes.py` — `FakeToolCallingModel` (see the `build_agent` note above for why it's custom, not a LangChain built-in) and `RecordingCallbackHandler` (in-memory stand-in for a real telemetry backend).
- `tests/fixtures.py` — mock response payloads for `news_sources.py`'s fetchers, matching real responses captured during live verification (Perigon excepted — unverified, no API key available).
- `tests/conftest.py` — `isolated_notes_file` fixture, monkeypatches `agent.NOTES_FILE` so `save_note` tests never touch the real `notes.jsonl`.
- HTTP mocking via the `requests_mock` pytest fixture (auto-registered by the `requests-mock` package).

See `docs/telemetry-and-testing-plan.md` for what's covered, what's explicitly not, and what's still planned (a real telemetry backend for normal runs, CI/CD).
