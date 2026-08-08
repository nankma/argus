# Deployment Plan

Goal: run the final version of this agent in Docker, orchestrated via
Kubernetes, deployed to a cloud service. Nothing here is built yet — this
doc exists to capture the goal and the open questions before implementation
starts, same pattern as `docs/telemetry-and-testing-plan.md`.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Containerize `agent.py` (Dockerfile) | Done — see below |
| 2 | Kubernetes manifests (Deployment, Service, Secret, etc.) | Not started |
| 3 | Cloud provider / hosting target | **Decided and live: Oracle Cloud Always Free** — see below |
| 4 | Refactor `main()` from interactive CLI to a headless service | Done — Telegram bot, polling mode |
| 5 | Security review + hardening | Reviewed — see `docs/security-plan.md`. Secrets management (finding 2) done: OCI Vault + Instance Principals, live and verified |

## Live deployment: Oracle Cloud Always Free

Running today on **`VM.Standard.E2.1.Micro`** (AMD, 1/8 OCPU, 1GB RAM),
region `us-sanjose-1`, Ubuntu 24.04 Minimal. Not the originally-planned
Ampere A1 shape — switched after repeatedly hitting
`Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1`
(a well-known, long-running Always Free A1 capacity shortage; `us-sanjose-1`
also only has a single AD, so "try another AD" wasn't an option, and
Always Free A1 eligibility is home-region-only — creating it in a
different region would have meant real charges). E2.1.Micro has no such
capacity problem and is reliably available.

What's running: `combined_bot.py` (see `docs/bot-features-plan.md` item 1
and `CLAUDE.md`) in a single Docker container, `--restart unless-stopped`,
using the shared `myfirstagent-data` volume for `subscribers.db` — the
same setup verified locally, rebuilt and redeployed on the VM. **Verified
end-to-end for real**: sent a live Telegram message to `@mnkInfo_bot` and
got a real reply back through the deployed instance, not just "the
container is Up."

Setup notes for whoever revisits this:
- Public IP assignment during instance creation got stuck (the
  "Automatically assign public IPv4 address" toggle stayed disabled even
  with a public subnet selected) — a known OCI console bug where inline
  subnet creation during the instance wizard doesn't propagate subnet-type
  state correctly. Fixed by creating the VCN/public subnet separately via
  the VCN Wizard first (fully provisioned/saved), then selecting the
  already-existing VCN/subnet from the dropdown in Create Instance instead
  of creating either inline.
- Added a 1GB swap file (`/swapfile`, via `fallocate`/`mkswap`/`swapon`,
  persisted in `/etc/fstab`) as OOM insurance — the VM only has 1GB RAM
  and no swap existed by default. The running container uses roughly
  130-160MB at idle, well within budget, but swap is cheap insurance
  against spikes.
- Docker installed via the official `get.docker.com` script; image built
  directly on the VM (native x86_64) after `scp`-ing the source files over
  — sidesteps any cross-architecture build complexity entirely, and avoids
  needing to set up git credentials on the VM for a private repo clone.
- Same Docker-volume-ownership gotcha as local testing: a fresh named
  volume is root-owned by default, and the non-root `mambauser` in the
  container can't write to it until `chown`'d — see `docs/security-plan.md`
  finding 13's spirit, fixed the same way as the local Docker setup was.

**Revisit later if Ampere A1 capacity frees up**: 2 OCPU/12GB is a lot more
headroom than E2.1.Micro's 1/8 OCPU/1GB. `bot.py`/`admin_bot.py` could be
split back into two containers at that point if isolation becomes more
valuable than the memory savings `combined_bot.py` was built for. Not
urgent — the current setup is stable and verified working.

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

**Fixed as part of the Dockerfile work:** `agent.py`'s `PHOENIX_ENDPOINT`
was hardcoded to `http://localhost:4317`, which only works for local dev —
inside a container, `localhost` is the container's own network namespace,
not wherever Phoenix's Docker container/Kubernetes service actually runs.
Now reads from a `PHOENIX_ENDPOINT` env var (defaulting to the old
`localhost` value, so nothing changes for local dev) — deploying will need
to set it to wherever Phoenix's service actually is (e.g. a Kubernetes
service DNS name once that manifest exists).

### Live Phoenix deployment: dedicated Oracle VM, no Docker

