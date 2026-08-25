import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from hermes_state import SessionDB


class RecordingDb:
    def __init__(self, row=None):
        self.row = row
        self.created_model_config = None
        self.updated_model_configs = []

    def get_session(self, _session_id):
        return self.row

    def create_session(self, *, session_id, source, model, model_config):
        self.created_model_config = dict(model_config)
        self.row = {
            "id": session_id,
            "source": source,
            "model": model,
            "model_config": json.dumps(model_config),
        }

    def update_session_meta(self, _session_id, model_config, model):
        self.updated_model_configs.append(json.loads(model_config))
        self.row["model_config"] = model_config
        self.row["model"] = model

    def replace_messages(self, *_args, **_kwargs):
        return None

    def get_messages_as_conversation(self, *_args, **_kwargs):
        return []


def _runtime_agent(*, transport="websocket-cached", provider="openai-codex"):
    return SimpleNamespace(
        model="gpt-5.4",
        requested_provider=provider,
        provider=provider,
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        responses_transport=transport,
        api_key="secret-that-must-not-be-persisted",
    )


def test_acp_create_and_update_persist_complete_non_secret_runtime():
    db = RecordingDb()
    agent = _runtime_agent()
    manager = SessionManager(agent_factory=lambda: agent, db=db)

    state = manager.create_session(cwd="/tmp/project")

    expected = {
        "cwd": "/tmp/project",
        "requested_provider": "openai-codex",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "responses_transport": "websocket-cached",
    }
    assert db.created_model_config == expected
    assert "api_key" not in db.created_model_config

    state.agent.responses_transport = "websocket"
    manager.save_session(state.session_id)

    assert db.updated_model_configs[-1] == {
        **expected,
        "responses_transport": "websocket",
    }


def test_acp_restore_passes_persisted_transport_to_agent_builder(monkeypatch):
    db = RecordingDb(
        row={
            "id": "acp-restored",
            "source": "acp",
            "model": "gpt-5.4",
            "model_config": json.dumps(
                {
                    "cwd": "/tmp/project",
                    "provider": "openai-codex",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "api_mode": "codex_responses",
                    "responses_transport": "websocket-cached",
                }
            ),
        }
    )
    manager = SessionManager(db=db)
    captured = {}

    def make_agent(**kwargs):
        captured.update(kwargs)
        return _runtime_agent(transport=kwargs["responses_transport"])

    monkeypatch.setattr(manager, "_make_agent", make_agent)

    state = manager.get_session("acp-restored")

    assert state is not None
    assert captured["responses_transport"] == "websocket-cached"


