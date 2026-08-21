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


# --- A4 message building --------------------------------------------------


def test_review_message_escapes_html_in_every_untrusted_field():
    """All three sources are untrusted for HTML: name and description are
    model output, and the title is a real headline -- ampersands in
    headlines ("AT&T", "R&D") are common, not an edge case. Telegram
    rejects the whole send on an unescaped &/</>, and since nothing about a
    stored proposal changes between cycles, one that hit this would fail
    identically forever: never raised, visible only in a log."""
    text, _ = admin_bot.build_category_review(
        "M&A", 5,
        [("AT&T buys <startup> for $2B", "https://e/1")],
        "mergers & acquisitions -- not <Finance>",
        ["AI", "R&D"],
    )

    assert "M&amp;A" in text
    assert "AT&amp;T buys &lt;startup&gt;" in text
    assert "mergers &amp; acquisitions" in text
    assert "R&amp;D" in text
    # the structural tags this file adds itself must survive
    assert "<b>" in text and "<i>" in text


def test_review_message_handles_a_missing_description():
    text, _ = admin_bot.build_category_review("Media", 5, [("A story", "https://e/1")], None, ["AI"])

    assert "no description drafted" in text


def test_review_buttons_carry_the_category_name():
    _, keyboard = admin_bot.build_category_review("Media", 5, [], "d", ["AI"])

    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["cat:activate:Media", "cat:merge:Media", "cat:reject:Media"]


def test_merge_keyboard_offers_only_the_given_targets():
    keyboard = admin_bot._merge_target_keyboard("Media", ["AI", "Software", "Hardware", "IT"])

    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["cat:into:Media:AI", "cat:into:Media:Software",
                    "cat:into:Media:Hardware", "cat:into:Media:IT"]
    assert len(keyboard.inline_keyboard) == 2, "three per row"


def test_every_callback_fits_telegram_s_limit():
    """64 bytes, and the merge variant packs two names into one payload."""
    long_name = "A" * 32          # users_db.MAX_CATEGORY_NAME_LENGTH
    keyboard = admin_bot._merge_target_keyboard(long_name, ["Consumer", "Regulation"])

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64
