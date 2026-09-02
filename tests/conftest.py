import pytest

import app_settings
from trailsign import Settings

# Required settings, injected before the imports below -- module-level
# constants like news_cache.CACHE_DIR are computed once at import time,
# so this has to run before those imports, not inside a fixture
# (fixtures run per-test, long after collection-time imports already
# happened). Values are placeholders -- individual tests that care
# override them via monkeypatch.setattr on the module constant directly
# (see isolated_news_cache etc. below), not by touching Settings again.
# See docs/standaloneplan/01-settings-migration.md's "Migration
# methodology" for why these are required=True with no code-level
# fallback in the modules themselves.
app_settings.reset_settings_for_tests(Settings({
    "storage": {
        "news_cache_dir": "news_cache",
        "message_archive_dir": "message_archive",
        "subscribers_db_file": "subscribers.db",
    },
    "models": {
        "main": {"url": "https://example.invalid", "model": "fake-main", "api-key": "fake-key"},
        "guardrail": {"url": "https://example.invalid", "model": "fake-guardrail", "api-key": "fake-key"},
    },
}))

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
