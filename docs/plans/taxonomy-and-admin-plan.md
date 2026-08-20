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

```sql
CREATE TABLE IF NOT EXISTS categories (
    name         TEXT PRIMARY KEY,     -- the label the classifier emits
    description  TEXT NOT NULL,        -- the one-line gloss in the prompt
    created_at   TEXT NOT NULL,
    created_by   TEXT,                 -- 'seed' | 'admin:<chat_id>'
    active       INTEGER NOT NULL DEFAULT 1,
    merged_into  TEXT,                 -- set when merged, points at survivor
    centroid     BLOB                  -- optional, see A5
);

CREATE TABLE IF NOT EXISTS category_proposals (
    label        TEXT PRIMARY KEY,     -- an out-of-taxonomy label the model used
    hits         INTEGER NOT NULL,     -- how many times it's been proposed
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    examples     TEXT,                 -- JSON list of a few article titles
    status       TEXT NOT NULL         -- 'pending' | 'accepted' | 'rejected'
);
```

**Name as primary key, not an integer id.** Cached article files store
category *names* as strings, and the cache is a separate store from the
DB (files on a volume, not rows). An integer id would mean either
rewriting every cached article on a rename, or a join the file store
can't do. Names are stable enough — a rename is a merge, which A4 handles.

**`merged_into` rather than deleting.** An article cached last week
carries the old name. Keeping a tombstone that points at the survivor lets
reads resolve the old name instead of silently dropping those articles out
of every filter.

**`centroid` is reserved now, unused at first.** The measurements in
`cluster-measurements.md` point at eventually classifying with embeddings
(nearest centroid, no API cost) instead of an LLM call per batch. Whether
or not that happens, the taxonomy is shared infrastructure between both
designs — only the classifier changes. Adding the column now is free;
migrating a live table later is not.

### A2. The classifier reads the taxonomy from the DB

`news_classify` currently builds both the prompt and the validation set
from the `Category` Literal. Both become DB reads:

- `_CLASSIFY_PROMPT`'s category list is generated from `categories` rows
  (`name` + `description`), cached per process with an explicit
  invalidation on write.
- `_CATEGORY_SET` (already used for filtering, added 2026-08-20) reads
  from the same place.

**This is already half-done.** `ArticleCategories.categories` was changed
from `list[Category]` to `list[str]` on 2026-08-20 for an unrelated
reason — a single invented label was failing whole batches. That change
removed the static Literal from the schema, which is exactly the thing
that would have blocked a dynamic taxonomy. What remains is swapping the
constant for a query.

The 13 current categories are seeded on first run, `created_by='seed'`.

### A3. Noticing a category is missing

**Not per-article.** 1,866 of 2,266 cached articles currently have no
categories. Even with the backlog fixed, a normal cycle of ~276 articles
produces some that legitimately have none — sports, weather, off-topic
noise. An alert per uncategorized article is hundreds a day and would be
muted within a week. "Has no category" does not mean "needs a new
category"; it usually means "isn't relevant."

Two signals that *do* mean it, both already available:

1. **Rejected labels.** When the model emits a label outside the
   taxonomy, that is the model stating what it wanted. `_valid_categories`
   already drops and logs these; instead it upserts into
   `category_proposals` and increments `hits`. `Education` is the first
   real instance.
2. **Cohesive clusters of uncategorized articles.** Run the clustering
   from `docs/analysis/tools/build_taxonomy.py` over articles with no
   categories. A cluster of ≥8 with coherence well above the random-pair
   baseline is a topic with no home. This is a batch job, not per-cycle.

Alert when `hits` crosses a threshold (start at 5, tune from data), not on
first sight. One label proposed once is a model slip; the same label five
times is a gap.

### A4. The admin loop

On threshold, the admin bot sends:

> The classifier has proposed **Education** 5 times and it isn't in the
> taxonomy. Examples:
> · "Stanford launches free AI curriculum for high schools"
> · "How universities are rewriting CS degrees around LLMs"
>
> Current categories: AI, Software, Hardware, IT, Startups, Finance,
> Stock, Policy, Security, Research, Consumer, Robotics, Crypto
>
> [ Add as new ] [ Merge into… ] [ Reject ]

Reuses `CallbackQueryHandler`, which `admin_bot.py` already uses for
approve/deny — no new interaction pattern.

- **Add as new** → prompts for a one-line description (it goes in the
  classifier prompt, so it matters), inserts the row.
- **Merge into…** → shows existing categories, sets `merged_into`.
- **Reject** → `status='rejected'`, stop proposing it.

### A5. Propagating a new category

Adding a category has two consequences, and the second is the one that
gets forgotten.

**Interests must be re-mapped.** `interest_categories` caches
interest→categories, and `get_cached_interest_categories` treats any row
as a hit. A new category is invisible to every already-cached interest
until the cache is invalidated. So: **adding a category deletes every row
in `interest_categories`**, and the next push cycle re-resolves them.

**Use the LLM for this; don't optimise it.** The table currently holds
**8 rows**. Re-classifying every interest is one batched call. The API
cost that motivated the embedding work in `cluster-measurements.md` is
about *articles* — thousands per day, growing — not interests, which are
a dozen strings of stable vocabulary. Spending design effort avoiding an
LLM call here is optimising the wrong term.

**Past articles are not re-classified.** A new category applies going
forward. Back-filling would mean re-classifying the whole cache on every
category addition, which is the expensive direction. (Separately, there
*is* an unresolved backlog problem — articles cached during the
2026-08-17 outage have no categories and are never retried. That needs
distinguishing "never classified" from "classified as nothing applies",
which today are both `categories: []`. Tracked as its own item, not part
of this plan.)

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
