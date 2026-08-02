"""Regression tests for #52374 — raw clarify tool-call JSON must never leak
into the chat as a tool-progress bubble.

The adapter's ``send_clarify`` is the user-facing rendering of a clarify
prompt (interactive buttons, or the numbered-text fallback).  The gateway's
tool-progress callback used to also render a progress bubble for the
``clarify`` tool.started event — in verbose mode that bubble contains the raw
tool-call args JSON (``{"question": ..., "choices": [...]}``), and because the
progress queue drains on a background task the JSON landed right underneath
the rendered interactive prompt on Slack.
"""

import importlib
import sys
import time
import types

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class ProgressCaptureAdapter(BasePlatformAdapter):
    """Records every send so the test can assert nothing leaked."""

    def __init__(self, platform=Platform.SLACK):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.clarify_prompts = []
        self.clarify_response = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="m-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def send_clarify(
        self,
        chat_id,
        question,
        choices,
        clarify_id,
        session_key,
        metadata=None,
    ) -> SendResult:
        self.clarify_prompts.append(
            {
                "chat_id": chat_id,
                "question": question,
                "choices": choices,
            }
        )
        if self.clarify_response is not None:
            from tools.clarify_gateway import resolve_gateway_clarify

            assert resolve_gateway_clarify(clarify_id, self.clarify_response)
        return SendResult(success=True, message_id="clarify-1")


class FailingClarifyAdapter(ProgressCaptureAdapter):
    """Fails delivery so the gateway must cancel only the generated request."""

    async def send_clarify(
        self,
        chat_id,
        question,
        choices,
        clarify_id,
        session_key,
        metadata=None,
    ) -> SendResult:
        self.clarify_prompts.append(
            {
                "chat_id": chat_id,
                "question": question,
                "choices": choices,
            }
        )
        return SendResult(success=False, error="delivery failed")


class ResolveThenFailClarifyAdapter(ProgressCaptureAdapter):
    """Resolves first, then reports a failed/timed-out delivery future."""

    def __init__(self, *, raise_timeout: bool):
        super().__init__(platform=Platform.DISCORD)
        self.raise_timeout = raise_timeout

    async def send_clarify(
        self,
        chat_id,
        question,
        choices,
        clarify_id,
        session_key,
        metadata=None,
        *,
        generation=None,
        responder_id=None,
    ) -> SendResult:
        from tools.clarify_gateway import resolve_gateway_clarify

        assert resolve_gateway_clarify(
            clarify_id,
            "winner",
            session_key=session_key,
            generation=generation,
            responder_id=responder_id,
        )
        if self.raise_timeout:
            raise TimeoutError("delivery future timed out after callback")
        return SendResult(success=False, error="late delivery failure")


