# Observability & Debugging Playbook

What this project actually learned about diagnosing live issues, written
down so the next debugging session doesn't have to rediscover it. Three
things prompted this: `docker logs` turning out to be silently empty for
an entire session's worth of deploys, the technique that was used instead
(querying Phoenix's traces directly) working well enough to be worth
recording precisely, and a live diagnostic workflow (hot-patching a
running container instead of rebuilding for every experiment) that got
reused often enough to be worth naming.

## Incident: `docker logs` was empty this whole session

**What happened:** a user asked why a periodic push notification took
longer to arrive than its configured interval. `sudo docker logs
myfirstagent-bot` returned **zero lines** — not just missing the push
cycle's own log lines (`news_push.py` had per-cycle logging added earlier
the same session specifically for this), but missing the container's own
startup messages ("Both bots ready (polling)...", Phoenix's OTel
registration banner) that unconditionally `print()` on every boot. The
process was clearly running (it was actively serving Telegram messages),
so this wasn't a crash — the output was going somewhere that wasn't
`docker logs`.

**Root cause:** Python buffers stdout in full-block mode when it isn't
connected to a TTY, which is exactly the situation for any `docker run
-d` container (no `-t`). `print()` calls accumulate in an internal buffer
that's only flushed when it fills (several KB) or the process exits.
Since `combined_bot.py` runs forever and doesn't print at high volume,
the buffer effectively never filled — so nothing ever reached the log
stream, for the container's entire uptime, across every deploy this
session.

**Fix:** `ENV PYTHONUNBUFFERED=1` in the `Dockerfile`. Forces
unbuffered stdout/stderr regardless of TTY status — the standard fix for
this exact class of Docker logging gap. Verified live: after redeploying,
`docker logs` immediately showed the startup banner and Phoenix
registration output that had never appeared before.

**Lesson:** a print-based logging fix isn't actually a fix until you've
confirmed the prints reach `docker logs` — this was checked belatedly,
after already shipping the news_push.py per-cycle logging in an earlier
commit and assuming it worked. The `build-locally-deploy-remotely` skill
now has an explicit "check docker logs has output" step before the rest
of the post-deploy smoke test, specifically so this isn't re-assumed
next time.

## Technique: querying Phoenix traces directly instead of guessing

Several bugs this session (an output-guardrail false positive, a
duplicate-classification report, the push-delivery timing question) were
diagnosed by pulling real trace data from Phoenix rather than re-running
inputs locally and hoping to reproduce them. This is more reliable than
local reproduction for anything involving LLM non-determinism, and it's
the only way to inspect what already happened in the past (a local
re-run can't tell you what a *specific* historical call actually saw).

### Getting a bearer token

Phoenix runs with `PHOENIX_ENABLE_AUTH=true` (see `docs/security-plan.md`
finding 17), so GraphQL queries need the System API Key. It's stored as a
5th Vault secret (`PHOENIX_API_KEY_SECRET_OCID`), fetched the same way
the bot container fetches its own secrets:

```bash
ssh -i <key> ubuntu@<bot-vm-ip> \
  "sudo docker exec myfirstagent-bot bash -c 'cd /app && ./docker-entrypoint.sh printenv PHOENIX_API_KEY'"
```

`docker-entrypoint.sh printenv PHOENIX_API_KEY` works because the
entrypoint script fetches every `*_SECRET_OCID` it finds set, exports the
real value, then `exec`s whatever command follows — `printenv` here
instead of the real `python3 combined_bot.py`, just to get the resolved
value out.

### Querying spans

Phoenix's GraphQL endpoint is `http://localhost:6006/graphql` — only
reachable from the Phoenix VM itself (or same-VCN traffic), not exposed
publicly (see `docs/security-plan.md` finding 17), so run the query via
SSH to the Phoenix VM (`<phoenix-vm-ip>`), not the bot VM.

First, find the project's node ID (once per investigation, or just
memoize it — for this project's `myfirstagent` project it's
`UHJvamVjdDoy`, i.e. base64 of `Project:2`):

```bash
curl -s -X POST http://localhost:6006/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PHOENIX_API_KEY" \
  -d '{"query":"query { projects { edges { node { id name } } } }"}'
```

Then pull spans in a time window:

