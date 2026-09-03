"""
PhoenixProvider -- exports LLM-call spans to a self-hosted Arize
Phoenix instance. "llm" kind only: this is for inspecting the LLM
call tree specifically (Phoenix's own UI is built around that), not
general app-event logging.

Deliberately does NOT call arize-phoenix-otel's own `phoenix.otel.
register()`. Inspected its actual implementation directly (installed
into a scratch dir, not this project's environment, purely to read the
source): register() unconditionally constructs and returns its OWN
`phoenix.otel.TracerProvider` subclass -- there is no parameter to hand
it an existing provider to attach to instead. That subclass is also the
one whose special "a default processor gets silently discarded unless
you pass replace_default_processor=False" behavior caused the real
dual-write incident in docs/plans/observability-platform-plan.md.

Sidestepping register() avoids that whole failure class at the root,
rather than working around it: Phoenix's collector ingests plain OTLP
under the hood (register() itself just wraps a standard OTLP HTTP/gRPC
exporter -- see phoenix.otel.otel.HTTPSpanExporter), so this provider
builds a plain OTLPSpanExporter pointed at Phoenix's endpoint and adds
it to the SAME shared, plain TracerProvider every other provider in
this package attaches to, via ordinary add_span_processor -- identical
shape to otlp.py, just carrying Phoenix's project-name resource
attribute (set once, at shared-provider construction time, by
telemetry.setup_telemetry -- see that module) instead of a vendor auth
header.

A consequence worth stating plainly: this provider needs NO Phoenix
package at all, `arize-phoenix-otel` included -- every class it imports
(OTLPSpanExporter, BatchSpanProcessor) is already a required dependency
of this project (see otlp.py, identical imports). Nothing new goes into
environment.yml for this. If a future change to this file ever imports
anything from a `phoenix`/`phoenix.otel` module, that reintroduces the
exact dependency this design avoided -- re-read the reasoning above
first, and never import from a bare `phoenix` module either way (that
would mean the full `arize-phoenix` package, which imports pandas at
`import phoenix` time and is blocked by Windows Smart App Control on
this dev machine -- see CLAUDE.md's own landmine on this).
"""

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from telemetry_providers import Level

#: The resource attribute Phoenix groups spans into projects by (see
#: phoenix.otel.otel.PROJECT_NAME -- 'openinference.project.name').
#: Not imported from the phoenix package itself: importing anything
#: from phoenix.otel here would make arize-phoenix-otel a hard runtime
#: dependency of this provider even when telemetry.providers has no
#: phoenix entry configured, unlike every other provider in this
#: package (which only imports optional SDKs lazily, inside
#: initialize()). The literal
#: string is Phoenix's own public, documented resource-attribute
#: convention, not an internal we're reaching into.
PROJECT_NAME_RESOURCE_KEY = "openinference.project.name"


class PhoenixProvider:
    TYPE = "phoenix"
    KIND = frozenset({"llm"})
    # See telemetry_providers/__init__.py's module docstring: span-based,
    # so telemetry.py's coordinator never calls this class's log().
    SPAN_BASED = True

    def initialize(self, config: dict, tracer_provider: TracerProvider, kind: str) -> None:
        """config needs `endpoint` (a locally-run Phoenix instance's
        OTLP endpoint, e.g. http://localhost:6006/v1/traces -- no
        hosted default, this project's own Phoenix instance was
        retired, see docs/plans/observability-platform-plan.md).
        `project_name` is read by telemetry.setup_telemetry when it
        builds the LLM provider's Resource, not by this method -- by
        the time initialize() runs here, the resource is already fixed
        (Resource is set once, at TracerProvider construction).
        `tracer_provider`/`kind` here are ALWAYS telemetry.py's llm
        provider/"llm" -- phoenix's KIND is {"llm"} only, so
        setup_telemetry() never calls this with the general provider.
        `kind` itself is unused (ignored, satisfies the shared Protocol
        signature -- see telemetry_providers/__init__.py)."""
        exporter = OTLPSpanExporter(endpoint=config["endpoint"], headers=config.get("headers", {}))
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    def log(self, scope: str, event: str, message: str | dict,
            level: int = Level.INFO, tags: tuple[str, ...] = (),
            exc: BaseException | None = None) -> None:
        """Never called -- see SPAN_BASED and otlp.py's identical
        docstring on this same method."""
