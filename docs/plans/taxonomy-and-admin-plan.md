# Plan: DB-backed taxonomy, admin-in-the-loop growth, and an admin console

Status: **proposed, not started.** Written 2026-08-20 from a design
conversation. Nothing here is built.

Two related pieces:

- **A.** Move the category taxonomy out of code into the database, and add
  a loop where the system notices it needs a new category and asks an
  admin, rather than waiting for someone to notice and redeploy.
- **B.** An admin console for the data behind the bot — categories,
  subscribers, and what subscribers have been doing.

They're sequenced: B has nothing to manage until A exists.

---

## Why now

The 13 categories in `news_classify.Category` are a `Literal` in source.
Adding one means editing code, running `code-reviewer` → `qa-engineer` →
`deploy-engineer`, and redeploying the container. That is a heavy process
for what is fundamentally a data change, and the cost shows: the taxonomy
is labelled "an explicit v1" in its own docstring and has never been
revised, through a month of production traffic that has given clear
evidence it needs revising.

Concretely, measured (see `docs/analysis/cluster-measurements.md`):

- The model reached for `Education`, a label outside the taxonomy. Until
  2026-08-20 that failed the whole 50-article batch; now it is dropped and
  logged. Nobody reads the log.
- Real subscriber interests have no category that fits: `光通訊` and `AAOI`
  route to Entertainment & Media, `semiconductors` to Markets & Stocks.
  There is no Telecom/Networking category and no Hardware/Semiconductor
  one distinct from generic Hardware.

---

## A. DB-backed taxonomy

### A1. Schema

One table for the taxonomy, with a status enum, plus a separate log of
sightings.

```sql
CREATE TABLE IF NOT EXISTS categories (
    name          TEXT PRIMARY KEY,   -- the label the classifier emits
    description   TEXT,               -- one-line gloss; goes in the prompt.
                                      -- NULL while merely proposed
    status        TEXT NOT NULL,      -- see the state machine below
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,      -- 'seed' | 'model' | 'admin:<chat_id>'
    decided_at    TEXT,               -- when an admin last changed status
    decided_by    TEXT,
    merged_into   TEXT REFERENCES categories(name),
    centroid      BLOB                -- reserved; see A6
);

CREATE TABLE IF NOT EXISTS category_sightings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,      -- the proposed label, not a FK:
                                      -- sightings can precede the row
    seen_at       TEXT NOT NULL,
    article_link  TEXT,               -- one example, for the admin prompt
    article_title TEXT
);
CREATE INDEX IF NOT EXISTS category_sightings_name_at
    ON category_sightings (name, seen_at);
```

#### Why one table rather than a separate proposals table

An earlier draft of this plan had `categories` and `category_proposals` as
separate tables. Approving a proposal then meant *migrating a row*:
duplicating the name, inventing a description, and either copying or
abandoning the sighting history. One table with a status makes approval a
single field update, and the sighting history stays attached to the thing
it is evidence about.

#### Why `status` and not an `active` boolean

`active = 0` would have to mean two opposite things: "proposed, not yet
approved" and "was approved, now retired". They need opposite handling —
a retired category must never be re-proposed, while a proposed one should
keep accumulating evidence. One boolean cannot carry that.

```
                    model emits an unknown label
                                │
                                ▼
                          ┌───────────┐
        admin rejects ◄───│ proposed  │───► admin approves
              │           └───────────┘             │
              ▼                                     ▼
        ┌──────────┐                          ┌──────────┐
        │ rejected │                          │  active  │◄── 'seed'
        └──────────┘                          └────┬─────┘
     never re-proposed,                            │
     sightings still counted                admin retires │ admin merges
     silently (see A4)                            ▼       ▼
                                          ┌──────────┐ ┌──────────┐
                                          │ retired  │ │  merged  │
                                          └──────────┘ └──────────┘
                                        not offered to    reads resolve
                                        the classifier,   to merged_into
                                        old articles keep
                                        the label
```

Only `status='active'` rows are offered to the classifier. Everything else
exists so the system can remember a decision instead of re-litigating it.

#### Why `name` is the primary key

