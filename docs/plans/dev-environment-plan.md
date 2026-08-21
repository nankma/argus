# A dev environment, so tests stop running against production

Written 2026-08-21. Status: **proposed, nothing built.**

`tools/run_smoke_tests.py` opens an SSH tunnel to the production VM and
drives the live bot. That is how the 2026-08-21 incident happened: the
smoke tests created real subscriber rows in the production database, and
those rows then drew billed work every six hours. The flag that now
excludes them (`is_test`, `9bb8f7c`) contains the damage; it does not
address testing against production at all.

Everything below is about removing that need.

## What "against production" actually costs

Worth being specific, because the obvious cost is not the main one:

- **Shared state.** Smoke tests write to the same `subscribers.db` real
  users are in. That is what leaked.
- **Shared budget.** Every smoke run spends the same DeepSeek balance the
  product runs on. A full run is roughly 25–30 model calls, three of them
  full agent loops.
- **No safe failure.** A test cannot exercise "what happens when the model
  returns garbage" or "what happens at the rate limit" without doing it to
  the live bot.
- **Serialised.** Only one person can smoke-test at a time, and never
  while a deploy is in flight.

## The three pieces, and how much each actually needs

The ask was a fake bot interface, a local LLM, and end-to-end coverage.
Those are three separable problems with very different costs, and the
cheapest one carries most of the value.

### Piece 1 — the bot interface: already built

`test_api.py` is exactly this. It exposes an HTTP endpoint that calls
`bot.process_message` directly, bypassing Telegram entirely, and it exists
precisely so tests exercise the real pipeline rather than a
reimplementation that could drift. `run_smoke_tests.py` already drives it.

Nothing new is needed for input. The one gap is **output**: replies come
back in the HTTP response, but *push digests* are sent through the
Telegram API, so a local run cannot see them without a stub. That is a
small addition — a `send` callable is already a parameter of
`news_push.run_push_cycle`, so a local runner passes one that appends to a
list instead of calling Telegram.

`bot.main()`/`combined_bot.main()` require `TELEGRAM_BOT_TOKEN`,
`ADMIN_BOT_TOKEN` and `ADMIN_CHAT_ID` at import time. For a local run
these can be any placeholder string as long as nothing calls out — worth
confirming rather than assuming, since a `telegram.error.InvalidToken` at
startup would be a fast, clear failure.

### Piece 2 — isolation: the actual fix, and it is nearly free

Both stores are already environment-configurable:

```
SUBSCRIBERS_DB_FILE   → a local file
NEWS_CACHE_DIR        → a local directory
NEWS_ARCHIVE_DIR      → a local directory
PHOENIX_ENABLED       → unset, so telemetry is a no-op
```

Pointing those at a scratch directory and running the same code locally
removes **every** problem listed above except the model budget. No new
service, no container, no fake anything.

**This is the recommendation for the first step.** The incident was not
caused by using a real model — it was caused by using the real *database*.
Running the identical pipeline against a scratch DB with the real DeepSeek
model would have prevented it entirely, costs a few cents per run, and is
a shell script rather than a project.

### Piece 3 — a local model: real, but genuinely harder than it looks

Free runs need a model, and the pipeline asks two different things of one:

| use | needs | who calls it |
|---|---|---|
| guardrail router, output check, classification, interest normalization | `with_structured_output` | `guardrails`, `news_classify` |
| the agent loop | **`bind_tools`** + streaming | `agent.build_agent` |

The second is the hard requirement. Options, honestly compared:

**a. `tools/claude_cli_model.py`** — already exists, already used by
`verify_classification.py` and `measure_guardrails --via-claude`, bills a
Claude Code subscription rather than API credit. It covers the first row
completely. It **cannot** cover the agent loop: its own docstring says so,
because it implements only `with_structured_output` and the loop needs
`bind_tools()`. Extending it is possible — the CLI does support tool
definitions — but it is real work and the result would not stream.

**b. Ollama with a small tool-calling model** (not currently installed).
Qwen2.5 and Llama 3.1/3.2 in the 3B–7B range support tool calling and run
on a dev box. Free and offline. The risk is not capability but
*assertions*: several smoke cases check semantic properties — "the reply
mentions 6", "the redirect message mentions the memory limit", "the follow
-up is in Spanish" — and a 3B model will fail some of those for reasons
that have nothing to do with the code under test. A test suite that fails
for model-quality reasons gets ignored, which is worse than not having it.

**c. `tests/fakes.py::FakeToolCallingModel`** — already exists, exercises
the loop deterministically, and is what the 447 pytest tests use. Perfect
for logic, useless for the thing smoke tests are for (does the real prompt
get the real model to do the real thing).

**Recommendation**: split the suite by what each case is actually testing.
Cases that assert on *routing and state* — which category was chosen, what
landed in the database, whether push got enabled — can run against (a) or
(b) and be part of a free, frequent local run. Cases that assert on
*generated text quality* need a real model and stay a deliberate,
occasional, paid run. Trying to make one suite serve both is what forces
the choice between "expensive" and "flaky".

## Proposed shape

```
tools/dev_env.py          brings up a scratch environment:
                          temp SUBSCRIBERS_DB_FILE / NEWS_CACHE_DIR /
                          NEWS_ARCHIVE_DIR, PHOENIX_ENABLED unset,
                          placeholder Telegram tokens, test_api on
                          localhost, and a push `send` that collects
                          instead of transmitting

tools/run_smoke_tests.py  gains --target local|prod, defaulting to LOCAL.
                          Pointing it at production becomes the deliberate
                          act rather than the default.
```

The default is the important part. The incident did not happen because
running against production was possible; it happened because it was the
only option and therefore the routine one.

## Sequencing

1. **`tools/dev_env.py` with the real model.** Removes shared state and
   shared failure immediately, for a few cents a run. Smallest step, most
   of the value.
2. **`--target local|prod`, defaulting to local.** Makes production a
   choice.
3. **Split the smoke cases** into state-asserting and text-asserting.
   Needed before a cheap model can be substituted without flakiness.
4. **A free model for the state-asserting half.** Either extend the Claude
   CLI shim with `bind_tools`, or add Ollama. Decide with the split in
   hand, since the split determines how much of the suite has to survive
   a weaker model.

## Open questions

- **Does the local push path need a real Telegram send at all?** Probably
  not, but `bot._send_digest` does HTML fallback handling on `BadRequest`
  that a collecting stub would never exercise — so some formatting bugs
  would only appear in production. Worth knowing which assertions lose
  coverage.
- **Should the dev environment ingest real news?** Real sources make it
  slow and non-deterministic; a frozen snapshot (there is already one in
  `docs/analysis/data/`) makes it fast and repeatable but stops testing the
  fetchers. Likely both, behind a flag.
- **Does anything else write to production during a test run?** The
  smoke tests were the known case. `tools/measure_guardrails.py` and
  `tools/run_eval.py` also make live calls; whether they touch
  subscriber state has not been checked.
