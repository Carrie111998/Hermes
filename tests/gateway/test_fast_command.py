"""Tests for gateway /fast support and Priority Processing routing."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import gateway.run as gateway_run
from agent.agent_init import _merge_custom_provider_extra_body
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None
    init_count = 0
    turn_request_overrides = []

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        type(self).init_count += 1
        self.model = kwargs["model"]
        self.provider = kwargs["provider"]
        self.base_url = kwargs["base_url"]
        self.request_overrides = dict(kwargs.get("request_overrides") or {})
        self._custom_providers = [
            {
                "base_url": self.base_url,
                "extra_body": {
                    "reasoning_effort": "xhigh",
                    "service_tier": "flex",
                },
            }
        ]
        _merge_custom_provider_extra_body(self, self._custom_providers)
        self.context_compressor = None
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
            "persist_user_timestamp": persist_user_timestamp,
        }
        _merge_custom_provider_extra_body(self, self._custom_providers)
        type(self).turn_request_overrides.append(dict(self.request_overrides))
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


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
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
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_discord_auto_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="user-1",
        thread_id="999",
        parent_chat_id="100",
        auto_thread_created=True,
        auto_thread_initial_name="raw user prompt",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_handle_fast_command_global_flag_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await runner._handle_fast_command(_make_event("/fast fast --global"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"
    # Global write supersedes the session override.
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_session_fast_override_beats_config_default(monkeypatch, tmp_path):
    """A session /fast normal wins over agent.service_tier: fast in config."""
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    event = _make_event("/fast normal")
    session_key = runner._session_key_for_source(event.source)

    response = await runner._handle_fast_command(event)

    assert "NORMAL" in response
    # Override stores explicit None (normal) and wins over config "fast".
    assert session_key in runner._session_service_tier_overrides
    assert runner._resolve_session_service_tier(session_key=session_key) is None
    # A different session still gets the config default.
    assert runner._resolve_session_service_tier(session_key="other-session") == "priority"


@pytest.mark.asyncio
async def test_cached_custom_provider_overrides_survive_fast_then_normal_turn(monkeypatch):
    _CapturingAgent.init_count = 0
    _CapturingAgent.turn_request_overrides = []
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: set())

    runner = _make_runner()
    runner.config.multiplex_profiles = False
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "gpt-5.4",
        {
            "api_key": "***",
            "base_url": "http://my-endpoint/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
        },
    )
    tiers = iter(["priority", None])
    runner._resolve_session_service_tier = lambda **_kwargs: next(tiers)
    runner._resolve_session_reasoning_config = lambda **_kwargs: None
    runner._get_system_prompt_for_channel = lambda *_args, **_kwargs: ""
    runner._refresh_fallback_model = lambda: None
    runner._enforce_agent_cache_cap = lambda: None

    source = _make_source()
    run_kwargs = {
        "context_prompt": "",
        "history": [],
        "source": source,
        "session_id": "session-1",
        "session_key": "agent:main:telegram:dm:12345",
    }

    await runner._run_agent(message="fast turn", **run_kwargs)
    await runner._run_agent(message="normal turn", **run_kwargs)

    assert _CapturingAgent.init_count == 1
    assert _CapturingAgent.turn_request_overrides == [
        {
            "service_tier": "priority",
            "extra_body": {"reasoning_effort": "xhigh"},
        },
        {
            "extra_body": {
                "reasoning_effort": "xhigh",
                "service_tier": "flex",
            }
        },
    ]
