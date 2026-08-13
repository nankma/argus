"""
Telegram bot entry point for the agent — the headless alternative to
agent.py's CLI REPL. Polling mode: no public endpoint or TLS needed, and
the same shape works locally and in a long-running Kubernetes Deployment
later. See docs/deployment-plan.md.

Reuses build_agent/run_agent/setup_telemetry from agent.py unchanged — this
file only adds the Telegram-specific plumbing (per-chat history, handler
registration, the polling loop).

Access is gated by an approval workflow (see docs/bot-features-plan.md item
1): ADMIN_CHAT_ID is always allowed; anyone else's first message registers
a pending request in the shared subscribers DB (users_db.py) and notifies
the admin via admin_bot.py — a separate bot/token — with Approve/Deny
buttons attached to the message.

Run:
    conda activate myfirstagent
    export DEEPSEEK_API_KEY=<your-deepseek-key>
    export TELEGRAM_BOT_TOKEN=<your-bot-token>
    export ADMIN_CHAT_ID=<your-telegram-numeric-user-id>
    export ADMIN_BOT_TOKEN=<second-bot-token-for-admin_bot.py>
    python bot.py
"""

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from langchain_deepseek import ChatDeepSeek
from agent import MODEL, build_agent, run_agent, setup_telemetry
import guardrails
import news_ingest
import news_push
import test_api
import users_db

TELEGRAM_MESSAGE_LIMIT = 4096

# How often the periodic-push scheduler checks who's due (see
# register_push_job below) -- independent of any individual subscriber's
# push_interval_hours (users_db.MIN_PUSH_INTERVAL_HOURS floors that at 1h),
# just fine-grained enough that a due subscriber isn't kept waiting long
# past their actual interval.
PUSH_TICK_SECONDS = 900

# Same tick shape as PUSH_TICK_SECONDS -- check frequently, let each
# source's own interval (news_ingest._SOURCE_INTERVAL_HOURS, 4h default,
# longer for budget-capped sources) decide whether this tick actually does
# anything. See docs/local-news-cache-plan.md.
INGEST_TICK_SECONDS = 900

# Per-chat conversation history. In-memory only — lost on restart, same as
# the CLI's messages list. Not persisted; fine for now, revisit if needed.
# Each value is (messages, timestamps) -- a parallel list of when each
# message was added, needed for the age-based trim below since LangChain
# message objects/dicts carry no wall-clock timestamp of their own.
chat_histories: dict[int, tuple[list, list[datetime]]] = {}

# Real question, 2026-08-09: does the layered system prompt (agent.py's
# _compose_prompt) or this conversation history risk a context-window
# overflow over a long-lived process? The prompt layers don't -- they're
# rebuilt fresh on every call, never accumulated. This history DOES:
# run_agent resends the full accumulated list on every turn, with no
# trimming anywhere, so an active chat on a long-uptime process would grow
# without bound -- rising cost every turn, and eventually exceeding
# DeepSeek's context window outright rather than degrading gracefully.
# Trimmed aggressively on purpose: this bot's replies are effectively
# stateless per-topic (a news summary from an hour ago has little bearing
# on a new question), so there's little value in keeping much around.
MAX_HISTORY_AGE = timedelta(hours=1)
MAX_HISTORY_MESSAGES = 20


def _trim_history(messages: list, timestamps: list[datetime], now: datetime) -> tuple[list, list[datetime]]:
    """Drops messages older than MAX_HISTORY_AGE, then caps to the most
    recent MAX_HISTORY_MESSAGES -- both constraints apply together, so
    the result is always within both."""
    kept = [(m, t) for m, t in zip(messages, timestamps) if now - t <= MAX_HISTORY_AGE]
    kept = kept[-MAX_HISTORY_MESSAGES:]
    if not kept:
        return [], []
    trimmed_messages, trimmed_timestamps = zip(*kept)
    return list(trimmed_messages), list(trimmed_timestamps)


