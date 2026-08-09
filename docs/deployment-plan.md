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
| 6 | CD (continuous deployment) | **Design decided, not built** — GitHub Actions self-hosted runner on the user's home machine, see "Open questions" below. Next infrastructure item, queued after a few pending features |
| 7 | LINE as a second client, alongside Telegram | **On hold** — see `docs/multi-channel-plan.md`. Researched (webhook/TLS approach, registrar pricing, account setup), but LINE's free tier caps push messages at 200/month account-wide, which would gut the periodic-push feature. Parked pending a business model decision |

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

### Connection details (safe to document — no secrets)

An IP address is a routing detail, not a credential, and the private key
itself never leaves the local machine or gets committed — so this is safe
to keep here for whoever next needs to deploy or debug, instead of
re-discovering it from the OCI console each time.

- **Bot VM** (`instance-mnk-20260807-2035`, runs `myfirstagent-bot` via
  `combined_bot.py`): public IP `<bot-vm-ip>`, private IP `10.0.0.7`.
- **Phoenix VM** (`instance-mnk-phoenix-20260808-1012`, runs the Phoenix
  telemetry collector/UI): public IP `<phoenix-vm-ip>`, private IP
  `10.0.0.234`.
- **SSH private key**: `<ssh-key-file>`, kept locally under
  `<local-key-directory>` — not in this repo, not committed, referenced
  by path only. `ssh -i "<path-to-ssh-key>" ubuntu@<bot-vm-ip>`.
- Both VMs are `us-sanjose-1`/Phoenix-region Always Free shapes under the
  same tenancy; user is `ubuntu` on both (default for the Ubuntu 24.04
  Minimal image).

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

**Runs always-on, not on-demand — a deliberate change from the original
plan.** Initially the systemd unit was left `disabled` (start only when
debugging) specifically to cap resource/OOM exposure. At the user's
request this was flipped to `systemctl enable --now phoenix` (starts on
boot, `Restart=on-failure` keeps it up) — explicitly to "push the limit
and see what we actually need" now that the bot is also wired to send
real traffic. Revisit if memory pressure (see the community reports
above) becomes a real problem on this VM specifically, not just a
theoretical one.

**Secured multiple ways:**
1. Phoenix's native auth is enabled (`PHOENIX_ENABLE_AUTH=true`,
   `PHOENIX_SECRET` set to a random 64-char value) — **critically, also
   overrides `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`**, since Phoenix's
   default admin login is the well-known `admin`/`admin` and enabling
   auth alone does *not* change that default — a real gotcha caught by
   reading Phoenix's own source (`phoenix.auth.DEFAULT_ADMIN_PASSWORD`).
2. Port 6006 (web UI) and 4317 (OTLP) are **not opened in the OCI
   security list to the public internet** — confirmed by testing from an
   external network that the port is unreachable. Human web UI access is
   via SSH tunnel only (`ssh -i <key> -L 6006:localhost:6006
   ubuntu@<phoenix-vm-ip>`, then browse `localhost:6006`) — so even a
   hypothetical auth bypass still requires SSH key access to reach the
   port at all. The systemd unit file itself is also locked to
   `600`/root-owned, since it holds `PHOENIX_SECRET` and the admin
   password in plaintext.
3. Port 4317 (OTLP only, not 6006) **is** opened, but only to the VCN's
   private subnet (`10.0.0.0/24`), not the public internet — so the bot
   VM can push traces without the endpoint being internet-reachable. Two
   layers had to be opened for this, not just one: the OCI-level Security
   List (console), **and** the VM's own local `iptables` rules — Oracle's
   Ubuntu images ship a host-level firewall (`REJECT ... 
   icmp-host-prohibited` catch-all, allowing only SSH by default)
   independent of the cloud-level security list. Missing the second layer
   produced a confusing `No route to host` rather than a timeout, which is
   what actually revealed it was two separate firewalls, not one. Rule
   added via `iptables -I INPUT <pos> -p tcp -s 10.0.0.0/24 --dport 4317 -j
   ACCEPT` and persisted with `netfilter-persistent save`.
4. **Login uses an email address, not the username** — the default admin
   account's login identifier is `admin@localhost`, not `admin`; typing
   just `admin` into the login form's email field fails. Not obvious from
   the UI copy.

