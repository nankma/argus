"""
Telegram bot entry point for the agent — the headless alternative to
agent.py's CLI REPL. Polling mode: no public endpoint or TLS needed, and
the same shape works locally and in a long-running Kubernetes Deployment
later. See docs/deployment-plan.md.

Reuses build_agent/run_agent/setup_telemetry from agent.py unchanged — this
file only adds the Telegram-specific plumbing (per-chat history, handler
registration, the polling loop).

Run:
    conda activate myfirstagent
    export DEEPSEEK_API_KEY=<your-deepseek-key>
    export TELEGRAM_BOT_TOKEN=<your-bot-token>
    python bot.py
"""

import asyncio
import os
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from langchain_deepseek import ChatDeepSeek
from agent import MODEL, build_agent, run_agent, setup_telemetry

TELEGRAM_MESSAGE_LIMIT = 4096

# Per-chat conversation history. In-memory only — lost on restart, same as
# the CLI's messages list. Not persisted; fine for now, revisit if needed.
chat_histories: dict[int, list] = {}


def split_for_telegram(text: str) -> list[str]:
    """Telegram rejects messages over 4096 characters. Split on that
    boundary, preferring to break at the last newline within a chunk."""
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return [text]
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MESSAGE_LIMIT:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = TELEGRAM_MESSAGE_LIMIT
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    messages = chat_histories.setdefault(chat_id, [])
    messages.append({"role": "user", "content": update.message.text})

    agent = context.bot_data["agent"]
    try:
        messages = await asyncio.to_thread(run_agent, agent, messages)
    except Exception as exc:
        await update.message.reply_text(f"Something went wrong: {exc}")
        return
    chat_histories[chat_id] = messages

    for chunk in split_for_telegram(messages[-1].content):
        await update.message.reply_text(chunk)


def main():
    setup_telemetry()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    model = ChatDeepSeek(model=MODEL)
    agent = build_agent(model)

    app = Application.builder().token(token).build()
    app.bot_data["agent"] = agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Telegram bot ready (polling). Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
