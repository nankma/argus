import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import category_ops
import interest_cache_ops
import storage

# --- categories taxonomy (docs/plans/taxonomy-and-admin-plan.md A1) -------


def test_init_seeds_the_taxonomy_as_active(isolated_subscribers_db):
    """Counts the seed rather than hardcoding a number: the taxonomy is data
    now, and a test asserting "13" turns every future category addition into
    a test failure that says nothing useful."""
    active = dict(category_ops.get_active_categories())

    # everything seeded except Policy, which _migrate_split_policy retires
    expected = {name for name, _ in category_ops.SEED_CATEGORIES} - {"Policy"}
    assert set(active) == expected
    assert active["Stock"].startswith("stock price moves")


def test_seeding_is_idempotent_and_does_not_resurrect_a_retired_category(
    isolated_subscribers_db,
):
    """INSERT OR IGNORE rather than a count check: an admin who retires a
    category must not find it back after the next restart."""
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text("UPDATE categories SET status = 'retired' WHERE name = 'Crypto'"))

    storage.init_db()
    category_ops.bootstrap()

    names = [name for name, _ in category_ops.get_active_categories()]
    assert "Crypto" not in names


def test_get_active_categories_excludes_non_active_statuses(isolated_subscribers_db):
    category_ops.record_category_sighting("Education", datetime(2026, 8, 20, tzinfo=timezone.utc))

    names = [name for name, _ in category_ops.get_active_categories()]
    assert "Education" not in names, "a proposed category is not offered to the classifier"


def test_active_category_order_is_stable(isolated_subscribers_db):
    """The prompt is built from this order, so an unstable one would change
    the prompt string between runs for no reason."""
    assert category_ops.get_active_categories() == category_ops.get_active_categories()


def test_recording_a_sighting_creates_a_proposed_category(isolated_subscribers_db):
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    category_ops.record_category_sighting("Education", now, "https://e.com/a", "A title")

    assert category_ops.count_recent_sightings(now) == {"Education": 1}


def test_a_sighting_does_not_resurrect_a_decided_category(isolated_subscribers_db):
    """Evidence must not overturn a decision someone already made. A
    rejected label keeps accumulating sightings -- they answer "was
    rejecting this right?" later -- but never returns to 'proposed'."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    category_ops.record_category_sighting("Education", now)
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text("UPDATE categories SET status = 'rejected' WHERE name = 'Education'"))

    category_ops.record_category_sighting("Education", now)

    with storage.get_storage()._engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM categories WHERE name = 'Education'")
        ).fetchone()[0]
    assert status == "rejected"
    assert category_ops.count_recent_sightings(now) == {}, "rejected never alerts again"


def test_sightings_outside_the_window_do_not_count(isolated_subscribers_db):
    """The threshold asks "how often recently", not "how often ever" -- a
    counter column could not express that, which is why sightings are a log."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    category_ops.record_category_sighting("Education", now - timedelta(days=60))
    category_ops.record_category_sighting("Education", now - timedelta(days=2))

    assert category_ops.count_recent_sightings(now, days=30) == {"Education": 1}


def test_pruning_drops_only_sightings_past_retention(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    category_ops.record_category_sighting("Education", now - timedelta(days=60))
    category_ops.record_category_sighting("Education", now - timedelta(days=2))

    assert category_ops.prune_category_sightings(now, days=30) == 1
    assert category_ops.count_recent_sightings(now, days=30) == {"Education": 1}


def test_resolve_category_name_follows_a_merge(isolated_subscribers_db):
    """An article cached before a merge still carries the old name; it must
    resolve to the survivor rather than dropping out of every filter."""
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text(
            "UPDATE categories SET status = 'merged', merged_into = 'Finance' "
            "WHERE name = 'Stock'"
        ))

    assert category_ops.resolve_category_name("Stock") == "Finance"
    assert category_ops.resolve_category_name("Finance") == "Finance"
    assert category_ops.resolve_category_name("Nonexistent") is None


def test_resolve_category_name_survives_a_merge_cycle(isolated_subscribers_db):
    """A merge chain that loops would otherwise hang the push cycle."""
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text("UPDATE categories SET status='merged', merged_into='Finance' WHERE name='Stock'"))
        conn.execute(text("UPDATE categories SET status='merged', merged_into='Stock' WHERE name='Finance'"))

    assert category_ops.resolve_category_name("Stock") is None


# --- Policy split (2026-08-20) --------------------------------------------


