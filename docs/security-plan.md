# Security Plan

A security review of the codebase and current local Docker deployment,
done before committing to a cloud provider (`docs/deployment-plan.md` item
3). Nothing here is a blocker for *choosing* a cloud provider — but a few
items are worth deciding before building Kubernetes manifests, since
retrofitting secrets handling after the fact is more work than deciding it
upfront.

## Bottom line

No critical, actively-exploitable vulnerability found. The two real gaps
are **secrets stored in plaintext** (findings 1-2) and **no rate limiting**
(finding 3) — both worth fixing, neither urgent enough to block picking a
cloud provider and starting on Kubernetes manifests in parallel.

## Status

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Historically-leaked DeepSeek key still in git history | Resolved | Confirmed current key is a different value (see below) — no action needed beyond awareness |
| 2 | Secrets stored in plaintext, readable via `docker inspect` | Medium | Not started — needs a decision before K8s manifests |
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
`ADMIN_BOT_TOKEN` in cleartext (names and values both) — anyone with
access to the Docker daemon on the host can read all four secrets. On a
single-user personal machine this is low risk (whoever has Docker access
already has full control of the box anyway). It matters more once this
runs on a cloud host or in Kubernetes, where more principals might have
some level of access to the node/cluster without needing full ownership.

This is the same gap `docs/deployment-plan.md`'s "Secrets management" open
question already flags — worth resolving as part of choosing a cloud
provider, since Kubernetes `Secret` objects are **base64-encoded, not
encrypted**, by default (equivalent exposure to what's already true
today) — real protection needs either envelope encryption at the cluster
level, a cloud-native secret store (AWS Secrets Manager, GCP Secret
Manager, etc.) integrated via a CSI driver, or a tool like Sealed Secrets.
Decide this alongside the cloud provider choice, not after.

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

## Recommendation on cloud deployment timing

None of the above blocks moving forward on `docs/deployment-plan.md` item
3 (choosing a cloud provider) — nothing here is an active, exploitable
hole. Suggested order:

1. Decide secrets management (finding 2) **as part of** the cloud
   provider decision, since the right answer depends on which provider
   (native secret manager availability differs) — don't pick a provider
   first and retrofit this after.
2. Add rate limiting (finding 3) and CI vulnerability/secrets scanning
   (findings 7-8) before or shortly after the first real cloud deployment
   — cheap, and cheaper to do before more users are approved.
3. Everything else (findings 4, 6, 9) can happen whenever convenient —
   none are urgent.
