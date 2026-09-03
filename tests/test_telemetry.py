import json
import os

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from trailsign import Settings

import agent
import telemetry
from tests.fakes import FakeSpan


class _FakeProcessor:
    """See telemetry_providers/otlp.py's own test file for why this
    exists -- a real TracerProvider calls .shutdown() on its processors
    at interpreter exit, which blows up harmlessly-but-noisily against a
    bare mock return value."""

    def shutdown(self):
        pass


def _set_telemetry_settings(monkeypatch, providers=None):
    """Injects a fake Settings whose only content is telemetry.providers
    (ONE list -- see telemetry.py's module docstring for why there's no
    separate list per category), and monkeypatches telemetry.get_settings
    to return it -- same pattern news_sources.py's tests use for
    news_source.api. Also resets telemetry's module-level fan-out state,
    since setup_telemetry() otherwise only resets it on its own next
    call."""
    settings = Settings({"telemetry": {"providers": providers or []}})
    monkeypatch.setattr(telemetry, "get_settings", lambda: settings)
    telemetry._file_providers = []


@pytest.fixture(autouse=True)
def _reset_tracer_provider(monkeypatch):
    """Every test in this file calls setup_telemetry(), which calls
    trace.set_tracer_provider() -- a real, process-global, one-way call
    outside of tests (OTel logs a warning and ignores a second call by
    default). Route it through a no-op in tests instead, matching how
    the old test_logfire_logger.py avoided the same problem, so tests
    don't interfere with each other or with a real provider some other
    test/module may have already installed."""
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda provider: None)


def test_setup_telemetry_returns_none_and_touches_nothing_when_unconfigured(monkeypatch):
    _set_telemetry_settings(monkeypatch)
    assert telemetry.setup_telemetry() is None
    assert telemetry._file_providers == []


def test_setup_telemetry_raises_for_an_unknown_configured_type(monkeypatch):
    from trailsign import SettingsError
    _set_telemetry_settings(monkeypatch, providers=[{"type": "nonexistent", "endpoint": "x"}])
    with pytest.raises(SettingsError, match="nonexistent"):
        telemetry.setup_telemetry()


def test_setup_telemetry_activates_otlp_provider_and_builds_both_internal_providers(monkeypatch):
    """otlp's KIND is {"general","llm"} -- one config entry needs both
    internal TracerProviders built, since it feeds both categories (see
    telemetry.py's module docstring for why there's no separate list a
    deployer has to configure per category anymore)."""
    exporter_calls = []
    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter",
                        lambda endpoint, headers: exporter_calls.append(endpoint) or "exporter")
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    _set_telemetry_settings(monkeypatch, providers=[{"type": "otlp", "endpoint": "https://example.test/v1/traces"}])

    general_provider, llm_provider = telemetry.setup_telemetry()

    assert general_provider is not None
    assert llm_provider is not None
    assert general_provider is not llm_provider
    # otlp is SPAN_BASED, so it never lands in _file_providers -- proof
    # it was actually initialized (twice -- once per internal provider)
    # is that its exporter got built twice, for the same endpoint.
    assert exporter_calls == ["https://example.test/v1/traces"] * 2
    assert telemetry._file_providers == []


def test_setup_telemetry_activates_file_general_provider(monkeypatch, tmp_path):
    path = tmp_path / "events.log"
    _set_telemetry_settings(monkeypatch, providers=[{"type": "file", "path": str(path)}])

    general_provider, llm_provider = telemetry.setup_telemetry()

    assert len(telemetry._file_providers) == 1
    # file's KIND is {"general"} only -- no llm provider gets built for
    # a config that has nothing needing one.
    assert llm_provider is None


def test_setup_telemetry_puts_phoenix_project_name_on_the_llm_resource(monkeypatch):
    """Phoenix groups spans into projects via a resource attribute, not
    a span attribute or a header -- Resource is fixed once, at
    TracerProvider construction (see telemetry.py's own comment on this
    in setup_telemetry). Only meaningful on the LLM provider -- verified
    directly on the real Resource object, not by trusting the code path,
    since this had no regression coverage at all before (found by
    qa-engineer, confirmed correct by hand, not previously guarded)."""
    monkeypatch.setattr("telemetry_providers.phoenix.OTLPSpanExporter",
                        lambda endpoint, headers: "exporter")
    monkeypatch.setattr("telemetry_providers.phoenix.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    _set_telemetry_settings(monkeypatch, providers=[
        {"type": "phoenix", "endpoint": "http://localhost:6006/v1/traces", "project_name": "my-proj"},
    ])

    general_provider, llm_provider = telemetry.setup_telemetry()

    assert general_provider is None  # phoenix's KIND is {"llm"} only
    assert llm_provider.resource.attributes["openinference.project.name"] == "my-proj"