def test_policy_is_retired_and_replaced_by_its_four_parts(isolated_subscribers_db):
    """Policy's description was literally "regulation, government, legal,
    antitrust" -- a bundle of four things, measured absorbing 65% of every
    category assignment on a general-news probe."""
    active = {name for name, _ in category_ops.get_active_categories()}

    assert "Policy" not in active
    assert {"Regulation", "Government", "Legal", "Antitrust"} <= active


def test_retired_policy_still_resolves(isolated_subscribers_db):
    """Articles cached before the split carry the Policy label and must not
    silently resolve to nothing -- that would drop them out of every filter."""
    assert category_ops.resolve_category_name("Policy") == "Policy"


def test_split_does_not_re_retire_a_deliberately_reactivated_policy(
    isolated_subscribers_db,
):
    """The migration is guarded by a marker, not by Policy's own status. An
    admin who reactivates Policy on purpose must not find it retired again
    after the next restart -- the same resurrection problem _seed_categories'
    INSERT OR IGNORE avoids in the other direction."""
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text("UPDATE categories SET status = 'active' WHERE name = 'Policy'"))

    storage.init_db()
    category_ops.bootstrap()

    assert "Policy" in {name for name, _ in category_ops.get_active_categories()}


def test_split_is_recorded_as_a_migration_not_an_admin_decision(isolated_subscribers_db):
    with storage.get_storage()._engine.begin() as conn:
        row = conn.execute(
            text("SELECT status, decided_by FROM categories WHERE name = 'Policy'")
        ).fetchone()
    assert row == ("retired", "migration")


def _undo_policy_split(conn) -> None:
    """Rolls a database that already went through init_db() (and therefore
    already ran _migrate_split_policy once, via isolated_subscribers_db) back
    to a pre-migration state: Policy active again, the four new rows gone,
    the migration marker gone. Lets a test simulate "a database that looked
    like production before this migration ever ran" without needing a
    checkout of main's category.py -- the table SCHEMA is unchanged by this
    branch, only SEED_CATEGORIES' data and the new _migrate_split_policy
    call, so undoing just those two effects reconstructs the pre-migration
    state exactly."""
    conn.execute(text(
        "UPDATE categories SET status = 'active', decided_at = NULL, decided_by = NULL "
        "WHERE name = 'Policy'"
    ))
    conn.execute(text(
        "DELETE FROM categories WHERE name IN ('Regulation', 'Government', 'Legal', 'Antitrust')"
    ))
    conn.execute(text("DELETE FROM health_state WHERE key = 'policy_split_migrated'"))


