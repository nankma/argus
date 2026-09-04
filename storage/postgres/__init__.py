"""
PostgresStorage -- inherits SqliteStorage wholesale and overrides only the
two places SQLite and Postgres genuinely diverge: ignore-on-conflict
inserts (split across two composable methods, _insert_ignore_prefix/
_on_conflict_nothing -- see _primitives.py's docstring for why) and schema
introspection (_list_columns). Three methods, two divergent operations.
Every domain mixin's SQL (subscriber/category/push_outcome/api_budget/
interest_cache/source_state) is unchanged from SqliteStorage; this file
exists to hold exactly those three overrides and nothing else.
"""

from sqlalchemy import inspect

from storage.sqlite import SqliteStorage


class PostgresStorage(SqliteStorage):
    def _insert_ignore_prefix(self, table: str) -> str:
        return f"INSERT INTO {table}"

    def _on_conflict_nothing(self, conflict_cols: list[str]) -> str:
        return f"ON CONFLICT ({', '.join(conflict_cols)}) DO NOTHING"

    def _list_columns(self, table: str) -> set[str]:
        return {col["name"] for col in inspect(self._engine).get_columns(table)}
