# Telemetry catalog: every span reaching Logfire, and what reads it

No decision history here — read `docs/plans/observability-platform-plan.md`
for how/why this got built, and `docs/system-overview.md` §C4/§C5 for the
architecture narrative. This doc describes **what the code currently
emits**: every span this project's own source actually sends to Logfire,
its real attribute keys, and which alert (if any) reads it. Attribute
keys and `level`/`message` behavior were cross-checked against a live
`records` query (see `docs/reference/observability-and-debugging.md` for
the technique) to catch any drift from what a docstring claims, but the
inventory itself — which spans exist, from which module — is the code,
not a point-in-time query result. Keep this in sync with the code when a
span is added, renamed, or removed; a query re-check is only for
catching attribute-shape drift, not for deciding what belongs here.

## Service name

The code sets exactly one: `myfirstagent`, via
`Resource.create({"service.name": SERVICE_NAME})` in `agent.py`'s
`setup_telemetry()` (also forced onto `OTEL_SERVICE_NAME` before any
provider is built, so every path — Phoenix-driven or Logfire-only —
agrees). Every span below is under this service; nothing in the current
codebase produces any other `service_name`.

## `level` — what the two values mean

Only two values have ever been observed: **9** (INFO) and **17** (ERROR),
the standard OpenTelemetry severity-number scale (9 = info range start, 17
= error range start). This project never sets level explicitly — every
span here uses the plain OTel API (`tracer.start_as_current_span`), not
Logfire's own logging SDK (`logfire.info(...)` etc., deliberately not used
— see `docs/plans/telemetry-and-testing-plan.md`'s reasoning on why the
plain OTel API was chosen). Logfire derives `level=17` automatically
whenever a span's `otel_status_code` is `ERROR` (i.e. an exception
propagated out of the `with tracer.start_as_current_span(...)` block);
everything else defaults to `level=9`. There is no `level=13` (WARN) or
anything else in this codebase's own spans — a span is either "completed
fine" or "raised."

## `message` — always just the span name

Every span's `message` column exactly equals its `span_name` (confirmed
across every span type sampled). This is Logfire's own default rendering
when a span carries no explicit message template — again a consequence of
using the plain OTel API rather than Logfire's SDK. **The real content is
in `attributes`, never in `message`** — don't query or display `message`
expecting it to carry anything beyond the span name.

## This project's own spans (`myfirstagent` service)