**Bot is now wired to Phoenix and verified end-to-end.** `myfirstagent-bot`
runs with `PHOENIX_ENABLED=true` and `PHOENIX_ENDPOINT=http://10.0.0.234:4317`
(Phoenix's private IP — not the public one, per the security-list setup
above). One thing that wasn't obvious going in: **`PHOENIX_ENABLE_AUTH`
blocks the OTLP trace-ingestion endpoint too, not just the human web UI**
— the bot ran with no errors for a while but zero traces actually landed
in Phoenix, because unauthenticated OTLP pushes were being silently
rejected. Fixed by creating a **Phoenix System API Key** (via the
`createSystemApiKey` GraphQL mutation — no REST/UI shortcut for this
found) and setting `PHOENIX_API_KEY` — `phoenix.otel.register()` picks
this up automatically from the environment if not passed explicitly, so
no `agent.py` code change was needed. The key itself is stored as another
OCI Vault secret (`phoenix-api-key`) and fetched by
`docker-entrypoint.sh` exactly like the other four — see finding 2 in
`docs/security-plan.md`. Verified for real: a live message through the
bot produced a full LangGraph trace (tool calls, token usage, model name)
visible in Phoenix's `myfirstagent` project.

**How to query Phoenix without the human admin login.** The same System
API Key works as a bare `Authorization: Bearer <key>` header for read
queries too (`/v1/projects`, `/v1/projects/<id>/spans`, etc.), not just
for writing traces — confirmed by querying project/span data with it
directly, no session cookie or admin password involved. This means
diagnosing an issue later (in this session or a future one) doesn't need
the human admin's password at all: SSH to the bot VM (already has
Instance Principal access to the vault), fetch `phoenix-api-key` the same
way `docker-entrypoint.sh` does, then `curl` Phoenix's API through the
same SSH-tunnel-or-VCN-internal path. No local environment setup needed
on the dev machine for this.

**Incident alerting — `telemetry_monitor.py`.** Since OpenTelemetry
doesn't raise export failures into application code (by design — a
telemetry outage shouldn't crash the app), a silent Phoenix outage would
otherwise go unnoticed. `combined_bot.py` runs a periodic (default 300s)
TCP reachability check against Phoenix's OTLP host:port, edge-triggered
(alerts only on state *change* — down→up or up→down — not every check,
so an ongoing outage doesn't spam the admin every interval) via
`admin_bot.py`'s token. Only starts when `PHOENIX_ENABLED` is set, so it's
a no-op for local dev/tests. **Verified for real, both directions**:
stopped Phoenix, confirmed the "unreachable" alert arrived in
`@mnkInfoAdmin_bot` at the next check; restarted it, confirmed the
"reachable again" alert arrived at the following check.

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
- **CD (continuous deployment) — decided (option C below), not built yet.
  Queued as the next infrastructure item, after a few pending features are
  built first (see `docs/bot-features-plan.md`).** Deployment today is
  entirely manual: build locally, `docker save | ssh ... docker load` onto
  the VM (see the `build-locally-deploy-remotely` skill), stop/restart the
  container by hand.

  Two options were considered and rejected before landing on a third:
  - **(A) GitHub Actions builds + SSHes directly to the VM to deploy** —
    simplest, reuses the existing manual flow verbatim, but requires a
    private key with shell access to the VM to live in GitHub Secrets
    (narrowable via a forced `command=` in `authorized_keys`, but it's
    still a standing credential in a third party's cloud, and a compromise
    of GitHub or the repo's Actions config would grant it).
  - **(B) GitHub Actions pushes to a registry (OCIR), the VM pulls on its
    own schedule** — better isolation (CI never touches the VM directly,
    only a narrower registry-push credential), consistent with this
    project's Vault/Instance-Principal "no standing broad credential"
    pattern, but meaningfully more infrastructure to build (registry
    setup, image versioning, a polling script on the VM).

  **(C) Chosen: a GitHub Actions self-hosted runner on the user's own home
  machine.** The runner polls GitHub outbound (no inbound port needed at
  home, same "no inbound ports" principle as finding 11) — when `main`
  gets a push and CI passes, GitHub dispatches the deploy job to this
  runner instead of a GitHub-hosted one. The build step runs locally on
  that machine (same as today, just automated instead of run by hand); the
  deploy step is the same `docker save | ssh ... docker load` already in
  use, using the SSH private key that already lives on that machine — it
  never needs to be uploaded to GitHub at all. A final step notifies the
  admin (via a direct `sendMessage` call to `admin_bot.py`'s token, no
  need to go through the running bot process) once the deploy succeeds or
  fails.

  This beats both (A) and (B) on the thing this project has consistently
  optimized for — no long-lived credential leaves a device the user
  actually controls — without (B)'s extra infrastructure. The tradeoff:
  deploys only happen while that home machine and its runner service are
  actually running (queued otherwise, not lost); and the runner is a new
  "something can execute code on this machine" surface, a non-issue for a
  solo-maintained repo but worth re-examining if this repo ever gets a
  second committer.

  **Still open once this gets built:** what triggers a deploy (push to
  `main` after CI passes, most likely — accepting that this project has no
  staging/production split, so every merge would auto-deploy the only
  environment that exists) and rollback (no process designed yet for
  reverting a bad auto-deploy).
