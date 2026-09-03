import os
import subprocess
import sys
from pathlib import Path

import pytest

import app_settings
from trailsign import Settings

_REPO_ROOT = Path(__file__).parent.parent


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


def test_resolved_optional_returns_default_when_path_absent():
    app_settings.reset_settings_for_tests(Settings({}))
    assert app_settings.resolved_optional("telemetry.enabled") is None
    assert app_settings.resolved_optional("telemetry.enabled", default=False) is False


def test_resolved_optional_returns_default_when_node_is_unresolvable(monkeypatch):
    """The behavior this helper exists for: a PRESENT trailsign-resolve
    node whose env var isn't set still raises SettingsError out of
    Settings.resolved() even with default= given -- default= only covers
    a path that's absent entirely. resolved_optional must catch that and
    fail open the same way, or every env-var-bridged optional setting
    (telemetry.enabled, bot tokens if they were optional, etc.) would
    crash instead of degrading."""
    monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
    fake = Settings({"telemetry": {"enabled": {
        "trailsign-resolve": "environment-variable", "name": "SOME_UNSET_VAR",
    }}})
    app_settings.reset_settings_for_tests(fake)
    assert app_settings.resolved_optional("telemetry.enabled", default=False) is False


def test_resolved_optional_returns_the_resolved_value_when_present():
    app_settings.reset_settings_for_tests(Settings({"telemetry": {"enabled": "true"}}))
    assert app_settings.resolved_optional("telemetry.enabled", default=False) == "true"


def test_required_true_call_site_raises_when_settings_missing(monkeypatch, tmp_path):
    """End-to-end check of the actual fail-loud contract (not just
    app_settings.py in isolation): a required=True call site
    (news_cache.CACHE_DIR) must raise SettingsError at import time when
    no settings.yml is present, not silently fall back to anything. Run
    in a fresh subprocess since the constant is computed once at first
    import, which has already happened by the time any test in this
    process runs -- see docs/standaloneplan/01-settings-migration.md's
    "Migration methodology" rule 1."""
    monkeypatch.delenv("SETTINGS_FILE", raising=False)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import news_cache"],
        cwd=str(tmp_path),  # no settings.yml here
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "SettingsError" in result.stderr
    assert "storage.news_cache_dir" in result.stderr
