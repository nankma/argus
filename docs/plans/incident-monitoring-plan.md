# Incident Monitoring Plan

Nothing here is built yet — this doc exists to capture the goal and the
open design questions before implementation starts, same pattern as the
other `docs/*-plan.md` files. Raised 2026-08-16 while defining the
`qa-engineer` subagent's responsibilities: QA owns *designing* incident
criteria (what condition should actually raise an alert), separate from
whoever eventually builds the monitor that watches for them.

**`healthcheck.py`, described below as one of the two existing narrower
pieces, was RETIRED 2026-08-29** — deleted, not kept. Every reference to
it below is historical context for why this doc's questions were framed
the way they were, not a description of current state. See
`docs/system-overview.md` §C5 and `docs/plans/observability-platform-plan.md`'s
2026-08-29 "healthcheck.py retired" section for what actually exists
now: `news_ingest._pull_source`'s `ingest_source_pull` span (structured,
per-source, queryable from Logfire) plus four planned Logfire alerts,
replacing the "exactly two conditions" this doc describes below. Left
unedited otherwise — bringing the rest of this doc's reasoning
up to date against the new mechanism is `qa-engineer`'s own ongoing
responsibility (this doc is explicitly theirs to maintain), not done as
part of the code change that retired `healthcheck.py`.

## The gap this is meant to close

This project has two existing, narrower pieces that already do part of
this job:

