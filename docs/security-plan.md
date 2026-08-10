# Security Plan

A security review of the codebase and Docker deployment, originally done
before committing to a cloud provider (`docs/deployment-plan.md` item 3).
**Oracle Cloud is now chosen and the bot is live** on a
`VM.Standard.E2.1.Micro` instance — findings below updated where the move
from "local only" to "actually on the internet" changes anything.

## Bottom line

No critical, actively-exploitable vulnerability found. **Secrets in
plaintext (finding 2) is now resolved** — OCI Vault + Instance Principals,
implemented and verified live. The remaining real gap is **no rate
limiting** (finding 3) — worth fixing, not an active exploit today.

## Status

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Historically-leaked DeepSeek key still in git history | Resolved | Confirmed current key is a different value (see below) — no action needed beyond awareness |
| 2 | Secrets stored in plaintext, readable via `docker inspect` | Resolved | **Done and verified live** — OCI Vault + Instance Principals, see below |
| 3 | No rate limiting on approved users | Medium | Not started |
| 4 | Unapproved strangers can spam admin notifications | Low | Not started — cheap to fix, low urgency |
| 5 | LLM prompt-injection surface via external content (`search_news`) | Low (currently) | Monitor — re-assess whenever new tools are added |
| 6 | `admin_bot.py` confirms its own existence to non-admins | Trivial | Not started — optional |
| 7 | No dependency/image vulnerability scanning in CI | Medium | Not started |
| 8 | No automated secrets-scanning in CI (currently manual, per-commit) | Medium | Not started |
| 9 | GitHub branch protection enforcement — unconfirmed | Unknown | Carried over from `docs/telemetry-and-testing-plan.md`, still unresolved |
| 10 | Non-root container user | Good — already correct | No action needed |
| 11 | No inbound ports (polling-only architecture) | Good — already correct | No action needed |
| 12 | Admin identity check relies on Telegram-verified `chat_id`/`from_user.id` | Good — already correct | No action needed |
| 13 | `subscribers.db` has no backup — single Docker volume, no copy anywhere | Medium | Not started — availability risk, now live on the real VM, not just local |
| 14 | Cloud VM OS-level hardening | Medium | **Partly done** — root login disabled, `MaxAuthTries` lowered, unattended-upgrades confirmed enabled on both VMs. SSH source-IP restriction still outstanding |
| 15 | IAM policy scoping for whichever secret manager is used | Low | Design-time reminder, not a current gap (nothing built yet) |
| 16 | No audit logging on secret access | Low | Comes largely free once a real vault is adopted (finding 2) |
| 17 | Phoenix telemetry access control | Resolved | **Done and verified live** — native auth, network isolation, Vault-stored API key, see below |
| 18 | Docs written under a private-repo assumption, published unreviewed | Resolved | **Caught before the public push** — VM IPs and key paths redacted, see below |

## Findings

### 1. Historically-leaked DeepSeek key — resolved

Earlier in this project, a DeepSeek API key string was committed in
`agent.py`'s docstring (commit `f3560a9`) and removed two commits later
(`f676cad`) — but git history is permanent, so that string is still
retrievable by anyone who can read the repo's full history (`git log -p`),
regardless of the repo's public/private setting today.

**Checked this session**: compared the currently-configured
`DEEPSEEK_API_KEY` against the leaked string via a SHA-256 hash comparison
(never printing either value in plaintext) — **they don't match**. The key
in active use today is not the one that was ever exposed in git history.
No rotation needed.

Residual, low-priority item: the dead string is still sitting in git
history. Harmless on its own (it's not a live credential), but if this
repo is ever made public, purging it properly would need `git filter-repo`
or GitHub's secret-scanning removal tooling — not urgent given it's
already inert.

### 2. Secrets stored in plaintext, readable via `docker inspect`

Confirmed empirically this session:

```
docker inspect myfirstagent-bot --format '{{json .Config.Env}}'
```

returns `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`, and
`ADMIN_BOT_TOKEN` in cleartext (names and values both). Of these, three
are real bearer credentials that grant real access if leaked — the
DeepSeek API key and the two Telegram bot tokens (one per bot, from
BotFather). `ADMIN_CHAT_ID` is different in kind: it's just the owner's
numeric Telegram user ID, not a credential — leaking it doesn't let anyone
impersonate the admin, since `check_access()`/`handle_decision()` trust
Telegram's own server-verified `chat_id`/`from_user.id` fields (see
finding 12), which a client can't forge. It's grouped here only because
it's part of the same env-var surface `docker inspect` exposes, not
because it carries the same risk as the other three.

