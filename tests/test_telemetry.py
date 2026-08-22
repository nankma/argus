import agent


def test_setup_telemetry_noop_when_disabled(monkeypatch):
    """No PHOENIX_ENABLED -> register() must never be called. This is what
    keeps pytest runs from trying to reach a Phoenix collector that isn't
    there (locally without Docker running, or in CI)."""
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(agent, "register", lambda **kwargs: calls.append(kwargs))

    agent.setup_telemetry()

    assert calls == []


def test_setup_telemetry_registers_when_enabled(monkeypatch):
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    calls = []
    monkeypatch.setattr(agent, "register", lambda **kwargs: calls.append(kwargs))

    agent.setup_telemetry()

    assert len(calls) == 1
    assert calls[0]["project_name"] == "myfirstagent"
    assert calls[0]["endpoint"] == agent.PHOENIX_ENDPOINT
    assert calls[0]["auto_instrument"] is True


# --- Logfire ---------------------------------------------------------------
#
# See docs/plans/observability-platform-plan.md. Phoenix and Logfire are
# deliberately able to run together: retiring the Phoenix VM is the last
# step of that migration, and dual-writing is what makes comparing them
# possible first.


import pytest


def test_setup_telemetry_ignores_a_logfire_key_without_the_enable_flag(monkeypatch):
    """The load-bearing one. LOGFIRE_API_KEY is present in the development
    environment, so if the exporter keyed off the credential alone, every
    local script and every pytest run would start shipping spans to a real
    hosted service. The enable flag is what keeps that from happening --
    same contract as PHOENIX_ENABLED."""
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    monkeypatch.delenv("LOGFIRE_ENABLED", raising=False)
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)
    made = []
    monkeypatch.setattr(agent, "_logfire_processor", lambda token: made.append(token))

    assert agent.setup_telemetry() is None
    assert made == []


def test_setup_telemetry_raises_when_enabled_without_a_key(monkeypatch):
    """Loud rather than silent: a bot that looks instrumented and isn't is
    the exact failure this plan exists to prevent."""
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    monkeypatch.delenv("LOGFIRE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LOGFIRE_API_KEY"):
        agent.setup_telemetry()


def test_setup_telemetry_keeps_phoenix_alive_when_logfire_is_added(monkeypatch):
    """The 2026-08-21 regression, pinned.

    Enabling Logfire alongside Phoenix silently stopped Phoenix receiving
    anything. Phoenix's TracerProvider is not a plain OTel one: its
    add_span_processor defaults to replace_default_processor=True, which
    shuts down the exporter register() just installed. Spans kept being
    produced and simply went somewhere else, so nothing looked broken.

    The previous version of this test could not have caught it. Its fake
    provider appended, unconditionally -- convenient, and wrong about the
    thing under test. This one models the real contract: a default
    processor that is discarded unless the caller opts out."""
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)

    class PhoenixLikeProvider:
        """Mirrors phoenix.otel.TracerProvider.add_span_processor."""
        def __init__(self):
            self.processors = ["phoenix-default"]
            self.has_default = True

        def add_span_processor(self, processor, replace_default_processor=True):
            if self.has_default and replace_default_processor:
                self.processors = []          # Phoenix drops its own exporter
                self.has_default = False
            self.processors.append(processor)

    provider = PhoenixLikeProvider()
    monkeypatch.setattr(agent, "register", lambda **kwargs: provider)
    monkeypatch.setattr(agent, "_logfire_processor", lambda token: "logfire")

    agent.setup_telemetry()

    # Both, not either. Losing the first line is the regression.
    assert "phoenix-default" in provider.processors
    assert "logfire" in provider.processors


def test_logfire_endpoint_is_derived_from_the_token_region():
    """The region lives in the token prefix, so the endpoint cannot
    disagree with the credential that authenticates against it."""
    assert agent.logfire_traces_endpoint("pylf_v2_us_abc") == \
        "https://logfire-us.pydantic.dev/v1/traces"
    assert agent.logfire_traces_endpoint("pylf_v1_eu_abc") == \
        "https://logfire-eu.pydantic.dev/v1/traces"


def test_logfire_endpoint_refuses_an_unrecognised_region(monkeypatch):
    """Guessing a default region here would export US traffic against an EU
    token, which fails as a 401 the OTLP HTTP exporter only logs -- i.e.
    telemetry that silently goes nowhere."""
    with pytest.raises(ValueError, match="region"):
        agent.logfire_traces_endpoint("pylf_v2_zz_abc")


def test_logfire_processor_targets_the_regional_endpoint_with_the_token(monkeypatch):
    """Exercises the real body of _logfire_processor, which every other
    test stubs out. Catches an import-path typo -- the failure mode there
    is an exception at startup, but only on a deploy that actually enables
    Logfire, which is the worst place to find out."""
    import agent as agent_module

    captured = {}

    class FakeExporter:
        def __init__(self, endpoint, headers):
            captured["endpoint"] = endpoint
            captured["headers"] = headers

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        FakeExporter)
    monkeypatch.setattr(
        "opentelemetry.sdk.trace.export.BatchSpanProcessor",
        lambda exporter: ("processor", exporter))

    token = "pylf_v2_eu_" + "x" * 40
    kind, exporter = agent_module._logfire_processor(token)

    assert kind == "processor"
    assert captured["endpoint"] == "https://logfire-eu.pydantic.dev/v1/traces"
    assert captured["headers"] == {"Authorization": token}


def test_setup_telemetry_without_phoenix_builds_and_installs_its_own_provider(monkeypatch):
    """The Logfire-only path: no Phoenix means nothing has built a provider
    or instrumented LangChain yet, so setup_telemetry must do both itself.
    Asserts the provider is installed GLOBALLY -- a provider that is built
    but never registered collects nothing, and looks identical to one that
    works until you go looking for spans."""
    import agent as agent_module

    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)

    installed, instrumented, added = [], [], []

    class FakeProvider:
        def __init__(self, resource=None):
            self.resource = resource
        def add_span_processor(self, p):
            added.append(p)

    monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", FakeProvider)
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", installed.append)
    monkeypatch.setattr(
        "openinference.instrumentation.langchain.LangChainInstrumentor",
        lambda: type("I", (), {"instrument": lambda self, tracer_provider: instrumented.append(tracer_provider)})())
    monkeypatch.setattr(agent_module, "_logfire_processor", lambda token: "logfire-processor")

    provider = agent_module.setup_telemetry()

    assert installed == [provider]
    assert instrumented == [provider]
    assert added == ["logfire-processor"]
