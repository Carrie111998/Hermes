import asyncio
import builtins

from tools.send_message_tool import _send_telegram


def test_telegram_missing_dependency_recommends_uv(monkeypatch):
    real_import = builtins.__import__

    def without_telegram(name, *args, **kwargs):
        if name == "telegram" or name.startswith("telegram."):
            raise ImportError("telegram unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_telegram)

    result = asyncio.run(_send_telegram("token", "123", "hello"))

    assert result == {
        "error": (
            "python-telegram-bot not installed. "
            "Run: uv pip install python-telegram-bot"
        )
    }