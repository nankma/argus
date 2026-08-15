# Docs index

Three kinds of document, each answering a different question — this
index exists so it's clear which is which at a glance.

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

No decision history — read `system-overview.md`'s own Appendix B or the
matching plan doc for that. These should stay accurate to the running
system or they're actively misleading, not just stale.

| Doc | Covers |
|---|---|
| `system-overview.md` | Full architecture, request pipeline, design principles |
| `ai-news-sources.md` | The live source registry — what's enabled, what needs a key |
| `infrastructure.md` | VM topology, security model, deploy process shape (no real IPs/keys — see `local-infra/infrastructure.yaml`) |

## `docs/reference/` — how to do something

Technique and tool usage, not a decision record.

| Doc | Covers |
|---|---|
| `setup.md` | First-time environment setup, running any piece locally |
| `observability-and-debugging.md` | Diagnosing a live issue — Phoenix traces, hot-patching |
| `local-testing-api-plan.md` | `test_api.py`'s design and how to use it (named `-plan` from before it was built; content is now a reference, not an open plan) |
| `try-it.md` | User-facing invite doc — how to try the live bot |
