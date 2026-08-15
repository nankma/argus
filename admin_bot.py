"""
Admin-only companion bot for approving/denying access requests to the
public info bot (bot.py) — see docs/plans/bot-features-plan.md item 1. Kept as a
separate bot/token deliberately: approval controls never appear on the same
surface a stranger could message, and every message/button tap here is
still re-checked against ADMIN_CHAT_ID regardless.

Shares subscribers.db with bot.py (see users_db.py) — both processes must
run against the same file, so co-locate them (same container/host, or a
shared volume once this is containerized — see docs/plans/deployment-plan.md).

Run:
    conda activate myfirstagent
    export ADMIN_BOT_TOKEN=<second-bot-token-from-botfather>
    export ADMIN_CHAT_ID=<your-telegram-numeric-user-id>
    export TELEGRAM_BOT_TOKEN=<the-info-bot-token>   # used to notify approved/denied users
    python admin_bot.py
"""

import os
from telegram import Bot, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import users_db


async def reject_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != context.bot_data["admin_chat_id"]:
        await update.message.reply_text("This bot is private.")
        return
    await update.message.reply_text("Use the Approve/Deny buttons on pending request messages.")


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["admin_chat_id"]:
        await query.answer("Not authorized.", show_alert=True)
        return

    action, chat_id_str = query.data.split(":")
    chat_id = int(chat_id_str)
    approved = action == "approve"
    users_db.decide(chat_id, approved)

    await query.answer()
    await query.edit_message_text(query.message.text + f"\n\n{'Approved' if approved else 'Denied'}.")

    notice = (
        "You've been approved — send a message to get started."
        if approved
        else "Your access request was denied."
    )
    await Bot(token=context.bot_data["info_bot_token"]).send_message(chat_id=chat_id, text=notice)


def main():
    users_db.init_db()
    app = Application.builder().token(os.environ["ADMIN_BOT_TOKEN"]).build()
    app.bot_data["admin_chat_id"] = int(os.environ["ADMIN_CHAT_ID"])
    app.bot_data["info_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    app.add_handler(CallbackQueryHandler(handle_decision))
    app.add_handler(MessageHandler(filters.ALL, reject_non_admin))

    print("Admin bot ready (polling). Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
