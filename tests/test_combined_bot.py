import asyncio

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
import agent as agent_module
import combined_bot
import users_db

FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def test_build_info_app_wires_bot_data(monkeypatch, isolated_subscribers_db):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    app = combined_bot.build_info_app(
        agent="fake-agent", admin_chat_id=999, admin_bot_token="admin-token", guard_model="fake-guard-model"
    )

    assert app.bot_data["agent"] == "fake-agent"
    assert app.bot_data["admin_chat_id"] == 999
    assert app.bot_data["admin_bot_token"] == "admin-token"
    assert app.bot_data["guard_model"] == "fake-guard-model"
    # build_info_app grants the admin restricted-source access at startup
    # (news_sources.RESTRICTED_SOURCES: NewsAPI, Perigon) -- confirm it
    # actually happened, not just that it didn't crash.
    assert users_db.get_restricted_sources_enabled(999) is True
    handlers = [h for group in app.handlers.values() for h in group]
    assert any(isinstance(h, MessageHandler) for h in handlers)
    # the periodic-push scheduler (docs/bot-features-plan.md item 5) and
    # the news-cache ingestion job (docs/local-news-cache-plan.md) must
    # both be wired up in the combined process too, not just standalone
    # bot.py -- asserting the callback names, not just a count, so a
    # future job silently failing to register (or one accidentally
    # registered twice) fails loudly here.
    job_callback_names = {job.callback.__name__ for job in app.job_queue.jobs()}
    assert job_callback_names == {"_push_job", "_ingest_job", "_health_check_job"}
    # Real incident, 2026-08-09: /start went unhandled (only checking "any
    # MessageHandler" wouldn't have caught this -- it needs its own
    # CommandHandler, since the plain-text MessageHandler excludes all
    # commands). Assert the exact command set so a future missing command
    # fails loudly here instead of silently in production.
    commands = {next(iter(h.commands)) for h in handlers if isinstance(h, CommandHandler)}
    assert commands == {"start", "interests", "language"}


def test_build_admin_app_wires_bot_data(monkeypatch):
    monkeypatch.setenv("ADMIN_BOT_TOKEN", FAKE_TOKEN)
    app = combined_bot.build_admin_app(admin_chat_id=999, info_bot_token="info-token")

    assert app.bot_data["admin_chat_id"] == 999
    assert app.bot_data["info_bot_token"] == "info-token"
    handlers = [h for group in app.handlers.values() for h in group]
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
    assert any(isinstance(h, MessageHandler) for h in handlers)


async def _run_and_cancel(coro_factory):
    task = coro_factory()
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return task


def test_start_telemetry_monitor_skipped_when_phoenix_disabled(monkeypatch):
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    task = asyncio.run(
        _run_and_cancel(lambda: combined_bot._start_telemetry_monitor("admin-token", 999))
    )
    assert task is None


def test_start_telemetry_monitor_skipped_when_endpoint_has_no_host(monkeypatch):
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    monkeypatch.setattr(agent_module, "PHOENIX_ENDPOINT", "not-a-valid-url")
    task = asyncio.run(
        _run_and_cancel(lambda: combined_bot._start_telemetry_monitor("admin-token", 999))
    )
    assert task is None


def test_start_telemetry_monitor_starts_when_enabled(monkeypatch):
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    monkeypatch.setattr(agent_module, "PHOENIX_ENDPOINT", "http://10.0.0.234:4317")
    task = asyncio.run(
        _run_and_cancel(lambda: combined_bot._start_telemetry_monitor("admin-token", 999))
    )
    assert isinstance(task, asyncio.Task)