| `span_name` | `otel_scope_name` | Emitted by | Attributes (verified keys) | Cadence | Read by |
|---|---|---|---|---|---|
| `argus_heartbeat` | `argus.news_push` | `news_push._emit_heartbeat` | `heartbeat.job` (`"push_tick"`), `heartbeat.push_enabled_subscribers` (int) | Once per push cycle (every `PUSH_TICK_SECONDS`=900s), unconditionally | `argus bot liveness` reads for ANY span from the service in 30min, not this one specifically — but this is what keeps that generic dead-man's-switch satisfied even during an ingest-only outage |
| `push_outcome` | `argus.news_push` | `news_push._record` | `push.subscriber` (opaque id), `push.outcome` (`delivered`/`nothing_new`/`model_error`/others per code — only `delivered`/`nothing_new` seen in the last 30 days), `push.generated` (bool), `push.detail` (free text) | Once per subscriber per push cycle | `argus model errors` (`push.outcome='model_error'`), `argus delivery ratio` (`delivered` vs `push.generated` ratio) |
| `html_validation_attempt` | `argus.news_push` | `news_push._emit_html_validation_attempt` | `push.subscriber`, `topic`, `attempt` (1-3), `valid` (bool), `reason` (only present when `valid=false`, e.g. `"disallowed tag <hr>"`) | Once per HTML-validation retry attempt, every attempt (not just failures) | *(planned, not built)* `argus html validation retry`/`argus html validation exhausted` |
| `ingest_heartbeat` | `argus.news_ingest` | `news_ingest._emit_heartbeat` | `heartbeat.job` (`"ingest_tick"`) | Once per ingest cycle (every `INGEST_TICK_SECONDS`=900s), unconditionally, before any per-source work | `argus ingest liveness` |
| `ingest_source_pull` | `argus.news_ingest` | `news_ingest._pull_source` | `pull.source`, `pull.outcome` (`not_due`/`budget_exhausted`/`success`/`failed`), `pull.expected_interval_hours`, `pull.sections_attempted`, `pull.sections_failed` | Once per source per ingest cycle, for every registered source regardless of outcome | `argus ingest pull stalled`, `argus ingest pull failures`, `argus ingest source stale`. **Zero spans observed in the 30-day window as of 2026-08-30** — the code shipped 2026-08-29 but the ingest job's post-deploy dispatch hang (see this project's `project-ingest-hang-post-deploy-20260825` memory) has prevented it from ever running successfully since |
| `fetch_source` | `news_sources` | `news_sources.traced_fetch` | `source_key`, `section` (query-capable sources) or `query` (RSS), `restricted` (bool), `article_count` (int), `error` (only present on failure, redacted of API keys — see `_redact`) | Once per section fetch attempt — nests as a child span inside `ingest_source_pull` (or under `search_news` when called from the agent's on-demand tool) | Not read by any alert directly today — the closest is `argus ingest pull failures`, which reads the coarser `ingest_source_pull.pull.outcome='failed'` instead. This is the natural place to add a section-level failure-count alert later if `ingest pull failures`' source-level granularity ever turns out too coarse |

## Auto-instrumented spans (not hand-written — `openinference-instrumentation-langchain`)

Everything under `otel_scope_name = 'openinference.instrumentation.langchain'`
is produced automatically by `auto_instrument=True` wiring up the agent's
LangGraph/LangChain execution (`agent.py`'s `setup_telemetry()`) — this
project never names these spans itself. Seen in the last 30 days:
`ChatDeepSeek`, `RunnableSequence`, `PydanticToolsParser`, `tools`,
`model`, `search_news`, `LangGraph`, `save_note`, `FakeToolCallingModel`
(test-only). None of these are read by any alert today — `argus model
errors` reads `push_outcome`'s own `model_error` outcome instead of
querying these directly, since that's already the point where a model
failure is known to have affected an actual subscriber, not just an LLM
call in isolation. Useful for manual trace debugging (following one
request's full chain through Logfire's UI) but not part of the alerting
surface as designed.

## Every alert, and the span(s)/attribute(s) it depends on

Full query text and ids: `local-infra/infrastructure.yaml`'s
`logfire.alerts` (gitignored — real ids/queries live there, not here).
This table is the quick cross-reference; that file is the source of truth.

| Alert | Span(s) | Condition |
|---|---|---|
| `argus bot liveness` | any span, `service_name='myfirstagent'` | none in 30 min — dead man's switch for the whole process, not any one job |
| `argus model errors` | `push_outcome` | `push.outcome='model_error'` in 30 min |
| `argus delivery ratio` | `push_outcome` | `delivered` count < 80% of `push.generated=true` count, 24h, only once ≥5 generated |
| `argus ingest liveness` | `ingest_heartbeat` | none in 30 min |
| `argus ingest pull stalled` | `ingest_source_pull` | no `pull.outcome='success'` anywhere in 1h |
| `argus ingest pull failures` | `ingest_source_pull` | more than 5 `pull.outcome='failed'` in 30 min |
| `argus ingest source stale` | `ingest_source_pull` | per source (`GROUP BY pull.source`): time since last `pull.outcome='success'` exceeds `3 × pull.expected_interval_hours` |
| `argus html validation retry` *(planned)* | `html_validation_attempt` | `valid=false` AND `attempt` in (1,2) |
| `argus html validation exhausted` *(planned)* | `html_validation_attempt` | `attempt=3 AND valid=false` |

**Known gap, surfaced 2026-08-30**: no alert currently watches
`pull.outcome='budget_exhausted'` specifically. A source hitting its own
daily API cap (`news_ingest._DAILY_CAPS` — currently `perigon`, `newsapi`)
is a deliberate self-throttle, not a failure, so it's correctly excluded
from `argus ingest pull failures`. But nothing pages if a source stays
budget-capped far longer than expected either (that would eventually
surface via `argus ingest source stale` once enough time passes relative
to that source's own interval, but there's no dedicated, faster signal for
"we're throttling ourselves more than intended" specifically). Not built —
flagged here so it isn't lost, add a query on `pull.outcome='budget_exhausted'`
if this granularity turns out to matter.
