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

Not designed, not built. Tracked here so it isn't lost — `qa-engineer`
is responsible for turning this into an actual criteria list and a
concrete design next, not for building the monitor itself (that's
implementation work for whoever picks it up once the design exists,
likely `deploy-engineer` or the main thread, following the same
build-something-real-then-verify discipline as everything else in this
project).
