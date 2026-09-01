"""Shared Settings bootstrap for this project.

One process-wide `trailsign.Settings` instance, built once, used by every
module migrated onto it (see docs/README.md for why settings now flow
through the `trailsign` library instead of raw `os.environ` reads,
originally designed as part of this project's own settings work).

The settings file's own path is the one config value that can never live
inside the settings file itself -- `SETTINGS_FILE` env var, defaulting to
`settings.yml` in the working directory. If neither resolves to a real
file, this falls back to an empty `Settings({})`: every migrated call
site still works via its own `default=` argument, so a deployment or a
test with no settings.yml at all doesn't break -- it just gets 100%
defaults, same behavior as before this migration started.
"""

import os

from trailsign import Settings

_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


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