Cached article files store category **names** as strings, and that cache
is a separate store from the DB — YAML files on a volume, not rows. An
integer id would mean either rewriting every cached file when a name
changes, or a join the file store cannot perform. Names are stable enough:
a rename is a merge, which A5 handles.

#### Why sightings are a log, not a counter on the category row

A counter cannot expire. `Education` proposed three times in January and
twice in June reads as "5" — but that is six months of noise, not a
trend. What the threshold needs to ask is *"how often in the last 30
days"*, which requires timestamps.

This is the same reasoning `users_db.PUSHED_LINK_RETENTION_HOURS` already
follows: prune by age, not by count. That constant replaced a
count-based cap for a related reason — truncating by count let a busy
period silently evict entries that still mattered.

Retention: prune `category_sightings` older than
`CATEGORY_SIGHTING_RETENTION_DAYS = 30` on each ingestion cycle, alongside
the existing cache cleanup.

### A2. The classifier reads the taxonomy from the DB

`news_classify` builds both the prompt and the validation set from the
`Category` Literal today. Both become reads of `status='active'`:

- The category list in `_CLASSIFY_PROMPT` is generated from `name` +
  `description`.
- `_CATEGORY_SET`, already used for filtering since 2026-08-20, reads the
  same rows.

**Half of this is already done, by accident.** `ArticleCategories.categories`
was changed from `list[Category]` to `list[str]` on 2026-08-20 to fix an
unrelated bug — one invented label was failing whole 50-article batches.
That change removed the static Literal from the structured-output schema,
which is exactly what would otherwise block a dynamic taxonomy. What
remains is swapping a module constant for a query.

Both reads are cached per process and invalidated on any write to
`categories`. The bot is a single long-running process, so an uncached
read per classification batch would also be fine; the cache is for the
prompt string, which is rebuilt from every active row.

The 13 current categories are seeded on first run with
`status='active', created_by='seed'`.

### A3. Recording a sighting

When `_valid_categories` drops a label that isn't in the active set, it
now also records it:

1. `INSERT INTO category_sightings (name, seen_at, article_link, article_title)`.
2. `INSERT OR IGNORE INTO categories (name, status, created_at, created_by)
   VALUES (?, 'proposed', ?, 'model')` — so the first sighting also creates
   the proposed row, with a NULL description until an admin supplies one.

Step 2 is `OR IGNORE` rather than an upsert on purpose: if the row already
exists as `rejected`, `retired`, or `merged`, that decision stands. A
sighting never resurrects a decided category.

**Not per-article alerting.** 1,866 of 2,266 cached articles currently
have no categories, and even with the backlog fixed a normal ~276-article
cycle produces some that legitimately have none — sports, weather,
off-topic noise. "Has no category" almost always means "isn't relevant",
not "needs a new category". The two signals that *do* mean it are the
out-of-taxonomy labels recorded here, and (separately, as a batch job)
cohesive clusters among uncategorized articles using
`docs/analysis/tools/build_taxonomy.py`.

### A4. Threshold and the admin prompt

On each ingestion cycle, after recording sightings:

```sql
SELECT c.name, COUNT(s.id) AS hits
FROM categories c
JOIN category_sightings s ON s.name = c.name
WHERE c.status = 'proposed'
  AND s.seen_at >= :window_start
GROUP BY c.name
HAVING hits >= :threshold;
```

`CATEGORY_PROPOSAL_THRESHOLD` starts at **5 within 30 days** and is
explicitly a guess to be replaced. `Education` is currently a sample of
one, and choosing a threshold from one data point is not a decision, it is
a placeholder. Step 2 of the sequencing below exists to collect the
distribution first.

A `rejected` category keeps accumulating sightings but never alerts. That
is deliberate: it costs one row per sighting and it answers "was rejecting
this right?" later. If a rejected label keeps appearing at ten times the
threshold, that is worth knowing.

The admin bot then sends, reusing the `CallbackQueryHandler` it already
uses for approve/deny:

> The classifier has proposed **Education** 7 times in the last 30 days.
> Recent examples:
> · "Stanford launches free AI curriculum for high schools"
> · "How universities are rewriting CS degrees around LLMs"
>
> Active categories: AI, Software, Hardware, IT, Startups, Finance, Stock,
> Policy, Security, Research, Consumer, Robotics, Crypto
>
> [ Activate ] [ Merge into… ] [ Reject ]

