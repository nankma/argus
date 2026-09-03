"""
Pluggable telemetry-provider interface. Each provider class lives in its
own module under this package and declares TYPE (a registry key, e.g.
TYPE = "otlp") and KIND (a frozenset subset of {"general", "llm"} --
which capability(ies) it provides: "general" for app-event logging,
"llm" for LLM-call tracing). There is ONE settings list,
telemetry.providers -- no separate list per KIND -- telemetry.py's
coordinator reads a class's own KIND to decide which internal
TracerProvider(s) an entry's class gets initialize()'d against; see
that module's own docstring for why one config list still safely
produces two internally-isolated categories. Auto-discovered from this
package at process startup, same mechanism as news_adapters/ (see that
package's own __init__.py) -- structural typing, no explicit
subclassing.

SPAN_BASED (bool) distinguishes how a "general"-capable provider
actually receives events: a span-based provider (e.g. otlp -- Logfire,
Grafana Cloud, SigNoz, OpenObserve, Phoenix's own OTLP ingestion) is
attached as a span processor on telemetry.py's general TracerProvider,
so emitting ONE span per event already fans out to every span-based
general-capable provider automatically via OTel's own multi-processor
delivery -- calling each such provider's own log() separately would
create a duplicate span per provider and double-export the same event.
telemetry.py's EventLogger therefore emits that one span itself
(whenever at least one span-based general provider is configured) and
calls log() directly ONLY on non-span-based providers (file.py) instead
of on every configured provider uniformly. log() still exists on every
provider (Protocol uniformity, and the option for a future
non-processor-based provider that genuinely needs its own per-call
hook) -- a span-based provider's own log() is simply never invoked in
practice and can be a no-op.

"llm"-only providers (phoenix) instrument LLM-call tracing entirely
through auto-instrumentation/span-processor attachment during
initialize(); nothing calls their log() at all, since the llm category
has no per-call logging concept -- an LLM trace is the auto-instrumented
span tree itself.
"""

import importlib
import inspect
import pkgutil
from typing import Protocol

from opentelemetry.sdk.trace import TracerProvider
from trailsign import SettingsError


class Level:
    """OTel standard severity numbers -- Logfire's `level` column (and any
    other OTLP-compatible backend willing to read arbitrary attributes)
    reads these via the `logfire.level_num` attribute (see otlp.py).
    DEBUG (5) deliberately omitted -- not needed yet -- but the numbering
    leaves room for it without renumbering anything else."""

    TRACE = 1
    INFO = 9
    WARN = 13
    ERROR = 17
    FATAL = 21


class TelemetryProvider(Protocol):
    """TYPE is a class attribute (e.g. TYPE = "otlp"), read directly off
    the class (not an instance) by discover_provider_types -- it names
    which telemetry.providers[].type entry this class handles. KIND
    declares which capability(ies) this provider supports ({"general"},
    {"llm"}, or both) -- telemetry.py's coordinator uses this to decide
    which internal provider(s) an entry gets routed to, there is no
    settings-level "which list" concept a deployer configures. SPAN_BASED
    (see module docstring) says whether telemetry.py's EventLogger calls
    this provider's own log() directly, or relies on the general
    TracerProvider's automatic multi-processor fan-out instead."""

    TYPE: str
    KIND: frozenset[str]
    SPAN_BASED: bool

    def initialize(self, config: dict, tracer_provider: TracerProvider, kind: str) -> None:
        """kind is which of THIS instance's KIND values the given
        tracer_provider corresponds to ("general" or "llm") -- for a
        dual-KIND class (otlp), initialize() is called once per
        applicable internal provider, on a fresh instance each time (see
        telemetry.py's _activate), so `kind` tells that one call which
        provider it's attaching to right now. A single-KIND class
        (file.py, phoenix.py) only ever sees its own one value."""
        ...

    def log(self, scope: str, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        ...


def discover_provider_types() -> dict[str, type]:
    """Scans every module in this package for classes declaring TYPE --
    {TYPE: class}. Runs once at process startup (called from telemetry.
    setup_telemetry), not per-call -- a handful of files, negligible
    cost (same reasoning as news_adapters.discover_adapter_types).

    obj.__module__ == module.__name__ filters out classes merely
    imported into a module (e.g. one provider file importing another's
    class for reuse) -- only a class actually DEFINED in that module
    counts as "discovered there". Modules starting with "_" are
    skipped -- internal helpers, not providers."""
    types: dict[str, type] = {}
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(__path__):
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            provider_type = getattr(obj, "TYPE", None)
            if provider_type and obj.__module__ == module.__name__:
                types[provider_type] = obj
    return types


def validate_configured_types(discovered: dict[str, type], configured: list[dict]) -> None:
    """Raises if any telemetry.providers[].type has no matching class in
    telemetry_providers/ -- fails the whole process at startup rather
    than silently dropping that one provider, per the same "don't start
    the service" requirement news_adapters' validate_configured_types
    was built for. Only checks the type EXISTS -- there's no "configured
    under the wrong list" case to check anymore (see telemetry.py's
    module docstring: one list, routed by KIND automatically, not a
    deployer's choice of which list to put an entry under)."""
    missing = {entry["type"] for entry in configured} - discovered.keys()
    if missing:
        raise SettingsError(
            f"telemetry provider list references type(s) {sorted(missing)}, "
            f"but no class with that TYPE exists in telemetry_providers/ "
            f"(found: {sorted(discovered)})"
        )
