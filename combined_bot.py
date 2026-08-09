"""
Runs bot.py's and admin_bot.py's Telegram Applications together in one
process/event loop, instead of as two separate processes/containers.

Why: each Application independently loads LangChain, python-telegram-bot,
etc. into memory -- running two OS processes duplicates that. This matters
on a small VM (e.g. Oracle's Always Free VM.Standard.E2.1.Micro, 1GB RAM)
where that duplication is a real constraint. Merging into one process
halves it, without changing the two-bot-token security design (see
docs/bot-features-plan.md item 1) -- @mnkInfo_bot and @mnkInfoAdmin_bot
stay two separate Telegram identities, just served by one Python process.

bot.py and admin_bot.py keep their own standalone main()s too -- this file
only adds a combined option, reusing their handler functions and bot_data
wiring unchanged.

Also runs telemetry_monitor.py's Phoenix health-check loop alongside the
two bots (only when PHOENIX_ENABLED is set) -- alerts the admin via
admin_bot.py's token if Phoenix's OTLP endpoint (PHOENIX_ENDPOINT, from
agent.py) goes unreachable or recovers, since a telemetry outage is
otherwise silent (OpenTelemetry doesn't raise export failures into
application code by design).

Run:
    conda activate myfirstagent
    export DEEPSEEK_API_KEY=<your-deepseek-key>
    export TELEGRAM_BOT_TOKEN=<info-bot-token>
    export ADMIN_BOT_TOKEN=<admin-bot-token>
    export ADMIN_CHAT_ID=<your-telegram-numeric-user-id>
    export PHOENIX_ENABLED=true          # optional
    export PHOENIX_ENDPOINT=http://<phoenix-host>:4317   # optional
    python combined_bot.py
"""

import asyncio
import contextlib
import os
import signal
from urllib.parse import urlparse
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from langchain_deepseek import ChatDeepSeek
import agent as agent_module
from agent import MODEL, build_agent, run_agent, setup_telemetry
import bot as info_bot
import admin_bot
import telemetry_monitor
import users_db


def build_info_app(agent, admin_chat_id: int, admin_bot_token: str, guard_model=None) -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["agent"] = agent
    # Reuses the same model instance for guardrail classification calls
    # (guardrails.py) -- no separate model/infra, see docs/guardrails-plan.md.
    app.bot_data["guard_model"] = guard_model
    app.bot_data["admin_chat_id"] = admin_chat_id
    app.bot_data["admin_bot_token"] = admin_bot_token
    app.add_handler(CommandHandler("start", info_bot.handle_start_command))
    app.add_handler(CommandHandler("interests", info_bot.handle_interests_command))
    app.add_handler(CommandHandler("language", info_bot.handle_language_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, info_bot.handle_message))
    info_bot.register_push_job(app)
    return app


def build_admin_app(admin_chat_id: int, info_bot_token: str) -> Application:
    app = Application.builder().token(os.environ["ADMIN_BOT_TOKEN"]).build()
    app.bot_data["admin_chat_id"] = admin_chat_id
    app.bot_data["info_bot_token"] = info_bot_token
    app.add_handler(CallbackQueryHandler(admin_bot.handle_decision))
    app.add_handler(MessageHandler(filters.ALL, admin_bot.reject_non_admin))
    return app


def _start_telemetry_monitor(admin_bot_token: str, admin_chat_id: int) -> asyncio.Task | None:
    """Starts the Phoenix health-check/alert loop (telemetry_monitor.py) only
    when telemetry is actually enabled -- pointless (and noisy, since
    PHOENIX_ENDPOINT defaults to localhost) otherwise, e.g. in local dev
    without PHOENIX_ENABLED set."""
    if not os.environ.get("PHOENIX_ENABLED"):
        return None
    parsed = urlparse(agent_module.PHOENIX_ENDPOINT)
    if not parsed.hostname:
        return None
    port = parsed.port or 4317
    return asyncio.create_task(
        telemetry_monitor.monitor_telemetry_health(admin_bot_token, admin_chat_id, parsed.hostname, port)
    )


async def run_both(
    info_app: Application, admin_app: Application, admin_bot_token: str, admin_chat_id: int
) -> None:
    async with info_app, admin_app:
        await info_app.start()
        await info_app.updater.start_polling()
        await admin_app.start()
        await admin_app.updater.start_polling()

        monitor_task = _start_telemetry_monitor(admin_bot_token, admin_chat_id)

        print("Both bots ready (polling). Ctrl+C to stop.")
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows: no add_signal_handler, Ctrl+C raises KeyboardInterrupt instead

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if monitor_task:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task
            await admin_app.updater.stop()
            await admin_app.stop()
            await info_app.updater.stop()
            await info_app.stop()


def main():
    setup_telemetry()
    users_db.init_db()
    admin_chat_id = int(os.environ["ADMIN_CHAT_ID"])
    admin_bot_token = os.environ["ADMIN_BOT_TOKEN"]
    info_bot_token = os.environ["TELEGRAM_BOT_TOKEN"]

    model = ChatDeepSeek(model=MODEL)
    agent = build_agent(model)

    info_app = build_info_app(agent, admin_chat_id, admin_bot_token, guard_model=model)
    admin_app = build_admin_app(admin_chat_id, info_bot_token)

    asyncio.run(run_both(info_app, admin_app, admin_bot_token, admin_chat_id))


if __name__ == "__main__":
    main()
