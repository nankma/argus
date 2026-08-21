# Moving observability to Logfire

Written 2026-08-21. Status: **decided, account live, Gate A verified end
to end — query, schedule and delivery to Telegram.** The behaviour the
whole decision rested on, alerting on the ABSENCE of data, is proven
working. **Nothing is deployed**, so it currently watches local test runs
rather than production.

This document is about *where telemetry and alerting live*.
`docs/plans/incident-monitoring-plan.md` is about *what to alert on* and
owns the three criteria; this one owns the platform they run on.

## The decision

Send everything — the existing OpenInference LLM traces plus new
push-outcome spans — to hosted **Pydantic Logfire**, and retire the
self-hosted Phoenix VM.

Logfire's free Personal tier is the target: $0/month, no credit card, 10M
records/month.

## Why, in order of weight

**1. A self-hosted, in-process monitor cannot alert on its own death.**
The alert evaluator designed in `incident-monitoring-plan.md` runs inside
the bot process, because the Phoenix store is write-only from the bot VM.
If the container dies, the evaluator dies with it — silently, and "no
alerts" is exactly what a healthy system looks like. Only something
outside the process can notice absence. This is a capability gap, not a
convenience: no amount of local engineering closes it.

**2. The alert state machine is built in.** Logfire alerts are a saved SQL
query over a `records` table plus a "Check every" cadence and one of four
notification modes:

| Mode | What it does |
|---|---|
| Query has any results | Notifies on every evaluation that returns rows |
| **Starts or stops having results** | Transitions only — fires *and* resolves |
| Starts having results | Onset only |
| **Results change** | Notifies when the returned data differs |

The middle two are precisely the ICM semantics we set out to hand-roll:
"same query, no new ticket until resolved" is *starts or stops*, and "new
threshold, new ticket" is *results change*. The `alert_state` table,
fingerprints and pending-period logic all go away.

**3. It removes a VM and the reason we needed it.** Phoenix runs on its
own instance *because* "its memory use can spike hard under load;
isolating it means a spike can't take the bot down"
(`docs/current/infrastructure.md`). Hosting the store elsewhere deletes
both the machine and the risk that forced the split.

