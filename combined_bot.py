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

Run:
    conda activate myfirstagent
    export DEEPSEEK_API_KEY=<your-deepseek-key>
    export TELEGRAM_BOT_TOKEN=<info-bot-token>
    export ADMIN_BOT_TOKEN=<admin-bot-token>
    export ADMIN_CHAT_ID=<your-telegram-numeric-user-id>
    python combined_bot.py
"""

import asyncio
import os
import signal
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from langchain_deepseek import ChatDeepSeek
from agent import MODEL, build_agent, run_agent, setup_telemetry
import bot as info_bot
import admin_bot
import users_db


def build_info_app(agent, admin_chat_id: int, admin_bot_token: str) -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["agent"] = agent
    app.bot_data["admin_chat_id"] = admin_chat_id
    app.bot_data["admin_bot_token"] = admin_bot_token
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, info_bot.handle_message))
    return app


def build_admin_app(admin_chat_id: int, info_bot_token: str) -> Application:
    app = Application.builder().token(os.environ["ADMIN_BOT_TOKEN"]).build()
    app.bot_data["admin_chat_id"] = admin_chat_id
    app.bot_data["info_bot_token"] = info_bot_token
    app.add_handler(CallbackQueryHandler(admin_bot.handle_decision))
    app.add_handler(MessageHandler(filters.ALL, admin_bot.reject_non_admin))
    return app


async def run_both(info_app: Application, admin_app: Application) -> None:
    async with info_app, admin_app:
        await info_app.start()
        await info_app.updater.start_polling()
        await admin_app.start()
        await admin_app.updater.start_polling()

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

    info_app = build_info_app(agent, admin_chat_id, admin_bot_token)
    admin_app = build_admin_app(admin_chat_id, info_bot_token)

    asyncio.run(run_both(info_app, admin_app))


if __name__ == "__main__":
    main()