def _get_trimmed_history(chat_id: int) -> tuple[list, list[datetime]]:
    """Reads this chat's history and trims it, storing the trimmed result
    back immediately -- stale entries get dropped every turn regardless
    of whether *this* turn's exchange ends up persisted (see
    handle_message), not just when a new message happens to push past
    the limit."""
    messages, timestamps = chat_histories.get(chat_id, ([], []))
    messages, timestamps = _trim_history(messages, timestamps, datetime.now(timezone.utc))
    chat_histories[chat_id] = (messages, timestamps)
    return messages, timestamps


_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>")
_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _normalize_markdown_bold(text: str) -> str:
    """Safety net for when the model ignores agent.py's "no Markdown,
    HTML only" instruction and emits **bold** anyway (a real incident,
    2026-08-08 — see the smoke-test table in the
    build-locally-deploy-remotely skill). Prompt compliance alone isn't
    reliable enough (same lesson as guardrails.py's classifiers), so this
    converts stray **bold** into real Telegram HTML instead of relying
    purely on the model following the rule -- turns a literal-asterisk
    bug into a no-op when the model behaves, and into a fix when it
    doesn't."""
    return _MARKDOWN_BOLD_RE.sub(r"<b>\1</b>", text)


_TREND_REPORT_MARKER = "📰"


def _strip_report_preamble(text: str) -> str:
    """Safety net for when the model narrates its process before the
    actual trend report despite agent.TREND_REPORT_STRUCTURE explicitly
    forbidding it ("no preamble... start directly with the 📰 title
    line"). Real incident, 2026-08-09: verified live that this prompt-only
    instruction alone did not reliably stop the model writing things like
    "Let me compile these into a report" before the real content -- same
    lesson as _normalize_markdown_bold. If the report marker appears
    anywhere but the very start, strips everything before it; a no-op for
    replies that never use the marker (confirmations, etc.)."""
    idx = text.find(_TREND_REPORT_MARKER)
    if idx > 0:
        return text[idx:]
    return text


def _is_html_balanced(text: str) -> bool:
    """True if `text` has no unclosed HTML tag — open/close tag counts
    match. Doesn't validate proper nesting, just depth — good enough given
    the small, LLM-produced tag vocabulary this bot actually asks for
    (b, i, a), not arbitrary/adversarial HTML."""
    depth = 0
    for m in _HTML_TAG_RE.finditer(text):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth < 0:
            return False
    return depth == 0


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def split_for_telegram(text: str) -> list[str]:
    """Telegram rejects messages over 4096 characters. Split on that
    boundary, preferring to break at a newline, and never at a point that
    would leave an HTML tag open in one chunk with its closing tag in the
    next — Telegram would reject that chunk as unparseable entities."""
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
        while split_at > 0 and not _is_html_balanced(text[:split_at]):
            prev_newline = text.rfind("\n", 0, split_at)
            split_at = prev_newline if prev_newline > 0 else split_at - 1
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def notify_admin(admin_bot_token: str, admin_chat_id: int, chat_id: int, user) -> None:
    """Ping the admin with Approve/Deny buttons for a new access request.
    Sent via the admin bot's own token (not this bot's) so the resulting
    button tap's callback_query lands on admin_bot.py's update stream,
    not this process's."""
    label = f"@{user.username}" if user.username else (user.first_name or str(chat_id))
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"approve:{chat_id}"),
                InlineKeyboardButton("Deny", callback_data=f"deny:{chat_id}"),
            ]
        ]
    )
    await Bot(token=admin_bot_token).send_message(
        chat_id=admin_chat_id,
        text=f"New access request from {label} (chat_id={chat_id}).",
        reply_markup=keyboard,
    )


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Gate access per docs/bot-features-plan.md item 1. Returns True if the
    sender may proceed; otherwise replies explaining why and returns
    False."""
    chat_id = update.effective_chat.id
    if chat_id == context.bot_data["admin_chat_id"]:
        return True

    status = users_db.get_status(chat_id)
    if status == users_db.APPROVED:
        return True
    if status == users_db.PENDING:
        await update.message.reply_text("Your access request is still pending approval.")
        return False
    if status == users_db.DENIED:
        await update.message.reply_text("Access denied.")
        return False

    user = update.effective_user
    users_db.request_access(chat_id, user.username, user.first_name)
    await update.message.reply_text(
        "This bot is private. Your access request was sent to the owner — "
        "you'll be notified once it's reviewed."
    )
    await notify_admin(
        context.bot_data["admin_bot_token"], context.bot_data["admin_chat_id"], chat_id, user
    )
    return False


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start -- Telegram's standard bot-initiation command, sent
    automatically by the client's own "START" button on first contact
    with any bot. Real incident, 2026-08-09: a genuinely new user's very
    first interaction is /start, not free text -- and the plain-text
    MessageHandler explicitly excludes all commands (~filters.COMMAND),
    so without a dedicated handler here, that first message went
    completely unhandled: no reply, no pending-request DB row, no error
    anywhere. Every brand-new user hit this, not just one -- it's the
    literal first thing Telegram prompts someone to do.

    check_access() already handles the new/pending/denied cases fully
    (registers the request, notifies the admin, replies to the user) and
    returns False for all of them, so there's nothing more to do here in
    those cases. Only an *already-approved* user typing /start again
    falls through to the capabilities message below, since check_access
    returns True silently for them."""
    if not await check_access(update, context):
        return
    await update.message.reply_text(guardrails.REDIRECT_MESSAGE, parse_mode=ParseMode.HTML)