def test_setup_telemetry_skips_an_entry_whose_credential_is_unresolvable(monkeypatch):
    """One bad optional value in one entry must not take down the other
    configured providers -- same contract news_source.api's
    _resolved_api_key established, applied here to any nested
    trailsign-resolve node (e.g. headers.Authorization), not just an
    api-key field specifically."""
    exporter_calls = []
    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter",
                        lambda endpoint, headers: exporter_calls.append(endpoint) or "exporter")
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    monkeypatch.delenv("UNSET_TELEMETRY_TOKEN", raising=False)
    settings = Settings({
        "telemetry": {
            "providers": [
                {"type": "otlp", "endpoint": "https://good.test/v1/traces"},
                {
                    "type": "otlp",
                    "endpoint": "https://bad.test/v1/traces",
                    "headers": {
                        "Authorization": {"trailsign-resolve": "environment-variable", "name": "UNSET_TELEMETRY_TOKEN"},
                    },
                },
            ],
        },
    })
    monkeypatch.setattr(telemetry, "get_settings", lambda: settings)
    telemetry._file_providers = []

    telemetry.setup_telemetry()

    # Only the good entry's exporter got built -- the bad one was
    # dropped silently, not raised, and didn't take the good one with it.
    # (Twice, one per internal provider -- otlp's KIND covers both.)
    assert exporter_calls == ["https://good.test/v1/traces"] * 2


def test_get_event_logger_fans_out_to_every_configured_general_provider(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter", lambda endpoint, headers: "exporter")
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    path = tmp_path / "events.log"
    _set_telemetry_settings(monkeypatch, providers=[
        {"type": "otlp", "endpoint": "https://example.test/v1/traces"},
        {"type": "file", "path": str(path)},
    ])
    telemetry.setup_telemetry()

    logger = telemetry.get_event_logger("argus.testmod")
    span = FakeSpan()
    monkeypatch.setattr(logger._tracer, "start_as_current_span", lambda name: span)

    logger.log("something_happened", "a message")

    # File provider got the event.
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "something_happened"
    # Exactly ONE span was created for the one otlp entry -- not one per
    # configured general provider (see telemetry_providers/__init__.py's
    # module docstring on why double-emission would be wrong).
    assert span.attrs["logfire.msg"] == "[argus.testmod] a message"


def test_get_event_logger_always_prints_even_when_nothing_is_configured(monkeypatch, capsys):
    _set_telemetry_settings(monkeypatch)
    telemetry.setup_telemetry()

    logger = telemetry.get_event_logger("argus.testmod")
    logger.log("something_happened", "a message")

    assert "[argus.testmod] a message" in capsys.readouterr().out


# --- Kind isolation: general and llm spans must never cross-contaminate ---
#
# Real bug, found by code review 2026-09-03: an earlier version of this
# module built ONE shared TracerProvider and attached every configured
# provider's processor to it, general and llm alike. A TracerProvider's
# attached processors are global to that provider -- nothing scopes one
# to "only EventLogger.log() spans" -- so every general app-event span
# ALSO reached every llm-only provider (e.g. Phoenix silently receiving
# router-failed/ingest events). That version also required a deployer to
# write the SAME otlp/Logfire entry out twice (once under a general
# list, once under an llm list) to get both categories -- which is
# exactly the config duplication that produced a real double-export.
# The one-list-routed-by-KIND redesign (also 2026-09-03) fixes both at
# once: one settings entry, its class's own KIND decides which
# internal provider(s) it attaches to, and those internal providers are
# still genuinely separate TracerProvider objects. The tests below use
# REAL TracerProvider/SimpleSpanProcessor/InMemorySpanExporter objects
# (not the _FakeProcessor/plain-string mocks this file's other tests
# use), since a mocked processor can't reveal a cross-provider leak --
# only checking where a real span actually landed can.


def test_dual_kind_otlp_entry_receives_both_general_and_llm_spans_from_one_config(monkeypatch):
    """otlp's KIND is {"general","llm"} -- ONE telemetry.providers entry
    (no duplication, unlike the old two-list schema) must still result
    in a general-category span AND an llm-category span (standing in
    for what LangChain's real auto-instrumentation emits directly onto
    the llm provider) both reaching Logfire -- via two independently
    built processors from that one config line, not by sharing one."""
    built_exporters = []

    def fake_exporter(endpoint, headers):
        exp = InMemorySpanExporter()
        built_exporters.append(exp)
        return exp

    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter", fake_exporter)
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor",
                        lambda exporter: SimpleSpanProcessor(exporter))
    _set_telemetry_settings(monkeypatch, providers=[
        {"type": "otlp", "endpoint": "https://logfire-us.pydantic.dev/v1/traces",
         "headers": {"Authorization": "tok"}},
    ])

    general_provider, llm_provider = telemetry.setup_telemetry()

    # Two independent exporters got built from the ONE config entry --
    # one per internal provider, general activated before llm (see
    # setup_telemetry()'s own loop order).
    assert len(built_exporters) == 2
    general_exporter, llm_exporter = built_exporters

    with general_provider.get_tracer("test").start_as_current_span("general_event"):
        pass
    with llm_provider.get_tracer("test").start_as_current_span("llm_call"):
        pass

    assert [s.name for s in general_exporter.get_finished_spans()] == ["general_event"]
    assert [s.name for s in llm_exporter.get_finished_spans()] == ["llm_call"]


