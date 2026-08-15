# Infrastructure — current state

What's actually running, right now, and how the pieces connect. This is
a **current-truth** reference (what IS), not a plan doc — for the
decisions and incidents behind these choices, see `docs/plans/deployment-plan.md`
and `docs/plans/security-plan.md`. For the real IPs, keys, and OCIDs this doc
deliberately omits, see `local-infra/infrastructure.yaml` — gitignored,
never published, kept in sync by whoever last touched the deployment
(see the `deploy-engineer` subagent).

## Topology: two VMs, one VCN

Both `VM.Standard.E2.1.Micro` (Oracle Always Free), same region,
compartment, and private subnet (`10.0.0.0/24`), reachable with the same
SSH key pair.

| | Bot VM | Phoenix VM |
|---|---|---|
| Runs | `myfirstagent-bot` Docker image (`combined_bot.py`) | Phoenix, natively — **not Docker** (a venv + systemd service, `phoenix serve`) |
| Public IP | `<bot-vm-ip>` | `<phoenix-vm-ip>` |
| Private IP | `10.0.0.7` | `10.0.0.234` |
| Why separate | — | Phoenix's memory use can spike hard under load; isolating it means a spike can't take the bot down too |

**Phoenix is not Docker, on purpose to note** — every other service in
this project runs in Docker; this is the one deliberate exception (see
`docs/plans/deployment-plan.md`'s "Live Phoenix deployment" section for why:
`arize-phoenix`'s conda-forge package was badly stale, and this is a
single-purpose ops VM, not part of the tracked `environment.yml`).

## Security model

- **Secrets never sit in plaintext on either VM.** Both fetch what they
  need from **OCI Vault** at process startup via **Instance Principal**
  auth (no standing credential on the box) — the bot container only ever
  receives `*_SECRET_OCID` env vars, resolved by `docker-entrypoint.sh`.
  See `docs/plans/security-plan.md` finding 2.
- **Two independent firewall layers, both must allow a port.** OCI's
  Security List (cloud-level) and each VM's own `iptables` (host-level,
  restrictive by default on Oracle's Ubuntu images). Missing either
  layer leaves a port closed regardless of the other — this has bitten
  the project once already (a confusing `No route to host` instead of a
  clean timeout, which is what actually revealed there were two
  firewalls, not one).
- **Phoenix's ports (6006 web UI, 4317 OTLP) are not open to the public
  internet at all.** 4317 is open only from the bot VM's subnet
  (`10.0.0.0/24`), for trace ingestion. 6006 has no inbound rule at
  all — human access is SSH-tunnel-only, by design.
- **Phoenix has its own auth** (`PHOENIX_ENABLE_AUTH=true`, a random
  secret, and the default admin password explicitly overridden — the
  default `admin`/`admin` login is a well-known trap that enabling auth
  alone does not fix). A System API Key doubles as the bot's trace-push
  credential and a bearer token for read queries — no separate
  human-password dependency for diagnostics.
- **The bot itself has no inbound ports at all** — polling mode against
  Telegram's API, not webhooks. Nothing to firewall on that side.

## Deployment

Build locally, transfer, restart — never build on the VM itself (see the
`build-locally-deploy-remotely` skill for why). Owned end to end by the
**`deploy-engineer`** subagent: build → transfer → restart → verify
(`docker logs`, `tools/run_smoke_tests.py`, `tools/check_telemetry.py`).
It reads the real connection details from `local-infra/infrastructure.yaml`
rather than asking to have them repeated.

## Data persistence

`subscribers.db` lives in a Docker named volume (`myfirstagent-data`) on
the bot VM, mounted at `/data` — survives container recreation, not
backed up anywhere else yet (see `docs/plans/security-plan.md` finding 13).
Phoenix's own trace data lives at `~/.phoenix/phoenix.db` on the Phoenix
VM, outside any container.