async def handle_interests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/interests -- show current interests. /interests <comma, separated,
    topics> -- set them. /interests clear -- clear them. Stored per-chat
    in users_db.py; injected into the agent's context on future messages
    (see handle_message) so it can prioritize a subscriber's own topics
    when their question is general -- see docs/bot-features-plan.md."""
    if not await check_access(update, context):
        return

    chat_id = update.effective_chat.id
    text_after_command = update.message.text.partition(" ")[2].strip()

    if not text_after_command:
        interests = users_db.get_interests(chat_id)
        if interests:
            await update.message.reply_text("Your interests: " + ", ".join(interests))
        else:
            await update.message.reply_text(
                "You haven't set any interests yet. Use /interests topic1, topic2 to set them."
            )
        return

    if text_after_command.lower() == "clear":
        users_db.set_interests(chat_id, [])
        await update.message.reply_text("Interests cleared.")
        return

    interests = [t.strip() for t in text_after_command.split(",") if t.strip()]
    users_db.set_interests(chat_id, interests)
    await update.message.reply_text("Interests updated: " + ", ".join(interests))


async def handle_language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language -- show current reply-language preference. /language
    <name> -- set it (e.g. "/language Spanish"). /language clear --
    reset to matching whatever language the user writes in (the default).
    Mirrors handle_interests_command; also settable via natural language
    (the set_language tool, routed by the "set_language" category -- see
    guardrails.py) since this is the same command-or-conversation dual
    surface every other subscription feature in this bot has."""
    if not await check_access(update, context):
        return

    chat_id = update.effective_chat.id
    text_after_command = update.message.text.partition(" ")[2].strip()

    if not text_after_command:
        language = users_db.get_language(chat_id)
        if language:
            await update.message.reply_text(f"Your reply language is set to: {language}")
        else:
            await update.message.reply_text(
                "No reply language set -- I match whichever language you write in. "
                "Use /language <name> (e.g. /language Spanish) to set one."
            )
        return

    if text_after_command.lower() == "clear":
        users_db.set_language(chat_id, None)
        await update.message.reply_text("Reply language cleared -- back to matching your message's language.")
        return

    users_db.set_language(chat_id, text_after_command)
    await update.message.reply_text(f"Reply language set to: {text_after_command}")


