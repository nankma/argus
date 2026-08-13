import pytest
import agent
import news_cache
import users_db


@pytest.fixture
def isolated_news_cache(monkeypatch, tmp_path):
    """Point news_cache.CACHE_DIR at a temp directory for the duration of a
    test, so cache tests never touch the real news_cache/ directory."""
    path = tmp_path / "news_cache"
    monkeypatch.setattr(news_cache, "CACHE_DIR", str(path))
    return path


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