**Activate** prompts for a description before flipping status — the
description goes into the classifier prompt for every subsequent article,
so a vague one silently degrades classification from then on. The model
should draft it from the example articles and the admin edit rather than
face a blank field.

### A5. Lifecycle operations, and what each one touches

Three stores hold category names: the `categories` table, the
`interest_categories` cache, and the cached article YAML files. Every
operation has to account for all three.

| Operation | `categories` | `interest_categories` | Article files |
|---|---|---|---|
| **Propose** | insert `proposed` | — | — |
| **Activate** | → `active`, set description | **delete all rows** | — |
| **Reject** | → `rejected` | — | — |
| **Merge A→B** | A → `merged`, `merged_into='B'` | rewrite A→B, dedupe | untouched; resolved on read |
| **Retire** | → `retired` | **re-resolve affected** | untouched |
| **Delete** | hard delete | — | — |

#### Activate: invalidate the whole interest cache

`get_cached_interest_categories` treats any existing row as a hit, so a
newly active category is invisible to every already-cached interest until
those rows are gone. Adding a category therefore **deletes every row in
`interest_categories`**; the next push cycle re-resolves them via
`resolve_interest_categories`.

**Use the LLM for this, and don't optimise it.** That table currently
holds **8 rows**. Re-classifying every interest is one batched call. The
API cost that motivated the embedding work in
`docs/analysis/cluster-measurements.md` is about *articles* — thousands
per day and growing — not interests, which are a dozen strings of stable
vocabulary. Avoiding an LLM call here optimises the wrong term.

#### Merge: rewrite interests, resolve articles on read