class ClarifyThenToolAgent:
    """Emits a clarify tool.started (with raw args) then a normal tool."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb(
                "tool.started",
                "clarify",
                "Which environment?",
                {"question": "Which environment?", "choices": ["staging", "production"]},
            )
            time.sleep(0.35)
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class RoutineClarifyAgent:
    """Calls clarify for a decision the blockers-only policy must own."""

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        answer = self.clarify_callback(
            "Should I inspect the patch and run the focused tests?",
            ["Proceed", "Stop"],
        )
        return {
            "final_response": answer,
            "messages": [],
            "api_calls": 1,
        }


class DeployClarifyAgent:
    """Calls clarify for production authority the policy must not assume."""

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        answer = self.clarify_callback(
            "Should I merge and deploy this to production?",
            ["Deploy to production", "Cancel"],
        )
        return {
            "final_response": answer,
            "messages": [],
            "api_calls": 1,
        }


class FailedDeliveryScopeAgent:
    """Observes whether a sibling request survived clarify send failure."""

    sibling_session_key = ""

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        answer = self.clarify_callback("Pick one", ["A", "B"])
        from tools import clarify_gateway

        sibling_survived = clarify_gateway.has_pending(self.sibling_session_key)
        return {
            "final_response": f"{answer}|sibling={sibling_survived}",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter, hermes_home):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
    from tests.gateway._profile_authority import install_frozen_profile_authority

    install_frozen_profile_authority(runner, hermes_home)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    runner.config = types.SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _install_fakes(monkeypatch, mode):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", mode)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ClarifyThenToolAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register terminal emoji

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    return gateway_run


@pytest.mark.parametrize("mode", ["verbose", "all"])
@pytest.mark.asyncio
async def test_clarify_tool_never_renders_progress_bubble(monkeypatch, tmp_path, mode):
    """No progress bubble for clarify — in any mode, especially verbose.

    Verbose mode used to dump the raw args JSON
    (``{"question": ..., "choices": [...]}``) into the chat right under the
    interactive prompt (#52374).
    """
    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter, tmp_path)
    gateway_run = _install_fakes(monkeypatch, mode)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="dm")

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-clarify-leak",
        session_key="agent:main:slack:dm:C1",
    )

    assert result["final_response"] == "done"
    all_content = "\n".join(
        [m["content"] for m in adapter.sent] + [e["content"] for e in adapter.edits]
    )
    # Raw clarify args JSON must not leak anywhere.
    assert '"question"' not in all_content
    assert '"choices"' not in all_content
    assert "Which environment?" not in all_content
    # No clarify progress line at all (verb "Asking" / tool name).
    assert "clarify" not in all_content
    assert "Asking" not in all_content
    # The unrelated terminal tool still renders progress normally.
    assert "pwd" in all_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_type", "response", "question", "choices"),
    [
        (
            RoutineClarifyAgent,
            "Proceed",
            "Should I inspect the patch and run the focused tests?",
            ["Proceed", "Stop"],
        ),
        (
            DeployClarifyAgent,
            "Cancel",
            "Should I merge and deploy this to production?",
            ["Deploy to production", "Cancel"],
        ),
    ],
)
async def test_gateway_never_classifies_clarify_question_text(
    monkeypatch,
    tmp_path,
    agent_type,
    response,
    question,
    choices,
):
    """Every model-authored clarify call reaches the user unchanged."""

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    adapter.clarify_identity_version = "malformed-third-party-capability"
    adapter.clarify_response = response
    runner = _make_runner(adapter, tmp_path)
    gateway_run = _install_fakes(monkeypatch, "off")

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_type
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    session_key = "agent:main:discord:dm:C1"
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="C1",
        chat_type="dm",
    )
    run_generation = runner._begin_session_run_generation(session_key)
    result = await runner._run_agent(
        message="finish the task",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-llm-semantic-authority",
        session_key=session_key,
        run_generation=run_generation,
    )

    assert result["final_response"] == response
    assert adapter.clarify_prompts == [
        {
            "chat_id": "C1",
            "question": question,
            "choices": choices,
        }
    ]
    from tools import clarify_gateway

    assert clarify_gateway.get_pending_for_session(
        session_key,
        include_choice_prompts=True,
    ) is None


def test_gateway_generation_owner_cancels_old_prompt_and_rejects_stale_worker(
    tmp_path,
):
    from tools import clarify_gateway

    with clarify_gateway._lock:
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()
        clarify_gateway._current_generations.clear()

    runner = _make_runner(ProgressCaptureAdapter(), tmp_path)
    session_key = "agent:main:slack:dm:generation-owner"
    first = runner._begin_session_run_generation(session_key)
    clarify_gateway.register(
        "old-generation",
        session_key,
        "Old?",
        ["A"],
        generation=first,
        identity_v1=True,
    )

    second = runner._begin_session_run_generation(session_key)
    assert second == first + 1
    assert clarify_gateway.wait_for_response(
        "old-generation",
        timeout=0.01,
        session_key=session_key,
        generation=first,
    ) is None
    with pytest.raises(ValueError, match="stale clarify generation"):
        clarify_gateway.register(
            "late-old-worker",
            session_key,
            "Late?",
            ["A"],
            generation=first,
            identity_v1=True,
        )


@pytest.mark.asyncio
async def test_failed_clarify_delivery_cancels_only_exact_request(
    monkeypatch,
    tmp_path,
):
    """A failed send cannot clear another pending prompt in the same session."""
    from tools import clarify_gateway

    with clarify_gateway._lock:
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()

    adapter = FailingClarifyAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter, tmp_path)
    gateway_run = _install_fakes(monkeypatch, "off")

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FailedDeliveryScopeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    session_key = "agent:main:discord:dm:C1"
    FailedDeliveryScopeAgent.sibling_session_key = session_key
    clarify_gateway.register(
        "existing-sibling",
        session_key,
        "Existing question?",
        ["Keep waiting"],
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="C1",
        chat_type="dm",
        user_id="user-1",
    )

    result = await runner._run_agent(
        message="start",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-failed-clarify-delivery",
        session_key=session_key,
    )

    assert result["final_response"] == (
        "[clarify prompt could not be delivered]|sibling=True"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_timeout", [False, True])
async def test_resolver_winner_survives_late_delivery_failure_without_leak(
    monkeypatch,
    tmp_path,
    raise_timeout,
):
    """A callback answer wins even if the send future later fails or times out."""
    from tools import clarify_gateway

    with clarify_gateway._lock:
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()
        clarify_gateway._current_generations.clear()

    adapter = ResolveThenFailClarifyAdapter(raise_timeout=raise_timeout)
    runner = _make_runner(adapter, tmp_path)
    gateway_run = _install_fakes(monkeypatch, "off")

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RoutineClarifyAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    session_key = "agent:main:discord:dm:C-race"
    run_generation = runner._begin_session_run_generation(session_key)
    result = await runner._run_agent(
        message="start",
        context_prompt="",
        history=[],
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="C-race",
            chat_type="dm",
            user_id="user-1",
        ),
        session_id="sess-resolve-delivery-race",
        session_key=session_key,
        run_generation=run_generation,
    )

    assert result["final_response"] == "winner"
    with clarify_gateway._lock:
        assert not clarify_gateway._entries
        assert session_key not in clarify_gateway._session_index
