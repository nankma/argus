import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ParseMode

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


# --- A4 handle_category_decision -------------------------------------------
#
# This is the seam the message builder and the DB operations get joined
# at: parsing callback_data, the authorization check, the merge two-step,
# and the edit-message call. Every bug in the previous three rounds on this
# project lived in a seam like this one, not in either side it joins.


def _make_category_callback_update(from_id, data, message_text="Original proposal text."):
    query = MagicMock()
    query.from_user = SimpleNamespace(id=from_id)
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    # text_html as well as text: the handler reads the HTML form so the
    # outcome edit keeps the bold name and italic description the admin was
    # shown. A fake with only `text` would pass against code that silently
    # flattens the message -- which is exactly what happened once.
    query.message = SimpleNamespace(text=message_text, text_html=message_text)
    update = MagicMock()
    update.callback_query = query
    return update


def _propose(name, now=None):
    now = now or datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.record_category_sighting(name, now, "https://e/1", "A story")


def test_category_decision_rejects_non_admin(isolated_subscribers_db):
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=111, data="cat:activate:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context(admin_chat_id=999)))

    update.callback_query.answer.assert_called_once_with("Not authorized.", show_alert=True)
    update.callback_query.edit_message_text.assert_not_called()
    assert dict(users_db.get_active_categories()).get("Healthcare") is None


def test_category_decision_activate_flips_status_and_edits_the_message(isolated_subscribers_db):
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=999, data="cat:activate:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    assert "Healthcare" in dict(users_db.get_active_categories())
    update.callback_query.answer.assert_called_once_with()
    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nActivated.", parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_activate_twice_reports_no_change_and_does_not_reactivate(
    isolated_subscribers_db,
):
    """Two admins, or a double-tap. The DB guard makes the second press a
    no-op; the handler must say so rather than implying it worked twice."""
    _propose("Healthcare")
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.activate_category("Healthcare", "admin:1", now)
    update = _make_category_callback_update(from_id=999, data="cat:activate:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nAlready decided -- no change.", parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_reject_flips_status(isolated_subscribers_db):
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=999, data="cat:reject:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Healthcare'"
        ).fetchone()[0]
    assert status == "rejected"
    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nRejected.", parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_reject_twice_reports_no_change(isolated_subscribers_db):
    _propose("Healthcare")
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    users_db.reject_category("Healthcare", "admin:1", now)
    update = _make_category_callback_update(from_id=999, data="cat:reject:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nAlready decided -- no change.", parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_merge_shows_the_target_keyboard_without_deciding(
    isolated_subscribers_db,
):
    """The 'Merge into...' tap is a two-step: it must only replace the
    keyboard, never touch the DB or the message text, until a target is
    picked."""
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=999, data="cat:merge:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    update.callback_query.answer.assert_called_once_with()
    update.callback_query.edit_message_text.assert_not_called()
    update.callback_query.edit_message_reply_markup.assert_called_once()
    keyboard = update.callback_query.edit_message_reply_markup.call_args[0][0]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == [f"cat:into:Healthcare:{name}" for name, _ in users_db.get_active_categories()]
    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Healthcare'"
        ).fetchone()[0]
    assert status == "proposed", "picking 'Merge into...' must not decide anything by itself"


def test_category_decision_into_merges_and_rewrites_interests(isolated_subscribers_db):
    _propose("Healthcare")
    users_db.set_interest_categories("hospitals", ["Healthcare"])
    update = _make_category_callback_update(from_id=999, data="cat:into:Healthcare:AI")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    assert users_db.resolve_category_name("Healthcare") == "AI"
    assert users_db.get_cached_interest_categories(["hospitals"]) == {"hospitals": ["AI"]}
    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nMerged into AI.", parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_into_a_non_active_target_reports_failure(isolated_subscribers_db):
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=999, data="cat:into:Healthcare:Nonexistent")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Healthcare'"
        ).fetchone()[0]
    assert status == "proposed", "a refused merge must not change status"
    update.callback_query.edit_message_text.assert_called_once_with(
        "Original proposal text.\n\nCould not merge into Nonexistent -- it isn't active.",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


def test_category_decision_unknown_action_is_rejected_without_touching_the_db(
    isolated_subscribers_db,
):
    _propose("Healthcare")
    update = _make_category_callback_update(from_id=999, data="cat:frobnicate:Healthcare")

    asyncio.run(admin_bot.handle_category_decision(update, _make_context()))

    update.callback_query.answer.assert_called_once_with("Unknown action.", show_alert=True)
    update.callback_query.edit_message_text.assert_not_called()
    with users_db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM categories WHERE name = 'Healthcare'"
        ).fetchone()[0]
    assert status == "proposed"


def test_the_outcome_edit_preserves_the_formatting_the_admin_was_shown():
    """The message was sent with parse_mode=HTML. `query.message.text`
    hands it back with the markup stripped, so re-sending that as plain
    text flattens the bold name and italic description -- the record of
    what the admin agreed to stops looking like what they were shown.

    Worth a test because this was claimed as fixed in a commit message
    while the code still said `.text`: a silently-failed edit produced a
    false record, and nothing checked."""
    import inspect

    source = inspect.getsource(admin_bot.handle_category_decision)

    assert "query.message.text_html" in source
    assert "query.message.text}" not in source, "the entity-stripped form must not be used"
    assert "parse_mode=ParseMode.HTML" in source


def test_outcome_escaping_leaves_apostrophes_alone():
    """Telegram's HTML parser understands only &lt; &gt; &amp;. Escaping an
    apostrophe to &#x27; would show the entity to the admin instead of an
    apostrophe -- which is why quote=False, not the default."""
    import html as html_mod

    escaped = html_mod.escape("Could not merge into R&D -- it isn't active.", quote=False)

    assert "isn't" in escaped
    assert "R&amp;D" in escaped