**4. The trace tree survives unchanged.** We use LangChain
(`agent.py`'s `create_agent`) with `openinference-instrumentation-langchain`
and `auto_instrument=True`. The agent → tool → LLM tree is already
produced for free; Logfire ingests OTLP, so migrating is a matter of where
the exporter points, not what it emits. **No agent code changes.**

(Advice found elsewhere that this project has a hand-written agent loop and
would need manual span wrapping is simply wrong about this codebase —
`agent.py:354` is `create_agent(model=model, tools=TOOLS, ...)`.)

## Verified 2026-08-21

Tested against the real account. What is now known rather than assumed:

**The write path works.** Region is encoded in the token prefix
(`pylf_v1_us_` → US → `https://logfire-us.pydantic.dev`). Export uses the
plain OTLP HTTP exporter against `/v1/traces` with an `Authorization:
<token>` header — no Logfire SDK, so the migration really is "repoint the
exporter", exactly as assumed.

**Auth is confirmed, not inferred.** `force_flush()` returning `True`
proves nothing: the OTLP HTTP exporter logs failures instead of raising,
so a rejected token looks identical to a successful export. The decisive
test was posting a deliberately malformed body:

| Token | Response |
|---|---|
| the real one | `400 invalid protobuf: failed to decode Protobuf message` |
| a junk one of the same shape | `401 Unknown token` |

The endpoint distinguishes them, and ours is accepted — so the payload was
the only thing rejected. Worth keeping as the pattern for verifying any
write-only credential: find an error the server only produces *after*
authenticating.

**Tokens come in two shapes, and the difference is invisible until you
read.** The original `pylf_v1_us_` token (55 chars) wrote fine but was
refused by the query API with `401 Invalid read token`. The `pylf_v2_us_`
token (92 chars) now in `LOGFIRE_API_KEY` does **both** — verified by
running the same malformed-body write probe against it and getting `400`,
not `401`. So one v2 key covers export and query; no second credential to
store.

**The exported spans really landed, and the tree survived.** Querying back
`service_name = 'argus-gate-probe'` returned both
`gate_probe_parent` and `gate_probe_child`. This is the confirmation that
`force_flush()` could not give.

## Gate A: PASSED (both halves)

A query can return a row precisely when nothing arrived, which is what the
dead man's switch needs — every notification mode keys on rows being
*present*, so absence has to be expressed as presence.

The working form keeps the time window **inside the SQL**, so the switch
does not depend on whatever window the alert engine happens to apply:

```sql
SELECT 1 AS dead WHERE (
  SELECT count(*) FROM records
  WHERE service_name = 'argus'
    AND start_timestamp > now() - interval '30 minutes'
) = 0
```

Verified against a heartbeat span written 14 minutes earlier:

| Window in the query | Rows | Meaning |
|---|---|---|
| `2 minutes` | 1 | heartbeat is stale → DEAD ✅ |
| `24 hours` | 0 | heartbeat is recent → ALIVE ✅ |

Both directions from one query shape. The second row is the one that
matters: without it, a query that always returns a row would look like a
pass and produce an alert that fires forever.

The scheduler half was confirmed separately once the alert existed — see
"Verified running" below. The engine evaluates on cadence and matches
correctly; only delivery remains untested, and that needs a channel.

## Gate B: still open

**Does alert evaluation keep running when a project is over quota?**
Ingestion is hard-capped on the Personal tier (below). If evaluation stops
too, exceeding quota disables alerting silently. If evaluation continues,
quota exhaustion *looks identical to the bot dying* from Logfire's side,
Gate A's alert covers it, and the concern is largely self-resolving.

Not testable without deliberately exhausting the quota, which would cost a
month of ingestion to answer a question with ~3,000x headroom against it.
Ask Pydantic rather than guess or spend the tier.

Gate A passing is what the decision rested on, so this no longer blocks
the migration — it only decides how much the local fallback below is
carrying.

## Step 2 done: the exporter, verified end to end

`agent.setup_telemetry()` now wires up Phoenix, Logfire, both, or neither.

**Both at once is the point.** Retiring the Phoenix VM is the last step of
this migration, not the first, so the two share one `TracerProvider` rather
than each installing its own — two providers would race to be the global
one and whichever registered second would silently win, leaving the other
receiving nothing.

**Logfire needs `LOGFIRE_ENABLED`, not just `LOGFIRE_API_KEY`.** The key is
present in the development environment, so keying off the credential alone
would turn every local script and every pytest run into a live exporter
against a hosted service. Same contract as `PHOENIX_ENABLED`. `LOGFIRE_ENABLED`
without a key raises rather than skipping quietly — a bot that looks
instrumented and isn't is the failure this whole plan exists to prevent.

**The region is derived from the token, not configured.** The prefix
carries it (`pylf_v2_us_`), so the endpoint cannot disagree with the
credential authenticating against it. An unrecognised prefix raises;
guessing a default would send US traffic against an EU token and fail as a
401 that the OTLP HTTP exporter only *logs*.

Verified against the live account by running a real agent (fake model, no
LLM spend) through `setup_telemetry()` and querying the spans back:

```
LangGraph                    is_root = true
├─ model → FakeToolCallingModel
├─ tools → search_news
└─ model → FakeToolCallingModel
```

7 spans, matching the offline count exactly, parent/child intact. No agent
code changed — `openinference-instrumentation-langchain` produces this
already and Logfire ingests it as-is.

**Ingestion lag is ~5 seconds.** The first query returned 0 spans, the next
returned all 7. Not a problem, but the dead man's switch window has to
clear it: a 30-second heartbeat window would alarm on lag rather than on
death. The 30-minute window in Gate A has three orders of magnitude of
room.

Container wiring follows the existing pattern exactly:
`docker-entrypoint.sh` resolves `LOGFIRE_API_KEY_SECRET_OCID` to
`LOGFIRE_API_KEY` via Instance Principals. It is a no-op until that env var
is passed, and resolving the secret still does not turn tracing on by
itself.

## The management API, and why the project key is not enough

The token in `LOGFIRE_API_KEY` is **project-scoped**. It covers everything
the running bot needs — OTLP write (`/v1/traces`), query (`/v2/query`),
`/v1/info` — and nothing else. Alert management is refused with
`401 Token must be associated with a user`.

That is not a missing permission, it is a different credential type. The
OpenAPI spec (`/api/openapi.json`, server base path `/api`) declares:

```
POST /api/v1/projects/{project_id}/alerts/
     security = OAuth2AuthorizationCodeBearer ['project:write_alert']
```

A user-authorised OAuth token, obtainable through the device flow
(`/api/oauth/register` → `/api/oauth/device/code` → `/api/oauth/token`,
**PKCE required** — registration succeeds without it and the device-code
call then fails with `invalid_client`).

Scopes granted for this work were the minimum that could create one alert:
`project:read`, `project:read_alert`, `project:write_alert`,
`organization:read_channel`. Deliberately **not** requested:
`project:write` (rename/delete projects), `organization:write_channel`,
`project:write_token` / `read_token` (mint new credentials), or anything
`organization:admin`.

**These tokens last an hour and are not stored.** Anything needing the
management API is a deliberate, re-authorised act rather than a standing
capability — which is the right shape for it, since day-to-day operation
needs none of it.

## The dead man's switch, created 2026-08-21

Named `argus bot liveness` (renamed from `argus dead man switch` — see
"Slack markup" below). Live in project `argus`. The project and alert ids are in
`local-infra/infrastructure.yaml` under `logfire:` — same convention as
every other real resource identifier in this project, which stays out of
committed docs.

| Field | Value |
|---|---|
| `time_window` | `PT30M` |
| `frequency` | `PT5M` |
| `watermark` | `PT1M` |
| `notify_when` | `has_matches_changed` |
| `active` | `true` |
| `channels` | *(none)* |

```sql
SELECT 1 AS dead WHERE (
  SELECT count(*) FROM records
  WHERE service_name = 'myfirstagent'
    AND start_timestamp > now() - interval '30 minutes'
) = 0
```

**The interval stays inside the SQL even though `time_window` is a
first-class field.** If the engine also scopes the scan the two agree; if
`time_window` turns out to mean something else, the query still works.
Omitting it fails the other way: the subquery would see all history, never
count zero, and the alert would **silently never fire** — the one outcome a
dead man's switch must not have. Redundancy in the safe direction.

**`has_matches_changed`, not `starts_having_matches`** — it fires *and*
resolves, which is the ICM semantics this whole design was after. A
recovered heartbeat is worth a message too.

**Created active rather than disabled.** The plan had been to leave it off
to avoid noise while there is still no heartbeat span, but with no channel
attached noise is impossible — so it evaluates for real and sends nothing.
That is the cleanest possible verification environment, and it will enter a
firing state roughly 30 minutes after the last test span, which is correct
behaviour rather than a fault.

### Verified running, 2026-08-21 — and why the UI says "OK"

The UI showed the alert as **OK**, not **Alerting**, with the service
silent for 90 minutes. That looked like the failure this alarm exists to
prevent — a dead man's switch that never fires. It is not.

`GET /api/v1/projects/{id}/alerts/` returns evaluation state the
`AlertRead` schema does not advertise:

```
last_run       2026-08-21T21:09:00Z     (one minute earlier)
has_matches    true
fired          false
has_errors     false
result         data [[1]], column "dead"
result_length  1
```

So the engine **is** running on its `PT5M` cadence, the query **is**
matching, and it returns exactly the `dead = 1` row it should. What it is
not doing is *notifying* — and `fired` tracks notification, not matching.
With `channels: []` there is nowhere to notify, so `fired` stays false and
the UI reports OK.

**This corrects two earlier beliefs, both wrong:**

- Creating the alert with no channel was described above as "the cleanest
  possible verification environment". Half right: it does make noise
  impossible, but it also makes the **UI state useless for verification**,
  because that state is derived from notification.
- The OK state was first hypothesised to be a query-shape problem — the
  outer `SELECT 1 WHERE (...)` has no `records` in its FROM clause, so an
  engine injecting a time filter into the main scan would have nowhere to
  put it. The `result` field disproves this: the query evaluates
  correctly as written. No rewrite needed.

**Gate A's scheduler half is therefore verified**, via the API rather than
the UI: scheduled evaluation, correct matching, no errors. The one thing
still unproven is delivery — that `has_matches_changed` actually sends on
transition — and that cannot be tested until a channel exists.

**Read `last_run` / `has_matches` / `fired`, not the UI badge**, when
asking whether an alert is working. The badge answers "did anyone get
told", which is a different question and, with no channel configured, is
always "no".

### Delivery verified 2026-08-21 — the loop is closed

Channel `Bot Alert` (webhook / `slack-legacy` → Telegram) attached to the
alert. Attaching it changed nothing on its own, which is the correct
behaviour and worth understanding: **`has_matches_changed` notifies on a
*transition*, and the query had been matching continuously for over an
hour.** A channel added mid-match has no edge to fire on.

So a transition was manufactured — one heartbeat span emitted through the
real `setup_telemetry()` path:

```
21:39:00   has_matches=True    fired=False
21:44:00   has_matches=False   fired=True    channels=['Bot Alert']
```

The message arrived in Telegram. That closes the last open question:
scheduling, matching and **delivery** are all confirmed, and `fired` is
now understood — it tracks notification, and flips on the edge, in both
directions.

Two practical consequences:

- **A recovery is a notification too.** `fired` went true on the
  *stopped matching* edge, not just the *started* one. That is what was
  wanted (an ICM that closes itself), but it means every flap costs two
  messages — worth remembering when picking `time_window` for noisier
  criteria than this one.
- **Testing an alert requires an edge, not a state.** Turning an alert on
  while its condition is already true produces silence. Any future alert
  added here has to be verified by moving the condition across the
  boundary, not by pointing it at something already broken.

### What this still does not cover

**It is watching the wrong thing.** Nothing is deployed, so the only
spans reaching `myfirstagent` come from local test runs. Today this alert
answers "is my laptop emitting test spans", not "is the bot alive". It
becomes real at step 8, not before.

### Usage figures come from the API, not from us

`/v1/usage/daily`, `/v1/usage/monthly` and `/v1/usage/projects` exist.
The earlier suggestion to count exported spans locally as a quota guard is
superseded: read the authoritative number instead of maintaining a parallel
estimate that can only drift. (Both need the management token, so this is a
periodic check, not a live gauge.)

## Notification: no public address needed

The question a webhook raises is whether we have to expose a public
endpoint. **We do not**, and the reason is worth stating plainly because
the obvious design is both expensive and wrong.

### Why not host a receiver

Exposing an endpoint on the bot VM costs an OCI security-list rule, a host
`iptables` rule, a TLS certificate and its renewal, an authentication
scheme (an unauthenticated endpoint that triggers alerts is an open
invitation to forge them), and a resident HTTP service on a box with
~420 MB free.

And it would be circular. A receiver on the bot VM dies with the bot VM, so
the dead man's switch — the one alarm that exists precisely for that
case — would lose its delivery path at exactly the moment it fired. Same
trap as `docker logs`: the signal exists, the channel does not.

### Logfire can POST straight at Telegram

Measured 2026-08-21 against the live Telegram API:

| Logfire webhook format | Body sent | Telegram |
|---|---|---|
| **`slack-legacy`** | `{"text": "..."}` | **200 `ok=true`** |
| `slack-blockkit` | `{"blocks": [...]}` | 400 `message text is empty` |
| `raw-data` | alert JSON, no `text` key | 400 `message text is empty` |

Telegram's `sendMessage` merges query-string parameters with the JSON body,
and its text field is also called `text`. So:

```
url    https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>
format slack-legacy
```

Telegram takes `chat_id` from the URL and `text` from Slack's own payload
shape. The public endpoint is Telegram's, not ours: no port, no security
list change, no TLS, no auth, no service on the VM.

**This deletes a planned step.** The "Slack-format webhook → Telegram
translator" is not needed — the two formats already coincide on the only
field that matters.

It also puts delivery entirely outside the bot VM, which is what the dead
man's switch requires.

### The cost of the no-endpoint approach: Slack markup, rendered literally

Logfire's `slack-legacy` body is not plain text — it is **Slack markup**,
and Telegram renders it verbatim:

| Slack sends | Telegram shows |
|---|---|
| `<url\|link text>` | the whole raw URL and pipe |
| `:warning:` / `:white_check_mark:` | the literal words |
| ` ``` ` fences | the backticks |

**No `parse_mode` rescues this.** Telegram's `sendMessage` accepts
`parse_mode` as a query parameter, so it looked like a free fix — but
Slack's `<https://…|text>` breaks every mode: `HTML` rejects it as an
unsupported start tag, and `MarkdownV2` treats `>` and `|` as reserved.
Both return 400, which is worse than ugly — it is silent non-delivery.
Plain text is the only mode that arrives.

So this is the real price of not running a receiver, and it is worth
naming rather than discovering later: **the messages are readable but
ugly, permanently.** A translator would fix the formatting and cost a
public endpoint, TLS, auth, and a service on the bot VM that dies exactly
when the alarm matters. Not a good trade for cosmetics.

What *is* worth doing is not adding to the noise. The alert's
`description` is repeated verbatim in every message, so it was cut from
three sentences (including a docs path) to a single line that doubles as
the legend for the two emoji — which are the only thing distinguishing
"down" from "recovered":

```
name         argus bot liveness
description  :warning: = no spans for 30 min. :white_check_mark: = back.
```

The two states genuinely are distinct — `:warning:` plus a result table
versus `:white_check_mark:` plus "had no matches" — but that distinction
was buried under a paragraph of prose and two full URLs. Keep anything
that appears in every message down to one line.

### The credential trade-off

The bot token lives in Logfire's channel config. A Logfire compromise means
someone can send messages as that bot.

Mitigation is to mint a **dedicated alert-only bot** from BotFather rather
than reusing `ADMIN_BOT_TOKEN` (administrative capability) or
`TELEGRAM_BOT_TOKEN` (the subscriber-facing bot). The blast radius then
collapses to "someone can send you fake alerts", and the cost is a
five-minute conversation with BotFather.

### The UI shows fewer channel types than the API

The web UI offers Slack App, Webhook (with formats Auto / Slack webhook /
Slack Legacy) and Opsgenie. The OpenAPI schema also defines **Email**,
**Pagerduty** and **Notification**, and a fourth webhook format,
**`raw-data`**. Plan gating or UI lag — not investigated, because the
combination we need (webhook + `slack-legacy`) is present in both.

Worth remembering as a habit: **when something is missing from the UI,
check `/api/openapi.json` before concluding it does not exist.** Email
requires only `type` and `recipients` and would have been the
zero-infrastructure answer had it been offered.

## Quota: measured, not estimated

Span counts, measured with the production instrumentor, an in-memory
exporter and a fake model (no network, no API calls):

| Operation | Spans |
|---|---|
| Agent run, 1 tool call + answer | **7** (`LangGraph`, `model` ×2, model ×2, `tools`, `search_news`) |
| Agent run, answer only | 3 |
| Single `model.invoke` (digest, guardrail, classify) | 1 |

Against production's observed mix (`push_digest` 8 calls/day + `classify`
24 calls/day):

