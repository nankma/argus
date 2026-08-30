# Infrastructure — current state

What's actually running, right now, and how the pieces connect. This is
a **current-truth** reference (what IS), not a plan doc — for the
decisions and incidents behind these choices, see `docs/plans/deployment-plan.md`
and `docs/plans/security-plan.md`. For the real IPs, keys, and OCIDs this doc
deliberately omits, see `local-infra/infrastructure.yaml` — gitignored,
never published, kept in sync by whoever last touched the deployment
(see the `deploy-engineer` subagent).

## Topology: two VMs, one VCN — but only one is in the live path

Both `VM.Standard.E2.1.Micro` (Oracle Always Free), same region,
compartment, and private subnet (`10.0.0.0/24`), reachable with the same
SSH key pair.

| | Bot VM | Second VM |
|---|---|---|
| Runs | `myfirstagent-bot` Docker image (`combined_bot.py`) — the entire live service | Formerly Phoenix (LLM tracing), natively — **not Docker** (a venv + systemd service, `phoenix serve`). **Retired 2026-08-24** — see below |
| Public IP | see `local-infra/infrastructure.yaml` | see `local-infra/infrastructure.yaml` (changes on stop/start — this is an ephemeral, not reserved, public IP) |
| Private IP | `10.0.0.7` | `10.0.0.234` |

**Telemetry is Logfire now, not Phoenix.** `agent.setup_telemetry()`
sends spans straight to Logfire's cloud API (`LOGFIRE_ENABLED`/
`LOGFIRE_API_KEY`) via `logfire_logger.py`'s `LogfireLogger` — no second
VM, no self-hosted collector, no OTLP hop across the private subnet. The
bot VM's `docker run` has carried no `PHOENIX_*` vars since 2026-08-23;
the Phoenix code path itself (`phoenix.otel.register()`, `PHOENIX_ENABLED`/
`PHOENIX_ENDPOINT`, `telemetry_monitor.py`) was removed from this repo
entirely, not just left unused.

**The second VM (`instance-mnk-phoenix-20260808-1012` in
`local-infra/infrastructure.yaml`'s `vm_phoenix` entry) still exists —
it was powered off, not deleted.** Its boot volume still holds ~269 MB of
historical Phoenix trace data (`~/.phoenix/phoenix.db`, newest span
2026-08-23T00:18:48Z) and the `phoenix` systemd service is still
`enabled` (though it was manually stopped and disabled 2026-08-26 while
evaluating it for the work below, so it won't auto-start again on the
next boot the way it did on 2026-08-26's) — powering it back on and
re-enabling the service brings Phoenix's UI back for historical lookups.

**It briefly had a different purpose lined up — noun-based keyness
scoring for the push digest's offbeat/novelty selection (see
`docs/analysis/cluster-measurements.md`) — and does not anymore.** The
plan was reconsidered once the real memory footprint was actually
measured on this VM (84.6 MB peak RSS for the whole computation over
2673 real articles): that number fits comfortably inside the *bot* VM's
own existing headroom, so keyness runs there instead, folded into
`news_ingest.py` alongside `news_embed.py`'s embedding step — same
machine, same cadence, no cross-VM sync at all. A separate periodic job
on a second VM would have created a real staleness window (a freshly-
ingested article having no keyness score until the next scheduled batch
run caught up) for exactly the newest, most novelty-relevant content --
running it in-process on the same 15-minute ingestion cycle removes that
gap entirely, not just shrinks it. See `news_keyness.py`'s own module
docstring for the shipped design.

This second VM is not currently used for anything. It's a real, paid-for
Always Free resource sitting idle with Phoenix's old data still on it,
not currently earmarked for a next purpose.

**Why Phoenix was ever separate from the bot VM in the first place**:
its memory use could spike hard under load, and co-locating it with the
bot risked one taking down the other. That reasoning does NOT
automatically transfer to every future workload considered for this
VM — the keyness paragraph above is a direct example of a workload
where the real numbers said co-location was fine, not a case for a
second VM at all.

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
- **The bot itself has no inbound ports at all** — polling mode against
  Telegram's API, not webhooks. Nothing to firewall on that side.
- **Logfire needs no inbound ports on either VM at all** — the bot VM
  makes an outbound HTTPS call to Logfire's cloud API, the same direction
  as every other external call it already makes (DeepSeek, the news
  sources). This is a real simplification from the Phoenix era, which
  needed its own 4317/6006 firewall rules, its own auth, and its own
  System API Key (all still true of the second VM's Phoenix install, but
  irrelevant while it's stopped and nothing sends spans to it).

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
Logfire's trace data lives entirely on Logfire's own cloud infrastructure,
not on either VM. The second VM's old Phoenix trace data
(`~/.phoenix/phoenix.db`, ~269 MB, frozen as of 2026-08-23) is still on
that VM's disk, outside any container, from before Phoenix was retired.

The local news article cache (`news_cache.py`) and its archive of expired
articles are also meant to live inside that same mounted volume, via the
`NEWS_CACHE_DIR`/`NEWS_ARCHIVE_DIR` env vars — both default to a relative
path if unset, which resolves onto the container's own (non-persistent)
filesystem rather than the volume. A real incident: this went unset for
some time, so every redeploy silently reset the article cache to empty
with no error anywhere. Anything meant to survive a container restart —
not just `subscribers.db` — needs its env var pointed explicitly inside
`/data`; an unset one fails silently, not loudly, so this needs verifying
via `docker inspect` after every deploy, not assumed from "the deploy
succeeded."
