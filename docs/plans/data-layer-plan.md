# Data Layer Plan (SQLite → Shared Database)

Nothing here is built — this doc records a decision that was made and
deferred, so the reasoning isn't lost. Same pattern as
`docs/plans/multi-channel-plan.md`.

**The concern raised:** SQLite runs on a single machine. It can't be
shared between hosts and it can't scale out. That means the service will
eventually hit a wall, and the migration will have to happen at some
point. The instinct was to solve it early, while it's cheap, rather than
under pressure later.

**The decision: deferred, deliberately.** Not because the concern is
wrong — it's correct — but because the cost lands twice if it's done now.

## Status

| # | Item | Status |
|---|------|--------|
| 1 | Move off SQLite to a shared database | **Seam built, 2026-09-03** (`storage/`, `subscriber_ops.py`/`category_ops.py`/`push_outcome_ops.py`/`api_budget_ops.py`/`interest_cache_ops.py`/`source_state_ops.py`, `storage.database.type` setting) — a real `PostgresStorage` exists (inherits `SqliteStorage`, overrides only the two genuinely divergent operations -- ignore-on-conflict inserts and schema introspection, three methods total), but **not yet proven against a live Postgres instance or switched on for any real deployment**; INT and PROD both still run `type: sqlite`. Next step: point `settings.int.yml` at a real Postgres instance and verify end to end. See the layering/design discussion in chat 2026-09-03 for the Business/Data-Access/Storage split this landed on. |
| 2 | Backup for the current SQLite file | Not built — accepted risk at pilot stage |
| 3 | Oracle NoSQL Database Cloud Service | **Rejected** — region-locked, verified |
| 4 | Oracle Autonomous Database (Always Free) | **Candidate, unverified** — see open questions |

## Why it's deferred rather than done

Three reasons, in order of weight:

1. **Migrating now means paying twice.** The eventual target depends on
   requirements that don't exist yet — how many users, whether a second
   host is ever needed, whether there's a webhook-based channel forcing a
   different topology (`docs/plans/multi-channel-plan.md`). Choosing a database
   before those are known means likely choosing wrong and migrating again.

2. **The data is currently reconstructible.** Subscriber records are
   approval status, interests, language, and push settings. If lost, users
   re-subscribe and re-state preferences. Unpleasant, not catastrophic —
   this is a pilot with no paying users, and that was an explicit,
   accepted trade at the time.

3. **Nothing today needs sharing.** Both bots and the scheduler run in a
   single process on a single host (`docs/system-overview.md` Appendix B.1), so
   there is no second consumer that SQLite is currently blocking. The
   limitation is real but not yet *binding*.

## What the migration would actually involve

**Superseded by the 2026-09-03 seam** (see Status row 1) — `users_db.py`
no longer exists; its ~60 functions were split into six `*_ops.py` DAL
modules over a `storage/` package (`SqliteStorage`/`PostgresStorage`).
Left below as the historical reasoning that motivated keeping persistence
behind one narrow interface, which is exactly what made the actual split
low-risk when it happened.

Smaller than it might appear, because the data access is already behind a
single module.

| Aspect | Current state |
|---|---|
| **Schema** | One table, `subscribers`, 12 columns |
| **Access layer** | All reads/writes go through `users_db.py` — roughly 20 public functions |
| **Callers** | 5 modules (`agent.py`, `bot.py`, `admin_bot.py`, `combined_bot.py`, `news_push.py`) — **none of which touch SQL directly** |
| **Migration handling** | Additive columns already handled by an `_ensure_column` helper at startup |

**The important property: no caller writes SQL.** Every module goes
through `users_db.py`'s function API, so swapping the storage engine means
rewriting the *inside* of one module while its public functions keep the
same signatures. The callers shouldn't need to change at all.

That is worth protecting deliberately — the moment a caller reaches past
`users_db.py` and issues its own query, the migration cost multiplies.
**Keeping all persistence behind that one module is the concrete thing
that keeps this decision cheap to defer.**

Two things would still need real thought at migration time:

- **JSON-encoded columns.** `interests` and `pushed_links` are stored as
  JSON text. A relational target might justify normalizing them into
  proper tables; a document store wouldn't. This is the one place the
  schema is shaped by SQLite's limitations rather than by the domain.
- **Concurrency semantics.** Some operations are read-modify-write (e.g.
  appending to a list). Single-process SQLite makes that safe by accident;
  a shared database with multiple writers would need explicit handling.

## Options evaluated

**Object Storage backup — rejected as solving the wrong problem.**
Periodically copying the SQLite file to a bucket would address *data
loss*, and it's cheap. But the concern raised wasn't durability, it was
**shareability and scale** — and a backup does nothing for either. Worth
revisiting purely as a durability measure (it's on the security review
list), but it is not a step toward the actual goal.

**Oracle NoSQL Database Cloud Service — rejected, verified.** Investigated
earlier in the project. Its Always Free tier is available only in a single
region, and a trial/free-tier tenancy is capped at one subscribed region —
confirmed not from documentation alone but from the actual console error
(*"exceeded maximum regions"*) when attempting to subscribe. Tables
created outside that region showed no Always Free eligibility and only
paid capacity modes. Dead end on the free tier.

**Oracle Autonomous Database (Always Free) — the candidate.** A genuinely
free-forever managed relational database (2 instances, 1 OCPU / 20 GB
each). Being relational, it maps closely to the existing schema, which
keeps the migration mechanical rather than a redesign. **Availability in
the tenancy's home region was never confirmed** — see open questions.

## Triggers for revisiting

Any one of these turns the deferred decision into a live one:

- **A second host needs the data** — the most likely trigger. Splitting
  the bots into separate containers, or scaling the public service out
  (§A2), immediately requires shared state.
- **Real users with data worth keeping.** The "reconstructible" argument
  above expires the moment losing the data would actually cost someone
  something.
- **A webhook-based channel** (`docs/plans/multi-channel-plan.md`) that changes
  the deployment topology.
- **Write concurrency becoming real** — multiple writers make
  single-file SQLite an active liability rather than a passive limit.

## Open questions

- **Is Always Free Autonomous Database available in this tenancy's home
  region?** This is the blocking unknown, and it's exactly what sank the
  NoSQL option. It needs checking in the console before any design work —
  the earlier lesson being that regional availability claims must be
  verified against the actual account, not the marketing page.
- Relational (Autonomous DB) or document-shaped storage? Relational is a
  closer fit to the current schema; a document store would suit the
  JSON-encoded columns better. The answer depends on whether those columns
  get normalized during migration.
- Should backup (item 2) be done independently and sooner, given it's
  cheap and addresses a different risk? It doesn't advance the migration,
  but "the data is reconstructible" is an argument that weakens over time.
