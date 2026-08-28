import pytest
import agent
import message_archive
import news_cache
import news_keyness
import users_db
from tests.fakes import fake_pos_tag, fake_word_tokenize


@pytest.fixture
def isolated_news_cache(monkeypatch, tmp_path):
    """Point news_cache.CACHE_DIR at a temp directory for the duration of a
    test, so cache tests never touch the real news_cache/ directory."""
    path = tmp_path / "news_cache"
    monkeypatch.setattr(news_cache, "CACHE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def isolated_message_archive(monkeypatch, tmp_path):
    """Point message_archive.ARCHIVE_DIR at a temp directory for every
    test, autouse -- unlike news_cache's NEWS_ARCHIVE_DIR (default off,
    an explicit fixture is enough), MESSAGE_ARCHIVE_DIR defaults to a
    real relative directory (archiving is meant to always be on, see
    that module's docstring), so ANY test exercising handle_message/
    send_push_digest/run_push_cycle end-to-end -- not just tests that
    know to ask for isolation -- would otherwise write real files into
    the repo's working directory. Confirmed happening 2026-08-28 before
    this was autouse: a full test run left dozens of real .json files in
    ./message_archive/."""
    path = tmp_path / "message_archive"
    monkeypatch.setattr(message_archive, "ARCHIVE_DIR", str(path))
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


@pytest.fixture
def fake_nltk(monkeypatch):
    """Swaps news_keyness's pos_tag/word_tokenize for deterministic fakes
    (tests/fakes.py) so keyness tests don't need the real NLTK data files
    downloaded -- same reasoning as FakeEmbedder standing in for
    model2vec: fast, no real model/data load, and predictable from the
    fixture's own text alone. Every alphanumeric token in a fixture's
    title/summary counts as a noun under this fake, not just the words a
    real tagger would classify that way."""
    monkeypatch.setattr(news_keyness, "pos_tag", fake_pos_tag)
    monkeypatch.setattr(news_keyness, "word_tokenize", fake_word_tokenize)
