"""Tests for Telegram adapter fail-closed auth fallback (#24457).

The _is_callback_user_authorized fallback must deny users by default
when TELEGRAM_ALLOWED_USERS is empty, instead of allowing everyone.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig, Platform


# -- Fake telegram modules (minimal stubs) --------------------------------

_fake_telegram_error = types.ModuleType("telegram.error")


class _TelegramError(Exception):
    pass


_fake_telegram_error.TelegramError = _TelegramError
_fake_telegram_error.BadRequest = type("BadRequest", (_TelegramError,), {})
_fake_telegram_error.NetworkError = type("NetworkError", (_TelegramError,), {})

_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(HTML="HTML")

_fake_telegram_request = types.ModuleType("telegram.request")
_fake_telegram_request.HTTPXRequest = type("HTTPXRequest", (), {"__init__": lambda *a, **kw: None})

_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.ApplicationBuilder = type("ApplicationBuilder", (), {
    "token": lambda self, *a: self,
    "build": lambda self: None,
})

_fake_telegram = types.ModuleType("telegram")
_fake_telegram.error = _fake_telegram_error
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram.ext = _fake_telegram_ext
_fake_telegram.request = _fake_telegram_request


@pytest.fixture(autouse=True)
def _inject_fake_telegram(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram", _fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", _fake_telegram_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", _fake_telegram_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", _fake_telegram_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", _fake_telegram_request)


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = object.__new__(TelegramAdapter)
    adapter.config = config
    adapter._config = config
    adapter._platform = Platform.TELEGRAM
    adapter._connected = True
    return adapter


class TestCallbackAuthFailClosed:
    """_is_callback_user_authorized fallback must be fail-closed."""

    def test_no_allowlist_no_allow_all_denies(self, monkeypatch):
        """No TELEGRAM_ALLOWED_USERS and no GATEWAY_ALLOW_ALL_USERS → deny."""
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        # Force the fallback path (no runner auth)
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is False


    def test_allowlist_with_matching_user_permits(self, monkeypatch):
        """TELEGRAM_ALLOWED_USERS contains the user → allow."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345,67890")
        adapter = _make_adapter()
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is True

    def test_registered_profile_check_precedes_process_env(self, monkeypatch):
        """Secondary-profile buttons must not use the primary allowlist."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "6940705170")
        adapter = _make_adapter()
        adapter._message_handler = None
        adapter.set_authorization_check(
            lambda user_id, chat_type, chat_id: user_id == "429731663"
        )

        assert adapter._is_callback_user_authorized(
            "429731663",
            chat_id="429731663",
            chat_type="dm",
        ) is True
        assert adapter._is_callback_user_authorized(
            "6940705170",
            chat_id="6940705170",
            chat_type="dm",
        ) is False

    def test_env_fallback_is_profile_scoped_in_multiplex(self, monkeypatch):
        """Fallback auth must not reuse another profile's allowlist."""
        from agent import secret_scope as ss

        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "primary-user")
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        adapter._message_handler = None
        adapter._authorization_check = None

        ss.set_multiplex_active(True)
        token = ss.set_secret_scope(
            {"TELEGRAM_ALLOWED_USERS": "personal-user"}
        )
        try:
            assert adapter._is_callback_user_authorized("personal-user") is True
            assert adapter._is_callback_user_authorized("primary-user") is False
        finally:
            ss.reset_secret_scope(token)
            ss.set_multiplex_active(False)


