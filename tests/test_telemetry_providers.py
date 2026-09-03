import subprocess
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider
from trailsign import SettingsError

import telemetry_providers
from telemetry_providers.file import FileProvider
from telemetry_providers.otlp import OtlpProvider
from telemetry_providers.phoenix import PhoenixProvider


class _FakeProcessor:
    """A real TracerProvider calls .shutdown() on its span processor at
    interpreter exit (atexit) -- a bare string/lambda return value from a
    mocked BatchSpanProcessor blows up harmlessly-but-noisily at that
    point. This gives it the one method it needs."""

    def shutdown(self):
        pass


# --- discovery / validation --------------------------------------------


def test_discover_provider_types_finds_all_three():
    discovered = telemetry_providers.discover_provider_types()
    assert discovered["otlp"] is OtlpProvider
    assert discovered["file"] is FileProvider
    assert discovered["phoenix"] is PhoenixProvider


def test_validate_configured_types_passes_for_known_types():
    discovered = telemetry_providers.discover_provider_types()
    telemetry_providers.validate_configured_types(discovered, [{"type": "otlp"}, {"type": "file"}])


def test_validate_configured_types_raises_for_an_unknown_type():
    discovered = telemetry_providers.discover_provider_types()
    with pytest.raises(SettingsError, match="langfuse"):
        telemetry_providers.validate_configured_types(discovered, [{"type": "langfuse"}])


def test_an_unknown_configured_type_fails_the_process_at_import_time():
    """Not just a testable helper function -- confirms the real
    "don't start the service" guarantee by running a fresh subprocess
    that mocks Settings to a bad telemetry.providers entry BEFORE
    importing telemetry, then calling setup_telemetry(). Same
    methodology news_adapters' own equivalent test uses."""
    script = (
        "import app_settings\n"
        "from trailsign import Settings\n"
        "app_settings.reset_settings_for_tests(Settings({\n"
        "    'telemetry': {'providers': [{'type': 'nonexistent_type', 'endpoint': 'x'}]},\n"
        "}))\n"
        "import telemetry\n"
        "telemetry.setup_telemetry()\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "SettingsError" in result.stderr
    assert "nonexistent_type" in result.stderr


# --- otlp.py -------------------------------------------------------------


def test_otlp_initialize_adds_a_span_processor_not_a_new_provider(monkeypatch):
    calls = []
    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter",
                        lambda endpoint, headers: ("exporter", endpoint, headers))
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor", lambda exporter: _FakeProcessor())

    class _FakeProvider:
        def add_span_processor(self, processor):
            calls.append(processor)

    provider = _FakeProvider()
    OtlpProvider().initialize({"endpoint": "https://example.test/v1/traces", "headers": {"Authorization": "tok"}},
                              provider, "general")

    assert len(calls) == 1
    assert isinstance(calls[0], _FakeProcessor)


# instrument_langchain is no longer this adapter's concern -- it moved
# to telemetry.py's setup_telemetry(), which triggers it once at the
# coordinator level for ANY llm-kind entry that asks for it, not just
# otlp ones (see telemetry.py's own tests, and otlp.py's module
# docstring for why: an otlp-only trigger meant phoenix-only configs
# could never get LangChain traces onto the llm provider at all).


# --- file.py ---------------------------------------------------------------


def test_file_provider_writes_json_lines(tmp_path):
    path = tmp_path / "events.log"
    provider = FileProvider()
    provider.initialize({"path": str(path)}, TracerProvider(), "general")

    provider.log("argus.testmod", "something_happened", "a message", tags=("a", "b"))
    provider.log("argus.testmod", "router_failed", "boom", exc=RuntimeError("x"))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import json
    first = json.loads(lines[0])
    assert first["scope"] == "argus.testmod"
    assert first["event"] == "something_happened"
    assert first["message"] == "a message"
    assert first["tags"] == ["a", "b"]
    second = json.loads(lines[1])
    assert "RuntimeError" in second["exception"]


def test_file_provider_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "events.log"
    provider = FileProvider()
    provider.initialize({"path": str(path)}, TracerProvider(), "general")

    provider.log("argus.testmod", "something_happened", "a message")

    assert path.exists()


def test_file_provider_fails_open_on_a_write_error(tmp_path):
    """A logging sink must never be the reason the thing it was logging
    about doesn't complete -- see file.py's own comment on this. Forces
    a real OSError (not a mock): "blocker" exists as a plain FILE, so
    mkdir(parents=True, exist_ok=True) on a path that needs "blocker" to
    be a directory raises FileExistsError (an OSError subclass)
    regardless of exist_ok, since the existing entry isn't a directory.
    log() must swallow this and simply not raise -- the caller
    (telemetry.EventLogger.log) already printed the event to stdout
    before this was ever called."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    provider = FileProvider()
    provider.initialize({"path": str(blocker / "events.log")}, TracerProvider(), "general")

    provider.log("argus.testmod", "something_happened", "a message")  # must not raise


def test_file_provider_log_carries_dict_message_keys(tmp_path):
    path = tmp_path / "events.log"
    provider = FileProvider()
    provider.initialize({"path": str(path)}, TracerProvider(), "general")

    provider.log("argus.testmod", "something_happened", {"message": "hi", "topic": "AI"})

    import json
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["topic"] == "AI"


# --- phoenix.py --------------------------------------------------------------


def test_phoenix_attaches_to_the_shared_provider_not_a_new_one(monkeypatch):
    """The one thing most worth a real test: PhoenixProvider must never
    build/install its own TracerProvider (that's the exact dual-write
    failure mode this whole redesign exists to avoid -- see
    docs/plans/observability-platform-plan.md). It builds a plain OTLP
    exporter/processor and adds that to whatever provider it's given."""
    import telemetry_providers.phoenix as phoenix_module
    calls = []
    monkeypatch.setattr(phoenix_module, "OTLPSpanExporter",
                        lambda endpoint, headers: ("exporter", endpoint, headers))
    monkeypatch.setattr(phoenix_module, "BatchSpanProcessor", lambda exporter: _FakeProcessor())

    class _FakeSharedProvider:
        def add_span_processor(self, processor):
            calls.append(processor)

    shared = _FakeSharedProvider()
    PhoenixProvider().initialize({"endpoint": "http://localhost:6006/v1/traces"}, shared, "llm")

    assert len(calls) == 1
    assert isinstance(calls[0], _FakeProcessor)
