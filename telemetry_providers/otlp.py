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
instrumented LLM spans -- a single config entry, not two. telemetry.py's
coordinator calls initialize() on a FRESH instance once per applicable
internal provider (general and/or llm), passing `kind` so this class
knows which one it's attaching to on any given call.

instrument_langchain (the "should LangChain's auto-instrumentation
attach to the llm provider" switch) is NOT this adapter's concern --
see telemetry.py's setup_telemetry(), which handles it once at the
coordinator level, independent of which llm-kind provider(s) end up
receiving the resulting spans. It used to live here (checking
config.get("instrument_langchain") per otlp entry), which meant
phoenix.py -- an llm-only provider with an equally good claim to
wanting LLM traces -- had no way to trigger it at all: configuring
ONLY a phoenix entry (no otlp entry alongside it) meant nothing ever
called LangChainInstrumentor, so Phoenix received zero spans despite
being correctly wired up in every other respect. Moving the trigger to
the coordinator (checked once, against every llm-kind entry regardless
of adapter type) fixes that for any current or future llm-kind
provider, not just otlp.
"""

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from telemetry_providers import Level


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
        silently lose."""
        exporter = OTLPSpanExporter(endpoint=config["endpoint"], headers=config.get("headers", {}))
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

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
