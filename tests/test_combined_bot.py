from telegram.ext import CallbackQueryHandler, MessageHandler
import combined_bot

FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def test_build_info_app_wires_bot_data(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    app = combined_bot.build_info_app(agent="fake-agent", admin_chat_id=999, admin_bot_token="admin-token")

    assert app.bot_data["agent"] == "fake-agent"
    assert app.bot_data["admin_chat_id"] == 999
    assert app.bot_data["admin_bot_token"] == "admin-token"
    handlers = [h for group in app.handlers.values() for h in group]
    assert any(isinstance(h, MessageHandler) for h in handlers)


def test_build_admin_app_wires_bot_data(monkeypatch):
    monkeypatch.setenv("ADMIN_BOT_TOKEN", FAKE_TOKEN)
    app = combined_bot.build_admin_app(admin_chat_id=999, info_bot_token="info-token")

    assert app.bot_data["admin_chat_id"] == 999
    assert app.bot_data["info_bot_token"] == "info-token"
    handlers = [h for group in app.handlers.values() for h in group]
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
    assert any(isinstance(h, MessageHandler) for h in handlers)
