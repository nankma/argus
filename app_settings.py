"""Shared Settings bootstrap for this project.

One process-wide `trailsign.Settings` instance, built once, used by every
module migrated onto it (see docs/README.md for why settings now flow
through the `trailsign` library instead of raw `os.environ` reads,
originally designed as part of this project's own settings work).

The settings file's own path is the one config value that can never live
inside the settings file itself -- `SETTINGS_FILE` env var, defaulting to
`settings.yml` in the working directory. If neither resolves to a real
file, this falls back to an empty `Settings({})` -- what that means
depends on the call site: one written with `default=<value>` still
works (an intentionally optional setting, e.g. news_cache.ARCHIVE_DIR);
one written with `required=True` raises `SettingsError` instead, on
purpose (see docs/standaloneplan/01-settings-migration.md's "Migration
methodology" -- a value the service always needs a real one for should
fail loudly on a missing settings.yml, not silently guess).
"""

import os
from typing import Any

from trailsign import Settings, SettingsError

_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def resolved_optional(path: str, default: Any = None) -> Any:
    """get_settings().resolved(path, default=default), but also treats a
    node that's PRESENT and unresolvable (e.g. an environment-variable
    bridge whose env var isn't set) as equivalent to the path being
    absent. Settings.resolved()'s own `default=` only covers the latter
    -- a present-but-unresolvable node still raises SettingsError
    regardless of `default=` (see
    docs/standaloneplan/01-settings-migration.md's "Migration
    methodology"). Use this instead of calling get_settings().resolved()
    directly for any setting that's meant to fail open when its
    underlying env var isn't set -- an optional feature flag or
    credential, as opposed to something required=True should guard."""
    try:
        return get_settings().resolved(path, default=default)
    except SettingsError:
        return default


def _load() -> Settings:
    path = os.environ.get("SETTINGS_FILE", "settings.yml")
    if not os.path.exists(path):
        return Settings({})
    return Settings.from_yaml(path)


def reset_settings_for_tests(settings: Settings | None = None) -> None:
    """Test-only. No-arg call forces the next get_settings() to reload
    from disk/env; pass a Settings instance to inject a fake for the
    duration of a test."""
    global _settings
    _settings = settings
