# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A minimal single-file agent built on the DeepSeek API via the `openai` SDK (DeepSeek is OpenAI Chat Completions–compatible; it is not Anthropic Messages API–compatible, so this is not a raw base-URL swap of an Anthropic client). It's a news-trend agent: it calls `search_news` to pull recent headlines, then writes a short trend-report article from what it finds, citing sources. It also has `save_note` for the model to persist arbitrary short notes.

There used to be an Anthropic-only `web_search` server tool here (Claude executes it itself with no local code) — it was dropped in the DeepSeek migration since DeepSeek has no equivalent built-in server tool, and replaced by `search_news`, a client-side tool.

## Setup & Run

Uses a dedicated Miniforge/conda environment named `myfirstagent` (not the system Python or `base`). Dependencies are conda-forge packages, declared in `environment.yml` — there is no `requirements.txt`/pip path for this project.

```powershell
conda env create -f environment.yml   # first time only, creates myfirstagent
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
python agent.py
```

If `conda` isn't recognized in a given shell (not initialized for that shell), call it via its full path instead, e.g. `& "C:\ProgramData\miniforge3\condabin\conda.bat" activate myfirstagent`, or invoke the env's interpreter directly: `& "C:\ProgramData\miniforge3\envs\myfirstagent\python.exe" agent.py`. To add a dependency later: `conda install -n myfirstagent -c conda-forge <package>` and add it to `environment.yml` to keep the env reproducible.

There is no test suite, lint config, or build step in this repository — it's a single script (`agent.py`).

## Architecture

Everything lives in `agent.py`:

- `TOOLS` — OpenAI function-calling format (`{"type": "function", "function": {...}}`): `save_note` and `search_news`.
- `execute_tool(name, tool_input)` — dispatch point for tool execution. Add new tools here (new `if name == "..."` branch) and declare their schema in `TOOLS`.
- `run_agent(messages)` — the agentic loop: calls `client.chat.completions.create()`, appends the assistant message (`message.model_dump(exclude_none=True)`, not just its text — this preserves any `tool_calls` on the message so the API can correlate them with the tool results sent back), and if `message.tool_calls` is present, executes each call and appends one `role: "tool"` message per call (with matching `tool_call_id`), looping until the model returns a message with no tool calls.
- `main()` — simple CLI REPL that accumulates a running `messages` list across turns and calls `run_agent` each turn.

Notes saved via `save_note` are appended as JSONL to `notes.jsonl` (created at runtime, not checked in).

`search_news` calls the [OK Surf News API](https://ok.surf/docs/api) (`NEWS_API_BASE`, `https://ok.surf/api/v1`) — no API key required. `GET /news-feed` returns all sections; `POST /news-section` with `{"sections": [...]}` scopes to specific Google News section names (`US`, `World`, `Business`, `Technology`, `Entertainment`, `Sports`, `Science`, `Health`). **Despite what the official docs say ("serialized JSON array of objects"), both endpoints actually return an object keyed by section name** (`{"Technology": [article, ...], "Business": [...], ...}`), not a flat array — confirmed by direct testing. `execute_tool` iterates `by_section.items()` accordingly; don't revert to indexing/slicing the response as a list. Each article only has `title`, `link`, `source`, `og` (image), and `source_icon` — no summary/body text or published date, so trend-spotting works off headlines and outlet names only.

`MODEL` is set to `"deepseek-chat"`; `"deepseek-reasoner"` is DeepSeek's reasoning-focused model for harder tasks.