```bash
curl -s -X POST http://localhost:6006/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PHOENIX_API_KEY" \
  -d '{
    "query": "query($id: ID!, $start: DateTime!, $end: DateTime!) { node(id: $id) { ... on Project { spans(first: 200, timeRange: {start: $start, end: $end}, sort: {col: startTime, dir: desc}) { edges { node { name spanKind startTime input { value } output { value } } } } } } }",
    "variables": {"id": "UHJvamVjdDoy", "start": "2026-08-08T22:00:00Z", "end": "2026-08-09T00:00:00Z"}
  }'
```

Useful additional `Span` fields beyond `input`/`output`: `statusCode`,
`statusMessage`, `events { name message }` (exceptions show up here),
`attributes` (the raw OpenInference attributes, worth checking if
`output.value` comes back `null` for a very recently-completed span —
seen once this session, possibly a brief Phoenix indexing lag for
sub-minute-old spans; re-querying a minute later resolved it).

### Reading the output

Span **names** are generic and don't say which of this project's several
structured-output calls produced them (`ChatDeepSeek`, `RunnableSequence`,
`PydanticToolsParser`, `LangGraph`, `model`, `tools`, plus a `tool` span
per actual tool call). Identify which call a span belongs to by matching
a distinctive substring in `input.value` against the known system
prompts:

| Substring in `input.value` | Which call |
|---|---|
| `"strict classifier... Classify the following user message"` | `guardrails.classify_message` (layer 2, the router) |
| `"strict classifier... Evaluate the following text on two independent questions"` | `guardrails.is_output_on_topic` (layer 4) |
| `"periodic news digest for a Telegram subscriber"` | `news_push.write_push_digest` |
| `"technology industry analyst and this Telegram bot's assistant"` | the main agent's model call (`agent.py`'s `_compose_prompt` output) |

A single logical operation shows up as a small cluster of spans with
startTimes a few milliseconds to ~2 seconds apart — e.g. a `classify_message`
call is `RunnableSequence` → `ChatDeepSeek` (the raw completion) →
`PydanticToolsParser` (the parsed structured result, where the actual
`{"on_topic": ..., "category": ...}` shows up in `output.value`). A full
`run_agent` call is `LangGraph` → `model` → `ChatDeepSeek`, optionally
followed by `tools` → one `tool` span per call → another `model` round.

## Technique: hot-patching a running container for live experiments

For anything that needs the *real* deployed DeepSeek model but doesn't
need a full image rebuild to test (a prompt tweak, a new function's logic
before it's finished), `docker cp` the changed file(s) straight into the
already-running container, then `docker exec` a throwaway Python script
that imports it fresh:

```bash
scp -i <key> agent.py guardrails.py ubuntu@<bot-vm-ip>:/tmp/
ssh -i <key> ubuntu@<bot-vm-ip> \
  "sudo docker cp /tmp/agent.py myfirstagent-bot:/app/agent.py && sudo docker cp /tmp/guardrails.py myfirstagent-bot:/app/guardrails.py"

# write a throwaway test script locally, scp + docker cp it in the same way, then:
ssh -i <key> ubuntu@<bot-vm-ip> \
  "sudo docker exec myfirstagent-bot bash -c 'source /usr/local/bin/_activate_current_env.sh && cd /app && ./docker-entrypoint.sh python3 my_test_script.py'"
```

`./docker-entrypoint.sh python3 my_test_script.py` (rather than a bare
`python3 my_test_script.py`) matters when the script needs real secrets
(`DEEPSEEK_API_KEY`, etc.) — the entrypoint fetches them from Vault first,
then `exec`s whatever command follows.

This does **not** persist: the running container's main process (PID 1,
`combined_bot.py`) already has the old code loaded in memory and keeps
using it until restarted — only *new* `docker exec` processes see the
patched files. Useful for fast iteration on a hypothesis before spending
a full build-transfer-restart cycle on it, but always follow up with the
real `docker build` → `docker save | ssh ... docker load` → restart flow
(see the `build-locally-deploy-remotely` skill) once the fix is settled,
and clean up (`docker exec ... rm -f`) any throwaway test scripts/chat
data left in the container or a test `chat_id`'s DB rows.

## See also

- `docs/security-plan.md` finding 17 — why Phoenix isn't publicly
  exposed and needs the System API Key for any access, read or write.
- `docs/guardrails-plan.md` — the guardrail reliability findings
  (classifier flakiness, structured-output vs. staged-text-prompt
  reliability) that most of this session's Phoenix-trace debugging was
  in service of.
- The `build-locally-deploy-remotely` skill — the full deploy workflow
  and post-deploy smoke-test checklist this playbook's incident notes
  feed into.