A second Always Free instance, `myfirstagent-phoenix`
(`VM.Standard.E2.1.Micro`, same VCN/subnet as the bot VM, same SSH key),
runs Phoenix — kept separate from the bot's VM deliberately, since
Phoenix's memory use can spike hard under load (community reports of
idle ~235MB ballooning to 11GB+ during traffic bursts, since it buffers
spans in memory unboundedly relative to ingest rate) — isolating it means
a Phoenix memory spike can't take the bot down with it.

**Not run in Docker, unlike everything else in this project** — and this
is a deliberate exception, not a lapse in the conda-forge/Docker
convention. The original reason `arize-phoenix` (the full package, not
`-otel`) was avoided was Windows Smart App Control blocking pandas'
compiled DLL (`docs/telemetry-and-testing-plan.md` item 3) — a
Windows-only problem that doesn't exist on this Linux VM. Also,
conda-forge's `arize-phoenix` package turned out to be badly stale
(version 0.1.0, vs. the real current ~19.x on PyPI) — so this one host
uses a plain Python venv + `pip install arize-phoenix` instead, which is
fine since it's a single-purpose ops VM, not part of the main project's
`environment.yml`-tracked environment. Run as a systemd service
(`/etc/systemd/system/phoenix.service`, `phoenix serve`) rather than a
container — one less moving part (no Docker daemon overhead) on an
already memory-constrained free-tier VM.

**Runs on-demand, not always-on.** The systemd unit is deliberately
`disabled` (won't start on boot) — start it only when actively debugging
an agent issue (`sudo systemctl start phoenix`), stop it when done
(`sudo systemctl stop phoenix`). Reduces both cost/resource exposure and
the OOM-under-load risk to zero when not in active use.

**Secured two ways, not just one:**
1. Phoenix's native auth is enabled (`PHOENIX_ENABLE_AUTH=true`,
   `PHOENIX_SECRET` set to a random 64-char value) — **critically, also
   overrides `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`**, since Phoenix's
   default admin login is the well-known `admin`/`admin` and enabling
   auth alone does *not* change that default — a real gotcha caught by
   reading Phoenix's own source (`phoenix.auth.DEFAULT_ADMIN_PASSWORD`),
   not documented prominently.
2. Port 6006 (web UI) and 4317 (OTLP) are **not opened in the OCI
   security list at all** — confirmed by testing from an external
   network that the port is unreachable. Access is via SSH tunnel only
   (`ssh -i <key> -L 6006:localhost:6006 ubuntu@<phoenix-vm-ip>`, then
   browse `localhost:6006`) — so even a hypothetical auth bypass still
   requires SSH key access to reach the port at all. The systemd unit
   file itself is also locked to `600`/root-owned, since it holds
   `PHOENIX_SECRET` and the admin password in plaintext.

**Not yet done**: the bot container isn't pointed at this Phoenix
instance yet (`PHOENIX_ENABLED`/`PHOENIX_ENDPOINT` aren't set on
`myfirstagent-bot`). Given Phoenix is only running some of the time,
worth confirming OpenTelemetry's exporter behaves as expected (fails
silently/retries in the background rather than blocking or crashing the
bot) when the endpoint is unreachable, before turning this on for the
live bot — not yet verified for this specific setup.

## The Dockerfile

`FROM mambaorg/micromamba:latest` — stays on conda-forge via micromamba
(same tool the CI workflow already uses) rather than introducing a second
pip requirements file that could drift from `environment.yml`. Installs
into the base image's `base` environment (not a named `myfirstagent` env —
unlike the local dev machine, a single-purpose container doesn't need
named-environment isolation, so there's nothing to gain from replicating
that scheme inside it).

