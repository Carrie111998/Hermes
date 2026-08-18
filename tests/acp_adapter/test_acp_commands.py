import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self._supports_active_turn_redirect = True
        self.steers = []
        self.redirects = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        return SimpleNamespace(outcome="allow")


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None

    def update_session_meta(self, *_args, **_kwargs):
        return None

    def replace_messages(self, *_args, **_kwargs):
        return None


class RecordingDb(NoopDb):
    def __init__(self):
        self.session: dict[str, Any] | None = None

    def get_session(self, *_args, **_kwargs):
        return self.session

    def create_session(self, *, session_id, model, model_config, **_kwargs):
        self.session = {
            "id": session_id,
            "model": model,
            "model_config": dict(model_config),
        }

    def update_session_meta(self, _session_id, model_config, model):
        assert self.session is not None
        self.session["model"] = model
        self.session["model_config"] = json.loads(model_config)


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


@pytest.fixture
def openrouter_collision(monkeypatch):
    from hermes_cli import models

    catalog = [
        ("collision-vendor/provider-collision-model", ""),
        ("collision-vendor/provider-collision-model:free", ""),
    ]
    monkeypatch.setattr(models, "fetch_openrouter_models", lambda **_kwargs: catalog)


@pytest.mark.parametrize(
    ("raw_model", "current_provider", "expected"),
    [
        (
            "anthropic:provider-collision-model",
            "anthropic",
            ("anthropic", "provider-collision-model"),
        ),
        (
            "anthropic:provider-collision-model",
            "google",
            ("anthropic", "provider-collision-model"),
        ),
        (
            "claude:provider-collision-model",
            "anthropic",
            ("anthropic", "provider-collision-model"),
        ),
        (
            "custom:local:provider-collision-model",
            "anthropic",
            ("custom:local", "provider-collision-model"),
        ),
        (
            "anthropic/provider-collision-model",
            "anthropic",
            ("anthropic", "provider-collision-model"),
        ),
        (
            "openrouter:collision-vendor/provider-collision-model",
            "anthropic",
            ("openrouter", "collision-vendor/provider-collision-model"),
        ),
        (
            "collision-vendor/provider-collision-model",
            "anthropic",
            ("openrouter", "collision-vendor/provider-collision-model"),
        ),
        (
            "provider-collision-model",
            "anthropic",
            ("openrouter", "collision-vendor/provider-collision-model"),
        ),
        (
            "provider-collision-model:free",
            "anthropic",
            ("openrouter", "collision-vendor/provider-collision-model:free"),
        ),
    ],
)
def test_acp_model_selection_uses_explicit_provider_before_detection(
    openrouter_collision,
    raw_model,
    current_provider,
    expected,
):
    """Recognized providers remain authoritative across the real detection chain."""
    assert (
        HermesACPAgent._resolve_model_selection(raw_model, current_provider) == expected
    )


@pytest.mark.asyncio
async def test_acp_set_model_persists_explicit_provider_without_turn(
    monkeypatch,
    openrouter_collision,
):
    initial_agent = FakeAgent()
    initial_agent.provider = "anthropic"
    initial_agent.model = "old-model"
    database = RecordingDb()
    manager = SessionManager(agent_factory=lambda: initial_agent, db=database)
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    rebuild = {}

    def rebuild_agent(**kwargs):
        rebuild.update(kwargs)
        agent = FakeAgent()
        agent.provider = kwargs["requested_provider"]
        agent.model = kwargs["model"]
        return agent

    monkeypatch.setattr(manager, "_make_agent", rebuild_agent)

    response = await acp_agent.set_session_model(
        "anthropic:provider-collision-model",
        state.session_id,
    )

    assert response is not None
    assert rebuild["requested_provider"] == "anthropic"
    assert rebuild["model"] == "provider-collision-model"
    assert state.agent.provider == "anthropic"
    assert state.model == "provider-collision-model"
    assert database.session is not None
    assert database.session["model"] == "provider-collision-model"
    assert database.session["model_config"]["provider"] == "anthropic"
    assert initial_agent.runs == []
    assert state.agent.runs == []


def test_acp_real_agent_gets_session_db_for_recall(monkeypatch):
    """ACP sessions persist to SessionDB; recall must receive the same DB handle."""
    captured = {}
    sentinel_db = NoopDb()

    class CapturingAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    def mod(name, **attrs):
        module = ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    monkeypatch.setitem(sys.modules, "run_agent", mod("run_agent", AIAgent=CapturingAgent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        mod("hermes_cli.config", load_config=lambda: {"model": {"default": "m", "provider": "p"}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        mod(
            "hermes_cli.runtime_provider",
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "p",
                "api_mode": "chat_completions",
                "base_url": "u",
                "api_key": "k",
                "command": None,
                "args": [],
            },
        ),
    )

    manager = SessionManager(db=sentinel_db)
    agent = manager._make_agent(session_id="acp-session", cwd=".")

    assert isinstance(agent, CapturingAgent)
    assert captured["session_db"] is sentinel_db
    assert captured["platform"] == "acp"
    assert captured["session_id"] == "acp-session"


@pytest.mark.asyncio
async def test_acp_steer_slash_command_injects_into_running_agent():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.is_running = True

    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="/steer prefer the simpler fix")],
    )

    assert response.stop_reason == "end_turn"
    assert fake.steers == ["prefer the simpler fix"]
    assert fake.runs == []








@pytest.mark.asyncio
async def test_acp_cancel_publishes_hard_stop_while_holding_runtime_lock():
    acp_agent, state, fake, _conn = make_agent_and_state()
    state.is_running = True
    state.current_prompt_text = "original request"
    observed = {}

    def interrupt():
        acquired = state.runtime_lock.acquire(blocking=False)
        observed["lock_held"] = not acquired
        if acquired:
            state.runtime_lock.release()

    fake.interrupt = interrupt

    await acp_agent.cancel(state.session_id)

    assert observed["lock_held"] is True
    assert state.cancel_event.is_set()
    assert state.interrupted_prompt_text == "original request"



