"""Tests that /new (and its /reset alias) clears session-scoped overrides."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None  # disables _evict_cached_agent lock path
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
async def test_new_command_only_clears_own_session():
    """/new must only clear the override for the session that triggered it."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    other_key = "other_session_key"

    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_model_overrides[other_key] = {
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_key": "***",
        "base_url": "",
        "api_mode": "anthropic",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._session_reasoning_overrides[other_key] = {"enabled": True, "effort": "low"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"
    runner._pending_model_notes[other_key] = "[Note: switched to claude-sonnet-4-6.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert other_key in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert other_key in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes
    assert other_key in runner._pending_model_notes


@pytest.mark.asyncio
async def test_reset_reply_can_hide_operator_details_and_use_custom_text(monkeypatch):
    """Public chats can confirm reset without model/provider/context metadata."""
    import gateway.run as gateway_run

    runner = _make_runner()
    runner._reset_notice_session_info = lambda source: "Model: secret-model\nProvider: secret-provider\n"
    runner._telegram_topic_new_header = lambda source: ""
    runner._is_telegram_topic_lane = lambda source: False
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "platforms": {
                    "telegram": {
                        "session_reset_reply": "New session started.",
                        "session_reset_details": False,
                    }
                }
            }
        },
    )

    reply = str(await runner._handle_reset_command(_make_event("/new")))

    assert reply == "New session started."
    assert "secret-model" not in reply
    assert "secret-provider" not in reply


@pytest.mark.asyncio
async def test_reset_reply_can_be_silent(monkeypatch):
    """An explicit empty reset reply suppresses the confirmation banner."""
    import gateway.run as gateway_run

    runner = _make_runner()
    runner._reset_notice_session_info = lambda source: "Model: secret-model\n"
    runner._telegram_topic_new_header = lambda source: ""
    runner._is_telegram_topic_lane = lambda source: False
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "session_reset_reply": "",
                "session_reset_details": False,
            }
        },
    )

    reply = str(await runner._handle_reset_command(_make_event("/reset")))

    assert reply == ""


@pytest.mark.asyncio
async def test_reset_reply_uses_routed_profile_config_outside_outer_scope(tmp_path):
    """Busy-path resets still resolve display settings from the routed profile."""
    runner = _make_runner()
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = lambda source: tmp_path
    runner._reset_notice_session_info = lambda source: "Model: secret-model\n"
    runner._telegram_topic_new_header = lambda source: ""
    runner._is_telegram_topic_lane = lambda source: False
    (tmp_path / "config.yaml").write_text(
        "display:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      session_reset_reply: Routed reset.\n"
        "      session_reset_details: false\n",
        encoding="utf-8",
    )

    reply = str(await runner._handle_reset_command(_make_event("/reset")))

    assert reply == "Routed reset."
    assert "secret-model" not in reply


@pytest.mark.asyncio
async def test_silent_reset_preserves_title_rejection_warning(monkeypatch):
    """Title validation warnings survive when ordinary reset copy is hidden."""
    import gateway.run as gateway_run

    runner = _make_runner()
    runner._session_db = SimpleNamespace(set_session_title=AsyncMock())
    runner._reset_notice_session_info = lambda source: "Model: secret-model\n"
    runner._telegram_topic_new_header = lambda source: ""
    runner._is_telegram_topic_lane = lambda source: False
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "session_reset_reply": "",
                "session_reset_details": False,
            }
        },
    )

    reply = str(
        await runner._handle_reset_command(_make_event("/new " + ("x" * 300)))
    )

    assert "title rejected" in reply.lower()
    assert "secret-model" not in reply


@pytest.mark.asyncio
async def test_custom_reset_reply_is_safely_sanitized(monkeypatch):
    """Configured reset copy cannot leak URL credentials or invalid surrogates."""
    import gateway.run as gateway_run

    credential = "abcdefghijklmnopqrstuvwxyz1234567890"
    custom_reply = (
        f"Reset https://user:{credential}@example.test/"
        f"?token={credential} \ud800"
    )
    runner = _make_runner()
    runner._reset_notice_session_info = lambda source: "Model: secret-model\n"
    runner._telegram_topic_new_header = lambda source: ""
    runner._is_telegram_topic_lane = lambda source: False
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "session_reset_reply": custom_reply,
                "session_reset_details": False,
            }
        },
    )

    reply = str(await runner._handle_reset_command(_make_event("/reset")))

    assert reply.startswith("Reset ")
    assert credential not in reply
    assert "\ud800" not in reply
    reply.encode("utf-8")