Anyone with access to the Docker daemon on the host can read all four
values. On a single-user personal machine this is low risk (whoever has
Docker access already has full control of the box anyway). It matters
more once this runs on a cloud host or in Kubernetes, where more
principals might have some level of access to the node/cluster without
needing full ownership.

This is the same gap `docs/deployment-plan.md`'s "Secrets management" open
question already flags — worth resolving as part of choosing a cloud
provider, since Kubernetes `Secret` objects are **base64-encoded, not
encrypted**, by default (equivalent exposure to what's already true
today) — real protection needs either envelope encryption at the cluster
level, a cloud-native secret store (AWS Secrets Manager, GCP Secret
Manager, etc.) integrated via a CSI driver, or a tool like Sealed Secrets.
Decide this alongside the cloud provider choice, not after.

**If Oracle Cloud is the chosen provider**, confirmed it has a real
equivalent to Azure Key Vault + Managed Service Identity, at no extra cost
within Always Free:

- **OCI Vault** — one service covering both Key Management (encryption
  keys) and **Secret Management** (arbitrary secrets — API keys, tokens,
  passwords), same combined shape as Azure Key Vault, not split across two
  products.
- **Instance Principals** — the Azure MSI equivalent. A Compute VM
  authenticates using its own instance identity (instance OCID), no
  long-lived credential stored on the box.
- **Dynamic Groups + IAM policy** — the Azure "grant this MSI an access
  policy on the Key Vault" equivalent. A Dynamic Group is defined by
  matching rules (compartment OCID, instance OCID, tags); an IAM policy
  statement then grants that Dynamic Group specific permissions (e.g.
  `read secret-bundles`) scoped to a compartment/vault — not tenancy-wide.
- **Always Free coverage**: 150 secrets per tenancy (40 versions each),
  unlimited free software-protected encryption keys, 20 free HSM key
  versions (then $0.53/version/month — not needed here). Comfortably
  covers this project's four secrets at zero cost.
