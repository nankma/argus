from opentelemetry import trace

from logfire_logger import Level, LogfireLogger
from tests.fakes import FakeSpan


class _FakeProcessor:
    """A real TracerProvider calls .shutdown() on its span processor at
    interpreter exit (atexit) -- a bare string/lambda return value from
    a mocked BatchSpanProcessor blows up harmlessly-but-noisily at that
    point. This gives it the one method it needs."""

    def shutdown(self):
        pass


def _patch_span(monkeypatch, logger):
    span = FakeSpan()
    monkeypatch.setattr(logger._tracer, "start_as_current_span", lambda name: span)
    return span


def test_log_prints_the_scope_and_message(monkeypatch, capsys):
    logger = LogfireLogger("argus.testmod")
    _patch_span(monkeypatch, logger)

    logger.log("something_happened", "a plain message")

    out = capsys.readouterr().out
    assert out.strip() == "[argus.testmod] a plain message"


def test_log_with_a_dict_message_carries_every_key_as_an_attribute(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("something_happened", {"message": "a message", "topic": "AI", "attempt": 2})

    assert span.attrs["topic"] == "AI"
    assert span.attrs["attempt"] == 2
    assert span.attrs["message"] == "a message"


def test_log_defaults_to_info_level(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("something_happened", "msg")

    assert span.attrs["logfire.level_num"] == Level.INFO


def test_log_honors_an_explicit_level(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("something_happened", "msg", level=Level.WARN)

    assert span.attrs["logfire.level_num"] == Level.WARN


def test_log_sets_tags_only_when_given(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("something_happened", "msg")
    assert "logfire.tags" not in span.attrs

    span2 = _patch_span(monkeypatch, logger)
    logger.log("something_happened", "msg", tags=("ingest", "perigon"))
    assert span2.attrs["logfire.tags"] == ("ingest", "perigon")


def test_log_sets_a_custom_message_distinct_from_the_span_name(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("router_failed", "the router blew up")

    assert span.attrs["logfire.msg"] == "[argus.testmod] the router blew up"


def test_log_records_the_exception_and_sets_error_status(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)
    exc = RuntimeError("boom")

    logger.log("router_failed", "the router blew up", exc=exc)

    assert span.exceptions == [exc]
    assert span.status.status_code == trace.StatusCode.ERROR


def test_log_without_an_exception_does_not_touch_status(monkeypatch):
    logger = LogfireLogger("argus.testmod")
    span = _patch_span(monkeypatch, logger)

    logger.log("something_happened", "all fine")

    assert span.exceptions == []
    assert span.status is None


def test_log_appends_the_exception_repr_to_the_printed_line(monkeypatch, capsys):
    logger = LogfireLogger("argus.testmod")
    _patch_span(monkeypatch, logger)
    exc = ValueError("bad value")

    logger.log("router_failed", "the router blew up", exc=exc)

    out = capsys.readouterr().out
    assert "the router blew up" in out
    assert repr(exc) in out


def test_setup_is_idempotent_across_repeated_calls(monkeypatch):
    """A second call must not rebuild the provider -- module-level state
    persisting a single provider per process is the whole point."""
    LogfireLogger._provider = None
    calls = []

    class FakeExporter:
        def __init__(self, endpoint, headers):
            calls.append((endpoint, headers))

    monkeypatch.setattr("logfire_logger.OTLPSpanExporter", FakeExporter)
    monkeypatch.setattr("logfire_logger.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    monkeypatch.setattr("logfire_logger.trace.set_tracer_provider", lambda provider: None)

    first = LogfireLogger.setup(service_name="svc", token="tok", endpoint="https://example.test/v1/traces")
    second = LogfireLogger.setup(service_name="different", token="different", endpoint="https://other.test")

    assert first is second
    assert len(calls) == 1  # exporter only ever constructed once
    LogfireLogger._provider = None  # don't leak state into other tests


def test_setup_instruments_langchain_only_when_asked(monkeypatch):
    LogfireLogger._provider = None
    monkeypatch.setattr("logfire_logger.OTLPSpanExporter", lambda endpoint, headers: "exporter")
    monkeypatch.setattr("logfire_logger.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    monkeypatch.setattr("logfire_logger.trace.set_tracer_provider", lambda provider: None)

    instrumented = []
    fake_instrumentor_module = type(
        "M", (), {
            "LangChainInstrumentor": lambda: type(
                "I", (), {"instrument": lambda self, tracer_provider: instrumented.append(tracer_provider)}
            )()
        }
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "openinference.instrumentation.langchain",
        fake_instrumentor_module,
    )

    provider = LogfireLogger.setup(
        service_name="svc", token="tok", endpoint="https://example.test",
        instrument_langchain=True,
    )

    assert instrumented == [provider]
    LogfireLogger._provider = None


def test_setup_skips_langchain_instrumentation_by_default(monkeypatch):
    LogfireLogger._provider = None
    monkeypatch.setattr("logfire_logger.OTLPSpanExporter", lambda endpoint, headers: "exporter")
    monkeypatch.setattr("logfire_logger.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    monkeypatch.setattr("logfire_logger.trace.set_tracer_provider", lambda provider: None)

    # If LangChainInstrumentor were imported/called without instrument_langchain=True,
    # this import failing (module not mocked) would raise -- proving it wasn't touched.
    LogfireLogger.setup(service_name="svc", token="tok", endpoint="https://example.test")

    LogfireLogger._provider = None
