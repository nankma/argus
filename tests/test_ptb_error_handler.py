import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import ptb_error_handler


def _register_and_capture_error_callback(monkeypatch, scope="argus.testmod"):
    """register_error_handler builds its own fresh EventLogger via
    get_event_logger(scope) rather than reusing a caller's own
    module-level _events -- monkeypatch the factory itself (bound in
    ptb_error_handler's own namespace via `from telemetry import
    get_event_logger`, not wherever register_error_handler happens to be
    called from) to intercept it. Returns (fake_events, callback) -- the
    fake .log MagicMock and the actual async callback PTB would call via
    add_error_handler."""
    fake_events = MagicMock()
    monkeypatch.setattr(ptb_error_handler, "get_event_logger", lambda s: fake_events)
    fake_app = MagicMock()
    captured = {}
    fake_app.add_error_handler = lambda cb: captured.setdefault("cb", cb)

    ptb_error_handler.register_error_handler(fake_app, scope)

    return fake_events, captured["cb"]


def test_register_error_handler_logs_a_handler_sourced_error(monkeypatch):
    """Confirmed from PTB's own source, not assumed: process_error passes
    the real Update for a failed message/command/callback handler."""
    fake_events, callback = _register_and_capture_error_callback(monkeypatch)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
    context = MagicMock()
    context.error = RuntimeError("handler boom")
    context.job = None

    asyncio.run(callback(update, context))

    fake_events.log.assert_called_once()
    args, kwargs = fake_events.log.call_args
    assert args[0] == "unhandled_ptb_exception"
    assert args[1]["chat_id"] == 42
    assert kwargs["level"] == ptb_error_handler.Level.ERROR
    assert kwargs["exc"] is context.error


def test_register_error_handler_logs_a_job_sourced_error(monkeypatch):
    """Confirmed from PTB's own source: Job._run catches every exception
    from a run_repeating callback (bot.py's _push_job/_ingest_job here)
    and routes it through process_error(None, exc, job=self) -- update
    is None, context.job is the real Job. Also confirms Job._run does
    NOT re-raise or stop the job's future scheduling -- this handler
    only adds the missing durable log, it doesn't change that PTB
    already retries the next tick regardless."""
    fake_events, callback = _register_and_capture_error_callback(monkeypatch)
    context = MagicMock()
    context.error = RuntimeError("job boom")
    context.job = SimpleNamespace(name="_push_job")

    asyncio.run(callback(None, context))

    args, kwargs = fake_events.log.call_args
    assert args[1]["job_name"] == "_push_job"
    assert "chat_id" not in args[1]
    assert kwargs["level"] == ptb_error_handler.Level.ERROR
    assert kwargs["exc"] is context.error


def test_register_error_handler_handles_a_job_error_with_no_chat(monkeypatch):
    """update is None AND effective_chat absent -- neither branch should
    raise (e.g. AttributeError on None.effective_chat)."""
    fake_events, callback = _register_and_capture_error_callback(monkeypatch)
    context = MagicMock()
    context.error = RuntimeError("boom")
    context.job = None

    asyncio.run(callback(None, context))  # must not raise

    args, kwargs = fake_events.log.call_args
    assert "chat_id" not in args[1]
    assert "job_name" not in args[1]
