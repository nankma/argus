"""
Telemetry coordinator: reads ONE list, telemetry.providers, from
Settings, and routes each entry to internal per-category
TracerProviders purely by the entry's discovered class's own KIND
attribute (see telemetry_providers/ for the provider interface and the
individual adapters). Replaces logfire_logger.py's LogfireLogger --
that class was Logfire-specific; this module is provider-agnostic,
matching the same news_adapters-style pluggable-adapter pattern already
used for news sources (see news_sources.py/news_adapters/).

ONE CONFIG LIST, TWO INTERNAL PROVIDERS -- both halves matter, for
different reasons, and an earlier version of this module got each
wrong in the opposite direction:

- A dual-KIND provider (otlp, KIND={"general","llm"}) needs exactly ONE
  settings entry to receive BOTH categories -- it does not belong to
  "the general list" or "the llm list", it belongs to BOTH, and forcing
  a deployer to write out the same endpoint/headers twice (once per
  list) is what actually produced this module's real double-export bug
  (see below): two independent config entries pointed at the same
  Logfire project, each innocently doing exactly what it was configured
  to do. Routing purely by KIND from ONE entry removes the duplication
  at the root instead of asking a deployer not to make it.
- But internally, general-category spans and llm-category spans still
  need to land in genuinely SEPARATE TracerProviders. A TracerProvider's
  attached span processors are global to that provider -- nothing about
  add_span_processor scopes a processor to "only spans emitted via
  EventLogger.log()" vs. "only spans LangChain's auto-instrumentation
  emits." An earlier version of this module built ONE shared provider
  for everything (before the one-list-per-KIND-routing redesign, back
  when it also had two separate settings lists) -- code review caught,
  and a live OTel repro confirmed, that this meant every general
  app-event span also reached every llm-only provider (e.g. Phoenix
  silently receiving router-failed/ingest events, contradicting its own
  "LLM call tree only" purpose). Two genuinely separate TracerProvider
  objects is what actually isolates the two categories -- a span
  produced by one provider's own get_tracer() can never reach a
  processor attached to a different provider object, regardless of how
  many processors either one has. A dual-KIND entry's provider class
  gets initialize()'d ONCE PER applicable internal provider (a fresh
  instance each time, see setup_telemetry()) -- two independent
  processors from one config entry, not one processor shared unsafely
  between categories.

telemetry.providers defaults to [] (telemetry off is a legitimate
state, not an error -- same "default=[] fails open" contract
news_source.rss/news_source.api already have). General-category events
go through this module's get_event_logger facade, which resolves
against the process-global ambient tracer -- see setup_telemetry()'s
trace.set_tracer_provider() call, always the general provider, never
the llm one. LLM-category tracing works entirely through
auto-instrumentation attached during a provider's initialize() --
LangChainInstrumentor is handed the llm provider EXPLICITLY as an
argument, it doesn't rely on the ambient global at all -- no per-call
method on that side; see telemetry_providers/__init__.py's module
docstring for why "general" and "llm" work so differently.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from trailsign import Settings, SettingsError

from app_settings import get_settings
from telemetry_providers import Level, discover_provider_types, validate_configured_types

SERVICE_NAME = "myfirstagent"

# Module-level, set fresh by every setup_telemetry() call -- same
# "one provider per process" shape LogfireLogger._provider had.
# Holds every configured non-span-based general provider (this batch:
# FileProvider instances) -- EventLogger.log() calls each of these
# directly, once per event, since they have no OTel processor to fan
# out through (see telemetry_providers/__init__.py's module docstring).
# SPAN_BASED providers need no equivalent list: EventLogger.log()
# always emits exactly one span unconditionally (OTel's own no-op
# tracer safely absorbs it when nothing real is attached), and that one
# span already reaches every SPAN_BASED provider via the shared
# TracerProvider's own multi-processor delivery.
_file_providers: list = []

# Persists across setup_telemetry() calls within one process, same
# reasoning the old otlp.py-local flag had: LangChainInstrumentor().
# instrument() isn't documented as idempotent, so a second call within
# the same process (tests call setup_telemetry() many times; a real
# deployment shouldn't ever call it twice, but nothing prevents it)
# must not re-instrument.
_langchain_instrumented = False


def _raw_provider_entries() -> list[dict]:
    """Raw (unresolved) telemetry.providers entries. Reading this list
    via Settings.resolved() directly would recursively resolve every
    entry's nested trailsign-resolve nodes (e.g. headers.Authorization)
    too, and one entry with an unresolvable value (an unset env var)
    would raise SettingsError for the WHOLE list, taking every other
    configured provider down with it -- the identical gotcha
    news_sources.py's _raw_api_entries hit for news_source.api,
    confirmed live there; same trailsign mechanism, no need to
    re-verify separately here. Returns [] for a missing path or a
    non-list value, matching default=[]'s "empty is a legitimate off
    state" contract."""
    node = get_settings()._raw
    for part in "telemetry.providers".split("."):
        if not isinstance(node, dict) or part not in node:
            return []
        node = node[part]
    return node if isinstance(node, list) else []