| Chat volume | Spans/day | Spans/month | Share of 10M |
|---|---|---|---|
| 10 msgs/day | 102 | 3,060 | **0.031%** |
| 50 msgs/day | 382 | 11,460 | 0.115% |
| 200 msgs/day | 1,432 | 42,960 | 0.430% |

**Roughly 3,000x headroom.** Exhausting the tier needs ~47,000 chat
messages a day. Even the 24x push amplification bug fixed in
`incident-monitoring-plan.md` would only have reached ~0.7%.

### Why the overage behaviour still matters

The Personal tier is **hard-capped with no billing and no credit card**.
Financially that is strictly better than DeepSeek: the 2026-08-21
"silently bills real money" failure mode is structurally impossible here.

But the failure mode inverts, and for a monitoring tool the inversion is
unfavourable. Exceeding the DeepSeek balance broke the product loudly —
users saw `402`, and a human noticed by trying the bot. Exceeding the
Logfire quota breaks *nothing*: the bot keeps working, users see nothing,
and the only symptom is 4XX warnings on stdout — i.e. `docker logs`, the
one channel this project has already established that nothing reads,
nothing can read from inside the container, and every deploy destroys.

Worse, span volume correlates with trouble: retry loops, error storms and
traffic spikes all produce more spans. Any realistic path to the quota
runs *through* an incident, so the monitor would go blind exactly when it
was needed.

