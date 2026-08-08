import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bot
import users_db
from bot import TELEGRAM_MESSAGE_LIMIT, split_for_telegram


def test_split_for_telegram_short_text_unchanged():
    text = "Short reply."
    assert split_for_telegram(text) == [text]


def test_split_for_telegram_splits_long_text():
    text = "a" * (TELEGRAM_MESSAGE_LIMIT + 500)
    chunks = split_for_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT
    assert "".join(chunks) == text


def test_split_for_telegram_prefers_newline_boundary():
    # A newline just past the halfway point of the limit — the split should
    # land there rather than mid-line further into the first chunk.
    first_line = "x" * (TELEGRAM_MESSAGE_LIMIT - 10)
    second_line = "y" * 100
    text = f"{first_line}\n{second_line}"
    chunks = split_for_telegram(text)
    assert chunks[0] == first_line
    assert chunks[1] == second_line


def _make_update(chat_id, username="alice", first_name="Alice"):
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.effective_user = SimpleNamespace(id=chat_id, username=username, first_name=first_name)
    update.message = message
    return update


def _make_context(admin_chat_id=999):
    context = MagicMock()
    context.bot_data = {"admin_chat_id": admin_chat_id, "admin_bot_token": "fake-admin-token"}
    return context


def test_check_access_allows_admin(isolated_subscribers_db, monkeypatch):
    monkeypatch.setattr(bot, "notify_admin", AsyncMock())
    update = _make_update(chat_id=999)
    context = _make_context(admin_chat_id=999)
    assert asyncio.run(bot.check_access(update, context)) is True
    update.message.reply_text.assert_not_called()


def test_check_access_allows_approved_user(isolated_subscribers_db, monkeypatch):
    monkeypatch.setattr(bot, "notify_admin", AsyncMock())
    users_db.request_access(1, "alice", "Alice")
    users_db.decide(1, approved=True)
    update = _make_update(chat_id=1)
    assert asyncio.run(bot.check_access(update, _make_context())) is True


def test_check_access_blocks_pending_user(isolated_subscribers_db, monkeypatch):
    monkeypatch.setattr(bot, "notify_admin", AsyncMock())
    users_db.request_access(2, "bob", "Bob")
    update = _make_update(chat_id=2)
    assert asyncio.run(bot.check_access(update, _make_context())) is False
    reply = update.message.reply_text.call_args[0][0]
    assert "pending" in reply.lower()


def test_check_access_blocks_denied_user(isolated_subscribers_db, monkeypatch):
    monkeypatch.setattr(bot, "notify_admin", AsyncMock())
    users_db.request_access(3, "carol", "Carol")
    users_db.decide(3, approved=False)
    update = _make_update(chat_id=3)
    assert asyncio.run(bot.check_access(update, _make_context())) is False
    reply = update.message.reply_text.call_args[0][0]
    assert "denied" in reply.lower()


def test_check_access_registers_new_request_and_notifies_admin(isolated_subscribers_db, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(bot, "notify_admin", notify)
    update = _make_update(chat_id=4, username="dave", first_name="Dave")
    assert asyncio.run(bot.check_access(update, _make_context())) is False
    assert users_db.get_status(4) == users_db.PENDING
    notify.assert_called_once()
