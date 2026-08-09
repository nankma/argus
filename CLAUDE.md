# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A technology-industry news-trend agent built on **LangChain**, using DeepSeek as the LLM (`langchain-deepseek`'s `ChatDeepSeek`). It calls `search_news` to pull recent items from multiple tech-focused sources (Hacker News, arXiv, company blogs, tech press — see `docs/ai-news-sources.md`), then writes a short trend-report article from what it finds, citing sources. It also has `save_note` for the model to persist arbitrary short notes. Scope was AI-industry-only originally; broadened to technology industry generally (AI included, not AI-only) alongside per-user `interests` (see **Telegram Bot** below and `docs/bot-features-plan.md` item 3) — different subscribers can care about different tech topics, so the shared scope had to stop being hardcoded to one person's interest.

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

Core files: `agent.py` (agent/tools/CLI), `news_sources.py` (the pluggable source registry `search_news` draws on — see below), `news_push.py` (periodic-digest generation for proactive push — see **Telegram Bot** section below), `bot.py` (Telegram entry point — see **Telegram Bot** section below), `admin_bot.py` (the access-control companion bot), `combined_bot.py` (runs both bots in one process — the default for deployment, see **Running both bots in one process** below), and `users_db.py` (the SQLite store both bots share for approval state, interests, and push preferences).

`agent.py`:

- `save_note` / `search_news` / `update_interests` / `set_push_enabled` / `set_push_interval` / `set_language` — LangChain tools via the `@tool` decorator (`langchain_core.tools`), not hand-written JSON schemas. `TOOLS` is `[save_note, search_news, update_interests, set_push_enabled, set_push_interval, set_language]`. The last four read/write `users_db.py`, keyed off `chat_id` injected via `ToolRuntime.context` (see `run_agent`'s `context` param below), not supplied by the model.
- `build_agent(model)` — constructs the agent via `langchain.agents.create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)`. **Takes `model` as a parameter rather than importing/constructing a hardcoded `ChatDeepSeek` internally** — this is the dependency-injection point that lets tests substitute a fake chat model instead of hitting the real DeepSeek API. Use `tests.fakes.FakeToolCallingModel`, **not** LangChain's built-in `GenericFakeChatModel`/`FakeMessagesListChatModel` — neither overrides `bind_tools()`, and `create_agent` calls it unconditionally whenever tools are present, so both raise `NotImplementedError` (confirmed live). See `docs/telemetry-and-testing-plan.md`.
- `run_agent(agent, messages, callbacks=None)` — thin wrapper around `agent.invoke({"messages": messages}, config={"callbacks": callbacks})`. The full tool-calling loop (deciding to call a tool, executing it, feeding the result back, looping until a final answer) is handled internally by `create_agent` — there's no manual `while` loop here anymore, unlike the pre-LangChain version. `callbacks` is the second DI point: tests pass an in-memory/local-file callback handler instead of a real telemetry backend.
- `main()` — CLI REPL. Calls `setup_telemetry()`, constructs the real `ChatDeepSeek(model=MODEL)`, builds the agent once, then loops calling `run_agent`. `result["messages"]` from `agent.invoke()` are LangChain message objects (`.content`, not dict-style `["content"]`) — mixing them with plain `{"role": ..., "content": ...}` dicts in the same running `messages` list across turns works fine; LangChain normalizes both.
- `setup_telemetry()` — see **Telemetry** section below.

Notes saved via `save_note` are appended as JSONL to `notes.jsonl` (created at runtime, not checked in).

`search_news` no longer hits a single generic news API — as of this project's pivot to AI-industry focus, it aggregates across a **pluggable source registry** in `news_sources.py`. Each source is a `fetch(query, max_results) -> list[dict]` function normalized to `{"title", "link", "source", "summary", "published", "published_dt"}`; `SOURCE_REGISTRY` pairs each with an optional required env var, and `enabled_sources()` skips any source whose key isn't set — so `search_news` degrades gracefully instead of erroring when e.g. `NEWSAPI_API_KEY` is absent. Per-source fetch errors are caught individually in the tool so one broken source doesn't take down the whole call. See `docs/ai-news-sources.md` for the full source list (what's live, what needs a key, what was considered and rejected — e.g. Reddit's unauthenticated JSON access is now blocked) and how to add a new one.

`published_dt` (a parsed, timezone-aware UTC `datetime`, alongside the original raw `published` string) was added after a real complaint that on-demand queries "always return similar news" — `search_news` was dropping the `published` field before it reached the model, so there was no way to judge recency or notice a repeat. `search_news` now surfaces the raw `published` string in its output; `published_dt` itself exists mainly for `news_push.py`'s dedup filtering (see below), not for the tool-calling agent directly. Parsed via `_parse_iso_published` (HN/NewsAPI/GNews/Perigon's ISO-8601-ish fields) or `_parse_rss_published` (feedparser's `published_parsed`, more reliable than hand-parsing RSS/arXiv's raw date string).

