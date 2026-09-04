"""
The handful of operations where SQLite and Postgres genuinely diverge.
PostgresStorage inherits everything else from SqliteStorage unchanged and
overrides only the three methods below -- two composable string fragments
for ignore-on-conflict inserts, plus schema introspection (see
storage/postgres/__init__.py) -- every domain mixin's own SQL is written
once and shared by both backends.
"""

from sqlalchemy import text

from storage import schema


class PrimitivesMixin:
    def _insert_ignore_prefix(self, table: str) -> str:
        """The leading fragment of an ignore-on-conflict insert -- combined
        with `_on_conflict_nothing()` below by the caller around its own
        VALUES clause (which may include a raw SQL expression, e.g. a
        sort_order subquery, not just bound values -- that's why this is
        two composable string fragments rather than one method that takes
        a values dict). SQLite's `INSERT OR IGNORE` and Postgres's
        `INSERT ... ON CONFLICT (...) DO NOTHING` aren't expressible as one
        shared SQL string, unlike the upsert queries elsewhere in this
        package (SQLite's `ON CONFLICT ... DO UPDATE SET ... excluded.x` is
        already identical to Postgres's)."""
        return f"INSERT OR IGNORE INTO {table}"

    def _on_conflict_nothing(self, conflict_cols: list[str]) -> str:
        """Paired with `_insert_ignore_prefix()` -- empty for SQLite, since
        `OR IGNORE` in the prefix already does the job."""
        return ""

    def _list_columns(self, table: str) -> set[str]:
        """SQLite's `PRAGMA table_info`. Postgres overrides this with
        `sqlalchemy.inspect(engine).get_columns(table)`."""
        with self._engine.begin() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return {row[1] for row in rows}

    def create_schema(self) -> None:
        schema.metadata.create_all(self._engine)

    def ensure_columns(self) -> None:
        """Adds any column in schema.ADDITIVE_COLUMNS that an existing
        (pre-refactor, or pre-that-column) database file doesn't have yet.
        A no-op for a table just created by create_schema() above, which
        already has every column in its final shape."""
        for table, column, sql_type in schema.ADDITIVE_COLUMNS:
            if column in self._list_columns(table):
                continue
            with self._engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))

    def migrate_api_budget_table(self) -> None:
        """One-time migration from the original api_budget schema (one row
        per source, today's count only -- a single-column `source` PRIMARY
        KEY, even on a schema that already has a `date` column, as one
        transitional shape did) to one row per (source, date), so usage has
        queryable history. PK shape, not column presence, is the old
        schema's real signature -- a no-op on a fresh database
        (create_schema() above already created the new (source, date) PK
        directly) or one already migrated. Postgres never needs this (a
        fresh Postgres database always starts on the current schema), so
        the introspection below is deliberately SQLite-only; `pk_columns`
        is empty on any other dialect, which safely short-circuits to a
        no-op there too."""
        with self._engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(api_budget)")).fetchall() \
                if self._engine.dialect.name == "sqlite" else []
        pk_columns = [row[1] for row in rows if row[5] > 0]
        if pk_columns != ["source"]:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE api_budget RENAME TO api_budget_old"))
            conn.execute(text(
                """
                CREATE TABLE api_budget (
                    source TEXT NOT NULL,
                    date TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (source, date)
                )
                """
            ))
            conn.execute(text(
                "INSERT INTO api_budget (source, date, count) "
                "SELECT source, date, count FROM api_budget_old"
            ))
            conn.execute(text("DROP TABLE api_budget_old"))
