"""
Category taxonomy storage. Status literals ('active', 'proposed',
'rejected', 'retired', 'merged', 'system') are internal to this domain --
unlike subscriber status (PENDING/APPROVED/DENIED, shared across several
callers), nothing outside category_ops.py needs to name them, so they stay
as plain SQL literals here rather than parameters threaded down from DAL.
"""

import json

from sqlalchemy import text


class CategoryMixin:
    def get_active_categories(self) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT name, description FROM categories WHERE status = 'active' "
                "ORDER BY sort_order, name"
            )).fetchall()

    def resolve_category_name(self, name: str) -> str | None:
        """Follows `merged_into` so a name stored on an article cached
        before a merge still resolves to the surviving category. Prints
        (not raises) on a merge cycle, which should be impossible -- see
        category_ops.resolve_category_name's docstring."""
        seen = set()
        with self._engine.begin() as conn:
            while name and name not in seen:
                seen.add(name)
                row = conn.execute(
                    text("SELECT status, merged_into FROM categories WHERE name = :name"), {"name": name}
                ).fetchone()
                if row is None:
                    return None
                status, merged_into = row
                if status != "merged" or not merged_into:
                    return name
                name = merged_into
        print(f"[storage] merge cycle resolving category {name!r} -- taxonomy is corrupt")
        return None

    def record_category_sighting(self, name: str, seen_at: str, link: str | None, title: str | None) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                f"{self._insert_ignore_prefix('categories')} "
                "(name, status, created_at, created_by, sort_order) "
                "VALUES (:name, 'proposed', :seen_at, 'model', "
                "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories)) "
                f"{self._on_conflict_nothing(['name'])}"
            ), {"name": name, "seen_at": seen_at})
            conn.execute(text(
                "INSERT INTO category_sightings (name, seen_at, article_link, article_title) "
                "VALUES (:name, :seen_at, :link, :title)"
            ), {"name": name, "seen_at": seen_at, "link": link, "title": title})

    def count_recent_sightings(self, cutoff: str) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT s.name, COUNT(*) FROM category_sightings s "
                "JOIN categories c ON c.name = s.name "
                "WHERE c.status = 'proposed' AND s.seen_at >= :cutoff "
                "GROUP BY s.name"
            ), {"cutoff": cutoff}).fetchall()

    def prune_category_sightings(self, cutoff: str) -> int:
        with self._engine.begin() as conn:
            cursor = conn.execute(text("DELETE FROM category_sightings WHERE seen_at < :cutoff"), {"cutoff": cutoff})
            return cursor.rowcount

    def categories_ready_for_review(self, cutoff: str, threshold: int) -> list[tuple]:
        # HAVING must repeat the aggregate (COUNT(s.id)), not reference the
        # `hits` alias -- SQLite tolerates an alias here, Postgres doesn't
        # (real incident, found live on INT: psycopg2.errors.UndefinedColumn
        # "column 'hits' does not exist"). ORDER BY hits is fine -- alias
        # references there are standard SQL, both backends accept it.
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT c.name, COUNT(s.id) AS hits FROM categories c "
                "JOIN category_sightings s ON s.name = c.name "
                "WHERE c.status = 'proposed' AND c.alerted_at IS NULL AND s.seen_at >= :cutoff "
                "GROUP BY c.name HAVING COUNT(s.id) >= :threshold ORDER BY hits DESC"
            ), {"cutoff": cutoff, "threshold": threshold}).fetchall()

    def category_examples(self, name: str, limit: int) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT article_title, article_link FROM category_sightings "
                "WHERE name = :name AND article_title IS NOT NULL "
                "ORDER BY seen_at DESC, id DESC LIMIT :limit"
            ), {"name": name, "limit": limit}).fetchall()

    def mark_category_alerted(self, name: str, now: str, description: str | None) -> None:
        with self._engine.begin() as conn:
            if description is None:
                conn.execute(text("UPDATE categories SET alerted_at = :now WHERE name = :name"),
                             {"now": now, "name": name})
            else:
                conn.execute(text(
                    "UPDATE categories SET alerted_at = :now, description = :description WHERE name = :name"
                ), {"now": now, "description": description, "name": name})

    def activate_category(self, name: str, by: str, now: str, description: str | None) -> bool:
        with self._engine.begin() as conn:
            cursor = conn.execute(text(
                "UPDATE categories SET status = 'active', "
                "description = COALESCE(:description, description), decided_at = :now, "
                "decided_by = :by, sort_order = (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories) "
                "WHERE name = :name AND status = 'proposed'"
            ), {"description": description, "now": now, "by": by, "name": name})
            if not cursor.rowcount:
                return False
            conn.execute(text("DELETE FROM interest_categories"))
        return True

    def reject_category(self, name: str, by: str, now: str) -> bool:
        with self._engine.begin() as conn:
            cursor = conn.execute(text(
                "UPDATE categories SET status = 'rejected', decided_at = :now, decided_by = :by "
                "WHERE name = :name AND status = 'proposed'"
            ), {"now": now, "by": by, "name": name})
            return bool(cursor.rowcount)

    def merge_category(self, name: str, into: str, by: str, now: str) -> bool:
        with self._engine.begin() as conn:
            target = conn.execute(
                text("SELECT status FROM categories WHERE name = :into"), {"into": into}
            ).fetchone()
            if not target or target[0] != "active":
                return False
            cursor = conn.execute(text(
                "UPDATE categories SET status = 'merged', merged_into = :into, decided_at = :now, "
                "decided_by = :by WHERE name = :name AND status IN ('proposed', 'active')"
            ), {"into": into, "now": now, "by": by, "name": name})
            if not cursor.rowcount:
                return False
            for interest, raw in conn.execute(
                text("SELECT interest, categories FROM interest_categories")
            ).fetchall():
                cats = json.loads(raw)
                if name not in cats:
                    continue
                rewritten = list(dict.fromkeys(into if c == name else c for c in cats))
                conn.execute(text("UPDATE interest_categories SET categories = :cats WHERE interest = :interest"),
                             {"cats": json.dumps(rewritten), "interest": interest})
        return True

    def seed_categories(self, rows: list[tuple[str, str]], now: str,
                        unclassifiable_name: str, unclassifiable_description: str) -> None:
        """Populates the taxonomy on first run only -- an ignore-on-conflict
        insert, not a count check, so a category an admin later
        retired/renamed doesn't silently reappear on the next restart. See
        category_ops.SEED_CATEGORIES for the actual taxonomy content and
        category_ops.bootstrap()'s docstring for the full reasoning
        (including the 2026-08-20 Legal-already-proposed incident this
        promote-if-proposed step exists for)."""
        insert = (
            f"{self._insert_ignore_prefix('categories')} "
            "(name, description, status, created_at, created_by, sort_order) "
            "VALUES (:name, :description, :status, :created_at, :created_by, :sort_order) "
            f"{self._on_conflict_nothing(['name'])}"
        )
        with self._engine.begin() as conn:
            for i, (name, description) in enumerate(rows):
                conn.execute(text(insert), {
                    "name": name, "description": description, "status": "active",
                    "created_at": now, "created_by": "seed", "sort_order": i,
                })
                conn.execute(text(
                    "UPDATE categories SET status = 'active', description = :description, sort_order = :i "
                    "WHERE name = :name AND status = 'proposed'"
                ), {"description": description, "i": i, "name": name})
            conn.execute(text(insert), {
                "name": unclassifiable_name, "description": unclassifiable_description, "status": "system",
                "created_at": now, "created_by": "seed", "sort_order": len(rows),
            })

    def migrate_split_policy(self, now: str) -> None:
        """Retires `Policy` in favour of Regulation/Government/Legal/
        Antitrust, once -- guarded by a health_state marker rather than by
        checking Policy's own status, so an admin who deliberately
        re-activates Policy later doesn't have it silently retired again.
        See category_ops.MIGRATE_SPLIT_POLICY_NOTE for the full reasoning."""
        with self._engine.begin() as conn:
            done = conn.execute(
                text("SELECT value FROM health_state WHERE key = 'policy_split_migrated'")
            ).fetchone()
            if done:
                return
            conn.execute(text(
                "UPDATE categories SET status = 'retired', decided_at = :now, decided_by = 'migration' "
                "WHERE name = 'Policy' AND status = 'active'"
            ), {"now": now})
            conn.execute(text(
                f"{self._insert_ignore_prefix('health_state')} (key, value) VALUES ('policy_split_migrated', :now) "
                f"{self._on_conflict_nothing(['key'])}"
            ), {"now": now})