async def process_message(chat_id: int, user_text: str, agent, guard_model) -> dict:
    """The actual guardrail/agent/formatting pipeline, independent of
    Telegram's Update/Context objects -- extracted so test_api.py's local
    curl endpoint (docs/local-testing-api-plan.md) exercises this exact
    logic, not a separate reimplementation that could silently drift from
    what real Telegram traffic runs. handle_message (below) is now a thin
    wrapper: Telegram-specific I/O only, no pipeline logic of its own.

    Returns {"blocked_at": str|None, "category": str|None, "reply": str}.
    blocked_at names which layer stopped the message (None if it went all
    the way through) -- useful for a test caller to assert on without
    parsing the reply text or cross-referencing docker logs/Phoenix."""
    # Guardrail layer 1: free, local, zero-LLM-call pre-filter. See
    # docs/guardrails-plan.md for the incident this design responds to.
    if guardrails.fails_local_prefilter(user_text):
        return {"blocked_at": "layer1_prefilter", "category": None, "reply": guardrails.REDIRECT_MESSAGE}

    # Guardrail layer 2 -- now the router (docs/context-management-plan.md):
    # one structured-output call answers both "is this on-topic" and "what
    # kind of request is this", gating the expensive agent call and telling
    # it which layer-2 instructions/tool to reach for in the same pass.
    classification = await asyncio.to_thread(guardrails.classify_message, guard_model, user_text)
    if not classification.on_topic:
        return {"blocked_at": "layer2_router", "category": classification.category, "reply": guardrails.REDIRECT_MESSAGE}

    history, history_timestamps = _get_trimmed_history(chat_id)
    working_messages = history + [{"role": "user", "content": user_text}]

    # chat_id and category feed agent.py's dynamic-prompt middleware
    # (layers 2/3) and the update_interests/set_push_enabled tools (which
    # need to know *which* user's row to write) -- see
    # docs/context-management-plan.md's router design.
    try:
        result_messages = await asyncio.to_thread(
            run_agent,
            agent,
            working_messages,
            context={"chat_id": chat_id, "category": classification.category},
        )
    except Exception as exc:
        return {
            "blocked_at": "agent_error",
            "category": classification.category,
            "reply": f"Something went wrong: {exc}",
        }

    final_content = _strip_report_preamble(_normalize_markdown_bold(result_messages[-1].content))

    # Guardrail layer 4: re-checks the agent's actual output before it's
    # sent -- the layer that catches drift layers 1-3 missed, since the
    # failure is only visible in what the model wrote, not the input.
    output_on_topic = await asyncio.to_thread(
        guardrails.is_output_on_topic, guard_model, final_content, classification.category
    )
    if not output_on_topic:
        return {"blocked_at": "layer4_output_check", "category": classification.category, "reply": guardrails.REDIRECT_MESSAGE}

    # Only persisted once accepted -- a rejected exchange doesn't pollute
    # the conversation history the next turn sees. The trimmed base
    # (_get_trimmed_history, above) was already stored regardless of this
    # turn's outcome; here we just add this turn's new messages with a
    # fresh timestamp. new_messages is everything run_agent appended
    # beyond the trimmed history it was given -- the new user message
    # plus whatever AI/tool messages resulted from it.
    new_messages = result_messages[len(history):]
    now = datetime.now(timezone.utc)
    chat_histories[chat_id] = (result_messages, history_timestamps + [now] * len(new_messages))

    return {"blocked_at": None, "category": classification.category, "reply": final_content}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    result = await process_message(
        update.effective_chat.id,
        update.message.text,
        context.bot_data["agent"],
        context.bot_data["guard_model"],
    )
    final_content = result["reply"]

    chunks = split_for_telegram(final_content)
    for i, chunk in enumerate(chunks):
        if i > 0:
            # Small gap between sequential messages -- avoids hitting
            # Telegram's rate limit on rapid-fire sends and reads as a
            # steady stream instead of a burst.
            await asyncio.sleep(1)
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
        except BadRequest:
            # The model didn't produce valid HTML (unescaped &/</>, a tag
            # it wasn't asked to use, etc.) -- fall back to plain text so
            # the user still gets an answer instead of silence.
            await update.message.reply_text(_strip_html_tags(chunk))


