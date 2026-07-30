"""Tests for Discord channel_prompts resolution and injection."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None, **kwargs):
        request = kwargs.get("turn_routing_request")
        prepared = request.prepare_user_message(user_message) if request is not None else None
        type(self).last_run = {
            "persist_user_message": persist_user_message,
            "prepared": prepared,
            "request": request,
            "user_message": user_message,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_adapter():
    _ensure_discord_mock()
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter.config = MagicMock()
    adapter.config.extra = {}
    return adapter


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = "Global prompt"
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="12345",
        chat_type="thread",
        user_id="user-1",
    )


class TestResolveChannelPrompts:
    def test_no_prompt_returns_none(self):
        adapter = _make_adapter()
        assert adapter._resolve_channel_prompt("123") is None

    def test_match_by_channel_id(self):
        adapter = _make_adapter()
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"

    def test_numeric_yaml_keys_normalized_at_config_load(self):
        """Numeric YAML keys are normalized to strings by config bridging.

        The resolver itself expects string keys (config.py handles normalization),
        so raw numeric keys will not match — this is intentional.
        """
        adapter = _make_adapter()
        # Simulates post-bridging state: keys are already strings
        adapter.config.extra = {"channel_prompts": {"100": "Research mode"}}
        assert adapter._resolve_channel_prompt("100") == "Research mode"
        # Pre-bridging numeric key would not match (bridging is responsible)
        adapter.config.extra = {"channel_prompts": {100: "Research mode"}}
        assert adapter._resolve_channel_prompt("100") is None


@pytest.mark.asyncio
async def test_retry_preserves_channel_prompt(monkeypatch):
    runner = _make_runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1", last_prompt_tokens=10),
        load_transcript=lambda session_id: [
            {"role": "user", "content": "original message"},
            {"role": "assistant", "content": "old reply"},
        ],
        rewrite_transcript=MagicMock(),
    )
    runner._handle_message = AsyncMock(return_value="ok")

    event = MessageEvent(
        text="/retry",
        message_type=gateway_run.MessageType.COMMAND,
        source=_make_source(),
        raw_message=SimpleNamespace(),
        channel_prompt="Channel prompt",
    )

    result = await runner._handle_retry_command(event)

    assert result == "ok"
    retried_event = runner._handle_message.await_args.args[0]
    assert retried_event.channel_prompt == "Channel prompt"


@pytest.mark.asyncio
async def test_model_switch_note_is_provider_only_and_consumed_once(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    session_key = "agent:main:discord:thread:12345"
    note = "[Note: model was switched; provider-only sidecar.]"
    runner._pending_model_notes[session_key] = note

    (tmp_path / "config.yaml").write_text("agent:\n  system_prompt: Global prompt\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
        },
    )
    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    await runner._run_agent(
        message="first user message",
        context_prompt="Context prompt",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key=session_key,
        persist_user_message="first user message",
    )

    first = _CapturingAgent.last_run
    assert first["user_message"] == "first user message"
    assert first["request"].user_text == "first user message"
    assert first["persist_user_message"] == "first user message"
    assert first["prepared"].persist_user_message == "first user message"
    assert first["prepared"].user_message == f"{note}\n\nfirst user message"
    assert "route.decided" not in first["prepared"].user_message
    assert session_key not in runner._pending_model_notes

    await runner._run_agent(
        message="second user message",
        context_prompt="Context prompt",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key=session_key,
        persist_user_message="second user message",
    )

    second = _CapturingAgent.last_run
    assert second["prepared"].user_message == "second user message"
    assert second["prepared"].persist_user_message == "second user message"