def test_acp_fork_inherits_runtime_before_first_build_and_after_reload(
    monkeypatch, tmp_path
):
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_agent = _runtime_agent()
    manager = SessionManager(agent_factory=lambda: parent_agent, db=db)
    parent = manager.create_session(cwd="/tmp/project")
    parent.history.append({"role": "user", "content": "private parent context"})
    first_build = {}

    def build_child(**kwargs):
        first_build.update(kwargs)
        provider = kwargs.get("requested_provider") or "openrouter"
        transport = kwargs.get("responses_transport") or "sse"
        return _runtime_agent(transport=transport, provider=provider)

    monkeypatch.setattr(manager, "_make_agent", build_child)
    fork = manager.fork_session(parent.session_id, cwd="/tmp/project")

    assert fork is not None
    assert first_build["requested_provider"] == "openai-codex"
    assert first_build["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert first_build["api_mode"] == "codex_responses"
    assert first_build["responses_transport"] == "websocket-cached"

    persisted = json.loads(db.get_session(fork.session_id)["model_config"])
    assert persisted["requested_provider"] == "openai-codex"
    assert persisted["provider"] == "openai-codex"
    assert persisted["responses_transport"] == "websocket-cached"

    reloaded = SessionManager(db=db)
    reload_build = {}

    def restore_child(**kwargs):
        reload_build.update(kwargs)
        return _runtime_agent(
            transport=kwargs.get("responses_transport") or "sse",
            provider=kwargs.get("requested_provider") or "openrouter",
        )

    monkeypatch.setattr(reloaded, "_make_agent", restore_child)
    restored = reloaded.get_session(fork.session_id)

    assert restored is not None
    assert reload_build["requested_provider"] == "openai-codex"
    assert reload_build["responses_transport"] == "websocket-cached"


def test_acp_agent_builder_prefers_explicit_session_transport(monkeypatch):
    captured = {}

    class CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def module(name, **attrs):
        result = ModuleType(name)
        for key, value in attrs.items():
            setattr(result, key, value)
        return result

    monkeypatch.setitem(sys.modules, "run_agent", module("run_agent", AIAgent=CapturingAgent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        module(
            "hermes_cli.config",
            load_config=lambda: {"model": {"default": "gpt-5.4", "provider": "openai-codex"}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        module(
            "hermes_cli.runtime_provider",
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "openai-codex",
                "api_mode": "codex_responses",
                "responses_transport": "sse",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_key": "fresh-secret",
                "command": None,
                "args": [],
            },
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.mcp_startup",
        module(
            "hermes_cli.mcp_startup",
            ensure_mcp_discovery_before_agent_build=lambda **_kwargs: None,
        ),
    )

    manager = SessionManager(db=RecordingDb())
    manager._make_agent(
        session_id="acp-restored",
        cwd="/tmp/project",
        requested_provider="openai-codex",
        responses_transport="websocket-cached",
    )

    assert captured["responses_transport"] == "websocket-cached"


class SwitchingManager:
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.saved = []

    def get_session(self, session_id):
        return self.state if session_id == self.state.session_id else None

    def _make_agent(self, **kwargs):
        self.calls.append(kwargs)
        provider = kwargs["requested_provider"]
        transport = kwargs.get("responses_transport") or {
            "openai-codex": "websocket",
            "openrouter": "sse",
        }[provider]
        return _runtime_agent(transport=transport, provider=provider)

    def save_session(self, session_id):
        self.saved.append(session_id)


@pytest.mark.asyncio
async def test_set_session_model_preserves_transport_within_provider(monkeypatch):
    state = SimpleNamespace(
        session_id="acp-session",
        cwd="/tmp/project",
        model="gpt-5.3",
        agent=_runtime_agent(transport="websocket-cached"),
    )
    manager = SwitchingManager(state)
    acp_agent = HermesACPAgent(session_manager=manager)
    monkeypatch.setattr(
        acp_agent,
        "_resolve_model_selection",
        lambda _model, _provider: ("openai-codex", "gpt-5.4"),
    )

    await acp_agent.set_session_model("openai-codex:gpt-5.4", state.session_id)

    assert manager.calls[-1]["responses_transport"] == "websocket-cached"
    assert state.agent.responses_transport == "websocket-cached"


@pytest.mark.asyncio
async def test_set_session_model_uses_target_provider_runtime_transport(monkeypatch):
    state = SimpleNamespace(
        session_id="acp-session",
        cwd="/tmp/project",
        model="old-model",
        agent=_runtime_agent(transport="websocket-cached", provider="openai-codex"),
    )
    manager = SwitchingManager(state)
    acp_agent = HermesACPAgent(session_manager=manager)
    monkeypatch.setattr(
        acp_agent,
        "_resolve_model_selection",
        lambda _model, _provider: ("openrouter", "anthropic/claude-opus-4.6"),
    )

    await acp_agent.set_session_model("openrouter:anthropic/claude-opus-4.6", state.session_id)

    assert manager.calls[-1]["responses_transport"] is None
    assert state.agent.responses_transport == "sse"


def test_slash_model_preserves_transport_within_provider(monkeypatch):
    state = SimpleNamespace(
        session_id="acp-session",
        cwd="/tmp/project",
        model="gpt-5.3",
        agent=_runtime_agent(transport="websocket-cached"),
    )
    manager = SwitchingManager(state)
    acp_agent = HermesACPAgent(session_manager=manager)
    monkeypatch.setattr(
        acp_agent,
        "_resolve_model_selection",
        lambda _model, _provider: ("openai-codex", "gpt-5.4"),
    )

    acp_agent._cmd_model("gpt-5.4", state)

    assert manager.calls[-1]["responses_transport"] == "websocket-cached"
    assert state.agent.responses_transport == "websocket-cached"
