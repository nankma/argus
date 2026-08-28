# Observability & Debugging Playbook

What this project actually learned about diagnosing live issues, written
down so the next debugging session doesn't have to rediscover it. Three
things prompted this: `docker logs` turning out to be silently empty for
an entire session's worth of deploys, the technique that was used instead
(querying Phoenix's traces directly) working well enough to be worth
recording precisely, and a live diagnostic workflow (hot-patching a
running container instead of rebuilding for every experiment) that got
reused often enough to be worth naming.

**Read this first: Phoenix was retired 2026-08-24; Logfire is the live
telemetry backend now.** Everything below about Phoenix (GraphQL
queries, its own VM, its own bearer token) describes an architecture
that stopped receiving live traffic on 2026-08-23 — kept here because
it's still exactly right for digging into the ~269 MB of trace data
frozen on the (now-stopped, boot-volume-intact) Phoenix VM from before
that date, not because it's how to diagnose anything happening *today*.
For a CURRENT live issue, use the Logfire technique immediately below
instead.

## Technique: querying Logfire traces directly (current backend)

Same motivation as the Phoenix technique below it — pulling real trace
data beats re-running inputs locally and hoping to reproduce them,
especially for anything involving LLM non-determinism — just against
the backend actually receiving live traffic since 2026-08-24.

Logfire exposes a SQL-like read API. Resolve `LOGFIRE_API_KEY` the same
way the bot container does (it's a Vault secret, fetched at container
startup — see `docker-entrypoint.sh`), then query directly, e.g.:

```bash
ssh -i <key> ubuntu@<bot-vm-ip> \
  "sudo docker exec myfirstagent-bot bash -c 'cd /app && ./docker-entrypoint.sh printenv LOGFIRE_API_KEY'"
# then, with that resolved value:
curl -s "https://logfire-api.pydantic.dev/v1/query" \
  -H "Authorization: Bearer <resolved LOGFIRE_API_KEY>" \
  --data-urlencode "sql=SELECT trace_id, span_name, start_timestamp FROM records ORDER BY start_timestamp DESC LIMIT 5"
```

Verified working 2026-08-21 while diagnosing the Phoenix/Logfire
coexistence bug (see `docs/current/infrastructure.md` and this session's
telemetry-monitoring incident notes) — real, fresh spans came back this
way, confirming exactly what was and wasn't reaching Logfire at the time.
This is a thinner, less-explored technique than the Phoenix section below
(one confirmed working query, not a whole querying playbook) — extend it
here as it gets used more, rather than assuming Logfire's API surface
mirrors Phoenix's GraphQL one just because both are OpenTelemetry-based.

## Technique: managing Logfire alerts/channels (needs a fresh OAuth token)

**Read `docs/plans/observability-platform-plan.md` before touching alert
delivery at all** — it has the full reasoning, what's live today, and a
2026-08-28 lesson about exactly this gap costing real time once already.
This section is the mechanical how-to only.

`LOGFIRE_API_KEY` (the project-scoped write/query token above) **cannot**
create or edit alerts/channels — that needs a *user*-authorised OAuth
token via the device-code flow, which lasts 1 hour and is never stored.
Getting one takes one short human step (approving a URL in a browser);
everything else is scriptable. Current state (project/alert/channel ids,
what each alert fires on) lives in `local-infra/infrastructure.yaml`
under `logfire:` — read that first so you're not guessing at ids.

**1. Register a client and request a device code** (PKCE required — the
call fails with `invalid_client` without it):

```python
import base64, hashlib, secrets, requests, json

B = "https://logfire-us.pydantic.dev/api"
SCOPES = "project:read project:read_alert project:write_alert organization:read_channel organization:write_channel"

verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

r = requests.post(B + "/oauth/register", json={
    "client_name": "argus-alert-setup",
    "grant_types": ["urn:ietf:params:oauth:grant-type:device_code"],
    "token_endpoint_auth_method": "none",
    "application_type": "native",   # NOT "web" -- matters, confirmed 2026-08-28
    "scope": SCOPES,
}, timeout=30)
cid = r.json()["client_id"]

d = requests.post(B + "/oauth/device/code", data={
    "client_id": cid, "scope": SCOPES,
    "code_challenge": challenge, "code_challenge_method": "S256",
}, timeout=30).json()
print("APPROVE HERE:", d.get("verification_uri_complete") or d.get("verification_uri"))
print("USER CODE:", d.get("user_code"))
# save cid, verifier, and d (device_code etc.) for step 2
```

Only request the scopes the task actually needs — `project:write` (rename/
delete projects), `project:write_token`/`read_token` (mint credentials),
and anything `organization:admin` were deliberately never requested for
this project's alert work.

**2. Send the printed URL to a human, poll until they approve:**

```python
import time
interval = max(int(d.get("interval", 5)), 5)
deadline = time.time() + min(int(d.get("expires_in", 600)), 540)
while time.time() < deadline:
    r = requests.post(B + "/oauth/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": d["device_code"], "client_id": cid, "code_verifier": verifier,
    }, timeout=30)
    if r.status_code == 200:
        token = r.json()["access_token"]; break
    err = r.json().get("error")
    if err in ("authorization_pending", "slow_down"):
        time.sleep(interval); continue
    raise SystemExit(f"denied: {r.text}")
```

**3. Use the token** (1h lifetime — if a call 401s, get a new one, don't
try to refresh):

```bash
PROJECT_ID=71097da6-6be4-4621-9be2-e9d9aaaa23de   # see infrastructure.yaml

# List alerts (ids, active state, and each one's channel config):
curl -s "https://logfire-us.pydantic.dev/api/v1/projects/$PROJECT_ID/alerts/" \
  -H "Authorization: Bearer $TOKEN"

# Update a channel's webhook config (PATCH by channel_id -- repoints
# every alert using that channel at once; see infrastructure.yaml for
# which alerts currently share which channel):
curl -s -X PATCH "https://logfire-us.pydantic.dev/api/v1/organizations/<org_id>/channels/<channel_id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"config": {"type": "webhook", "format": "raw-data", "url": "<new url>"}}'
```

(The `PATCH .../channels/` shape above is inferred from `WebhookUpdate`'s
schema in Logfire's own `/api/openapi.json`, not yet exercised end-to-end
as of 2026-08-28 — verify the exact path/verb against that spec, or by
trying it, before trusting this blindly.)

**Discovering what a webhook actually delivers isn't in the OpenAPI
spec** — `WebhookFormat`'s enum (`auto`/`slack-blockkit`/`slack-legacy`/
`raw-data`) is documented, but the JSON body shape for each format is not
part of Logfire's public API surface. The only reliable way to learn one
is to point a real channel at a URL you control and force a genuine alert
transition (per this doc's own "Testing an alert requires an edge, not a
state" lesson) — reading a channel's *current* state doesn't fire it.

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

## Technique: querying Phoenix traces directly instead of guessing (historical — pre-2026-08-24 data only)

Several bugs this session (an output-guardrail false positive, a
duplicate-classification report, the push-delivery timing question) were
diagnosed by pulling real trace data from Phoenix rather than re-running
inputs locally and hoping to reproduce them. This is more reliable than
local reproduction for anything involving LLM non-determinism, and it's
the only way to inspect what already happened in the past (a local
re-run can't tell you what a *specific* historical call actually saw).

### Getting a bearer token

Phoenix runs with `PHOENIX_ENABLE_AUTH=true` (see `docs/plans/security-plan.md`
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
publicly (see `docs/plans/security-plan.md` finding 17), so run the query via
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

- `docs/plans/security-plan.md` finding 17 — why Phoenix isn't publicly
  exposed and needs the System API Key for any access, read or write.
- `docs/plans/guardrails-plan.md` — the guardrail reliability findings
  (classifier flakiness, structured-output vs. staged-text-prompt
  reliability) that most of this session's Phoenix-trace debugging was
  in service of.
- The `build-locally-deploy-remotely` skill — the full deploy workflow
  and post-deploy smoke-test checklist this playbook's incident notes
  feed into.
