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
