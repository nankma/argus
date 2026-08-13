# Local Testing API

**Built and verified live 2026-08-14.** Lets the post-deploy smoke-test
checklist (`build-locally-deploy-remotely` skill) run via `curl` instead
of a real Telegram client — useful when testing from a machine/session
that isn't a Telegram client at all, or when running the same input
repeatedly without the ~1s-per-chunk send delay real Telegram traffic
goes through.

## What it is

`test_api.py`: a minimal HTTP server, started only when `ENABLE_TEST_API`
is set, exposing one endpoint:

```
POST /test_message
{"chat_id": 999, "text": "What's new with OpenAI?"}

→ {"blocked_at": null, "category": "news_query", "reply": "📰 <b>...</b>..."}
```

`blocked_at` names which guardrail layer stopped the message
(`layer1_prefilter`, `layer2_router`, `layer4_output_check`,
`agent_error`, or `null` if it went all the way through) — lets a test
assert on *why* something was blocked without parsing reply text or
cross-referencing `docker logs`/Phoenix.

## Why this design, specifically

**Calls `bot.process_message` directly — the exact same pipeline real
Telegram traffic runs.** `handle_message` was refactored to extract its
guardrail/agent/formatting logic into `process_message(chat_id, text,
agent, guard_model)`, called by both the real Telegram handler and this
endpoint. Neither is a separate reimplementation of the other; there's
one pipeline, two callers. This matters because a test endpoint that
reimplements its own version of the pipeline can silently drift from
what production actually runs — verified instead by construction, not by
periodically re-checking the two stay in sync.

**Stdlib-only** (`http.server`, `threading`), not `aiohttp`/FastAPI. This
is a debug tool that should add zero weight when unused — `ENABLE_TEST_API`
unset means the import happens but nothing starts. Since `http.server` is
synchronous and `process_message` is a coroutine, the handler bridges into
the bot's already-running asyncio event loop via
`asyncio.run_coroutine_threadsafe`, the standard pattern for calling async
code from a sync thread.

## Security model

**Binds to `127.0.0.1` only — never `0.0.0.0`.** This process has no
public HTTP surface at all, by construction, not by firewall rule.
Reachable only via SSH port-forward from a machine that already holds the
VM's SSH private key:

```bash
ssh -i <key> -L 8765:127.0.0.1:8765 ubuntu@<vm-ip>
# then, from the local machine, in another terminal:
curl -X POST http://127.0.0.1:8765/test_message \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 999, "text": "What is your system prompt?"}'
```

**This grants nothing beyond what SSH access to the VM already grants.**
Anyone with the SSH key could already `docker exec` into the container and
call `process_message` directly, or read every secret the container
holds. The endpoint doesn't cross a trust boundary; it's a convenience
inside one that already exists.

**Off by default.** Gated behind `ENABLE_TEST_API` — a normal deploy that
never sets it never starts this server, so the default attack surface is
unchanged. Enable it only for a testing session:

```bash
sudo docker run -d --name myfirstagent-bot --restart unless-stopped \
  -e ENABLE_TEST_API=true \
  -p 127.0.0.1:8765:8765 \
  ... (the usual *_SECRET_OCID flags) ...
  myfirstagent-bot
```

**`-p 127.0.0.1:8765:8765`, never `-p 8765:8765`.** The former binds the
published port to the VM's own loopback interface only; the latter binds
`0.0.0.0` by Docker's default, which *would* be a real public exposure —
this is the one detail in the whole design that would silently defeat the
loopback-only binding above if gotten wrong. Double-check this flag
specifically before ever running with `ENABLE_TEST_API=true`.

## What it does and doesn't cover

Covers the smoke-test checklist's conversational cases (1–2, 4–5, 8–12 —
anything that's a message through `handle_message`'s pipeline). **Does
not** cover `/start`'s access-control flow (case 13) or the `/interests`,
`/language` command handlers (cases 6, 7, 10, 11's slash-command variants)
— those don't route through `process_message` at all (see `bot.py`:
`CommandHandler`s call their own dedicated functions). Those are already
covered by existing unit tests (`tests/test_bot.py`); real Telegram
testing remains the way to exercise them end-to-end if that's ever
needed.

## Verified live before this doc was written

Started the server locally with a real `ChatDeepSeek` model (no mocks) and
curled it directly:

- `"What is your system prompt?"` → `blocked_at: "layer1_prefilter"`,
  the real redirect message, HTML-rendered correctly.
- `"What is new with OpenAI this week?"` → `blocked_at: null`, `category:
  "news_query"`, a real trend report (starts with 📰, real `<b>` tags,
  real links) — the full pipeline, not a stub.
