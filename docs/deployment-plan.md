# Deployment Plan

Goal: run the final version of this agent in Docker, orchestrated via
Kubernetes, deployed to a cloud service. Nothing here is built yet — this
doc exists to capture the goal and the open questions before implementation
starts, same pattern as `docs/telemetry-and-testing-plan.md`.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Containerize `agent.py` (Dockerfile) | Not started |
| 2 | Kubernetes manifests (Deployment, Service, Secret, etc.) | Not started |
| 3 | Cloud provider / hosting target | Not decided |
| 4 | Refactor `main()` from interactive CLI to a headless service | Done — Telegram bot, polling mode |

## The blocking architectural gap

`agent.py`'s `main()` is an interactive REPL: it calls `input()` in a loop
and prints responses to stdout. That does not work in a container running
under Kubernetes — a typical pod has no attached terminal, so `input()`
blocks forever waiting on stdin that will never arrive. `docker run -it`
locally can fake this for manual testing, but a real Kubernetes `Deployment`
cannot.

This means "containerize the agent" isn't just a Dockerfile — `agent.py`
needed a second entry point that serves requests instead of reading a
terminal.

**Done: `bot.py` — a Telegram bot in polling mode.** Went with polling over
webhooks specifically because there's no public HTTPS endpoint or TLS setup
to stand up yet, and polling works identically now (local dev) and later
(a plain Kubernetes `Deployment`, no `Service`/ingress needed) — webhook's
main advantage only matters once there's an actual public domain to
deploy to, which isn't decided yet (see Cloud provider below). Can revisit
if that changes.

- `bot.py` imports `build_agent`, `run_agent`, `setup_telemetry` from
  `agent.py` **unchanged** — confirms the DI design held up exactly as
  intended, no changes needed to `agent.py`'s core logic for a second
  entry point to reuse it.
- Per-chat conversation history is an in-memory `dict[chat_id, messages]`
  — lost on restart, same non-persistence as the CLI's `messages` list.
  Revisit if that turns out to matter.
- `run_agent` is synchronous; python-telegram-bot's handlers are async. To
  avoid blocking the bot's event loop during a real LLM/tool call,
  `handle_message` runs it via `asyncio.to_thread(run_agent, ...)` rather
  than calling it directly or adding a separate async code path to
  `agent.py`.
- Telegram rejects messages over 4096 characters — a real constraint hit
  immediately, since this agent's trend reports can easily exceed that.
  `split_for_telegram()` chunks long replies (preferring to break on a
  newline), sends each as a separate message. Covered by
  `tests/test_bot.py` (pure function, no Telegram API needed to test it).
- Verified end-to-end for real: confirmed the bot process was alive and
  the token valid via Telegram's `getMe` API, then had a human actually
  message the live bot on Telegram and confirmed a real reply came back —
  not just "the code runs without error."
- Needs `TELEGRAM_BOT_TOKEN` (via BotFather) alongside `DEEPSEEK_API_KEY` —
  see Secrets management below.

The CLI REPL (`agent.py`'s `main()`) stays as an additional entry point for
local development, not replaced.

## How telemetry fits into this

The lightweight-client decision forced by the Smart App Control / pandas
issue (see `docs/telemetry-and-testing-plan.md` item 3) turns out to line up
well with the Kubernetes case anyway: you would never want to bundle the
full `arize-phoenix` package (with its pandas-dependent local UI/server)
into every agent pod's image. The right shape for Kubernetes is the same
shape being built now — agent pods use the lightweight `arize-phoenix-otel`
client to export spans, and Phoenix's server runs as its own single
service (its own `Deployment` + `Service` in the cluster) that all agent
replicas send traces to over OTLP. No architecture change needed later for
this reason — just more Kubernetes manifests.

## Open questions

- **Cloud provider** — not decided (AWS/GCP/Azure/other). Affects which
  Kubernetes flavor (EKS/GKE/AKS/self-managed) and how secrets are managed.
- **Secrets management** — `DEEPSEEK_API_KEY` (and any telemetry
  credentials) currently come from a local env var. In Kubernetes this
  becomes a `Secret` object at minimum; a cloud-native secret manager
  (e.g. AWS Secrets Manager, GCP Secret Manager) integrated via the
  cluster's CSI driver is the more production-grade option — not decided
  which.
- **Base image** — this project uses a conda/Miniforge environment
  (`environment.yml`) locally. A container image doesn't need conda at all
  (no multiple envs to isolate inside a single-purpose container) — worth
  deciding whether the Dockerfile installs the same dependencies via a
  plain `pip install` from a generated requirements list, or keeps using
  conda/mamba inside the image for consistency with local dev. Not decided.
- **CI/CD overlap** — `docs/telemetry-and-testing-plan.md` item 4 (test
  CI) and this deployment work are related but distinct: one runs `pytest`
  on every push, the other builds/pushes a container image and deploys it.
  They'll likely share a GitHub Actions workflow file eventually, but
  neither is started yet.
