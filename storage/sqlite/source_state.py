"""Per-source ingest bookkeeping -- when a source was last pulled, and the
published_dt of the newest article actually seen from it (see
source_state_ops.get_source_last_article_dt for why those are different
values)."""

from sqlalchemy import text


class SourceStateMixin:
    def get_source_last_pulled_at(self, source: str) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT last_pulled_at FROM source_pull_state WHERE source = :source"), {"source": source}
            ).fetchone()
        return row[0] if row else None

    def set_source_last_pulled_at(self, source: str, when: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO source_pull_state (source, last_pulled_at) VALUES (:source, :when)
                ON CONFLICT(source) DO UPDATE SET last_pulled_at = excluded.last_pulled_at
                """
            ), {"source": source, "when": when})

    def get_source_last_article_dt(self, source: str) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT last_article_dt FROM source_pull_state WHERE source = :source"), {"source": source}
            ).fetchone()
        return row[0] if row and row[0] else None

    def set_source_last_article_dt(self, source: str, when: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO source_pull_state (source, last_pulled_at, last_article_dt)
                VALUES (:source, :when, :when)
                ON CONFLICT(source) DO UPDATE SET last_article_dt = excluded.last_article_dt
                """
            ), {"source": source, "when": when})
