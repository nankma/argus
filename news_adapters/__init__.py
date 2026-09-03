"""
NewsSourceAdapter -- the pluggable interface for news_source.api entries.
Each adapter implements initialize(config) + pull(...) and declares a
TYPE class attribute naming which news_source.api[].type it handles.
Adapter classes are auto-discovered from this package's own modules at
process startup (discover_adapter_types), so adding a new credentialed
source is: write a new news_adapters/<name>.py declaring TYPE, add a
news_source.api entry with that type -- no registry to edit by hand. See
news_sources.py's module docstring for how this plugs into
SOURCE_REGISTRY, and hackernews.py/arxiv.py for the two free, always-on
adapters that DON'T go through news_source.api at all (no credential, no
override -- see news_sources._always_on_sources).

Concrete adapters do NOT subclass NewsSourceAdapter -- this file's
Protocol is structural typing, same convention as logfire_logger.py's
Logger Protocol / LogfireLogger (no explicit inheritance there either).
"""

import importlib
import inspect
import pkgutil
from datetime import datetime
from typing import Protocol

from trailsign import SettingsError


class NewsSourceAdapter(Protocol):
    """TYPE is a class attribute (e.g. TYPE = "newsapi"), read directly
    off the class (not an instance) by discover_adapter_types -- it
    names which news_source.api[].type entry this class handles.

    initialize(config) is called once, right after construction, with
    that source's own (already credential-resolved) settings entry --
    e.g. {"key": "newsapi", "type": "newsapi", "api-key": "...",
    "interval_hours": 24}. A no-credential adapter (hackernews, arxiv)
    just ignores it.

    pull(...) is the actual fetch -- same shape every adapter's
    implementation needs, `since`/`section` included even for an adapter
    that ignores them (e.g. Perigon), so every adapter is callable
    uniformly regardless of which optional behavior it actually
    supports."""

    TYPE: str

    def initialize(self, config: dict) -> None:
        ...

    def pull(self, query: str, max_results: int, since: datetime | None = None,
             section: str | None = None) -> list[dict]:
        ...


def discover_adapter_types() -> dict[str, type]:
    """Scans every module in this package for classes declaring a TYPE
    attribute -- {TYPE: class}. Runs once at process startup (called from
    news_sources.py's module-level SOURCE_REGISTRY construction), not
    per-call -- a handful of files, the cost is negligible and there's no
    reason to re-scan on every ingestion cycle (confirmed acceptable at
    this project's scale -- performance is a non-issue here).

    obj.__module__ == module.__name__ filters out classes merely
    imported into a module (e.g. if one adapter file imported another's
    class for reuse) -- only a class actually DEFINED in that module
    counts as "discovered there". Modules starting with "_" (e.g. _util)
    are skipped -- internal helpers, not adapters."""
    types: dict[str, type] = {}
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(__path__):
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            adapter_type = getattr(obj, "TYPE", None)
            if adapter_type and obj.__module__ == module.__name__:
                types[adapter_type] = obj
    return types


def validate_configured_types(discovered: dict[str, type], configured: list[dict]) -> None:
    """Raises if any news_source.api[].type has no matching class in
    news_adapters/ -- fails the whole process at startup rather than
    silently dropping that one source, per the explicit requirement: a
    config referencing an adapter class that doesn't exist should not
    start the service, not be silently skipped like an unresolvable
    credential is (see news_sources._api_sources_from_settings)."""
    missing = {entry["type"] for entry in configured} - discovered.keys()
    if missing:
        raise SettingsError(
            f"news_source.api references adapter type(s) {sorted(missing)}, "
            f"but no class with that TYPE exists in news_adapters/ "
            f"(found: {sorted(discovered)})"
        )