def test_migration_is_safe_on_a_realistic_pre_existing_database(isolated_subscribers_db):
    """Builds a database shaped like production before this migration ever
    ran -- 45 subscribers spanning approved/pending/denied, 13
    interest_categories mappings (none pointing at Policy, matching what was
    confirmed against the live DB), 5 proposed categories, and an unrelated
    health_state row -- then runs init_db() (which retires Policy and adds
    the four new categories) and asserts every pre-existing table/row is
    byte-identical afterwards, except Policy's own status/decided_at/
    decided_by. A migration that mutates existing rows (unlike a purely
    additive one) is exactly the kind of change a narrow "does Policy get
    retired" test can pass while still silently corrupting something else."""
    with storage.get_storage()._engine.begin() as conn:
        _undo_policy_split(conn)

        now = datetime.now(timezone.utc).isoformat()
        for i in range(1, 46):
            status = "approved" if i <= 40 else ("pending" if i <= 43 else "denied")
            conn.execute(
                text(
                    "INSERT INTO subscribers (chat_id, username, first_name, status, requested_at, "
                    "decided_at, interests, push_enabled, push_interval_hours, last_push_at, "
                    "pushed_links, language) VALUES "
                    "(:chat_id, :username, :first_name, :status, :requested_at, :decided_at, "
                    ":interests, :push_enabled, :push_interval_hours, :last_push_at, :pushed_links, :language)"
                ),
                {
                    "chat_id": 2000 + i, "username": f"user{i}", "first_name": f"First{i}",
                    "status": status, "requested_at": now,
                    "decided_at": now if status != "pending" else None,
                    "interests": json.dumps(["AI", "Finance"] if i % 2 == 0 else ["Policy watch"]),
                    "push_enabled": 1 if i % 3 == 0 else 0, "push_interval_hours": 24,
                    "last_push_at": None, "pushed_links": "{}", "language": "en",
                },
            )

        interest_rows = [
            ("AI", ["AI"]), ("Finance", ["Finance", "Stock"]),
            ("robotics stuff", ["Robotics"]), ("crypto news", ["Crypto"]),
            ("chip shortage", ["Hardware"]), ("cybersecurity", ["Security"]),
            ("startup funding", ["Startups"]), ("cloud computing", ["IT"]),
            ("gadgets", ["Consumer"]), ("academic ai research", ["Research"]),
            ("dev tools", ["Software"]), ("earnings calls", ["Finance"]),
            ("some obscure ticker", []),
        ]
        assert len(interest_rows) == 13  # matches the live DB's row count
        for interest, cats in interest_rows:
            conn.execute(
                text("INSERT INTO interest_categories (interest, categories) VALUES (:interest, :categories)"),
                {"interest": interest, "categories": json.dumps(cats)},
            )

        conn.execute(
            text("INSERT INTO health_state (key, value) VALUES ('some_other_marker', :value)"),
            {"value": json.dumps(["unrelated"])},
        )

        proposed = ["Robotaxi", "Quantum", "Espionage", "Wearables", "Chips Act"]
        for i, name in enumerate(proposed):
            conn.execute(
                text(
                    "INSERT INTO categories (name, description, status, created_at, created_by, sort_order) "
                    "VALUES (:name, NULL, 'proposed', :created_at, 'model', :sort_order)"
                ),
                {"name": name, "created_at": now, "sort_order": 100 + i},
            )

    def snapshot():
        with storage.get_storage()._engine.begin() as conn:
            tables = [r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).fetchall()]
            return {t: set(conn.execute(text(f"SELECT * FROM {t}")).fetchall()) for t in tables}

    before = snapshot()

    storage.init_db()
    category_ops.bootstrap()

    after = snapshot()

    # Every table except `categories` and `health_state` must be untouched.
    for table in before:
        if table in ("categories", "health_state"):
            continue
        assert after[table] == before[table], f"{table} changed by the migration"

    # health_state: the pre-existing unrelated row survives untouched, and
    # only the new migration marker is added.
    with storage.get_storage()._engine.begin() as conn:
        marker = conn.execute(
            text("SELECT value FROM health_state WHERE key = 'some_other_marker'")
        ).fetchone()
    assert marker is not None and json.loads(marker[0]) == ["unrelated"]

    # categories: only Policy's status/decided_at/decided_by changed, and
    # exactly the four new rows were added -- nothing else in the table
    # (the 5 proposed rows, the system "Other" row, the other 12 seed rows)
    # moved at all.
    before_cats = {row[0]: row for row in before["categories"]}
    after_cats = {row[0]: row for row in after["categories"]}
    changed_names = {
        name for name in before_cats
        if name in after_cats and before_cats[name] != after_cats[name]
    }
    assert changed_names == {"Policy"}
    assert set(after_cats) - set(before_cats) == {"Regulation", "Government", "Legal", "Antitrust"}
    assert not set(before_cats) - set(after_cats)  # nothing disappeared

    for name in ["Robotaxi", "Quantum", "Espionage", "Wearables", "Chips Act"]:
        assert after_cats[name][2] == "proposed"  # status column untouched


def test_migration_is_idempotent_on_a_realistic_database(isolated_subscribers_db):
    """Running init_db() a second time after the migration already applied
    must be a true no-op -- not just "Policy stays retired" (already covered
    by test_split_does_not_re_retire_a_deliberately_reactivated_policy) but
    literally zero rows anywhere change."""
    with storage.get_storage()._engine.begin() as conn:
        _undo_policy_split(conn)

    storage.init_db()  # first application of the migration
    category_ops.bootstrap()

    def snapshot():
        with storage.get_storage()._engine.begin() as conn:
            tables = [r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).fetchall()]
            return {t: set(conn.execute(text(f"SELECT * FROM {t}")).fetchall()) for t in tables}

    once = snapshot()
    storage.init_db()
    category_ops.bootstrap()
    storage.init_db()
    category_ops.bootstrap()
    twice = snapshot()

    assert once == twice