async def send_push_digest(bot: Bot, chat_id: int, text: str) -> None:
    """The `send` callback news_push.run_push_cycle() calls per due
    subscriber. Push digests go through the same HTML-formatting/trend-
    report prompt (agent.HTML_FORMATTING_RULES/TREND_REPORT_STRUCTURE) as
    a normal chat reply and can fail the same ways, so this reuses
    handle_message's exact pipeline instead of a simpler one-off send: the
    Markdown-leak and report-preamble safety nets, chunking for messages
    over Telegram's limit, and the BadRequest-on-bad-HTML fallback to
    plain text."""
    normalized = _strip_report_preamble(_normalize_markdown_bold(text))
    chunks = split_for_telegram(normalized)
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(1)
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)
        except BadRequest:
            await bot.send_message(chat_id=chat_id, text=_strip_html_tags(chunk))


async def _push_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    model = context.bot_data["guard_model"]

    async def send(chat_id: int, text: str) -> None:
        await send_push_digest(context.bot, chat_id, text)

    await news_push.run_push_cycle(model, send)


def register_push_job(app: Application) -> None:
    """Wires up the periodic-push scheduler (docs/bot-features-plan.md item
    5) -- requires the apscheduler dependency (see environment.yml) for
    Application.job_queue to exist at all. Called by both bot.py's own
    main() and combined_bot.py's, so standalone and combined deployment
    both get push. `first=10` just avoids doing real work in the same
    instant as startup; the actual per-subscriber due-check is time-based
    (users_db's push_interval_hours / last_push_at), not this delay."""
    app.job_queue.run_repeating(_push_job, interval=PUSH_TICK_SECONDS, first=10)


async def _ingest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    model = context.bot_data["guard_model"]
    # run_ingestion_cycle does synchronous network calls across every
    # enabled source -- offloaded the same way handle_message offloads
    # run_agent, so a slow cycle can't block the bot's event loop.
    await asyncio.to_thread(news_ingest.run_ingestion_cycle, model)


def register_ingest_job(app: Application) -> None:
    """Wires up the periodic news-cache ingestion job -- see
    docs/local-news-cache-plan.md. Same registration shape as
    register_push_job, called from the same places."""
    app.job_queue.run_repeating(_ingest_job, interval=INGEST_TICK_SECONDS, first=10)


async def _start_test_api(app: Application) -> None:
    """post_init hook -- run_polling() manages its own event loop
    internally, so this is the standalone-bot.py equivalent of
    combined_bot.py's run_both() starting test_api after the loop is
    already running (test_api.start() needs asyncio.get_running_loop())."""
    app.bot_data["test_api_server"] = test_api.start(app.bot_data["agent"], app.bot_data["guard_model"])


async def _stop_test_api(app: Application) -> None:
    test_api.stop(app.bot_data.get("test_api_server"))


def main():
    setup_telemetry()
    users_db.init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    model = ChatDeepSeek(model=MODEL)
    agent = build_agent(model)

    app = Application.builder().token(token).post_init(_start_test_api).post_shutdown(_stop_test_api).build()
    app.bot_data["agent"] = agent
    # Reuses the same model instance for guardrail classification calls
    # (guardrails.py) -- no separate model/infra, see docs/guardrails-plan.md.
    app.bot_data["guard_model"] = model
    app.bot_data["admin_chat_id"] = int(os.environ["ADMIN_CHAT_ID"])
    app.bot_data["admin_bot_token"] = os.environ["ADMIN_BOT_TOKEN"]
    # Idempotent -- safe to run every startup. Only the admin gets
    # search_news access to news_sources.RESTRICTED_SOURCES (NewsAPI,
    # Perigon) by default; granting it to anyone else is a plain DB update
    # (users_db.set_restricted_sources_enabled), not a new code path.
    users_db.set_restricted_sources_enabled(app.bot_data["admin_chat_id"], True)
    app.add_handler(CommandHandler("start", handle_start_command))
    app.add_handler(CommandHandler("interests", handle_interests_command))
    app.add_handler(CommandHandler("language", handle_language_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    register_push_job(app)
    register_ingest_job(app)

    print("Telegram bot ready (polling). Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
