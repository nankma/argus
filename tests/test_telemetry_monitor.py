import asyncio
from unittest.mock import AsyncMock, MagicMock

import telemetry_monitor


def _patch_bot(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(telemetry_monitor, "Bot", MagicMock(return_value=MagicMock(send_message=sent)))
    return sent


def test_check_and_alert_no_change_when_still_healthy(monkeypatch):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(telemetry_monitor, "check_phoenix_reachable", AsyncMock(return_value=True))
    result = asyncio.run(
        telemetry_monitor.check_and_alert("admin-token", 999, "10.0.0.234", 4317, was_healthy=True)
    )
    assert result is True
    sent.assert_not_called()


def test_check_and_alert_sends_down_alert_on_transition(monkeypatch):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(telemetry_monitor, "check_phoenix_reachable", AsyncMock(return_value=False))
    result = asyncio.run(
        telemetry_monitor.check_and_alert("admin-token", 999, "10.0.0.234", 4317, was_healthy=True)
    )
    assert result is False
    sent.assert_called_once()
    assert "unreachable" in sent.call_args.kwargs["text"]


def test_check_and_alert_no_repeat_alert_while_still_down(monkeypatch):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(telemetry_monitor, "check_phoenix_reachable", AsyncMock(return_value=False))
    result = asyncio.run(
        telemetry_monitor.check_and_alert("admin-token", 999, "10.0.0.234", 4317, was_healthy=False)
    )
    assert result is False
    sent.assert_not_called()


def test_check_and_alert_sends_recovery_alert_on_transition(monkeypatch):
    sent = _patch_bot(monkeypatch)
    monkeypatch.setattr(telemetry_monitor, "check_phoenix_reachable", AsyncMock(return_value=True))
    result = asyncio.run(
        telemetry_monitor.check_and_alert("admin-token", 999, "10.0.0.234", 4317, was_healthy=False)
    )
    assert result is True
    sent.assert_called_once()
    assert "reachable again" in sent.call_args.kwargs["text"]


def test_check_phoenix_reachable_false_on_connection_error(monkeypatch):
    async def fake_open_connection(host, port):
        raise OSError("no route to host")

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    result = asyncio.run(telemetry_monitor.check_phoenix_reachable("10.0.0.234", 4317, timeout=1))
    assert result is False
