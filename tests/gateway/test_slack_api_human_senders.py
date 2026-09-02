"""Tests for the Slack ``api_human_users`` / ``api_human_apps`` allowlists.

A message posted through the Web API with a *user* token is authored by a
real person, but Slack stamps it with the posting app's ``bot_id``/``app_id``
so ``_event_declares_bot_sender`` classifies it as bot traffic and the
message is dropped. Operators running their own front-ends (dashboards,
mobile shells) can allowlist those senders via
``platforms.slack.extra.api_human_users`` / ``.api_human_apps`` or the
``SLACK_API_HUMAN_USERS`` / ``SLACK_API_HUMAN_APPS`` env vars.

These tests pin the allowlist scope: only events that name a real ``user``
and are not ``subtype: bot_message`` may match, so classic bot posts (which
carry no ``user``) can never ride the allowlist.
"""

import sys
from unittest.mock import MagicMock

import pytest


# Mock slack-bolt / slack-sdk the same way test_slack_mention.py does.
def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402

from gateway.config import Platform, PlatformConfig  # noqa: E402


HUMAN_ID = "U_human"
FRONTEND_APP_ID = "A_frontend"


def _make_adapter(extra=None):
    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=dict(extra or {}))
    return adapter


def _api_post(**overrides):
    """A user-token chat.postMessage as delivered over Socket Mode:
    real ``user``, no ``client_msg_id``, stamped with the app's ids."""
    event = {
        "type": "message",
        "user": HUMAN_ID,
        "bot_id": "B_stamp",
        "app_id": FRONTEND_APP_ID,
        "text": "build the lunch app",
    }
    event.update(overrides)
    return event


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SLACK_API_HUMAN_USERS", raising=False)
    monkeypatch.delenv("SLACK_API_HUMAN_APPS", raising=False)


def test_api_post_is_bot_by_default():
    assert _make_adapter()._event_declares_bot_sender(_api_post()) is True


def test_config_user_allowlist_admits_api_post():
    adapter = _make_adapter({"api_human_users": HUMAN_ID})
    assert adapter._event_declares_bot_sender(_api_post()) is False


def test_config_user_allowlist_accepts_list_form():
    adapter = _make_adapter({"api_human_users": ["U_other", HUMAN_ID]})
    assert adapter._event_declares_bot_sender(_api_post()) is False


def test_env_app_allowlist_admits_api_post(monkeypatch):
    monkeypatch.setenv("SLACK_API_HUMAN_APPS", FRONTEND_APP_ID)
    assert _make_adapter()._event_declares_bot_sender(_api_post()) is False


def test_config_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("SLACK_API_HUMAN_USERS", HUMAN_ID)
    adapter = _make_adapter({"api_human_users": "U_someone_else"})
    assert adapter._event_declares_bot_sender(_api_post()) is True


def test_unlisted_sender_stays_bot():
    adapter = _make_adapter({"api_human_users": "U_other", "api_human_apps": "A_other"})
    assert adapter._event_declares_bot_sender(_api_post()) is True


def test_bot_message_subtype_never_matches():
    adapter = _make_adapter({"api_human_users": HUMAN_ID})
    event = _api_post(subtype="bot_message")
    assert adapter._event_declares_bot_sender(event) is True


def test_event_without_user_never_matches():
    """Classic bot posts carry no ``user`` — the app allowlist must not admit them."""
    adapter = _make_adapter({"api_human_apps": FRONTEND_APP_ID})
    event = _api_post()
    del event["user"]
    assert adapter._event_declares_bot_sender(event) is True


def test_plain_human_message_unaffected():
    event = {"type": "message", "user": HUMAN_ID, "client_msg_id": "x-1", "text": "hi"}
    assert _make_adapter()._event_declares_bot_sender(event) is False
