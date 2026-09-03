import os

import pytest

import agent
from logfire_logger import LogfireLogger


def test_setup_telemetry_noop_when_disabled(monkeypatch):
    """No LOGFIRE_ENABLED -> LogfireLogger.setup must never be called.
    This is what keeps pytest runs from trying to reach a real Logfire
    endpoint (locally or in CI)."""
    monkeypatch.delenv("LOGFIRE_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(LogfireLogger, "setup", classmethod(lambda cls, **kwargs: calls.append(kwargs)))

    result = agent.setup_telemetry()

    assert calls == []
    assert result is None


def test_setup_telemetry_ignores_a_logfire_key_without_the_enable_flag(monkeypatch):
    """The load-bearing one. LOGFIRE_API_KEY is present in the development
    environment, so if the exporter keyed off the credential alone, every
    local script and every pytest run would start shipping spans to a real
    hosted service. The enable flag is what keeps that from happening."""
    monkeypatch.delenv("LOGFIRE_ENABLED", raising=False)
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)
    calls = []
    monkeypatch.setattr(LogfireLogger, "setup", classmethod(lambda cls, **kwargs: calls.append(kwargs)))

    assert agent.setup_telemetry() is None
    assert calls == []


def test_setup_telemetry_treats_the_literal_string_false_as_enabled(monkeypatch):
    """Historical-compatibility edge case, preserved deliberately across
    the delivery/telemetry Settings migration: LOGFIRE_ENABLED was always
    a bare presence check (`if not os.environ.get(...)`), so ANY
    non-empty string enabled it, including the literal text "false" --
    not real boolean parsing. resolved_optional() carries that same
    semantics forward (it returns the raw resolved string, not a parsed
    bool), so this must still come out enabled. Real deployments have
    only ever set this to "true" (see local-infra/infrastructure.yaml),
    so this edge case has never mattered in practice -- but a future
    refactor that "fixes" this into real boolean parsing would silently
    change behavior with nothing here to catch it."""
    monkeypatch.setenv("LOGFIRE_ENABLED", "false")
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)
    calls = []
    monkeypatch.setattr(LogfireLogger, "setup", classmethod(lambda cls, **kwargs: calls.append(kwargs)))

    agent.setup_telemetry()

    assert len(calls) == 1


def test_setup_telemetry_raises_when_enabled_without_a_key(monkeypatch):
    """Loud rather than silent: a bot that looks instrumented and isn't is
    the exact failure this plan exists to prevent."""
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    monkeypatch.delenv("LOGFIRE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LOGFIRE_API_KEY"):
        agent.setup_telemetry()


def test_setup_telemetry_delegates_to_logfire_logger_with_the_right_args(monkeypatch):
    """setup_telemetry's own job, once Logfire is enabled with a key, is
    just to resolve the endpoint from the token's region and hand
    everything to LogfireLogger.setup -- building the actual provider is
    LogfireLogger's responsibility now (see tests/test_logfire_logger.py
    for that), not agent.py's."""
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    token = "pylf_v2_us_" + "x" * 40
    monkeypatch.setenv("LOGFIRE_API_KEY", token)

    calls = []

    def fake_setup(cls, **kwargs):
        calls.append(kwargs)
        return "the-provider"

    monkeypatch.setattr(LogfireLogger, "setup", classmethod(fake_setup))

    result = agent.setup_telemetry()

    assert result == "the-provider"
    assert len(calls) == 1
    assert calls[0]["service_name"] == agent.SERVICE_NAME
    assert calls[0]["token"] == token
    assert calls[0]["endpoint"] == "https://logfire-us.pydantic.dev/v1/traces"
    assert calls[0]["instrument_langchain"] is True


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


def test_setup_telemetry_names_the_service_before_calling_logfire_logger(monkeypatch):
    """Found in production 2026-08-23 (back when Phoenix's register() drove
    the provider and never set service.name itself): every span from the
    container reached Logfire as `service.name = unknown_service`.
    Production traffic was invisible under the name every query filters
    on, and the dead man's switch had been watching an empty set.

    The order matters as much as the value: the OTel SDK reads
    OTEL_SERVICE_NAME when it builds the default Resource and never looks
    again, so setting it after the provider is built would be too late.
    Phoenix is gone now, but the discipline this test pins -- set the env
    var before anything can build a Resource -- is still real (see
    LogfireLogger.setup's own docstring on why it deliberately doesn't
    set this itself)."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setenv("LOGFIRE_ENABLED", "true")
    monkeypatch.setenv("LOGFIRE_API_KEY", "pylf_v2_us_" + "x" * 40)

    seen = {}

    def fake_setup(cls, **kwargs):
        seen["name_at_setup_time"] = os.environ.get("OTEL_SERVICE_NAME")
        return object()

    monkeypatch.setattr(LogfireLogger, "setup", classmethod(fake_setup))

    agent.setup_telemetry()

    assert seen["name_at_setup_time"] == agent.SERVICE_NAME


def test_setup_telemetry_lets_a_deployment_override_the_service_name(monkeypatch):
    """setdefault, not assignment: running two instances that need distinct
    names should not require a code change."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "argus-staging")
    monkeypatch.delenv("LOGFIRE_ENABLED", raising=False)

    agent.setup_telemetry()

    assert os.environ["OTEL_SERVICE_NAME"] == "argus-staging"