- **`healthcheck.py`** — alerts the admin (via `admin_bot.py`'s channel)
  when `news_ingest`/`news_push`'s periodic jobs stop ticking at all.
  Exactly two conditions today: "has this job run in the last hour."
- **`telemetry_monitor.py`** — alerts on Phoenix's OTLP port becoming
  unreachable or recovering.

Both are real, working, narrow instances of "notice a problem and tell
a human" — but neither is a general system. There's no single place
that defines *what counts as an incident* for this project, and no
process for deciding when a new failure mode should get its own check.
Examples of conditions worth alerting on that aren't covered by anything
today: a news source silently returning zero results for days (not "the
job didn't run," but "it ran and found nothing, repeatedly, which is its
own kind of wrong"); a push subscriber never actually receiving a digest
despite `last_push_at` advancing; the guardrail pass rate silently
drifting down over time without any single measurement run catching it.

## What's actually needed

- **A defined set of incident criteria** — not just "the two things
  `healthcheck.py` happens to check today," but a deliberate list,
  owned and kept current by `qa-engineer` (see its own agent
  definition), the same way it owns the test plan.
- **A monitoring mechanism** that evaluates those criteria against real
  logs/state and decides when to actually raise one — `healthcheck.py`
  and `telemetry_monitor.py` are candidate starting points/components,
  not necessarily the final shape.
- A clear line between "log it" (already happening in a lot of places —
  `news_ingest`/`news_push`'s per-cycle prints) and "this specific
  logged condition should page someone" — most log lines should never
  become an incident; deciding which ones can is the actual design work
  here.

## Open questions — none resolved yet

- Where do incident criteria live — a config/data structure something
  can evaluate programmatically, or a document a human (or QA) reasons
  over periodically? The former scales better; the latter is far
  cheaper to start with at this project's size.
- Severity levels, or just binary alert/don't? `healthcheck.py` today is
  binary. Whether that stays sufficient depends on how many criteria
  this grows to.
- Where does this run — folded into `healthcheck.py`'s existing job-tick
  pattern (`bot.py`'s `JobQueue`), or a separate process/service? Keeping
  it in-process is cheaper; a separate process could reason over more
  than just this bot's own logs (e.g. Phoenix trace data) without adding
  load to the bot's own event loop.
- Does this ever need real log aggregation (logs currently only live in
  `docker logs`, ephemeral, lost on container recreation), or is
  current-state-plus-Phoenix-traces sufficient for the conditions this
  project actually cares about?

## Status

Designed 2026-08-21 for three criteria (below); still not built. Tracked here so it isn't lost — `qa-engineer`
is responsible for turning this into an actual criteria list and a
concrete design next, not for building the monitor itself (that's
implementation work for whoever picks it up once the design exists,
likely `deploy-engineer` or the main thread, following the same
build-something-real-then-verify discipline as everything else in this
project).


---

# 2026-08-21: the incident this document predicted

An abandoned-test-account leak drew billed, undeliverable LLM work every
six hours for eight days and exhausted the DeepSeek balance, taking real
subscribers' pushes down with it. Root cause and fix are in `9bb8f7c`;
this section is about detection, which is what this document is for.

**The condition was already written down here, five days early.** From the
2026-08-16 list of things nothing watches:

> a push subscriber never actually receiving a digest despite
> `last_push_at` advancing

That is precisely what happened, to 19 subscribers, for eight days. The
criterion existed; the check did not. So the useful lesson is not "we
failed to imagine this" — it is that a named criterion with nobody
building it is worth approximately nothing, and the open question below
about "a document a human reasons over periodically" is answered: nobody
reasoned over it.

## Why the obvious alarms would all have stayed silent

Worth recording, because it constrains what the eventual monitor has to
look like:

**The symptom was extra work, not failed work.** Every fetch, generation
and guardrail check succeeded. An error-rate alarm sees nothing.

**It arrived as a slow ramp** — one or two accounts per deploy. A
day-over-day spend threshold set anywhere sensible fires only at the end.

**Its one real error was already normal.** Each leaked account did produce
`BadRequest('Chat not found')`, but that line had been routine since smoke
testing began, so it read as noise rather than as "we are paying to
generate messages for users who do not exist."

**And liveness was never the problem.** `healthcheck.py` checks that jobs
tick. The jobs ticked perfectly, all eight days.

## The three criteria, in the order they are worth building

### 1. `chat_not_found` three times consecutively → disable that subscriber

Turns an unbounded leak into a bounded one. A chat that cannot receive
costs a full digest generation every interval, forever, and generation is
billed even though only the send fails. This alone would have capped the
incident at three cycles per account instead of eight days.

Worth having independently of the test-account fix: a real subscriber who
blocks the bot or deletes their account produces exactly this signature.

### 2. `model_error` on any push cycle → alert immediately

A 402 means every subscriber is down at once; there is no threshold worth
waiting for. This incident ran two hours in that state before a human
noticed by trying the bot by hand.

### 3. delivered / generated below ~80% over 24h → alert

**The metric that would have caught it on day one.** Generation is where
the money goes, delivery is where the value is, and during the incident
that ratio was 3 delivered out of 22 generated, every single cycle, from
the first leaked account onward.

It is a ratio rather than a count, so it does not drift as subscribers
grow, and it was wrong immediately rather than only once the volume
mattered. Everything else here is secondary to instrumenting this one
number.

Recording it needs one new thing: a per-subscriber outcome per cycle
(`delivered` / `nothing_new` / `not_relevant` / `blocked` /
`chat_not_found` / `model_error`) rather than only the print lines that
exist today.

## Token accounting

`users_db.api_budget` already counts **news-source** calls per
`(source, date)`, with history. There is no equivalent for LLM calls,
which are the expensive ones. Mirroring that table's shape —
`(date, caller, calls, input_tokens, output_tokens, cached_tokens)`,
written from a single wrapper so a new call site cannot escape accounting
— makes spend attributable rather than merely visible on an invoice.

`caller` is the load-bearing column: a total that doubles says nothing,
while `push_digest` doubling while everything else stays flat points
straight at the subscriber list. DeepSeek returns
`prompt_cache_hit_tokens` per response, so the cache's contribution comes
free (measured at 256 of 323 prompt tokens on a repeat call, billed around
a tenth).

**Stated plainly: a spend-drift alarm would NOT have caught this** until
near the end, for the slow-ramp reason above. It catches sudden
regressions — a retry loop, a prompt that grew tenfold, a model swap —
which is worth having and is not the same thing.

## A dashboard, and why not a web one

The bot VM is a `VM.Standard.E2.1.Micro`: 954 MB RAM, ~420 MB free with
the container resident, and any new port needs rules in both OCI's
security list and the host's `iptables`. A web dashboard is a service, a
port, TLS and auth — for one viewer.

`/status` in the admin bot instead. The channel is already authenticated
by `ADMIN_CHAT_ID`, reaches the admin wherever they are, adds no attack
surface, and renders as the Telegram HTML the bot already sends:

```
Subscribers   5 total · 2 push-enabled · 3 test
Push (24h)    8 delivered · 0 blocked · 0 chat_not_found · 0 model_error
              delivered/generated 100%
LLM (24h)     push_digest 8 calls / 21k tok    (7d median 8 / 20k)
              classify   24 calls / 61k tok    (7d median 22 / 58k)
Ingestion     272 articles · 100% classified · last cycle 06:41Z
Sources       26 enabled · 0 failing · newsapi due 00:49Z
Taxonomy      16 active · 9 proposed (0 past threshold)
```

Every line is a number this incident would have made obviously wrong, and
the trailing median beside each LLM figure makes drift readable without an
alarm having to fire first.

**Phoenix cannot be the answer here, for a concrete reason**: its UI port
(6006) is unreachable from the bot VM — verified 2026-08-21, OTLP on 4317
works and 6006 returns nothing. Telemetry goes in and nothing in the
running system can read it back. That also answers the open question below
about whether the monitor should live in-process: it has to, because the
out-of-process data store is currently write-only from where the bot sits.

## What this does not address

Detection is not prevention. Test traffic could create real, billable
subscribers; that is fixed structurally (`is_test`) rather than by
watching for it. Not testing against production at all is a separate
piece of work — `docs/plans/dev-environment-plan.md`.

---

# 2026-08-21: how industry does this, and what we should copy

Researched because the shape of the answer decides whether "monitoring"
here means adding services or adding rows.

## The pattern, and its four pieces

Every stack implements the same four, under different names:

| Piece | Azure | Open-source |
|---|---|---|
| telemetry ingestion | App Insights SDK → Log Analytics | OTel SDK → Collector → backend |
| query + alert rule | KQL scheduled query rule | Prometheus rules / Grafana Alerting |
| notification routing | ICM → mail / phone / SMS | Alertmanager receivers |
| **dedup until resolved** | ICM incident dedup | Alertmanager fingerprint + grouping |

The fourth is the only one with real substance, and it rests on a
distinction worth stating plainly: **rule evaluation is stateless — it
runs every interval and yields a boolean — while an alert is stateful,
with an identity and a lifecycle.** The identity is a fingerprint over the
rule plus its subject. "Same query, no new ticket" is fingerprint equality;
"new threshold, new ticket" is severity being part of the fingerprint.

Three things the plain threshold-and-notify model leaves out, all of which
we need:

**A pending period (`for:`).** The condition must hold for N consecutive
evaluations before the alert fires. Criterion 1's "three consecutive
`chat_not_found`" is already this, hand-rolled.

**Inhibition.** A firing high-severity alert suppresses the lower-severity
alerts downstream of it. **We need this on day one**: on 2026-08-21
criterion 2 (`model_error`) and criterion 3 (delivery ratio collapsed to
14%) would both have fired off one root cause. Two alerts per incident is
how an alert channel becomes noise.

**Symptom-based alerting** (Google SRE). Alert on what the user
experiences, not on causes. Criterion 3 is a symptom; criteria 1 and 2 are
causes. That ordering is why criterion 3 is the one that matters most.

## Why Phoenix is not the answer, in three independent ways

Any one of these would be sufficient:

1. **Open-source Phoenix has no alerting at all.** Threshold rules,
   PagerDuty/Opsgenie routing and anomaly detection are Arize AX, the
   commercial product. Phoenix OSS is a trace viewer and an eval harness.
2. **Phoenix cannot see the events the criteria are about.** We emit no
   custom spans — `grep` for `tracer`/`span`/`set_attribute` across
   `agent.py`, `news_push.py`, `news_ingest.py` and `guardrails.py` returns
   nothing. Only `openinference-instrumentation-langchain`'s automatic LLM
   spans reach it. `delivered`, `chat_not_found` and the delivery ratio are
   properties of a *Telegram send*, not of an LLM call. Phoenix has never
   had this data and would not have it even with alerting bolted on.
3. **The store is write-only from where the bot sits** — port 6006
   unreachable from the bot VM, OTLP 4317 fine (verified 2026-08-21).

### "But could we emit outcomes as spans and pull them back by API?"

Technically yes. Phoenix ingests OTLP *spans*, not logs, but a span with
`outcome=chat_not_found` as an attribute is a log in all but name, and
Phoenix exposes both GraphQL and REST (`/v1/spans`) to read them back.
There is even a second always-on host to run a puller on — Phoenix has
its own VM.

Rejected anyway, for a reason that gets stronger the closer you look:

**It saves none of the expensive work.** Emitting the span is the same
instrumentation as inserting the row. The part that costs is deciding
what the outcomes are and making every branch report one.

**And it inverts the dependency.** Phoenix runs on a separate VM
*precisely because* "its memory use can spike hard under load; isolating
it means a spike can't take the bot down"
(`docs/current/infrastructure.md`). That is a recorded judgement that
Phoenix is the less reliable of the two. Routing the alarm through it
makes the watchdog depend on the component we already isolated for being
unstable — and its failure mode is silence, which is the one failure mode
a monitor must not have.

Secondary but real: 6006 is unreachable from the bot VM, Phoenix has its
own auth to hold credentials for, the `phoenix.Client()` helper lives in
the full `arize-phoenix` package that imports pandas (see `CLAUDE.md`),
and span retention on a small disk is far heavier than outcome rows.

### Is Phoenix the right tool for what we DO use it for?

Mostly yes, narrowly. The case against is fair — we use none of its
datasets, experiments, evals, annotations or playground, and plain logs
could carry a prompt if we wrote it out ourselves.

What it uniquely gives is the **tree**: agent → tool call → LLM call →
tool call, nested, with token counts and latency per node. Flat log lines
cannot express that nesting, and it is exactly what answers "why did the
agent loop three times". We pay nothing for it —
`openinference-instrumentation-langchain` produces all of it with no code
of ours.

So: keep it for diagnosis, don't grow it into the alarm path, and don't
add a log service either — that would be a third store for a system with
one viewer, when SQLite already holds everything else. If Phoenix ever
stops being worth its VM, what replaces it is logging the prompt tree
ourselves, which is real work worth avoiding while it is free.

An SSH tunnel fixes *viewing*, which is real and worth keeping: when
something has already gone wrong and the question is "what exactly did that
digest prompt contain", Phoenix is the best tool we have and `/status`
will never replace it. But alerting has to run when nobody is watching, and
an alarm that only fires while a tunnel is open is not an alarm.

## Why not Prometheus + Alertmanager + Grafana

Two reasons, in order of weight:

**The expensive part is instrumentation, not machinery.** Nothing can
alert on "this digest was generated but not delivered" until the code says
so in a queryable form. That work is identical whether the sink is
Prometheus or SQLite — and having done it, the remaining state machine is
a table and about forty lines (upsert on breach; notify once when a
pending period elapses; notify resolved and clear when it stops). That is
Alertmanager's core, and it is smaller than the config needed to run
Alertmanager.

**And it does not fit.** The bot VM is a `VM.Standard.E2.1.Micro` — 954 MB
total, ~420 MB free with the container resident. Collector + Prometheus +
Grafana + Alertmanager exceeds that before storing a single sample, and
each wants a port, an OCI security-list rule, an `iptables` rule, TLS and
auth. For one viewer.

The mapping we use instead:

| App Insights | Here |
|---|---|
| Log Analytics table | SQLite `push_outcomes`, `llm_usage` |
| KQL | SQL |
| scheduled query rule | an evaluation pass on the push tick |
| ICM + dedup | an `alert_state` table keyed by fingerprint |
| notification channel | the admin bot (already authenticated by `ADMIN_CHAT_ID`) |
| dashboard | `/status` |

**The dashboard need not run on the VM at all.** `subscribers.db` is small;
copying it to a dev box and rendering locally costs production nothing —
the same move as tunnelling to Phoenix. `/status` answers "is it healthy
right now" from a phone; a local view answers "what has the trend been".

## Why the existing log lines cannot be the data source

They are not missing. `news_push.py` already printed every branch —
not-due, no-interests, due, no-new-articles, none-relevant, sent, blocked,
failed. Essentially the outcome enum this document asks for. Four things
stop them from being what an alarm reads:

- **No timestamp of their own.** Only the tick line carries
  `now.isoformat()`; the per-subscriber lines carry none. Docker's
  json-file driver stamps each line, so `docker logs -t` recovers it — but
  only through Docker.
- **Free text.** `sent digest -- 3 of 8 candidate(s) appeared in it` has to
  be regex-parsed, and rewording a print silently stops the monitor
  counting. It does not error; it just goes quiet.
- **Unreadable from inside the container.** `docker logs` is a host-side
  command. The evaluator has to run in-process (see above: the
  out-of-process store is write-only from here), and an in-process
  evaluator cannot read it. Mounting the Docker socket to work around this
  would hand the container host-level control — not a trade worth making
  for a log read.
- **Destroyed by our own deploys.** A container swap is a `docker rm`; the
  json-file log goes with it. There is no `--log-opt max-size` either, so
  between deploys it grows unbounded on a small disk.

So the outcome is now written **both** ways, from a single call site
(`news_push._record`) that prints and inserts together, so a new branch
cannot do one without the other. The prints stay exactly as useful as they
were for reading a specific cycle by hand.

## Correction: the leak was roughly 24x worse than "every six hours"

Found while instrumenting this, and it revises the incident's arithmetic.

`PUSH_TICK_SECONDS = 900` — the job ticks every 15 minutes and decides per
subscriber whether an interval has elapsed. `users_db.record_push` is what
advances `last_push_at`. On the delivery-failure path it was never reached:
the exception propagated to the per-subscriber catch-all, which `continue`d
past it. `is_subscriber_due` returns `True` for a `NULL` `last_push_at`.

Together: a subscriber whose chat cannot receive **regenerates a full
digest on every 15-minute tick, forever** — never every six hours. Three
LLM calls (`resolve_interest_categories`, `write_push_digest`,
`is_output_on_topic`) × 96 ticks/day × 19 leaked accounts is on the order
of 5,000 calls/day, which is the scale that actually empties a balance in
eight days. The earlier "~162 calls/day" estimate counted digests at their
nominal interval and is wrong for exactly this reason.

**Fixed 2026-08-21**, in two parts, because there were two independent
defects and either alone would have left the leak open:

**(A) Any failure after generation now advances `last_push_at`.** A
`generated` flag is set the moment `write_push_digest` returns; every
failure handler below that point records an empty push. The rule it
encodes: *once we have paid for a digest, the next attempt is a full
interval away, whatever went wrong afterwards.* This alone takes the
retry rate from every 15 minutes to every interval.

A model error *before* generation deliberately does not advance — nothing
was billed, and a transient provider blip should not cost the subscriber
their whole cycle. That asymmetry is the point of the flag; a blanket
"always advance on failure" would be simpler and wrong.

**(B) Three consecutive undeliverable cycles turn push off**
(`news_push.UNREACHABLE_STRIKES`). Only `delivered` breaks the streak and
only `chat_not_found` extends it; every other outcome is skipped rather
than treated as either — a `nothing_new` cycle attempts no send, so it is
evidence of nothing, and letting it reset the count would let a dead chat
with one quiet cycle in three bill digests forever.

Three rather than one because turning a real subscriber off is the more
expensive mistake: they simply stop receiving news, with no error to
notice. And only `push_enabled` is cleared — interests and language
survive, so a user who blocked the bot and later unblocks it turns push
back on and continues.

The exposure was not hypothetical once the test accounts were gone: a real
user who blocks the bot produces the identical signature.

**The admin is not notified yet.** A strike-out records a `disabled`
outcome and prints, and nothing reads either. That is the alerting work,
still to be designed.

## Status: step 1 built

`push_outcomes` (one row per subscriber per cycle that reached an
outcome), `news_push._record`, and the queries the three criteria need
(`push_outcome_counts`, `push_delivery_ratio`, `recent_outcomes_for`,
`prune_push_outcomes`) landed 2026-08-21. 17 tests.

Two design points worth keeping:

- **Failures are classified by which call raised, not by what the message
  says.** `_model_call` wraps exactly the LLM calls, so a provider
  rewording an error cannot silence criterion 2. The one place a message
  must be matched is separating a dead chat from malformed HTML — both
  arrive as `BadRequest` — and there the fallback is `cycle_failed`, so an
  unrecognised error under-reports criterion 1 rather than wrongly
  disabling a live subscriber.
- **`not_due` is not recorded.** It fires for every subscriber on every
  tick, and `healthcheck.py` already answers whether the job is running.

Criterion 1's *action* also landed (see the correction above): three
consecutive undeliverable cycles turn push off. Its *alert* did not —
nothing tells the admin it happened.

Still to build: the `alert_state` machine (with pending period and
inhibition), the admin-bot notifier, `llm_usage`, and `/status`.

### A note on verifying this one

Both fixes were mutation-checked — disabled in place, suite re-run,
confirmed the new tests go red, then restored. Worth recording because the
first attempt at that check reported success while silently changing
nothing (a piped heredoc that never landed), and three of the tests passed
against the mutated code for a reason that had nothing to do with the fix:
`get_push_enabled` returns `False` for a subscriber row that does not
exist, so `assert ... is False` held whether or not anything was turned
off. Each of those tests now creates the row first. A test that cannot
fail is worse than no test, and neither pytest nor a green run says which
kind you have.