def test_policy_split_does_not_touch_pre_existing_interest_category_mappings(
    isolated_subscribers_db,
):
    """The plan doc's general "Retire" lifecycle operation
    (docs/plans/taxonomy-and-admin-plan.md, A5a) calls for re-mapping any
    interest that pointed at the retired category, so it doesn't end up
    matching every article (see news_push.select_candidate_articles: an
    interest mapped to an empty category list is unrestricted). That
    re-mapping machinery is explicitly NOT built yet (A4 onward, per the
    doc's own Status line) -- this migration's docstring says it verified
    by hand that nothing live mapped to Policy and skipped the step on that
    basis, not because the step was performed.

    This pins that skip as real, observable behaviour: an interest that
    already mapped to ["Policy"] before the migration keeps that exact
    mapping afterwards, unre-mapped and unstripped. This is not itself a
    bug for THIS migration (verified against production data), but it is
    the gap that would matter for a future retirement that DOES have live
    mappings -- there is nothing here, or anywhere else in this codebase,
    that would re-map or even flag that case."""
    with storage.get_storage()._engine.begin() as conn:
        _undo_policy_split(conn)
        conn.execute(
            text("INSERT INTO interest_categories (interest, categories) VALUES (:interest, :categories)"),
            {"interest": "legacy policy watcher", "categories": json.dumps(["Policy"])},
        )

    storage.init_db()
    category_ops.bootstrap()

    # Unchanged -- not re-mapped to the four new categories, not stripped,
    # not emptied. Still literally ["Policy"].
    assert interest_cache_ops.get_cached_interest_categories(["legacy policy watcher"]) == {
        "legacy policy watcher": ["Policy"]
    }


def test_seeding_promotes_a_name_the_model_had_already_proposed(isolated_subscribers_db):
    """Regression test for a live defect. The Policy split added Legal as a
    seed category, but the classifier had proposed "Legal" two hours
    earlier, so INSERT OR IGNORE skipped it: on production it stayed
    `proposed` with a NULL description and never entered the prompt. The
    split was 3/4 applied and nothing reported it.

    A seed is a deliberate decision that the category should exist, and
    `proposed` is a decision waiting to be made -- so the seed wins."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    with storage.get_storage()._engine.begin() as conn:
        conn.execute(text("UPDATE categories SET status = 'proposed', description = NULL, "
                          "created_by = 'model' WHERE name = 'Legal'"))

    storage.init_db()
    category_ops.bootstrap()

    active = dict(category_ops.get_active_categories())
    assert "Legal" in active
    assert active["Legal"], "and it gets its seeded description, not NULL"


def test_seeding_does_not_promote_a_name_an_admin_decided_on(isolated_subscribers_db):
    """The other half. rejected/retired/merged are decisions someone made,
    and seeding must not overturn them -- which is what INSERT OR IGNORE was
    protecting in the first place."""
    for decided in ("rejected", "retired", "merged"):
        with storage.get_storage()._engine.begin() as conn:
            conn.execute(text("UPDATE categories SET status = :decided WHERE name = 'Legal'"), {"decided": decided})

        storage.init_db()
        category_ops.bootstrap()

        names = {name for name, _ in category_ops.get_active_categories()}
        assert "Legal" not in names, f"seeding overturned an admin '{decided}'"


# --- A4: category review lifecycle ----------------------------------------


def _propose(name, now, hits=1):
    for i in range(hits):
        category_ops.record_category_sighting(name, now, f"https://e.com/{name}{i}", f"{name} story {i}")


def test_only_proposals_past_the_threshold_are_raised(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)
    _propose("Media", now, hits=2)

    ready = category_ops.categories_ready_for_review(now, threshold=5)

    assert ready == [("Healthcare", 5)]


def test_a_proposal_is_raised_once_not_every_cycle(isolated_subscribers_db):
    """An admin who has been asked and hasn't answered must not be asked
    again every four hours. The proposal stays in the table for a future
    /proposals command; the alert is a push, not a reminder loop."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)

    assert category_ops.categories_ready_for_review(now, threshold=5)
    category_ops.mark_category_alerted("Healthcare", now, "hospitals, drugs, clinical tech")
    assert category_ops.categories_ready_for_review(now, threshold=5) == []


def test_activating_uses_the_description_drafted_at_alert_time(isolated_subscribers_db):
    """The draft is stored on the row rather than carried through Telegram's
    callback_data (64 bytes) or re-derived on the button press. What the
    admin read in the message is what ships into the classifier prompt."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)
    category_ops.mark_category_alerted("Healthcare", now, "hospitals, drugs, clinical tech")

    assert category_ops.activate_category("Healthcare", "admin:1", now) is True

    assert dict(category_ops.get_active_categories())["Healthcare"] == "hospitals, drugs, clinical tech"


def test_activating_clears_interest_mappings_so_the_new_category_applies(
    isolated_subscribers_db,
):
    """get_cached_interest_categories treats any existing row as a hit, so a
    newly active category is invisible to every already-mapped interest
    until those rows are gone."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    interest_cache_ops.set_interest_categories("robotics", ["Robotics"])
    _propose("Healthcare", now, hits=5)

    category_ops.activate_category("Healthcare", "admin:1", now)

    assert interest_cache_ops.get_cached_interest_categories(["robotics"]) == {}