def test_get_event_logger_llm_only_provider_never_receives_a_general_event(monkeypatch, tmp_path):
    """phoenix is KIND={"llm"} only -- configuring it must not make it a
    general-event target, even though it now sits in the SAME
    telemetry.providers list as a general-kind entry (file). Replaces a
    previous version of this test that only asserted
    _file_providers == [] -- trivially true for ANY non-file provider,
    and passed even though the property it claimed to test was false
    (see code-reviewer's finding, 2026-09-03)."""
    import telemetry_providers.phoenix as phoenix_module
    phoenix_exporter = InMemorySpanExporter()
    monkeypatch.setattr(phoenix_module, "OTLPSpanExporter", lambda endpoint, headers: phoenix_exporter)
    monkeypatch.setattr(phoenix_module, "BatchSpanProcessor",
                        lambda exporter: SimpleSpanProcessor(exporter))
    path = tmp_path / "events.log"
    _set_telemetry_settings(monkeypatch, providers=[
        {"type": "file", "path": str(path)},
        {"type": "phoenix", "endpoint": "http://localhost:6006/v1/traces"},
    ])

    general_provider, llm_provider = telemetry.setup_telemetry()
    logger = telemetry.get_event_logger("argus.testmod")
    logger.log("something_happened", "a message")

    # General path still works (file got the event) -- proves isolation
    # didn't just accidentally break the fan-out mechanism entirely.
    assert "something_happened" in path.read_text(encoding="utf-8")
    # ...but phoenix (llm-only) got nothing from that same call, even
    # though a real llm TracerProvider exists and phoenix's own
    # processor is attached to it.
    assert phoenix_exporter.get_finished_spans() == ()

    # Directly confirm the reverse holds too, with a real span
    # (general_provider/llm_provider are genuinely different objects
    # with genuinely different processors, not an artifact of this
    # file's mocked ambient tracer -- see _reset_tracer_provider).
    assert general_provider is not None
    assert llm_provider is not None
    with general_provider.get_tracer("argus.testmod").start_as_current_span("something_happened"):
        pass
    assert phoenix_exporter.get_finished_spans() == ()


def test_setup_telemetry_names_the_service_before_building_the_provider(monkeypatch):
    """Found in production 2026-08-23 (back when Phoenix's register()
    drove the provider and never set service.name itself): every span
    from the container reached Logfire as `service.name =
    unknown_service`. The order matters as much as the value: the OTel
    SDK reads OTEL_SERVICE_NAME when it builds the default Resource and
    never looks again, so setting it after the provider is built would
    be too late."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr("telemetry_providers.otlp.OTLPSpanExporter", lambda endpoint, headers: "exporter")
    monkeypatch.setattr("telemetry_providers.otlp.BatchSpanProcessor", lambda exporter: _FakeProcessor())
    _set_telemetry_settings(monkeypatch, providers=[{"type": "otlp", "endpoint": "https://example.test/v1/traces"}])

    seen = {}
    real_tracer_provider = telemetry.TracerProvider

    def fake_provider(*args, **kwargs):
        seen["name_at_build_time"] = os.environ.get("OTEL_SERVICE_NAME")
        return real_tracer_provider(*args, **kwargs)

    monkeypatch.setattr(telemetry, "TracerProvider", fake_provider)

    telemetry.setup_telemetry()

    assert seen["name_at_build_time"] == telemetry.SERVICE_NAME


def test_setup_telemetry_lets_a_deployment_override_the_service_name(monkeypatch):
    """setdefault, not assignment: running two instances that need
    distinct names should not require a code change."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "argus-staging")
    _set_telemetry_settings(monkeypatch)

    telemetry.setup_telemetry()

    assert os.environ["OTEL_SERVICE_NAME"] == "argus-staging"


# --- agent.py's thin wrapper + the standalone endpoint-derivation helper ---


def test_agent_setup_telemetry_delegates_to_telemetry_module(monkeypatch):
    calls = []
    monkeypatch.setattr(agent.telemetry, "setup_telemetry", lambda: calls.append(1) or "the-provider")

    result = agent.setup_telemetry()

    assert result == "the-provider"
    assert calls == [1]


def test_logfire_endpoint_is_derived_from_the_token_region():
    """A deployer's tool now (see agent.py's own comment on this
    section) -- computed once, by hand, to put the resolved value into
    settings.yml/settings.oracle.yml/settings.int.yml. Not called by
    setup_telemetry() anymore, but the derivation logic itself is
    unchanged and still worth a regression test."""
    assert agent.logfire_traces_endpoint("pylf_v2_us_abc") == \
        "https://logfire-us.pydantic.dev/v1/traces"
    assert agent.logfire_traces_endpoint("pylf_v1_eu_abc") == \
        "https://logfire-eu.pydantic.dev/v1/traces"


def test_logfire_endpoint_refuses_an_unrecognised_region():
    """Guessing a default region here would export US traffic against an
    EU token, which fails as a 401 the OTLP HTTP exporter only logs --
    i.e. telemetry that silently goes nowhere."""
    with pytest.raises(ValueError, match="region"):
        agent.logfire_traces_endpoint("pylf_v2_zz_abc")
