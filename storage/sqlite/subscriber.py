"""
Subscriber account/preferences storage -- shared by SqliteStorage and
PostgresStorage (see storage/sqlite/__init__.py). Every method here deals
in storage-native shapes (raw ints/strings straight off the row); business
shapes (bool, list[str], parsed JSON, defaults) are subscriber_ops.py's
job, one layer up.
"""

from sqlalchemy import text


class SubscriberMixin:
    def get_status(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT status FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row else None

    def request_access(self, chat_id: int, username: str | None, first_name: str | None,
                       status: str, requested_at: str) -> None:
        """A no-op if this chat_id already has a row -- re-messaging
        shouldn't reset a decision back to pending."""
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, username, first_name, status, requested_at)
                VALUES (:chat_id, :username, :first_name, :status, :requested_at)
                ON CONFLICT(chat_id) DO NOTHING
                """
            ), {"chat_id": chat_id, "username": username, "first_name": first_name,
                "status": status, "requested_at": requested_at})

    def decide(self, chat_id: int, status: str, decided_at: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                "UPDATE subscribers SET status = :status, decided_at = :decided_at WHERE chat_id = :chat_id"
            ), {"status": status, "decided_at": decided_at, "chat_id": chat_id})

    def list_pending(self, status: str) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(
                text("SELECT chat_id, username, first_name FROM subscribers WHERE status = :status"),
                {"status": status},
            ).fetchall()

    def get_interests(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT interests FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row else None

    def set_interests(self, chat_id: int, status: str, requested_at: str, interests_json: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, interests)
                VALUES (:chat_id, :status, :requested_at, :interests)
                ON CONFLICT(chat_id) DO UPDATE SET interests = excluded.interests
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "interests": interests_json})

    def mark_test_account(self, chat_id: int, status: str, requested_at: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, is_test)
                VALUES (:chat_id, :status, :requested_at, 1)
                ON CONFLICT(chat_id) DO UPDATE SET is_test = 1
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at})

    def get_push_enabled(self, chat_id: int) -> int | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT push_enabled FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row else None

    def set_push_enabled(self, chat_id: int, status: str, requested_at: str, enabled: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, push_enabled)
                VALUES (:chat_id, :status, :requested_at, :enabled)
                ON CONFLICT(chat_id) DO UPDATE SET push_enabled = excluded.push_enabled
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "enabled": enabled})

    def get_push_interval_hours(self, chat_id: int) -> int | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT push_interval_hours FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row else None

    def set_push_interval_hours(self, chat_id: int, status: str, requested_at: str, hours: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, push_interval_hours)
                VALUES (:chat_id, :status, :requested_at, :hours)
                ON CONFLICT(chat_id) DO UPDATE SET push_interval_hours = excluded.push_interval_hours
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "hours": hours})

    def get_pushed_links(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT pushed_links FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row else None

    def get_last_push_at(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT last_push_at FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row and row[0] else None

    def record_push(self, chat_id: int, status: str, requested_at: str,
                    last_push_at: str, pushed_links_json: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, last_push_at, pushed_links)
                VALUES (:chat_id, :status, :requested_at, :last_push_at, :pushed_links)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_push_at = excluded.last_push_at,
                    pushed_links = excluded.pushed_links
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at,
                "last_push_at": last_push_at, "pushed_links": pushed_links_json})

    def list_push_enabled_subscribers(self, status: str) -> list[tuple]:
        """Excludes accounts flagged is_test -- see subscriber_ops.mark_test_account
        for why that's a structural filter here rather than test cleanup."""
        with self._engine.begin() as conn:
            return conn.execute(text(
                """
                SELECT chat_id, interests, push_interval_hours, last_push_at, pushed_links, language,
                       restricted_sources_enabled
                FROM subscribers
                WHERE status = :status AND push_enabled = 1
                  AND (is_test IS NULL OR is_test = 0)
                """
            ), {"status": status}).fetchall()

    def get_language(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT language FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row and row[0] else None

    def set_language(self, chat_id: int, status: str, requested_at: str, language: str | None) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, language)
                VALUES (:chat_id, :status, :requested_at, :language)
                ON CONFLICT(chat_id) DO UPDATE SET language = excluded.language
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "language": language})

    def get_restricted_sources_enabled(self, chat_id: int) -> int | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT restricted_sources_enabled FROM subscribers WHERE chat_id = :chat_id"),
                {"chat_id": chat_id},
            ).fetchone()
        return row[0] if row else None

    def set_restricted_sources_enabled(self, chat_id: int, status: str, requested_at: str, enabled: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, restricted_sources_enabled)
                VALUES (:chat_id, :status, :requested_at, :enabled)
                ON CONFLICT(chat_id) DO UPDATE SET restricted_sources_enabled = excluded.restricted_sources_enabled
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "enabled": enabled})

    def get_interest_push_state(self, chat_id: int) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(
                text("SELECT topic, last_pushed_at FROM interest_push_state WHERE chat_id = :chat_id"),
                {"chat_id": chat_id},
            ).fetchall()

    def mark_interest_pushed(self, chat_id: int, topic: str, when: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO interest_push_state (chat_id, topic, last_pushed_at)
                VALUES (:chat_id, :topic, :when)
                ON CONFLICT(chat_id, topic) DO UPDATE SET last_pushed_at = excluded.last_pushed_at
                """
            ), {"chat_id": chat_id, "topic": topic, "when": when})

    def get_external_id(self, chat_id: int) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT external_id FROM subscribers WHERE chat_id = :chat_id"), {"chat_id": chat_id}
            ).fetchone()
        return row[0] if row and row[0] else None

    def set_external_id_if_null(self, chat_id: int, new_id: str) -> bool:
        """Returns whether the write took (False if the row doesn't exist,
        or another writer already set it first)."""
        with self._engine.begin() as conn:
            cursor = conn.execute(text(
                "UPDATE subscribers SET external_id = :new_id WHERE chat_id = :chat_id AND external_id IS NULL"
            ), {"new_id": new_id, "chat_id": chat_id})
            return bool(cursor.rowcount)

    def list_all_interests_raw(self) -> list[tuple]:
        with self._engine.begin() as conn:
            return conn.execute(
                text("SELECT interests FROM subscribers WHERE interests IS NOT NULL")
            ).fetchall()

    def get_push_consecutive_failures(self, chat_id: int) -> int | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT push_consecutive_failures FROM subscribers WHERE chat_id = :chat_id"),
                {"chat_id": chat_id},
            ).fetchone()
        return row[0] if row else None

    def set_push_consecutive_failures(self, chat_id: int, status: str, requested_at: str, count: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO subscribers (chat_id, status, requested_at, push_consecutive_failures)
                VALUES (:chat_id, :status, :requested_at, :count)
                ON CONFLICT(chat_id) DO UPDATE SET push_consecutive_failures = excluded.push_consecutive_failures
                """
            ), {"chat_id": chat_id, "status": status, "requested_at": requested_at, "count": count})