def test_a_second_press_of_activate_changes_nothing(isolated_subscribers_db):
    """Two admins, or one double-tap. Guarded by `AND status = 'proposed'`."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Healthcare", now, hits=5)

    assert category_ops.activate_category("Healthcare", "admin:1", now) is True
    assert category_ops.activate_category("Healthcare", "admin:2", now) is False


def test_rejecting_stops_it_being_raised_but_keeps_recording_sightings(
    isolated_subscribers_db,
):
    """Sightings after a rejection cost a row each and answer "was
    rejecting this right?" later. count_recent_sightings only counts
    'proposed', so it never alerts again."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Media", now, hits=5)
    assert category_ops.reject_category("Media", "admin:1", now) is True

    _propose("Media", now, hits=10)

    assert category_ops.categories_ready_for_review(now, threshold=1) == []
    with storage.get_storage()._engine.begin() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM category_sightings WHERE name = 'Media'")
        ).fetchone()[0]
    assert total == 15, "still recorded, just never raised"


def test_merging_rewrites_interests_without_a_model_call(isolated_subscribers_db):
    """The new mapping is known, so there is nothing to re-derive. Contrast
    activate, which must invalidate because the correct answer is unknown."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Antitrust2", now, hits=5)
    category_ops.activate_category("Antitrust2", "admin:1", now)
    interest_cache_ops.set_interest_categories("competition law", ["Antitrust2", "Legal"])

    assert category_ops.merge_category("Antitrust2", "Antitrust", "admin:1", now) is True

    assert interest_cache_ops.get_cached_interest_categories(["competition law"]) == {
        "competition law": ["Antitrust", "Legal"]
    }
    assert category_ops.resolve_category_name("Antitrust2") == "Antitrust"


def test_merging_deduplicates_when_both_names_were_present(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Antitrust2", now, hits=5)
    category_ops.activate_category("Antitrust2", "admin:1", now)
    interest_cache_ops.set_interest_categories("x", ["Antitrust2", "Antitrust"])

    category_ops.merge_category("Antitrust2", "Antitrust", "admin:1", now)

    assert interest_cache_ops.get_cached_interest_categories(["x"]) == {"x": ["Antitrust"]}


def test_merging_into_a_non_active_category_is_refused(isolated_subscribers_db):
    """Merging into a retired or already-merged category builds a chain
    whose only symptom is articles quietly resolving to nothing."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _propose("Media", now, hits=5)

    assert category_ops.merge_category("Media", "Policy", "admin:1", now) is False, "Policy is retired"
    assert category_ops.merge_category("Media", "Nonexistent", "admin:1", now) is False


def test_category_examples_come_back_newest_first(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    category_ops.record_category_sighting("Media", now - timedelta(days=2), "https://e/old", "Older")
    category_ops.record_category_sighting("Media", now, "https://e/new", "Newer")

    examples = category_ops.category_examples("Media", limit=2)

    assert [t for t, _ in examples] == ["Newer", "Older"]


def test_examples_from_one_cycle_are_returned_deterministically(isolated_subscribers_db):
    """Every sighting in an ingestion cycle shares a timestamp, so ordering
    by seen_at alone leaves which examples the admin sees unspecified --
    different between runs, for no reason."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    for i in range(5):
        category_ops.record_category_sighting("Media", now, f"https://e/{i}", f"Story {i}")

    first = category_ops.category_examples("Media", limit=3)
    assert first == category_ops.category_examples("Media", limit=3)
    assert [t for t, _ in first] == ["Story 4", "Story 3", "Story 2"], "most recent first"


def test_a_proposed_name_containing_a_colon_is_normalized(isolated_subscribers_db):
    """Telegram callback_data packs "cat:into:{name}:{target}" and the
    handler splits on ':'. A model-proposed label containing one would
    mis-parse on the button press -- the admin would be told "already
    decided" about a category that was never touched. Normalized where it
    is recorded, so the table only ever holds names the rest of the system
    can round-trip."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    category_ops.record_category_sighting("Health: Policy", now, "https://e/1", "T")

    assert category_ops.count_recent_sightings(now) == {"Health Policy": 1}


def test_an_overlong_proposed_name_is_truncated(isolated_subscribers_db):
    """callback_data caps at 64 bytes and the merge keyboard packs both a
    name and a target into it."""
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    category_ops.record_category_sighting("A" * 100, now)

    proposed = list(category_ops.count_recent_sightings(now))
    assert len(proposed[0]) == category_ops.MAX_CATEGORY_NAME_LENGTH


def test_a_name_that_normalizes_to_nothing_is_dropped(isolated_subscribers_db):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    category_ops.record_category_sighting("   :  ", now)

    assert category_ops.count_recent_sightings(now) == {}
