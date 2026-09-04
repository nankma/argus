"""Daily call-budget storage for rate-limited news sources."""

from sqlalchemy import text


class ApiBudgetMixin:
    def try_consume_api_budget(self, source: str, daily_cap: int, today: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT count FROM api_budget WHERE source = :source AND date = :today"),
                {"source": source, "today": today},
            ).fetchone()
            if row is None:
                conn.execute(text("INSERT INTO api_budget (source, date, count) VALUES (:source, :today, 1)"),
                             {"source": source, "today": today})
                return True
            if row[0] >= daily_cap:
                return False
            conn.execute(text(
                "UPDATE api_budget SET count = count + 1 WHERE source = :source AND date = :today"
            ), {"source": source, "today": today})
            return True

    def record_api_call(self, source: str, today: str) -> None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT count FROM api_budget WHERE source = :source AND date = :today"),
                {"source": source, "today": today},
            ).fetchone()
            if row is None:
                conn.execute(text("INSERT INTO api_budget (source, date, count) VALUES (:source, :today, 1)"),
                             {"source": source, "today": today})
            else:
                conn.execute(text(
                    "UPDATE api_budget SET count = count + 1 WHERE source = :source AND date = :today"
                ), {"source": source, "today": today})

    def get_api_budget_history(self, source: str, limit: int) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT date, count FROM api_budget WHERE source = :source ORDER BY date DESC LIMIT :limit"
            ), {"source": source, "limit": limit}).fetchall()

    def get_total_api_calls(self, source: str) -> int:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT SUM(count) FROM api_budget WHERE source = :source"), {"source": source}
            ).fetchone()
        return row[0] or 0