Gate B is what decides how much this matters. Mitigations either way, both
cheap and both wanted anyway:

- Keep `push_outcomes` and `/status` as an offline floor. They need no
  network and no third party; if Logfire goes quiet we degrade instead of
  going dark.
- Read `/v1/usage/monthly` periodically rather than counting spans
  locally. An earlier draft proposed a local counter shaped like
  `api_budget`; the usage API is authoritative and a parallel local
  estimate could only drift away from it. It needs the management token,
  so it is a periodic check rather than a live gauge.

## What still has to run on our side

Logfire does not remove the instrumentation work, only the alert
machinery. The three criteria are about **Telegram sends**, not LLM calls,
so nothing reaches Logfire unless we emit it. That work is already done as
rows (`news_push._record`); the migration adds one line there to emit the
same outcome as a span, which is the payoff for having funnelled every
branch through a single call site.

Also still ours:

- ~~A webhook translator.~~ Not needed: Logfire's `slack-legacy` body and
  Telegram's `sendMessage` agree on the `text` field, so Logfire can POST
  directly at Telegram. See "Notification" above.
- **The heartbeat itself.** Gate A's alert is only as good as something
  emitting a regular span to be missed.

## Secrets

The write token is in OCI Vault. Its OCID is recorded in
`local-infra/infrastructure.yaml` under `vault_secrets.logfire-api-key`
(gitignored — this project keeps zero real OCIDs in committed docs, and
that convention is not relaxed here even though an OCID is an identifier
rather than a secret).

