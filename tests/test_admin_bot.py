import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import admin_bot
import users_db


def _make_callback_update(from_id, chat_id, action):
    query = MagicMock()
    query.from_user = SimpleNamespace(id=from_id)
    query.data = f"{action}:{chat_id}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = SimpleNamespace(text="New access request from @dave (chat_id=4).")
    update = MagicMock()
    update.callback_query = query
    return update


def _make_context(admin_chat_id=999):
    context = MagicMock()
    context.bot_data = {"admin_chat_id": admin_chat_id, "info_bot_token": "fake-info-token"}
    return context


def _patch_bot(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(admin_bot, "Bot", MagicMock(return_value=MagicMock(send_message=sent)))
    return sent


def test_handle_decision_approves(isolated_subscribers_db, monkeypatch):
    sent = _patch_bot(monkeypatch)
    users_db.request_access(4, "dave", "Dave")
    update = _make_callback_update(from_id=999, chat_id=4, action="approve")
    asyncio.run(admin_bot.handle_decision(update, _make_context()))
    assert users_db.get_status(4) == users_db.APPROVED
    update.callback_query.edit_message_text.assert_called_once()
    sent.assert_called_once()


def test_handle_decision_denies(isolated_subscribers_db, monkeypatch):
    _patch_bot(monkeypatch)
    users_db.request_access(5, "erin", "Erin")
    update = _make_callback_update(from_id=999, chat_id=5, action="deny")
    asyncio.run(admin_bot.handle_decision(update, _make_context()))
    assert users_db.get_status(5) == users_db.DENIED


def test_handle_decision_rejects_non_admin(isolated_subscribers_db, monkeypatch):
    sent = _patch_bot(monkeypatch)
    users_db.request_access(6, "frank", "Frank")
    update = _make_callback_update(from_id=111, chat_id=6, action="approve")
    asyncio.run(admin_bot.handle_decision(update, _make_context(admin_chat_id=999)))
    assert users_db.get_status(6) == users_db.PENDING
    update.callback_query.answer.assert_called_once_with("Not authorized.", show_alert=True)
    sent.assert_not_called()
