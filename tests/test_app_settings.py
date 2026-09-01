import pytest

import app_settings
from trailsign import Settings


@pytest.fixture(autouse=True)
def _isolated_settings_singleton():
    """app_settings._settings is process-global; reset it before and
    after every test in this file so tests can't leak state into each
    other or into whatever runs next in the same pytest session."""
    app_settings.reset_settings_for_tests()
    yield
    app_settings.reset_settings_for_tests()


def test_get_settings_returns_a_singleton():
    first = app_settings.get_settings()
    second = app_settings.get_settings()
    assert first is second


def test_falls_back_to_empty_settings_when_no_file_configured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no settings.yml here
    monkeypatch.delenv("SETTINGS_FILE", raising=False)
    settings = app_settings.get_settings()
    assert settings.resolved("storage.news_cache_dir", default="news_cache") == "news_cache"


def test_settings_file_env_var_is_honored(monkeypatch, tmp_path):
    settings_path = tmp_path / "custom_settings.yml"
    settings_path.write_text("storage:\n  news_cache_dir: /tmp/from_file\n", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_FILE", str(settings_path))
    settings = app_settings.get_settings()
    assert settings.resolved("storage.news_cache_dir") == "/tmp/from_file"


def test_reset_settings_for_tests_can_inject_a_fake():
    fake = Settings({"storage": {"news_cache_dir": "injected"}})
    app_settings.reset_settings_for_tests(fake)
    assert app_settings.get_settings() is fake
    assert app_settings.get_settings().resolved("storage.news_cache_dir") == "injected"