Wiring follows the existing pattern exactly: `docker run` passes
`LOGFIRE_API_KEY_SECRET_OCID`, and `docker-entrypoint.sh` gains one more
block resolving it to `LOGFIRE_API_KEY` via Instance Principals. Nothing
new to design.

**One naming trap to check before wiring.** `LOGFIRE_API_KEY` is our name
for it. Logfire's own SDK reads `LOGFIRE_TOKEN`, and a plain OTLP exporter
authenticates through `OTEL_EXPORTER_OTLP_HEADERS`. Whichever transport we
end up using, the variable it actually reads must be set — a token present
under a name nothing reads fails silently, exports nothing, and looks
exactly like "no traffic yet". Verify the first export lands before
believing it works.

## Pseudonymisation

Subscriber content is not treated as sensitive at this stage — this is a
personal project, interests are not private data, and production may never
happen. Revisit if it does.

What is worth doing anyway, as hygiene rather than privacy engineering:
`chat_id` is a real Telegram user identifier and currently travels through
every log line and would travel through every span. Adding a stable
internal id in `users_db`, and putting only that on spans, keeps the
mapping in our own database and makes traces freely shareable. Small, and
best done before there is a backlog of spans carrying the raw id.

## Order of work

1. ~~Test Gate A~~ **done 2026-08-21** — passed, with a control query in
   both directions. Gate B stays open and no longer blocks.
