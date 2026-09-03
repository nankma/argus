"""
OtlpProvider -- generic OTLP/HTTP span exporter, config-driven (endpoint
+ headers only, no per-vendor logic). Covers any OTLP-compatible
backend: Logfire, Grafana Cloud, SigNoz, OpenObserve, and Phoenix's own
OTLP ingestion if a deployer wants to point at it directly instead of
using the phoenix.py adapter. Deriving a specific vendor's endpoint from
a token/region (e.g. Logfire's region-from-token-prefix convenience,
see tools/check_logfire.py's LOGFIRE_HOSTS) is deliberately NOT this
adapter's job -- settings.yml specifies the resolved endpoint directly,
computed once by whoever deploys.

KIND includes both "general" and "llm": one telemetry.providers entry
can receive both general event spans (this provider's log(), when it's
ever called directly -- see SPAN_BASED below) and LangChain-auto-
instrumented LLM spans (via instrument_langchain) -- a single config
entry, not two. telemetry.py's coordinator calls initialize() on a
FRESH instance once per applicable internal provider (general and/or
llm), passing `kind` so this class knows which one it's attaching to on
any given call -- see initialize()'s own docstring for why that matters
for instrument_langchain specifically.
"""

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from telemetry_providers import Level

_langchain_instrumented = False


class OtlpProvider:
    TYPE = "otlp"
    KIND = frozenset({"general", "llm"})
    # See telemetry_providers/__init__.py's module docstring: this
    # provider is span-based, so telemetry.py's coordinator emits the
    # one shared span itself (which this provider's own attached
    # processor then exports) rather than calling this class's log()
    # per event -- calling both would double-export.
    SPAN_BASED = True

    def initialize(self, config: dict, tracer_provider: TracerProvider, kind: str) -> None:
        """Adds one BatchSpanProcessor to whichever tracer_provider it's
        given -- never builds or installs its own provider. For a
        dual-KIND entry, telemetry.py calls this on a FRESH OtlpProvider
        instance once per applicable internal provider -- once with
        (general_provider, kind="general"), once with (llm_provider,
        kind="llm") -- so ONE settings entry ends up with two
        independent processors, one per category, from one config line
        (see module docstring).

        This is also the specific fix for a real incident documented in
        docs/plans/observability-platform-plan.md's "dual-write bug":
        phoenix.otel's TracerProvider subclass silently discards its
        own default processor the moment a second one is added unless
        told not to, so a second backend enabled alongside it received
        nothing while everything looked fine. Every provider in this
        package instead attaches to a plain TracerProvider via
        add_span_processor, which is additive by default on a plain
        OTel provider -- there is no "default processor" here to
        silently lose.

        instrument_langchain (config, default False): also attaches
        openinference's LangChain auto-instrumentation to this call's
        tracer_provider -- but ONLY when kind == "llm". Without this
        gate, a dual-KIND entry with instrument_langchain: true would
        instrument LangChain onto the GENERAL provider too (since
        initialize() is also called once for the general side of the
        same entry) -- LLM-call spans have no business on the general
        provider, so this is a real correctness gate, not a redundant
        check. Guarded by a module-level flag on top of that --
        LangChainInstrumentor().instrument() is not documented as
        idempotent, and more than one otlp entry could plausibly ask
        for it (e.g. Logfire AND Grafana Cloud both wanting LLM traces)
        -- only the first such request actually instruments."""
        exporter = OTLPSpanExporter(endpoint=config["endpoint"], headers=config.get("headers", {}))
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

        if kind == "llm" and config.get("instrument_langchain"):
            global _langchain_instrumented
            if not _langchain_instrumented:
                from openinference.instrumentation.langchain import LangChainInstrumentor
                LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
                _langchain_instrumented = True

    def log(self, scope: str, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        """Never called in practice -- SPAN_BASED providers receive
        events through the one span telemetry.EventLogger.log() emits
        directly on the general provider (called once per event
        regardless of how many SPAN_BASED general providers are
        configured -- see this package's __init__.py module docstring
        for why), not through a per-provider log() call. Exists only to
        satisfy TelemetryProvider structurally."""
