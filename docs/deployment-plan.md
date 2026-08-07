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
| 4 | Refactor `main()` from interactive CLI to a headless service | Not started — **blocking**, see below |

## The blocking architectural gap

`agent.py`'s `main()` is an interactive REPL: it calls `input()` in a loop
and prints responses to stdout. That does not work in a container running
under Kubernetes — a typical pod has no attached terminal, so `input()`
blocks forever waiting on stdin that will never arrive. `docker run -it`
locally can fake this for manual testing, but a real Kubernetes `Deployment`
cannot.

This means "containerize the agent" isn't just a Dockerfile — `agent.py`
needs a second entry point (or a replacement for `main()`) that serves
requests instead of reading a terminal.

**Decided: the headless trigger is a Telegram bot** — the agent will be
hooked up to Telegram rather than exposing a generic HTTP API. Still open,
because it changes the Kubernetes shape:

- **Polling mode** (`getUpdates` long-polling against Telegram's API) — the
  bot runs as a persistent background loop, broadly similar in shape to the
  current CLI's `while True` loop, just reading from Telegram instead of
  stdin and replying via `sendMessage` instead of `print()`. No public
  endpoint needed, no TLS/webhook registration — simpler to deploy (a plain
  `Deployment` with no `Service`/ingress needed), but requires the pod to be
  a durable long-running process rather than a request-scoped handler.
- **Webhook mode** — Telegram pushes updates to a public HTTPS endpoint we
  host. Fits the "HTTP API behind a Kubernetes `Service`/ingress" shape more
  conventionally, but needs a registered public URL + TLS, more moving
  parts for a Kubernetes deployment.

Not decided which. Either way:

- `build_agent`/`run_agent`'s existing dependency injection (model +
  callbacks as parameters, see `docs/telemetry-and-testing-plan.md`) should
  carry over unchanged — the new entry point just calls them differently
  than `main()` does, it shouldn't need to change `agent.py`'s core logic.
- Will need a Telegram bot token (via BotFather) — another secret to manage
  alongside `DEEPSEEK_API_KEY`, see Secrets management below.

The CLI REPL likely stays too, for local development — this would be an
additional entry point, not a replacement, unless told otherwise.

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