The old OK Surf News API integration (general Google News sections, no AI filtering) was removed in this pivot — if you see references to `NEWS_API_BASE` or `by_section` in old context, that's stale; it's gone from the codebase.

`MODEL` is set to `"deepseek-chat"` — confirmed live and working via direct API test (a search result claimed it was retired 2026-07-24; that was wrong or premature). Don't switch to `"deepseek-reasoner"` without changing the tools setup: per LangChain's own docs, DeepSeek-R1 (`deepseek-reasoner`) does not support tool calling, which this agent depends on for both `save_note` and `search_news`. `"deepseek-chat"` is currently aliased server-side to `deepseek-v4-flash` — confirmed via a real Phoenix trace's `model_name` field (see `docs/guardrails-plan.md`'s cost note) — DeepSeek's other current tier, `deepseek-v4-pro`, is meaningfully more expensive (~50x on cache hits per their pricing page) and not what this project uses anywhere.

`SYSTEM_PROMPT` asks the model to write its final answer as **Telegram HTML** (`<b>`, `<i>`, `<a href="">`), not Markdown — Telegram doesn't render Markdown symbols in plain-text messages, so unformatted output showed up as literal `#`/`**`/`[text](url)` characters. `bot.py` sends replies with `parse_mode=ParseMode.HTML` to match. See the `telegram-message-formatting` skill before editing this prompt or `bot.py`'s message-sending code — it has the full rationale (why HTML over MarkdownV2, structure/emoji conventions, what was deliberately not adopted from external references) synthesized from external Telegram-formatting skills plus this project's own testing. `search_news`'s output includes each article's `link` (added alongside this — it was missing before, so the model had no real URLs to cite).

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

## Telegram Bot

`bot.py` is the headless entry point — the CLI REPL's `input()` loop can't run in a container (see `docs/deployment-plan.md`), so this is what a real deployment would actually run. Polling mode (`Application.run_polling()`), not webhooks — no public HTTPS endpoint/TLS needed, and it's the same shape locally and in a future Kubernetes `Deployment`.

```powershell
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
$env:TELEGRAM_BOT_TOKEN = "<your-bot-token>"   # from @BotFather
$env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
$env:ADMIN_BOT_TOKEN = "<second-bot-token-for-admin_bot.py>"
python bot.py
```