- `CMD ["python", "combined_bot.py"]` — runs both Telegram bots (info +
  admin) in a single process/container, not the CLI (`agent.py`'s
  `input()` loop still can't run in a container) and not two separate
  containers either. Chosen specifically for small-RAM Always Free shapes
  (e.g. Oracle's `VM.Standard.E2.1.Micro`, 1GB) where running `bot.py` and
  `admin_bot.py` as two OS processes would each independently load
  LangChain/python-telegram-bot into memory — confirmed via `docker
  stats` that the combined container uses ~135MB vs. close to double for
  two. `bot.py`/`admin_bot.py` can still run standalone
  (`docker run ... myfirstagent-bot python bot.py`) if a future
  higher-RAM shape makes splitting them back into two containers
  preferable for isolation. See `docs/bot-features-plan.md` item 1 and
  `CLAUDE.md`'s "Running both bots in one process" section.
- `DEEPSEEK_API_KEY` / `TELEGRAM_BOT_TOKEN` / `ADMIN_CHAT_ID` /
  `ADMIN_BOT_TOKEN` / `PHOENIX_ENABLED` / `PHOENIX_ENDPOINT` /
  `SUBSCRIBERS_DB_FILE` are all runtime env vars (`docker run -e`, later a
  Kubernetes `Secret`/`ConfigMap`) — nothing is baked into the image.
- `.dockerignore` excludes `tests/`, `docs/`, `.git/`, etc. from the build
  context.
- **Image size: 907MB.** Larger than a `pip` + `python:slim` approach would
  produce — the tradeoff deliberately made for staying consistent with the
  project's conda-forge-everywhere convention (local dev, CI, and now the
  container all read the same `environment.yml`, zero drift risk). Worth
  revisiting later if image size becomes an actual problem (multi-stage
  build copying just the final env, or reconsidering pip for the container
  specifically) — not a problem yet, just a known tradeoff.
- **Verified for real, not just "the build succeeded":** ran the built
  image with real credentials, confirmed `python bot.py` actually executing
  inside the container via `docker top` (not just `Up` status, which only
  means the container didn't immediately exit), then had a human message
  the live bot again and confirmed a real reply came back through the
  containerized version specifically.

## Open questions

- ~~**Cloud provider** — not decided~~ **Decided: Oracle Cloud**, live on
  `VM.Standard.E2.1.Micro` (see above). Kubernetes flavor is now an open
  sub-question — OCI's managed option is OKE (Oracle Kubernetes Engine);
  not decided whether that's worth it at this scale vs. staying on plain
  Docker on the VM.
- ~~**Secrets management** — not implemented~~ **Done.** `DEEPSEEK_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_CHAT_ID` are fetched from
  OCI Vault via Instance Principal auth at container startup
  (`docker-entrypoint.sh`) — the container only ever receives
  `*_SECRET_OCID` values now, no plaintext secrets in `docker run -e` or
  `docker inspect`. See `docs/security-plan.md` finding 2 for the full
  setup and a real policy-naming bug hit and fixed along the way. Telemetry
  credentials aren't in Vault yet since Phoenix isn't deployed to the cloud
  yet — revisit when it is.
- **CI/CD overlap** — `docs/telemetry-and-testing-plan.md` item 4 (test
  CI) and this deployment work are related but distinct: one runs `pytest`
  on every push, the other builds/pushes a container image and deploys it.
  The existing `.github/workflows/ci.yml` doesn't build/push the Docker
  image yet — that's the natural next CI addition once a registry/target
  is decided.
- **CD (continuous deployment) — open question, not decided, revisit
  later.** Deployment today is entirely manual: build locally, `docker
  save | ssh ... docker load` onto the VM (see the
  `build-locally-deploy-remotely` skill), stop/restart the container by
  hand. Turning this into an automated pipeline (push to `main` →
  auto-deploy) raises questions not yet worked through:
  - **How does GitHub Actions reach the VM?** The VM has no public
    container registry pull set up — CI would need either (a) SSH access
    from the Actions runner (a private key stored as a GitHub Secret,
    itself a sensitive credential to manage carefully), or (b) push the
    built image to a registry (Docker Hub, or OCI's own Container
    Registry — OCIR) and have something on the VM pull from there instead
    of `docker load` over SSH.
  - **What triggers a deploy vs. just running tests?** Presumably a push
    to `main` after CI passes, but that also means every merge
    auto-deploys to the only environment that exists (no
    staging/production split) — worth deciding if that's actually wanted
    for a personal project, or if manual deploys are fine indefinitely
    given how infrequently this changes.
  - **Does the VM's tiny CPU matter here too?** If CD pulls a pre-built
    image (option (b) above) this is moot; if it triggers a build on the
    VM itself, this repeats the exact slowness problem the
    `build-locally-deploy-remotely` skill exists to avoid.
  - **Rollback** — if a bad deploy goes out automatically, what's the
    process to revert? Not designed at all yet.
  Not blocking anything today — manual deployment works fine at this
  project's current pace of changes. Come back to this if deploys start
  happening often enough that the manual steps become the bottleneck.