def _resolved_entry(raw_entry: dict) -> dict | None:
    """Resolves one entry's own nested trailsign-resolve nodes in
    isolation -- see _raw_provider_entries for why this can't be part
    of a bulk Settings.resolved() call on the whole list. None if any
    part of this ONE entry is unresolvable (an unset env var, e.g.) --
    that entry alone is dropped, silently, matching every other
    optional-provider path in this project's own "one bad config
    degrades quietly" contract; the others in the list are unaffected."""
    try:
        return Settings({"entry": raw_entry}).resolved("entry", required=True)
    except SettingsError:
        return None


def _activate(entry: dict, kind: str, provider_cls: type, provider: TracerProvider) -> None:
    """Initializes ONE FRESH instance of provider_cls against ONE
    internal provider, for ONE of the KINDs that class supports. A
    dual-KIND class (otlp) gets called once per applicable internal
    provider -- a separate instance each time, not one instance
    initialize()'d twice -- so nothing about a general-side attachment
    can share state with (or accidentally affect) the llm-side one; see
    otlp.py's initialize() for how it uses `kind` to keep
    instrument_langchain llm-only regardless of which call this is.
    Appends non-span-based general instances (file.py) to
    _file_providers so EventLogger.log() can call them directly (see
    that method's own comment on why span-based providers don't need
    an equivalent list)."""
    instance = provider_cls()
    instance.initialize(entry, provider, kind)
    if kind == "general" and not instance.SPAN_BASED:
        _file_providers.append(instance)


def setup_telemetry() -> tuple[TracerProvider | None, TracerProvider | None] | None:
    """Call once per process, before any span is emitted. Returns None
    (and touches nothing) if telemetry.providers is empty/absent -- the
    same no-op-when-unconfigured contract every test/CI run relies on
    (an empty fake Settings dict in tests/conftest.py needs no
    telemetry.* keys at all for this to behave correctly). Otherwise
    returns (general_provider, llm_provider) -- either element is None
    if nothing configured needed that category (no caller currently
    uses the return value for anything but this module's own tests;
    agent.py's thin wrapper just forwards it)."""
    global _file_providers, _langchain_instrumented
    _file_providers = []
    # Reset per call, same as _file_providers -- a fresh call builds a
    # fresh llm_provider (if any), and that new provider legitimately
    # needs its own instrumentation decision; the flag only exists to
    # stop this ONE call from instrumenting twice if more than one entry
    # asks for it (see the check further down), not to remember across
    # separate setup_telemetry() calls that it already happened once.
    _langchain_instrumented = False

    # Set before any provider is built, because the OTel SDK reads it
    # when it constructs the default Resource and never revisits it.
    # setdefault, not assignment: a deployment that wants a different
    # name per instance should be able to say so from the outside.
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)

    raw_entries = _raw_provider_entries()
    if not raw_entries:
        return None

    discovered = discover_provider_types()
    validate_configured_types(discovered, raw_entries)

    entries = [e for e in (_resolved_entry(raw) for raw in raw_entries) if e is not None]
    if not entries:
        return None

    # Which internal providers are actually needed -- purely a function
    # of what KIND the CONFIGURED entries' classes declare, never a
    # separate settings list a deployer has to get right (see module
    # docstring). A dual-KIND entry (otlp) makes both True from one
    # config line.
    needs_general = any("general" in discovered[e["type"]].KIND for e in entries)
    needs_llm = any("llm" in discovered[e["type"]].KIND for e in entries)

    # Two SEPARATE providers -- see module docstring for why one shared
    # provider was wrong. Only built when at least one entry actually
    # needs that category, so an unconfigured category never gets a
    # pointless empty TracerProvider.
    general_provider = None
    if needs_general:
        general_provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        # The ONLY thing that becomes the process-global ambient
        # provider -- EventLogger's ambient trace.get_tracer(scope)
        # calls (and this codebase's other pre-existing module-level
        # `_tracer = trace.get_tracer(__name__)` lines in news_sources.py
        # /news_ingest.py, which predate this refactor and emit
        # general-category spans themselves) resolve against whatever
        # this call sets. The llm provider is never installed globally --
        # LangChainInstrumentor is handed it directly (see otlp.py's
        # initialize), so it never needs to be.
        trace.set_tracer_provider(general_provider)

    llm_provider = None
    if needs_llm:
        # Phoenix groups spans into projects via a resource attribute,
        # not a span attribute or a header -- Resource is fixed once, at
        # TracerProvider construction, so this has to be decided before
        # the provider is built. Only meaningful on the llm provider --
        # general-category spans have no Phoenix project to belong to.
        # See telemetry_providers/phoenix.py's own docstring for the
        # resource-key source and why it's not imported from a phoenix
        # package here.
        resource_attrs = {"service.name": SERVICE_NAME}
        for entry in entries:
            if entry["type"] == "phoenix":
                resource_attrs["openinference.project.name"] = entry.get("project_name", "default")
        llm_provider = TracerProvider(resource=Resource.create(resource_attrs))

    for entry in entries:
        provider_cls = discovered[entry["type"]]
        if "general" in provider_cls.KIND:
            _activate(entry, "general", provider_cls, general_provider)
        if "llm" in provider_cls.KIND:
            _activate(entry, "llm", provider_cls, llm_provider)

    # Whether LangChain's auto-instrumentation attaches to the llm
    # provider is a property of the LLM CATEGORY, not of any one
    # provider class -- an llm-kind entry's own config asks for it
    # (instrument_langchain: true), regardless of which adapter(s)
    # actually attached processors to llm_provider above. Checked once
    # here rather than inside each adapter's own initialize(): that was
    # otlp.py's original design, and it meant a config with ONLY a
    # phoenix entry (no otlp entry alongside it) could never trigger
    # instrumentation at all, even though phoenix's entire purpose is
    # inspecting the LLM call tree -- see otlp.py's module docstring for
    # the full incident this fixes. Once instrumented, every processor
    # attached to llm_provider (from any llm-kind entry) receives the
    # resulting spans via the provider's own multi-processor delivery,
    # not just the entry that happened to ask for it.
    if llm_provider is not None and not _langchain_instrumented:
        wants_langchain = any(
            "llm" in discovered[entry["type"]].KIND and entry.get("instrument_langchain")
            for entry in entries
        )
        if wants_langchain:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument(tracer_provider=llm_provider)
            _langchain_instrumented = True

    return general_provider, llm_provider