- Imports `build_agent`/`run_agent`/`setup_telemetry` from `agent.py` **unchanged** — no changes were needed to `agent.py`'s core logic to add this second entry point, confirming the DI design actually paid off.
- `run_agent` is synchronous but python-telegram-bot's handlers are async — `handle_message` calls it via `asyncio.to_thread(...)` rather than blocking the bot's event loop.
- Per-chat history is an in-memory `dict[chat_id, messages]`, same non-persistence as the CLI's `messages` list — lost on restart.
- Telegram rejects messages over 4096 characters, which this agent's trend reports can easily exceed. `split_for_telegram()` chunks long replies (preferring a newline boundary, and never splitting in a way that would leave an HTML tag open in one chunk and its closing tag in the next) and sends each as a separate message with a 1s gap between chunks — covered by `tests/test_bot.py`. If Telegram rejects a chunk's HTML (`BadRequest` — usually the model produced unescaped `&`/`<`/`>` or an unexpected tag), `handle_message` retries that chunk as plain text via `_strip_html_tags()` rather than failing silently.
- `/interests` command (a `CommandHandler`, separate from the plain-text `MessageHandler`) — show/set/clear a per-chat list of topics, stored via `users_db.get_interests()`/`set_interests()`. `handle_message()` prepends a bracketed note with the caller's interests to the *agent-facing* copy of their message only (not what the guardrail classifiers see, and not what gets echoed back) — see `docs/bot-features-plan.md` item 3.
- `/language` command, same shape as `/interests` (show/set/clear), plus a natural-language surface (the `set_language` tool + router category) — see `docs/bot-features-plan.md` item 2. The stored preference is injected into every reply regardless of category (`agent.py`'s `_compose_prompt`) and into push digests separately (`news_push.py`'s `write_push_digest`, since digests don't go through `_compose_prompt` at all).

### Access control

`bot.py` is gated by an admin-approval workflow, not open to anyone who finds it — see `docs/bot-features-plan.md` item 1 for the full design rationale. Summary:

- `ADMIN_CHAT_ID` (your own numeric Telegram user ID, not username) always passes `check_access()` in `bot.py`.
- Anyone else's first message inserts a `pending` row into `users_db.py`'s SQLite `subscribers` table and pings the admin — via **`admin_bot.py`, a second bot with its own token** — with an inline-keyboard message (Approve / Deny buttons, not a text reply). This keeps approval controls off the same bot a stranger can message.
- Tapping a button fires a `callback_query` that `admin_bot.py`'s `CallbackQueryHandler` catches: updates `subscribers`, edits the message to show the decision, and messages the requester (via `bot.py`'s token) with the outcome.
- Run `admin_bot.py` alongside `bot.py` (same env, needs `ADMIN_BOT_TOKEN`, `ADMIN_CHAT_ID`, and `TELEGRAM_BOT_TOKEN` — the last one so it can notify approved/denied users):
  ```powershell
  conda activate myfirstagent
  $env:ADMIN_BOT_TOKEN = "<second-bot-token-from-botfather>"
  $env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
  $env:TELEGRAM_BOT_TOKEN = "<the-info-bot-token>"
  python admin_bot.py
  ```
- Both processes must point at the same SQLite file — `SUBSCRIBERS_DB_FILE` env var (defaults to `subscribers.db` in the working directory), same configurability reasoning as `agent.py`'s `PHOENIX_ENDPOINT`. Solved for local Docker via a shared named volume (`myfirstagent-data`) mounted into both containers — not yet solved for Kubernetes, see `docs/deployment-plan.md`.
- See `docs/security-plan.md` for a full security review (secrets handling, rate limiting, prompt-injection surface, CI scanning gaps) done before moving to cloud deployment.

### Periodic news push

Per-user digest pushed on a schedule, without the user asking first — see `docs/bot-features-plan.md` item 5 for the full design (including the `search_news`/`published_dt` fix that had to happen first) and the incident-driven rationale for each piece below.

