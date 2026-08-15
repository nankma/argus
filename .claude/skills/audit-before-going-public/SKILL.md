---
name: audit-before-going-public
description: Use when changing a repository from private to public, pushing to a newly-public repository for the first time, publishing docs or an artifact externally, or when content was written while a repo was private and its audience is about to widen.
---

# Audit before going public

## Overview

**"Safe to document" is a judgement about the audience, not about the
content.** Anything assessed as safe while a repository was private is
un-assessed the moment it goes public, and must be re-checked *before* the
visibility change.

The urgency is git history: **anything committed to a public repo stays
retrievable permanently.** Removing it in a later commit clears the working
tree only. A real fix requires a history rewrite and a force-push, so there
is no quiet correction after the fact. Audit before the push, not after.

## When to use

- Flipping a repo from private to public
- First push to a repo that just became public
- Publishing an artifact, doc site, or write-up externally
- Adding a public README/portfolio link to something previously internal
- Any doc written under "only I will read this" whose audience is widening

Not needed for pushes to a repo that was already public and already
audited — the assumption hasn't changed.

## What to look for

Ordered by how often it actually bites, not by severity:

| Category | Examples | Usually the right call |
|---|---|---|
| **Infrastructure identifiers** | Public IPs, hostnames, bucket names, instance IDs, cloud resource IDs | Redact public IPs/hostnames. Keep RFC 1918 (`10.x`, `192.168.x`, `172.16-31.x`) — meaningless externally and needed for topology to read |
| **Local filesystem paths** | `C:\Users\<name>\...`, `/home/<name>/...` | Redact — they leak a username even when the file itself is absent |
| **Credentials** | Keys, tokens, passwords, connection strings | Never publishable. If one was *ever* committed, rotate it — redaction doesn't help, history holds it |
| **Internal references** | Ticket IDs, internal URLs, colleague names, org-internal jargon | Judgement call; usually harmless, sometimes not |
| **Operational detail** | Exact versions, ports, unusual config that narrows an attack | Keep what aids comprehension, cut what only aids an attacker |

Distinguish a **variable name** from a **value**. `PHOENIX_SECRET` in prose
is documentation; `PHOENIX_SECRET=a1b2c3...` is a leak. Don't redact the
former — it makes docs worse for no gain.

## The check

Scan tracked files, then the staged diff before pushing:

```bash
# tracked content
grep -rnE "([0-9]{1,3}\.){3}[0-9]{1,3}" --include="*.md" --include="*.py" . \
  | grep -vE "10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|127\.0\.0\.1|0\.0\.0\.0"

grep -rniE "C:.Users.[A-Za-z0-9_-]+|/home/[a-z0-9_-]+|/Users/[A-Za-z0-9_-]+" --include="*.md" .

grep -rniE "BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key *[:=] *[\"'][A-Za-z0-9]{16,}|password *[:=] *[\"'][^\"']{8,}" .

# what is actually about to leave the machine
git diff --cached
```

Then fix the **rationale**, not just the content. A doc that says "safe to
document — no secrets" above now-redacted values contradicts itself and
will invite someone to put them back.

## Common mistakes

| Mistake | Why it fails |
|---|---|
| Pushing first, cleaning after | History is permanent; the cleanup is cosmetic |
| Redacting values but leaving the justification that permitted them | The next contributor reads the justification and restores them |
| Redacting private `10.x` addresses too | No security gain, and the architecture stops making sense |
| Treating a variable *name* as a secret | Degrades docs for zero benefit |
| Assuming "no secrets" means "safe" | IPs, usernames, and paths aren't secrets and still shouldn't be published |
| Checking only new commits | The exposure may predate this push by months |

## Real-world impact

This project's `docs/plans/deployment-plan.md` carried both production VMs'
public IPs, an SSH key filename, and the local directory containing it —
which also disclosed the operator's username. It was explicitly annotated
*"safe to document — no secrets,"* which was true when written and false
after the repo went public.

Caught in a pre-push scan with minutes to spare. Had it landed, removing it
would have required rewriting published history. See
`docs/plans/security-plan.md` finding 18.

The near-miss is the argument for this skill: the scan happened because
pushing to a newly-public repo *happened* to prompt a second look. That's
luck, not process.