- **Differences from Azure worth knowing**: a mandatory **7-day deletion
  grace period** on vaults/keys (secrets: minimum 1 day) — no instant
  purge; vault OCIDs are **region-scoped**, don't hardcode across regions;
  Instance Principal auth **only works from inside OCI Compute** — local
  dev/testing still needs env vars as it does today. Some third-party
  tooling (HashiCorp Vault's OCI plugin, External Secrets Operator) has
  reported occasional instance-principal detection friction — less
  turnkey than Azure MSI in a few integrations, though the core mechanism
  works as described.

**Implemented and verified live.** The design above is what got built:

- Vault `myfirstagent-vault` with a software-protected Master Encryption
  Key, and four Secrets (`deepseek-api-key`, `telegram-bot-token`,
  `admin-bot-token`, `admin-chat-id`) holding the real values.
- Dynamic Group matching this specific instance by OCID (`instance.id =
  '<ocid>'`, fetched from the VM's own metadata service rather than typed
  by hand — least-privilege, one exact instance, not a whole compartment
  — per finding 15) plus an IAM policy granting it `read secret-family`.
- `docker-entrypoint.sh` (see `Dockerfile`) fetches all four via `oci
  secrets secret-bundle get --auth instance_principal` at container
  startup and exports them as env vars before `exec`-ing
  `combined_bot.py` — the container itself never receives the real
  secret values via `docker run -e` anymore, only the four `*_SECRET_OCID`
  values (not sensitive — resource identifiers, same as any other OCID in
  this project's docs).
- **Verified end-to-end for real**: a live Telegram message round-trip
  through the container running with only `*_SECRET_OCID` env vars set,
  no plaintext secrets anywhere in `docker inspect`'s output.
- **Real bug hit and fixed along the way**: the first IAM policy attempt
  failed every request with `NotAuthorizedOrNotFound`, despite Instance
  Principal auth itself succeeding (a real API response came back, not an
  auth-token failure) — caused by the policy statement referencing a
  Dynamic Group name (`myfirstagent-dg`, from the initial suggested
  naming) that didn't match the group's *actual* name (the console's
  auto-generated `dg-mnk-...`, which is what actually got created).
  Fixed by editing the policy statement to reference the real group name.
  Worth remembering: OCI accepts a policy statement referencing a
  nonexistent dynamic-group name without any validation error at
  creation time — it just silently never matches anything, so this class
  of typo doesn't fail loudly until someone actually queries a secret and
  hits `NotAuthorizedOrNotFound`.
- Local/Docker Desktop testing is unaffected — `docker-entrypoint.sh`
  only fetches from Vault when a `*_SECRET_OCID` var is set, otherwise it
  falls through to whatever plain env vars were passed directly (which is
  what local testing still does).

## Is cloud-vault + workload identity the industry standard?

Yes — "no long-lived credential stored on the workload; authenticate via
a platform-verified identity, fetch secrets from a managed vault at
runtime" is the current mainstream best practice, not an Azure-specific
pattern. It goes by different names per platform (Azure Managed Identity,
AWS IAM Roles, GCP Workload Identity, OCI Instance Principals) but the
shape is the same everywhere. Other approaches that show up across the
industry, relevant if this project ever needs them:

- **HashiCorp Vault** — a cloud-agnostic, self-hostable (or HCP-managed)
  secrets manager. Common when avoiding lock-in to one cloud's native
  vault, running multi-cloud, or wanting **dynamic secrets** (short-lived,
  auto-rotated credentials issued on demand instead of long-lived static
  ones). Overkill for this project's single-provider, single-VM scale.
- **Kubernetes-native tools** (relevant once `docs/deployment-plan.md`
  item 2's manifests exist): **Sealed Secrets** (encrypts a secret so
  it's safe to commit to git — only the in-cluster controller can decrypt
  it) and **External Secrets Operator** (syncs secrets from a real vault —
  OCI Vault, AWS Secrets Manager, etc. — into native Kubernetes `Secret`
  objects automatically). Either pairs naturally with the OCI Vault design
  above once there's a cluster to run them in.
- **SOPS** (Mozilla) — encrypts individual config files using a cloud
  KMS/PGP/age key, so the encrypted files are safe to check into git
  directly. A lower-ceremony alternative to Sealed Secrets, worth
  considering when the K8s manifests get written.
- **Automatic secret rotation** — cloud secret managers (including OCI
  Vault) support scheduled/triggered rotation of stored credentials. This
  project doesn't rotate anything today (the DeepSeek key and the two
  Telegram bot tokens are all static, set-once). Not urgent at this scale, but worth designing for once a
  real vault is in place — rotation is far easier to bolt on early than
  retrofit later.

**What "automatic" actually means for third-party-issued credentials.**
No cloud vault (OCI Vault, Azure Key Vault, AWS Secrets Manager, GCP
Secret Manager) can auto-generate a new DeepSeek API key or a new
Telegram bot token — that requires the *issuing provider* to expose a
credential-creation API, and neither does (confirmed by checking their
docs):

- **DeepSeek** — key management is web-console-only
  (platform.deepseek.com/api_keys); no REST endpoint to create/rotate
  keys programmatically. **But it does support multiple simultaneously-
  valid keys per account** — creating a new one doesn't invalidate
  existing ones; revocation is a separate, explicit step. This makes a
  **zero-downtime rotation possible**: create the new key → point the
  service at it → confirm it actually works (a real message round-trip,
  not just "no error") → only then revoke the old key. No window where
  the server holds a key the provider no longer accepts.
- **Telegram bot tokens (`TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`)** — only
  regenerable via BotFather's `/token` or `/revoke` commands, no
  programmatic API either. Unlike DeepSeek, **this is a hard cutover with
  no overlap window** — the instant a new token is issued, the old one
  stops working. There's no way to "verify the new one works before the
  old one dies" the way DeepSeek allows.
  - This is where the scenario in the question — "the server still holds
    the old key but the provider only accepts the new one" — genuinely
    happens for these two secrets specifically, for however long it takes
    to push the new token into the running bot process.
  - **Why it's low-stakes for this project anyway**: `bot.py`/`admin_bot.py`
    use polling, not webhooks. Telegram queues undelivered updates
    server-side for any bot that isn't currently polling — confirmed
    directly in this project when the local bot process died
    unexpectedly and a friend's `/start`/`Hello` messages sat queued,
    intact, until polling resumed (see the access-control testing
    session). A rotation gap of a few seconds to a couple of minutes
    means delayed replies, not lost messages or failed deliveries — a
    webhook-based bot wouldn't have this safety net, since a delivery
    attempt during the gap would just fail.
  - Practical procedure: stage the new deployment/config first (image
    built, container ready to go with everything except the token) →
    trigger `/token` on BotFather → immediately update the vault value
    and restart the bot process → confirm it's polling again. Minimize
    the gap; don't eliminate it, because Telegram doesn't allow that.

**Automating the distribution half, even though generation stays manual.**
Once `docs/deployment-plan.md` item 2's Kubernetes manifests exist, the
standard pattern is **External Secrets Operator** (syncs a vault's current
value into a native Kubernetes `Secret`) paired with a tool like
**Reloader** (watches for `Secret` changes and automatically triggers a
rolling restart of the affected `Deployment`). That closes the loop on the
part that *can* be automated: a human still creates the new DeepSeek key
or Telegram token, but from the moment it's written into the vault, the
running service picks it up and restarts itself with no manual `docker
restart`/redeploy step. Kubernetes rolling updates also naturally keep old
pods serving traffic until new pods report healthy, which is exactly the
overlap DeepSeek's multi-key support allows for and Telegram's hard
cutover doesn't.

### 3. No rate limiting on approved users

Once approved, a user (including a well-meaning friend, or an approved
account that gets compromised) can send unlimited messages, each
triggering a DeepSeek API call plus calls to every enabled news source —
no per-user or global rate cap exists anywhere in `bot.py` or `agent.py`.
At "owner + a couple of friends" scale this is a cost-control gap more
than a security one, but worth a simple fix (e.g., a per-chat cooldown or
a daily message cap) before approving more users or deploying somewhere
the DeepSeek bill matters more.

### 4. Unapproved strangers can spam admin notifications

`check_access()` only calls `notify_admin()` once per distinct new
`chat_id` (subsequent messages from the same pending chat_id just get "still
pending" — confirmed in `users_db.request_access`'s `ON CONFLICT DO
NOTHING`). But nothing stops someone from creating many different Telegram
accounts and messaging the bot from each, generating one admin
notification per account. No cost impact (no DeepSeek calls happen for
unapproved users) — just notification noise. Low priority; a simple future
mitigation would be a global "max pending requests per hour" cap in
`bot.py`.

### 5. LLM prompt-injection surface via external content

`search_news` pulls content from external, uncontrolled sources (RSS
feeds, third-party news APIs) directly into the agent's context. A
compromised or malicious feed could embed prompt-injection text aimed at
manipulating the agent's next action. Current blast radius is small: the
agent's only tools are `save_note` (writes to a local file) and
`search_news` itself (read-only external fetch) — there's no tool that can
exfiltrate data, hit arbitrary attacker-chosen URLs, or take a destructive
action. Re-assess this finding whenever a new tool is added — the four
other planned features in `docs/bot-features-plan.md` (translation,
per-user source selection, proactive push) don't meaningfully raise this
risk on their own, but keep it in mind for anything added later that
expands what a tool call can actually do.

**Related but distinct, and now live**: a real *benign* scope-drift
incident (not malicious external content — an ambiguous user question
pulled the agent off its assigned role and into discussing its own
implementation) — see `docs/guardrails-plan.md` for the incident and the
four-layer mitigation (pre-filter, secondary classifier gateway, hardened
system prompt, output-side check). **Built and verified live**.

### 6. `admin_bot.py` confirms its own existence to non-admins

`reject_non_admin` replies "This bot is private." to anyone who isn't the
admin, rather than staying silent — a stranger who finds `@mnkInfoAdmin_bot`
learns it exists and is admin-gated, though nothing else is disclosed.
Trivial severity; optional fix is to not reply at all, at the cost of the
same "looks broken vs. deliberately restricted" UX tradeoff already
accepted for `bot.py`'s equivalent message. Not worth doing unless it
starts attracting attention.

### 7. No dependency/image vulnerability scanning in CI

`.github/workflows/ci.yml` runs `pytest` only — nothing scans
`environment.yml`'s pinned packages or the built Docker image for known
CVEs. Worth adding before this becomes an internet-reachable cloud
deployment: `docker scout cves` (built into Docker Desktop/CLI already) or
Trivy are the natural options, as either a CI step or a periodic scheduled
job.

### 8. No automated secrets-scanning in CI

The project's practice of scanning changed files for secret-like strings
before every commit (established after the historical key leak — see
finding 1) is currently manual, done by whoever/whatever is committing.
Nothing enforces it automatically. Adding a lightweight tool (e.g.,
`gitleaks`) as a CI step or pre-commit hook would make this systematic
instead of relying on remembering to do it every time.

### 9. GitHub branch protection — still unconfirmed

Carried over from `docs/telemetry-and-testing-plan.md` item 4: whether
branch protection on `main` is actually enforced (private repos need
GitHub Team/Enterprise for this, per earlier research in this project) was
never confirmed after being set up. Not re-checked this session — `gh` CLI
isn't available in this environment to verify programmatically. Needs a
manual check in GitHub's repo settings.

### 10-12. Already correct — no action needed

- **Non-root container user**: confirmed empirically this session
  (`id` inside the running container returns `uid=57439(mambauser)`, not
  root) — inherited from the `mambaorg/micromamba` base image, nothing
  extra needed.
- **No inbound ports**: the `Dockerfile` has no `EXPOSE`, and polling mode
  means the bot only makes outbound connections (to Telegram, DeepSeek,
  and news sources) — there's no listening port to secure or firewall.
  Preserve this when choosing cloud firewall/security-group rules:
  egress-only is sufficient, no inbound rule needed for the bot itself.
- **Admin identity check**: `ADMIN_CHAT_ID` is compared against
  `update.effective_chat.id` / `query.from_user.id`, both of which come
  from Telegram's own servers via the Bot API and can't be spoofed by a
  client — this is a trustworthy check, not a self-reported value an
  attacker could forge.

### 13. `subscribers.db` has no backup

The approval database (`users_db.py`) lives in a single Docker volume
(`myfirstagent-data`) with no copy anywhere else. This is an availability
concern, not a vulnerability — if the volume is lost (host disk failure,
accidental `docker volume rm`, bad migration), every approval decision
(who's approved, denied, pending) is gone, and every existing subscriber
would need to re-request access. Worth a simple periodic backup (e.g. a
scheduled `sqlite3 .backup` copied to cloud object storage) once this
moves to a cloud host — cheap insurance, not urgent at today's "owner plus
a couple of friends" scale.

### 14. Cloud VM OS-level hardening

Now live and relevant: Oracle `VM.Standard.E2.1.Micro`, `us-sanjose-1`,
Ubuntu 24.04 Minimal, see `docs/deployment-plan.md`. There are now **two**
such VMs — the bot (`myfirstagent-bot` instance) and a second, isolated
one running Phoenix (`myfirstagent-phoenix` — deliberately separate so a
Phoenix memory spike can't take the bot down; see
`docs/deployment-plan.md`'s "Live Phoenix deployment" section for that
VM's specific hardening: SSH-tunnel-only access, no public port 6006/4317,
native auth with the default `admin`/`admin` password overridden).

- **Done**: SSH is key-only by default (OCI's instance creation only
  offers SSH-key auth for the `ubuntu` login, no password auth was ever
  enabled — nothing to disable). A 1GB swap file was added (see
  deployment-plan.md) — availability/stability, not strictly security, but
  related to keeping the box from falling over under memory pressure.
- **Not done yet**: restricting SSH source IPs (currently open to `0.0.0.0/0`
  on port 22 via the default security list — fine for a single-owner
  personal box, but worth narrowing to the owner's actual IP range if that's
  stable enough to maintain); an OS patching cadence (no unattended-upgrades
  or manual patching schedule set up); disabling unused default services.
  None are urgent for a single-user personal VM, but worth doing before
  approving more subscribers or if this box starts holding anything more
  sensitive than it does today.


**Update 2026-08-09 — partly resolved.** Applied to both VMs via a
dedicated `/etc/ssh/sshd_config.d/99-hardening.conf` drop-in (rather than
editing the cloud image's own config, so the change is visible and
revertible):

- `PermitRootLogin no` — was `prohibit-password`, i.e. root could log in
  by key. Administration is done as `ubuntu` with sudo, so root login has
  no operational purpose; disabling it removes the most-targeted username
  on the internet as an option.
- `MaxAuthTries 3` — down from the default 6. Key auth succeeds on the
  first attempt, so this costs legitimate access nothing.
- `unattended-upgrades` confirmed **already enabled** on both hosts, which
  covers the patch-cadence item.

Config was validated with `sshd -t` before reloading, and a fresh
connection verified afterwards on each host.

**Still outstanding: SSH source-IP restriction.** Deliberately not applied
automatically. The operator's address is residential and therefore
dynamic; pinning host-level `iptables` to it risks a lockout requiring
serial-console recovery when the ISP rotates it. The correct place for
this control is the **OCI Security List**, which is cloud-level and can be
changed or reverted from the console without SSH access — making a
mistake recoverable rather than fatal. Left for the operator to apply
there.

**Deliberately not installed: fail2ban.** With password authentication
disabled, brute force cannot succeed, so fail2ban would defend against an
attack that already fails. It costs roughly 40 MB resident on a 1 GB host
that already runs the service at ~160 MB. Rejected as cargo-cult
hardening at this scale; revisit if password auth is ever enabled or the
host grows.

### 15. IAM policy scoping (design-time reminder)

Not a current gap — nothing is built yet — but worth stating as a
principle before the Dynamic Group + IAM policy from finding 2 gets
written for real: grant the narrowest permission that works (e.g. `read
secret-bundles` on a specific vault/compartment, not `manage` or
tenancy-wide access). Cheaper to get this right the first time than to
loosen-then-tighten later.

### 16. No audit logging on secret access

Right now there's no log of who/what accessed a secret, because secrets
aren't in a real vault yet (finding 2) — env vars passed via `docker run
-e` don't produce an access log. This mostly resolves itself once a real
secret manager is adopted: OCI Vault (like Azure Key Vault, AWS Secrets
Manager, etc.) logs every secret read via the platform's audit service by
default. Not something to build separately — just confirm it's turned on
when finding 2 is implemented.

### 17. Phoenix telemetry access control

Phoenix went from "not deployed" to "live and receiving real trace data
(conversation content, tool calls, token usage)" this session, which
raised its own access-control surface — worth a dedicated finding rather
than folding into finding 14 (that one's about OS-level VM hardening;
this is about Phoenix's own application-level access design). Full setup
detail lives in `docs/deployment-plan.md`'s "Live Phoenix deployment"
section; this is the security-relevant summary.

**Isolated on its own VM.** `myfirstagent-phoenix`, separate from the bot
— not primarily a security boundary (the main reason was Phoenix's memory
spikes potentially OOMing the bot too), but it does mean a Phoenix
compromise doesn't automatically hand over the bot's process/secrets, and
vice versa.

**Native auth, with the actual default-credential trap avoided.**
`PHOENIX_ENABLE_AUTH=true` plus a random `PHOENIX_SECRET` — but enabling
auth alone leaves the login as the well-known `admin`/`admin`, which
would have been a real hole (an authenticated-but-default-credentialed
instance is barely better than no auth). Fixed by also setting
`PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` to a random value, caught by
reading `phoenix.auth`'s source rather than assuming enabling auth was
sufficient on its own. Login identifier is an email (`admin@localhost`),
not the username — a UX trap, not a security one, but worth recording
since it looks like a login failure otherwise.

**Network isolation, not just app-level auth — defense in depth.** Ports
6006 (web UI) and 4317 (OTLP) are not open to the public internet at all;
confirmed by testing from outside that the ports are unreachable. Human
access to the UI is SSH-tunnel-only. The bot's OTLP traffic is allowed
only from the VCN's private subnet (`10.0.0.0/24`), not the internet —
requiring changes at **two independent firewall layers** (OCI's
cloud-level Security List, and the VM's own local `iptables`, which
Oracle's Ubuntu images ship with a restrictive default — SSH allowed,
everything else `REJECT`ed). Missing either layer would have left the
port effectively closed anyway, so the two layers aren't fully redundant
with each other, but the practical effect is the same principle as the
bot's own "no inbound ports needed" design (finding 11): even if
Phoenix's own auth were somehow bypassed, the port isn't reachable from
outside the VCN to begin with.

**OTLP ingestion needs its own credential — not just human login.** A
real gotcha: `PHOENIX_ENABLE_AUTH` blocks the trace-ingestion endpoint
too, not only the web UI — the bot ran with zero errors while every trace
was silently rejected, until a **Phoenix System API Key** was created
(via GraphQL `createSystemApiKey` — no REST/UI shortcut exists for this)
and passed as `PHOENIX_API_KEY`. This key is now a fifth OCI Vault secret
(`phoenix-api-key`), fetched by `docker-entrypoint.sh` exactly like the
other four in finding 2 — no plaintext credential in `docker run -e` or
`docker inspect`, same as everything else.

**No separate credential needed for future diagnostic access, human or
otherwise.** The same System API Key doubles as a bearer token for
*read* queries (`/v1/projects`, span data, etc.), confirmed directly —
not just write/ingestion. This means diagnosing a future issue (in this
session or any later one) never needs the human admin's password: fetch
`phoenix-api-key` from Vault via the bot VM's existing Instance Principal
access (the same mechanism `docker-entrypoint.sh` already uses), then
query Phoenix's API directly. One fewer shared human credential in the
loop, and one fewer thing that needs rotating/protecting outside Vault.

### 18. Content written under a private-repo assumption

**Caught 2026-08-09, immediately before the repository was made public.**

`docs/deployment-plan.md` recorded both VMs' **public IP addresses**, the
SSH private-key filename, and the local directory holding it — which also
disclosed the operator's Windows username. It carried an explicit
justification: *"safe to document — no secrets."*

That justification was correct **when it was written**, and wrong the
moment the repository went public. An IP genuinely isn't a credential, but
publishing the exact addresses of two live VMs removes an attacker's
reconnaissance step for free, and pairs badly with a repo that also
documents what runs on them.

**Why it had to be caught before the push, not after.** Anything committed
to a public repository stays in its git history permanently. Deleting it
in a later commit removes it from the working tree only — it remains
retrievable, and cleaning it properly requires a history rewrite plus a
force-push. There is no quiet fix after the fact.

**Resolution.** Public IPs, the key filename, and the local key directory
replaced with placeholders across `deployment-plan.md` and
`observability-and-debugging.md`. Private `10.0.0.x` addresses kept — RFC
1918, meaningless outside the VCN, and needed for the topology to be
comprehensible. Verified no secrets had ever been committed: the
`PHOENIX_SECRET` and `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` mentions are
variable *names*, not values, and no OCIDs or tokens appear in tracked
files.

**The generalizable finding**, and the reason this is recorded as a
security item rather than a tidy-up: **"safe to document" is a judgement
about the audience, not about the content.** Any assessment of that kind
made while a repo is private is invalidated by publication, and needs
re-running before the visibility change — not after.

Codified as the `audit-before-going-public` skill so the check happens by
default rather than by luck. It was luck this time: the scan happened only
because a push to a newly-public repo prompted a second look.

## Remaining work

Cloud provider is chosen, the bot is live, and secrets management
(finding 2, the item this whole review was originally gating) is done and
verified. What's left, roughly in order of value for the effort:

1. Rate limiting (finding 3) and a `subscribers.db` backup (finding 13) —
   both cheap, worth doing before approving more users.
2. CI vulnerability/secrets scanning (findings 7-8).
3. Remaining OS hardening on the live VM (finding 14: SSH source-IP
   restriction, a patch cadence) — not urgent for a single-user box.
4. Everything else (findings 4, 6, 9) whenever convenient.
4. Everything else (findings 4, 6, 9) can happen whenever convenient —
   none are urgent.
