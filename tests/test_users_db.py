import sqlite3

import users_db


def test_get_status_unknown_chat_returns_none(isolated_subscribers_db):
    assert users_db.get_status(12345) is None


def test_request_access_sets_pending(isolated_subscribers_db):
    users_db.request_access(1, "alice", "Alice")
    assert users_db.get_status(1) == users_db.PENDING


def test_request_access_does_not_reset_a_decision(isolated_subscribers_db):
    users_db.request_access(1, "alice", "Alice")
    users_db.decide(1, approved=True)
    users_db.request_access(1, "alice", "Alice")  # re-message after decision
    assert users_db.get_status(1) == users_db.APPROVED


def test_decide_approve(isolated_subscribers_db):
    users_db.request_access(2, "bob", "Bob")
    users_db.decide(2, approved=True)
    assert users_db.get_status(2) == users_db.APPROVED


def test_decide_deny(isolated_subscribers_db):
    users_db.request_access(3, "carol", "Carol")
    users_db.decide(3, approved=False)
    assert users_db.get_status(3) == users_db.DENIED


def test_list_pending_only_returns_pending(isolated_subscribers_db):
    users_db.request_access(4, "dave", "Dave")
    users_db.request_access(5, "erin", "Erin")
    users_db.decide(5, approved=True)
    pending = users_db.list_pending()
    assert [row[0] for row in pending] == [4]


def test_get_interests_empty_for_unset_chat(isolated_subscribers_db):
    assert users_db.get_interests(6) == []


def test_set_and_get_interests(isolated_subscribers_db):
    users_db.request_access(7, "frank", "Frank")
    users_db.set_interests(7, ["AI", "robotics"])
    assert users_db.get_interests(7) == ["AI", "robotics"]


def test_set_interests_upserts_when_no_existing_row(isolated_subscribers_db):
    # e.g. the admin, who never goes through request_access()
    users_db.set_interests(999, ["semiconductors"])
    assert users_db.get_interests(999) == ["semiconductors"]
    assert users_db.get_status(999) == users_db.APPROVED


def test_set_interests_overwrites_previous_value(isolated_subscribers_db):
    users_db.request_access(8, "grace", "Grace")
    users_db.set_interests(8, ["AI"])
    users_db.set_interests(8, ["quantum computing"])
    assert users_db.get_interests(8) == ["quantum computing"]


def test_init_db_migrates_schema_missing_interests_column(isolated_subscribers_db):
    # Simulate a DB created before the interests column existed.
    with sqlite3.connect(users_db.DB_FILE) as conn:
        conn.execute("DROP TABLE subscribers")
        conn.execute(
            """
            CREATE TABLE subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
    users_db.init_db()  # should migrate without error
    users_db.request_access(9, "henry", "Henry")
    users_db.set_interests(9, ["AI"])
    assert users_db.get_interests(9) == ["AI"]
