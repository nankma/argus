import pytest
import agent
import users_db


@pytest.fixture
def isolated_notes_file(monkeypatch, tmp_path):
    """Point agent.NOTES_FILE at a temp file for the duration of a test, so
    save_note tests never touch the real notes.jsonl. Confirmed necessary by
    an earlier ad-hoc test that wrote a real entry into notes.jsonl before
    this fixture existed."""
    path = tmp_path / "notes.jsonl"
    monkeypatch.setattr(agent, "NOTES_FILE", str(path))
    return path


@pytest.fixture
def isolated_subscribers_db(monkeypatch, tmp_path):
    """Point users_db.DB_FILE at a temp file for the duration of a test, so
    access-control tests never touch the real subscribers.db."""
    path = tmp_path / "subscribers.db"
    monkeypatch.setattr(users_db, "DB_FILE", str(path))
    users_db.init_db()
    return path