class EventLogger:
    """Facade bound to one scope -- what every module-level `_events =
    get_event_logger("argus.<module>")` call site holds. Preserves the
    exact log(event, message, level, tags, exc) signature and printed-
    line/span-attribute shape LogfireLogger.log already had; only the
    construction line at each call site changes
    (LogfireLogger("scope") -> get_event_logger("scope"))."""

    def __init__(self, scope: str):
        """Stores a real tracer immediately, at construction time --
        every `_events = get_event_logger("argus.<module>")` call site
        runs at module import, before setup_telemetry() has necessarily
        run. This is safe: OTel's trace.get_tracer() returns a proxy
        that starts actually working the moment
        trace.set_tracer_provider() is called later -- and per
        setup_telemetry()'s own module-docstring note, that call is
        ALWAYS made with the general provider specifically, never the
        llm one, which is what keeps every EventLogger's spans isolated
        to general-category processors (file.py aside, which bypasses
        the tracer entirely -- see log() below). Same established
        pattern this codebase's own module-level `_tracer =
        trace.get_tracer(__name__)` lines already rely on
        (news_sources.py, news_ingest.py) -- and it's what lets tests
        monkeypatch `<logger>._tracer.start_as_current_span` directly
        (see tests/fakes.py's FakeSpan)."""
        self._scope = scope
        self._tracer = trace.get_tracer(scope)

    def log(self, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        as_dict = message if isinstance(message, dict) else {"message": message}
        text = as_dict.get("message", str(as_dict))
        line = f"[{self._scope}] {text}"
        if exc is not None:
            line += f": {exc!r}"
        print(line)

        # Exactly ONE span per call, unconditionally, regardless of
        # whether any SPAN_BASED general provider is actually
        # configured -- self._tracer is a real OTel tracer either way
        # (see __init__), and OTel's own no-op tracer safely swallows
        # this when trace.set_tracer_provider() was never called with a
        # real provider, same "always try, no-op absorbs it" contract
        # LogfireLogger.log() always had. When multiple SPAN_BASED
        # general providers ARE configured (e.g. otlp AND a future
        # second general-kind provider), this one span still reaches
        # every one of them via the GENERAL provider's own
        # multi-processor delivery (all attached to that one provider,
        # never the llm provider -- see setup_telemetry()'s module
        # docstring) -- calling each provider's own log() here instead
        # would create one span PER provider and double-export the same
        # event, see telemetry_providers/__init__.py's module docstring.
        with self._tracer.start_as_current_span(event) as span:
            span.set_attribute("logfire.msg", line)
            span.set_attribute("logfire.level_num", level)
            if tags:
                span.set_attribute("logfire.tags", tags)
            for k, v in as_dict.items():
                span.set_attribute(k, v)
            if exc is not None:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))

        # Non-span-based providers (file.py) have no processor to fan
        # out through, so each gets an explicit call.
        for provider in _file_providers:
            provider.log(self._scope, event, message, level=level, tags=tags, exc=exc)


def get_event_logger(scope: str) -> EventLogger:
    """One instance per module, module-level, matching the established
    `_events = get_event_logger("argus.<module>")` convention every
    call site already uses (previously `LogfireLogger("argus.<module>")`).
    Safe to construct before setup_telemetry() runs, or when telemetry
    is entirely off -- log() always at least prints; span emission/file
    writes only happen for whichever providers setup_telemetry actually
    activated."""
    return EventLogger(scope)