2. ~~Point the exporter at Logfire~~ **done 2026-08-21** — dual-write with
   Phoenix, tree confirmed by querying spans back out.
3. Emit push outcomes as spans from `news_push._record`.
4. **Alert created 2026-08-21.** Still needs (a) a
   heartbeat span for it to watch, and (b) a notification channel, which
   needs `organization:write_channel` — neither is in place, so today it
   evaluates and tells nobody.
5. ~~A webhook channel~~ **done 2026-08-21** — `Bot Alert`, webhook /
   `slack-legacy` straight at Telegram's `sendMessage`, dedicated
   alert-only bot. Delivery confirmed by forcing a transition.
6. Port criteria 2 and 3 from `incident-monitoring-plan.md` as SQL alerts.
7. Internal id pseudonymisation.
8. Retire the Phoenix VM — last, and only once everything above is live
   and verified.

## Status

Account registered 2026-08-21, token in Vault and working for both export
and query. Gate A verified in full — SQL and scheduler. Steps 1-2 built and
tested (483 tests green). The dead man's switch alert exists and is
active but has nothing to watch and nowhere to report.

**Nothing is deployed.** Phoenix remains the live telemetry backend until
step 8, and Logfire receives nothing from production until a deploy passes
`LOGFIRE_API_KEY_SECRET_OCID` and `LOGFIRE_ENABLED`. Everything Logfire has
seen so far came from local test runs.

The probe spans from the gate test are in the Logfire project under
`service.name = argus-gate-probe`; they are throwaway and can be ignored.
