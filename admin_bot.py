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

import html
import os
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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


# --- category review (docs/plans/taxonomy-and-admin-plan.md A4) ------------
#
# The classifier records labels it reaches for that the taxonomy doesn't
# have (A3). Once one recurs often enough, the admin is asked here.
#
# Prefixed callback_data so this and the approve/deny handler can't catch
# each other's buttons -- they are registered with disjoint patterns.


def build_category_review(name: str, hits: int, examples: list[tuple[str, str]],
                          description: str | None, active: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """The message and buttons for one proposal.

    Shows the drafted description because it goes into the classifier
    prompt verbatim for every article afterwards -- the admin is approving
    that exact wording, not just a name, and a vague one silently degrades
    classification from then on."""
    # Everything interpolated here is escaped, because all three sources are
    # untrusted for HTML: `name` and `description` are model output, and
    # `title` is a real headline -- and ampersands in headlines ("AT&T",
    # "R&D", "Health & Wellness") are common, not an edge case. Telegram
    # rejects the whole send on an unescaped &/</>, and since nothing about
    # a stored proposal changes between cycles, such a proposal would fail
    # identically forever: never raised, and visible only in a log.
    # html.escape handles the ordering trap the telegram-message-formatting
    # skill warns about -- & must be escaped before < and >.
    esc = html.escape
    drafted = (f"<i>{esc(description)}</i>" if description
               else "<i>(no description drafted -- reject and add by hand)</i>")
    lines = [
        f"The classifier proposed <b>{esc(name)}</b> {hits} time(s) "
        f"and it isn't in the taxonomy.",
        "",
        "It would be described to the classifier as:",
        drafted,
        "",
        "Examples it was used for:",
    ]
    lines += [f"· {esc(title)}" for title, _ in examples] or ["· (none recorded)"]
    lines += ["", f"Active categories: {esc(', '.join(active))}"]

    buttons = [[
        InlineKeyboardButton("Activate", callback_data=f"cat:activate:{name}"),
        InlineKeyboardButton("Merge into…", callback_data=f"cat:merge:{name}"),
        InlineKeyboardButton("Reject", callback_data=f"cat:reject:{name}"),
    ]]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _merge_target_keyboard(name: str, active: list[str]) -> InlineKeyboardMarkup:
    """Three per row keeps the names readable on a phone. Only active
    categories are offered -- merging into a retired or already-merged one
    builds a chain whose only symptom is articles resolving to nothing."""
    rows, row = [], []
    for target in active:
        row.append(InlineKeyboardButton(target, callback_data=f"cat:into:{name}:{target}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def handle_category_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["admin_chat_id"]:
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split(":")
    action, name = parts[1], parts[2]
    by = f"admin:{query.from_user.id}"
    now = datetime.now(timezone.utc)
    active = [n for n, _ in users_db.get_active_categories()]

    if action == "merge":
        await query.answer()
        await query.edit_message_reply_markup(_merge_target_keyboard(name, active))
        return

    if action == "activate":
        # "Already decided" rather than an error: two admins, or one
        # double-tapping, is ordinary. The DB operations are guarded by
        # `AND status = 'proposed'`, so the second press changes nothing --
        # this just says so instead of implying it worked twice.
        decided = users_db.activate_category(name, by, now)
        outcome = "Activated." if decided else "Already decided -- no change."
    elif action == "reject":
        decided = users_db.reject_category(name, by, now)
        outcome = "Rejected." if decided else "Already decided -- no change."
    elif action == "into":
        target = parts[3]
        outcome = (f"Merged into {target}." if users_db.merge_category(name, target, by, now)
                   else f"Could not merge into {target} -- it isn't active.")
    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    await query.answer()
    # Buttons removed so a decision can't be re-tapped; the operations are
    # guarded server-side too, but a live button after a decision is a lie.
    # text_html, not text: the message was sent with parse_mode=HTML, and
    # `text` hands it back with the markup stripped. Re-sending that as
    # plain text would silently flatten the bold name and the italic
    # description the admin just approved -- the record of what they
    # agreed to would no longer look like what they were shown.
    # quote=False so apostrophes stay literal. Telegram's HTML parser only
    # understands &lt; &gt; &amp;, so escaping ' to &#x27; would show the
    # entity to the admin rather than an apostrophe.
    outcome = html.escape(outcome, quote=False)
    await query.edit_message_text(f"{query.message.text_html}\n\n{outcome}",
                                  parse_mode=ParseMode.HTML, reply_markup=None)


def main():
    users_db.init_db()
    app = Application.builder().token(os.environ["ADMIN_BOT_TOKEN"]).build()
    app.bot_data["admin_chat_id"] = int(os.environ["ADMIN_CHAT_ID"])
    app.bot_data["info_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    app.add_handler(CallbackQueryHandler(handle_category_decision, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(handle_decision, pattern=r"^(approve|deny):"))
    app.add_handler(MessageHandler(filters.ALL, reject_non_admin))

    print("Admin bot ready (polling). Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
