# Docs index

Three kinds of document, each answering a different question — this
index exists so it's clear which is which at a glance.

> A fourth kind lives in [`docs/analysis/`](analysis/README.md) — research
> and measurement: the news-ranking survey, the cross-domain
> sample-diversity survey, the measured cluster numbers, and the scripts
> that produce them. The split from `plans/` is deliberate: `plans/`
> records what we decided and built; `analysis/` records what we measured
> and what the literature says, most of which will never become code.

## `docs/` (top level) — kept at a stable path, linked externally

Thematically these belong in `docs/current/`/`docs/reference/` below, but
live at `docs/` directly instead: `README.md`, `tools/build_showcase.py`,
and outside links (e.g. a shared showcase page) reference them by exact
path, and a reorg that moves the file breaks those links silently. Moved
back here 2026-08-16 after the `docs/current/`/`docs/reference/` split
did exactly that.

| Doc | Covers |
|---|---|
| `system-overview.md` | Full architecture, request pipeline, design principles. No decision history — read its own Appendix B or the matching plan doc for that. Should stay accurate to the running system or it's actively misleading, not just stale |
| `try-it.md` | User-facing invite doc — how to try the live bot |

## `docs/plans/` — what we decided, what's done, what's still open

Carries history and reasoning, not just current state. Each has its own
Status table.

| Doc | Covers |
|---|---|
| `bot-features-plan.md` | Access control, translation, multi-user sessions, per-user sources, push |
| `context-management-plan.md` | Layered prompt design, the router, settings-dispatch refactor |
| `data-layer-plan.md` | SQLite vs. a shared database — deferred, why |
| `deployment-plan.md` | Containerization, cloud provider, CI/CD |
| `guardrails-plan.md` | The four-layer guardrail design and its incidents |
| `incident-monitoring-plan.md` | What should count as an incident — not designed yet |
| `local-news-cache-plan.md` | Periodic ingestion + local cache, two-stage filtering |
| `model-portability-plan.md` | Swapping/routing LLM models, the guardrail-measurement harness |
| `multi-channel-plan.md` | Adding LINE — on hold |
| `security-plan.md` | The numbered security findings and their status |
| `telemetry-and-testing-plan.md` | Test infrastructure, CI, Phoenix, what's covered vs. not |

## `docs/current/` — what actually exists, right now

No decision history — read `system-overview.md`'s own Appendix B (top
level, see above) or the matching plan doc for that. These should stay
accurate to the running system or they're actively misleading, not just
stale.

| Doc | Covers |
|---|---|
| `ai-news-sources.md` | The live source registry — what's enabled, what needs a key |
| `infrastructure.md` | VM topology, security model, deploy process shape (no real IPs/keys — see `local-infra/infrastructure.yaml`) |

## `docs/reference/` — how to do something

Technique and tool usage, not a decision record.

| Doc | Covers |
|---|---|
| `setup.md` | First-time environment setup, running any piece locally |
| `observability-and-debugging.md` | Diagnosing a live issue — Phoenix traces, hot-patching |
| `local-testing-api-plan.md` | `test_api.py`'s design and how to use it (named `-plan` from before it was built; content is now a reference, not an open plan) |
