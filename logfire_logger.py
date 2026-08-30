"""
A reusable Logfire client -- portable across projects, nothing here
hardcodes this repo's own naming (`argus`, `SERVICE_NAME`, etc; those are
passed in by the caller). Two responsibilities:

1. `LogfireLogger.setup(...)`, called once per process, wires up the
   OTel/Logfire pipeline (and optionally instruments LangChain's own
   execution onto the same provider).
2. Per-module `LogfireLogger` instances that print to stdout AND record a
   leveled event to Logfire from one call -- for the (common) case where
   there's no already-open span to attach an exception to. Where a span
   IS already open (e.g. news_ingest._pull_source's per-section fetch,
   already inside its own ingest_source_pull span), call
   `span.record_exception(exc)` directly instead -- this class doesn't
   need to be involved there.

Built to replace scattered `print(f"[module] ...")` calls inside `except`
blocks that only ever reached docker logs (ephemeral, not queryable, lost
on container swap) with something that reaches both docker logs AND
Logfire (queryable, alertable) from the same call.

`level`/`tags`/a custom `message` (distinct from the span name) are all
real, verified live against Logfire's actual OTLP ingestion (not assumed
from docs) via three specific attribute keys Logfire recognizes:
`logfire.level_num` (int, standard OTel severity-number scale -- see
`Level` below), `logfire.tags` (tuple/list of str), `logfire.msg` (str).
All three are consumed by Logfire and stripped out of the regular
`attributes` JSON, so they never clutter a `WHERE attributes->>'x'`
business-attribute query. See `docs/current/telemetry-catalog.md` for
the project-specific write-up of this finding.
"""

from typing import Protocol

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class Level:
    """OTel standard severity numbers -- Logfire's `level` column reads
    these directly via the `logfire.level_num` attribute. DEBUG (5)
    deliberately omitted -- not needed yet -- but the numbering leaves
    room for it without renumbering anything else."""

    TRACE = 1
    INFO = 9
    WARN = 13
    ERROR = 17
    FATAL = 21


class Logger(Protocol):
    """Records 'X happened' -- optionally with an exception -- somewhere
    both a human (stdout/docker logs) and an alert query (Logfire) can
    see it. One implementation today (LogfireLogger); swap in a
    different one later without touching call sites."""

    def log(self, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        ...


class LogfireLogger:
    """See module docstring. `setup()` is a classmethod (one pipeline per
    process); instances are per-module/per-scope and cheap to create."""

    _provider: TracerProvider | None = None

    @classmethod
    def setup(cls, service_name: str, token: str, endpoint: str,
              instrument_langchain: bool = False) -> TracerProvider:
        """Call once per process, before constructing any LogfireLogger
        or emitting any span. Idempotent: returns the existing provider
        on a second call rather than rebuilding it (and ignores its
        arguments then -- the first call wins).

        Does NOT set OTEL_SERVICE_NAME itself. The OTel SDK reads that
        env var once, when it builds a Resource, and never revisits it --
        so if anything else in the process might build a Resource first,
        the caller must set it BEFORE calling setup(). A portable class
        shouldn't assume it's the only thing touching OTel setup in a
        given process."""
        if cls._provider is not None:
            return cls._provider
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        if instrument_langchain:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument(tracer_provider=provider)
        exporter = OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": token})
        provider.add_span_processor(BatchSpanProcessor(exporter))
        cls._provider = provider
        return provider

    def __init__(self, scope: str):
        """`scope` becomes the OTel instrumentation-scope name AND the
        `[scope]` prefix on the printed line. Callers choose their own
        naming convention -- this class doesn't assume one."""
        self._scope = scope
        self._tracer = trace.get_tracer(scope)

    def log(self, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        """`message` as a plain string becomes `{"message": message}`; a
        dict is used as-is, with every key landing as a real span
        attribute (not just embedded in free text) -- pass whatever local
        context is useful to query on later, not just a human sentence.

        `event` is the span name -- required, explicit, short
        snake_case, chosen by the caller per failure mode (e.g.
        `router_failed`, not auto-derived from `message`), so each
        failure mode stays independently queryable
        (`WHERE span_name = 'router_failed'`) instead of falling back to
        grepping message text."""
        as_dict = message if isinstance(message, dict) else {"message": message}
        text = as_dict.get("message", str(as_dict))
        line = f"[{self._scope}] {text}"
        if exc is not None:
            line += f": {exc!r}"
        print(line)
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
