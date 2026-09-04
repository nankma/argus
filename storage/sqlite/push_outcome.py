"""Per-cycle push outcome storage -- see docs/plans/incident-monitoring-plan.md."""

from sqlalchemy import text


class PushOutcomeMixin:
    def record_push_outcome(self, chat_id: int, outcome: str, recorded_at: str, detail: str | None) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO push_outcomes (chat_id, outcome, recorded_at, detail) "
                "VALUES (:chat_id, :outcome, :recorded_at, :detail)"
            ), {"chat_id": chat_id, "outcome": outcome, "recorded_at": recorded_at, "detail": detail})

    def push_outcome_counts(self, cutoff: str) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(text(
                "SELECT outcome, COUNT(*) FROM push_outcomes WHERE recorded_at >= :cutoff GROUP BY outcome"
            ), {"cutoff": cutoff}).fetchall()

    def recent_outcomes_for(self, chat_id: int, limit: int) -> list[str]:
        with self._engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT outcome FROM push_outcomes WHERE chat_id = :chat_id "
                "ORDER BY recorded_at DESC, id DESC LIMIT :limit"
            ), {"chat_id": chat_id, "limit": limit}).fetchall()
        return [row[0] for row in rows]

    def prune_push_outcomes(self, cutoff: str) -> int:
        with self._engine.begin() as conn:
            cursor = conn.execute(text("DELETE FROM push_outcomes WHERE recorded_at < :cutoff"), {"cutoff": cutoff})
            return cursor.rowcount