`interest_categories` rows are rewritten (`A` → `B`, deduplicated within
each row's JSON list) rather than invalidated — the mapping is known, so
there is nothing to re-derive and no reason to pay for a call.

Article files are left alone. A read path resolves a name through
`merged_into` before matching. The alternative — rewriting every cached
YAML file — is a bulk operation over the whole cache for a rename, and the
tombstone costs one dictionary lookup per match. If merges turn out to be
frequent, revisit; a batch rewrite may become simpler than carrying the
indirection.

#### Retire: the hazard worth designing around

Retiring a category removes it from every interest that mapped to it. If
that leaves an interest with an **empty** list, that interest now matches
**every** article, because `select_candidate_articles` treats an empty
mapping as unrestricted (deliberately — an unclassified topic shouldn't be
starved).

So retiring a category can silently turn a subscriber's filter off. This
is not hypothetical: it is the same shape as the bug fixed on 2026-08-20,
where cached classification *failures* were stored as `[]` and six
subscribers were receiving entirely unfiltered news.

Retire therefore does not simply strip the name. It **re-resolves** every
interest that mapped to the retired category, and if re-resolution leaves
one empty, the admin is told which interests and which subscribers before
the retire commits.

#### Delete: only for categories that were never active

Hard delete is allowed only from `proposed` or `rejected`, and only when
no article file or interest row references the name. Anything that has
ever been `active` can be `retired` or `merged`, never deleted — deleting
it would leave orphaned names in article files that resolve to nothing and
silently drop those articles out of every filter.

### A6. The `centroid` column

Reserved, unused at first. The measurements in
`docs/analysis/cluster-measurements.md` point at eventually classifying by
nearest centroid over embeddings — no API cost per article — rather than
an LLM call per batch. The taxonomy is shared infrastructure between both
designs; only the classifier changes. Adding a nullable BLOB now is free,
and migrating a live table later is not.

If that lands, `Activate` gains one step: compute the centroid from the
sighted articles' embeddings and store it. Everything else in this design
is unchanged.

### A7. What is deliberately not solved here

**Articles classified before a category existed do not get it.** A new
category applies going forward. Backfilling would mean re-classifying the
whole cache on every activation, which is the expensive direction and
would grow with the cache.

Separately and unresolved: articles cached during the 2026-08-17 outage
have no categories and are **never retried**, because nothing distinguishes
"never classified" from "classified as nothing applies" — both are
`categories: []`. That is the same root cause as the interest-cache bug
fixed on 2026-08-20, on the article side, and it needs a `classified_at`
field on the cached record. Tracked separately; it is not part of this
plan and blocks the current 17% categorization rate from improving.

---

## B. Admin console

### B1. Bot commands first, not a web UI

The admin bot exists, is authenticated by `ADMIN_CHAT_ID`, and reaches the
admin wherever they are. A web console needs a service, a login, TLS, a
port, and rules in *both* firewall layers (OCI Security List and the
host's `iptables` — see `docs/current/infrastructure.md`), on a VM that
has ~250 MB of headroom.

Chat is a worse interface for merging categories than a table with
checkboxes. It is also roughly a tenth of the work and adds no new attack
surface. Start there; a web UI stays open if the commands turn out to be
genuinely painful in practice rather than in anticipation.

### B2. Commands

| Command | Does |
|---|---|
| `/categories` | List with article counts and subscriber counts |
| `/category_add <name>` | Prompts for description, inserts |
| `/category_merge <from> <into>` | Sets `merged_into`, invalidates interest cache |
| `/category_retire <name>` | `active=0`; stops being offered to the classifier, existing articles keep the label |
| `/proposals` | Pending `category_proposals` by hit count |
| `/users` | Subscribers, status, interests, push settings |
| `/user <chat_id>` | One subscriber in detail, recent activity |

**Proactive insert vs waiting:** wait. The measured evidence is that a
taxonomy invented up front is wrong in ways you can't predict — the
current 13 were invented up front and are wrong in three specific,
*measured* ways nobody anticipated. `/category_add` exists for when the
admin already knows (`Telecom` is a safe bet given two subscribers track
optical networking), but the proposals queue is the intended path.

### B3. Audit log

New table:

```sql
CREATE TABLE IF NOT EXISTS user_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- 'message' | 'setting_change' | 'push_sent' | 'blocked'
    detail      TEXT             -- JSON; see retention note
);
CREATE INDEX IF NOT EXISTS user_activity_chat_at ON user_activity (chat_id, at);
```

**Decide retention before writing the first row, not after.** This stores
what real people typed. The project already has the pattern —
`users_db.PUSHED_LINK_RETENTION_HOURS` prunes by age rather than letting
a column grow forever — and the same should apply here. A proposed
default: 30 days for `kind='message'` (the sensitive one), longer for
setting changes and push events, which are operational rather than
personal.

Worth stating plainly because it is easy to drift into: the useful
operational questions ("is this subscriber getting pushes?", "did their
interest change?", "was their message blocked by a guardrail?") are all
answerable from metadata. Storing full message text is a bigger
commitment than it looks, and should be a deliberate decision rather than
a side effect of `detail` being a free-form JSON column.

---

## Sequencing

1. **A1 + A2** — schema, seed the 13, classifier reads from DB. No
   behaviour change; the taxonomy is identical, it just lives elsewhere.
   This is the risky migration, so it lands alone and gets verified alone.
2. **A3** — proposals table, populate it from rejected labels. Still no
   admin interaction; just accumulate evidence and see what the threshold
   should actually be, from real data rather than a guess.
3. **A4 + A5** — the admin loop and interest-cache invalidation.
4. **B2** — the read-only commands (`/categories`, `/proposals`, `/users`)
   before the mutating ones.
5. **B3** — audit log, once the retention question is answered.

Step 2 is deliberately a waiting step. `Education` is currently a sample
of one, and picking an alert threshold from one data point is guessing.

---

## Open questions

- **Description text quality matters more than it looks.** Category
  descriptions go into the classifier prompt, so an admin typing a vague
  one silently degrades classification for every article afterwards. Worth
  showing the admin the resulting prompt fragment before committing, or
  having the model draft the description from the example articles.
- **What happens to `RESTRICTED_SOURCES` and per-category source rules?**
  Not currently modelled at all; if categories become data, source
  restrictions probably should too. Out of scope here, flagged.
- **Does a merge need to rewrite cached article files?** The
  `merged_into` tombstone avoids it, at the cost of a resolution step on
  every read. If merges turn out to be common, a batch rewrite may be
  simpler than carrying the indirection forever.