- `news_push.py` deliberately does **not** go through `build_agent`/`run_agent` — it fetches via `news_sources.enabled_sources()` directly, filters to genuinely-new articles (`fetch_new_articles`, judged by `published_dt` primarily and `users_db`'s remembered `pushed_links` as a fallback for unparseable dates), then does one plain `model.invoke()` (`write_push_digest`, no tools) to write the digest — guaranteeing no repeats instead of trusting the tool-calling agent not to re-search the same query.
- `users_db.py`: `push_interval_hours` (default 24, floored at `MIN_PUSH_INTERVAL_HOURS=1`), `last_push_at`, `pushed_links` — plus `set_push_interval_hours`/`list_push_enabled_subscribers` etc. Interval is set via natural language (the `set_push_interval` tool, offered alongside `set_push_enabled`), not a `/command` — same reasoning as the rest of the router design in `docs/context-management-plan.md`.
- `bot.register_push_job(app)` schedules one repeating `JobQueue` tick (`PUSH_TICK_SECONDS`, every 15 min) that calls `news_push.run_push_cycle()`; each subscriber's own `push_interval_hours`/`last_push_at` decides whether they're actually due (`is_subscriber_due`) — not one APScheduler job per subscriber, since the interval is user-changeable at runtime. Needs the `apscheduler` conda-forge package (see `environment.yml`) for `Application.job_queue` to be non-`None` at all. Wired into both `bot.py`'s standalone `main()` and `combined_bot.py`'s `build_info_app()` — the latter is what's actually deployed.
- `run_push_cycle` still runs `guardrails.is_output_on_topic` before sending — a subscriber's own `interests` are unsanitized text that end up in the digest prompt, the same injection surface the chat-reply guardrail already covers.
- `bot.send_push_digest()` reuses `handle_message`'s exact send pipeline (`_normalize_markdown_bold` → `split_for_telegram` → `parse_mode=HTML` → `BadRequest` fallback), since digests go through the same `agent.HTML_FORMATTING_RULES` prompt (extracted as a shared constant, along with `agent.TREND_REPORT_STRUCTURE`, so `_NEWS_QUERY_INSTRUCTIONS` and `news_push.py`'s prompt can't drift apart).

### Running both bots in one process (`combined_bot.py`)

`bot.py` and `admin_bot.py` can each run standalone (as shown above), but by
default the deployed image runs **`combined_bot.py`** instead, which runs
both Telegram `Application`s concurrently in a single asyncio event loop —
`build_info_app`/`build_admin_app` reuse `bot.py`'s and `admin_bot.py`'s
handler functions and `bot_data` wiring unchanged, so nothing about the
two-bot-token security design changes.

Why: running two separate OS processes duplicates LangChain/python-
telegram-bot's in-memory footprint (each independently loads its own copy).
On a small-RAM Always Free shape (e.g. Oracle's `VM.Standard.E2.1.Micro`,
1GB) that duplication is a real constraint — confirmed via `docker stats`
that the combined single-container setup uses ~135MB vs. what would be
close to double running as two containers. `bot.py`/`admin_bot.py` keep
their own standalone `main()`s for local dev flexibility or in case a
future higher-RAM shape (e.g. Ampere A1) makes splitting back into two
containers preferable for isolation — see `docs/deployment-plan.md`.

```powershell
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
$env:TELEGRAM_BOT_TOKEN = "<info-bot-token>"
$env:ADMIN_BOT_TOKEN = "<admin-bot-token>"
$env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
python combined_bot.py
```

### Running it in Docker

```powershell
docker build -t myfirstagent-bot .
docker run -d --name myfirstagent-bot --restart unless-stopped `
  -e DEEPSEEK_API_KEY=$env:DEEPSEEK_API_KEY `
  -e TELEGRAM_BOT_TOKEN=$env:TELEGRAM_BOT_TOKEN `
  -e ADMIN_CHAT_ID=$env:ADMIN_CHAT_ID `
  -e ADMIN_BOT_TOKEN=$env:ADMIN_BOT_TOKEN `
  -e SUBSCRIBERS_DB_FILE=/data/subscribers.db `
  -v myfirstagent-data:/data `
  myfirstagent-bot
```

`Dockerfile` uses `mambaorg/micromamba` and installs straight from `environment.yml` — same dependency source as local dev and CI, no separate pip requirements file. `CMD` runs `combined_bot.py` by default (both bots, one process/container — see above); override the command (`docker run ... myfirstagent-bot python bot.py`) to run either bot standalone in its own container instead. Image is ~900MB — a deliberate tradeoff for conda-forge consistency over a smaller `pip`+slim image; see `docs/deployment-plan.md` if that needs revisiting. `PHOENIX_ENDPOINT` is configurable via env var for this reason — `localhost` only resolves correctly for local dev, not once Phoenix runs as a separate container/service.
- Verified end-to-end against the real Telegram API, not just "the code runs": confirmed the token via `getMe`, then had a human message the live bot and confirmed a real reply came back.

### Deploying to the live Oracle VM

The above `docker build`/`docker run` is for local testing. The actual
deployment target is a real Oracle Cloud VM (see `docs/deployment-plan.md`
for the full setup). **Always build the image locally and transfer it —
never run `docker build` on the VM itself** — see the
`build-locally-deploy-remotely` skill for why (the VM is a tiny free-tier
shape; building there is slow and was once left in a corrupted state by
an unrelated interrupted SSH session):

```bash
docker build -t myfirstagent-bot .
docker save myfirstagent-bot:latest | ssh -i "<path-to-key>" ubuntu@<vm-ip> "sudo docker load"
```

Then on the VM, stop/remove the old container and `docker run` the new
image with the same flags as before. Secrets are fetched from OCI Vault
at container startup rather than passed as plain env vars — see
`docker-entrypoint.sh` and `docs/security-plan.md` finding 2 for how.
- Four further requested features (translation, DB-backed per-user preferences, per-user search-source selection, proactive push) are planned but not built — see `docs/bot-features-plan.md`. Access control (the fifth, and the urgent one) is done — see **Access control** above.

## Testing

```powershell
conda activate myfirstagent
pytest
```

No `DEEPSEEK_API_KEY` or network access needed — the whole suite runs in ~1.5s against fakes/mocks only. `pytest.ini` (`pythonpath = .`) is required for `tests/` to be able to `import agent`/`news_sources` at all; without it pytest's default import mode doesn't add the project root to `sys.path`.

- `tests/fakes.py` — `FakeToolCallingModel` (see the `build_agent` note above for why it's custom, not a LangChain built-in) and `RecordingCallbackHandler` (in-memory stand-in for a real telemetry backend).
- `tests/fixtures.py` — mock response payloads for `news_sources.py`'s fetchers, matching real responses captured during live verification (Perigon excepted — unverified, no API key available).
- `tests/conftest.py` — `isolated_notes_file` and `isolated_subscribers_db` fixtures, monkeypatching `agent.NOTES_FILE` / `users_db.DB_FILE` so tests never touch the real `notes.jsonl` / `subscribers.db`.
- `tests/test_telemetry.py` — only the `PHOENIX_ENABLED` gating logic (register not called when unset, called with expected args when set). Does not test actual tracing — that needs the real Docker container and is verified manually instead (see **Telemetry** above).
- `tests/test_bot.py` — `split_for_telegram()`'s chunking logic, plus `check_access()`'s branching (admin bypass, approved/pending/denied, new-request registration + admin notification), with `notify_admin` and the Telegram `Update`/`context` objects mocked. The actual bot integration is verified manually against the real Telegram API instead (see **Telegram Bot** above).
- `tests/test_admin_bot.py` — `handle_decision()`'s approve/deny/non-admin-tap branching, with `telegram.Bot` mocked.
- `tests/test_combined_bot.py` — `build_info_app`/`build_admin_app` wire up `bot_data` and register the right handler types; builds real (but unstarted) `Application` objects with fake tokens, no network calls (confirmed `Application.builder().build()` doesn't touch the network until `initialize()`/polling starts).
- `tests/test_users_db.py` — the `subscribers` SQLite table's CRUD functions, via the `isolated_subscribers_db` fixture (temp DB file per test).
- HTTP mocking via the `requests_mock` pytest fixture (auto-registered by the `requests-mock` package).

See `docs/telemetry-and-testing-plan.md` for what's covered, what's explicitly not, and what's still planned (CI/CD, LLM-judged end-to-end evaluation). See `docs/deployment-plan.md` for the plan to containerize `agent.py` itself and deploy to Kubernetes/cloud (separate from, but related to, the Docker usage here).
