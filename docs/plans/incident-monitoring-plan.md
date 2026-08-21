# Incident Monitoring Plan

Nothing here is built yet — this doc exists to capture the goal and the
open design questions before implementation starts, same pattern as the
other `docs/*-plan.md` files. Raised 2026-08-16 while defining the
`qa-engineer` subagent's responsibilities: QA owns *designing* incident
criteria (what condition should actually raise an alert), separate from
whoever eventually builds the monitor that watches for them.

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
