"""Behavior contracts for supervised sequential tool execution (#84719)."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from run_agent import AIAgent


def _tool_call(name: str, arguments: dict | None = None, call_id: str = "call-1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments or {}),
        ),
    )


@pytest.fixture()
def agent():
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance.client = MagicMock()
    instance._flush_messages_to_session_db = MagicMock(return_value=True)
    instance.sequential_tool_timeout_s = 0.5
    instance.max_abandoned_tool_workers = 2
    return instance


@pytest.mark.parametrize("configured", ["nan", "inf", "-inf"])
def test_non_finite_sequential_timeout_falls_back_to_finite_default(configured):
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value={"agent": {"sequential_tool_timeout": configured}},
    ):
        instance = AIAgent(api_key="test", base_url="https://example.test/v1")

    assert instance.sequential_tool_timeout_s == 420.0


def test_native_infinite_abandoned_worker_cap_falls_back_to_default():
    with patch(
        "hermes_cli.config.load_config_readonly",
        return_value={"agent": {"max_abandoned_tool_workers": float("inf")}},
    ):
        instance = AIAgent(api_key="test", base_url="https://example.test/v1")

    assert instance.max_abandoned_tool_workers == 2


def _run_in_daemon(agent: AIAgent, tool_call, messages: list[dict]):
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    failure: list[BaseException] = []

    def _target():
        try:
            agent._execute_tool_calls_sequential(
                assistant_message,
                messages,
                "task-deadline",
            )
        except BaseException as exc:  # pragma: no cover - asserted by caller
            failure.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, failure


def test_hung_single_tool_returns_timeout_and_preserves_tool_pairing(agent):
    release = threading.Event()
    started = threading.Event()
    messages: list[dict] = []
    agent._subdirectory_hints.check_tool_call = MagicMock(
        return_value="\n<context-hint>preserved</context-hint>"
    )

    def _hung(*args, **kwargs):
        started.set()
        release.wait()
        return "late result"

    with patch("run_agent.handle_function_call", side_effect=_hung):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        assert started.wait(timeout=1)
        thread.join(timeout=1.5)

    release.set()
    assert not thread.is_alive(), "a single hung tool must not wedge the turn"
    assert failure == []
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call-1"
    assert messages[0]["effect_disposition"] == "unknown"
    assert "timed out" in messages[0]["content"]
    assert "<context-hint>preserved</context-hint>" in messages[0]["content"].lower()


def test_stop_interrupts_sequential_waiter_without_waiting_for_deadline(agent):
    agent.sequential_tool_timeout_s = 30.0
    release = threading.Event()
    started = threading.Event()
    messages: list[dict] = []

    def _hung(*args, **kwargs):
        started.set()
        release.wait()
        return "late result"

    with patch("run_agent.handle_function_call", side_effect=_hung):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        assert started.wait(timeout=1)
        before = time.monotonic()
        agent.interrupt("user requested stop")
        thread.join(timeout=1)
        elapsed = time.monotonic() - before

    release.set()
    assert not thread.is_alive()
    assert elapsed < 1
    assert failure == []
    assert "cancelled" in messages[0]["content"].lower()
    assert messages[0]["effect_disposition"] == "unknown"


def test_setup_mcp_human_wait_is_not_charged_to_tool_deadline(agent):
    release = threading.Event()
    started = threading.Event()
    messages: list[dict] = []

    def _wait_for_user(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        return json.dumps({"status": "installed", "server": "linear"})

    agent.setup_mcp_callback = _wait_for_user
    thread, failure = _run_in_daemon(
        agent,
        _tool_call("setup_mcp", {"server": "linear"}),
        messages,
    )
    assert started.wait(timeout=1)
    time.sleep(0.1)
    assert thread.is_alive(), "human consent must use its own lifecycle timeout"

    release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert failure == []
    assert "installed" in messages[0]["content"]
    assert "effect_disposition" not in messages[0]


def test_setup_mcp_human_wait_is_not_charged_retroactively(agent):
    agent.sequential_tool_timeout_s = 0.15
    started = threading.Event()
    messages: list[dict] = []

    def _wait_for_user(*args, **kwargs):
        started.set()
        time.sleep(0.35)
        return json.dumps({"status": "installed", "server": "linear"})

    agent.setup_mcp_callback = _wait_for_user
    thread, failure = _run_in_daemon(
        agent,
        _tool_call("setup_mcp", {"server": "linear"}),
        messages,
    )
    assert started.wait(timeout=1)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert failure == []
    assert "installed" in messages[0]["content"]
    assert "timed out" not in messages[0]["content"]


def test_human_wait_extends_deadline_for_post_handler_processing(agent):
    from agent.tool_executor import (
        _ConcurrentToolAuthorizationGate,
        _SequentialToolSupervisor,
    )

    agent.sequential_tool_timeout_s = 0.15
    supervisor = _SequentialToolSupervisor(
        agent,
        function_name="clarify",
        authorization_gate=_ConcurrentToolAuthorizationGate(),
        suspend_deadline_after_dispatch=True,
    )

    def _callback():
        supervisor.begin_execution()
        time.sleep(0.3)
        supervisor.mark_execution_terminal()
        time.sleep(0.05)
        return "answered"

    assert supervisor.run(_callback) == "answered"


def test_timeout_before_dispatch_fences_late_middleware_callback(agent, monkeypatch):
    from hermes_cli import middleware

    release = threading.Event()
    middleware_entered = threading.Event()
    dispatched = threading.Event()
    messages: list[dict] = []

    def _late_middleware(
        function_name,
        function_args,
        callback,
        **kwargs,
    ):
        middleware_entered.set()
        release.wait(timeout=2)
        return callback(function_args)

    monkeypatch.setattr(middleware, "run_tool_execution_middleware", _late_middleware)

    def _dispatch(*args, **kwargs):
        dispatched.set()
        return "must not run"

    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        assert middleware_entered.wait(timeout=1)
        thread.join(timeout=1.5)
        assert not thread.is_alive()
        release.set()
        time.sleep(0.1)

    assert failure == []
    assert not dispatched.is_set(), "an abandoned pre-dispatch worker must stay fenced"
    assert messages[0]["effect_disposition"] == "none"


def test_abandoned_worker_budget_prevents_unbounded_thread_growth(agent):
    agent.max_abandoned_tool_workers = 1
    first_release = threading.Event()
    first_started = threading.Event()
    calls = 0

    def _dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            first_release.wait()
        return "ok"

    first_messages: list[dict] = []
    second_messages: list[dict] = []
    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        first, first_failure = _run_in_daemon(
            agent,
            _tool_call("web_search", call_id="first"),
            first_messages,
        )
        assert first_started.wait(timeout=1)
        first.join(timeout=1.5)
        assert not first.is_alive()

        second, second_failure = _run_in_daemon(
            agent,
            _tool_call("web_search", call_id="second"),
            second_messages,
        )
        second.join(timeout=1.5)

    first_release.set()
    assert first_failure == []
    assert second_failure == []
    assert not second.is_alive()
    assert calls == 1
    assert "worker capacity" in second_messages[0]["content"].lower()
    assert second_messages[0]["effect_disposition"] == "none"


def test_abandoned_worker_budget_is_reserved_atomically(agent):
    from agent.tool_executor import (
        _ConcurrentToolAuthorizationGate,
        _SequentialToolSupervisor,
    )

    agent.max_abandoned_tool_workers = 1
    agent.sequential_tool_timeout_s = 0.15
    owners_ready = threading.Barrier(3)
    callback_release = threading.Event()
    callback_count = 0
    callback_lock = threading.Lock()
    outcomes = []

    def _callback():
        nonlocal callback_count
        with callback_lock:
            callback_count += 1
        callback_release.wait(timeout=2)
        return "late"

    def _owner():
        supervisor = _SequentialToolSupervisor(
            agent,
            function_name="web_search",
            authorization_gate=_ConcurrentToolAuthorizationGate(),
        )
        owners_ready.wait(timeout=1)
        outcomes.append(supervisor.run(_callback))

    owners = [threading.Thread(target=_owner, daemon=True) for _ in range(2)]
    for owner in owners:
        owner.start()
    owners_ready.wait(timeout=1)
    for owner in owners:
        owner.join(timeout=1)

    callback_release.set()
    assert all(not owner.is_alive() for owner in owners)
    assert callback_count == 1
    assert sorted(outcome.result.error_type for outcome in outcomes) == [
        "abandoned_worker_capacity",
        "tool_timeout",
    ]


def test_timeout_emits_one_terminal_hook_even_if_worker_returns_late(agent, monkeypatch):
    from hermes_cli import lifecycle

    release = threading.Event()
    started = threading.Event()
    post_events: list[dict] = []
    messages: list[dict] = []

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")

    def _capture(name, **kwargs):
        if name == "post_tool_call":
            post_events.append(kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _capture)

    def _late(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        from model_tools import _emit_post_tool_call_hook

        _emit_post_tool_call_hook(
            function_name="web_search",
            function_args={},
            result="late success",
            task_id="task-deadline",
            tool_call_id=kwargs.get("tool_call_id", ""),
        )
        return "late success"

    with patch("run_agent.handle_function_call", side_effect=_late):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        assert started.wait(timeout=1)
        thread.join(timeout=1.5)
        release.set()
        time.sleep(0.15)

    assert failure == []
    assert len(post_events) == 1
    assert post_events[0]["status"] == "timeout"
    assert post_events[0]["tool_call_id"] == "call-1"


def test_stuck_synthetic_post_hook_cannot_rewedge_owner(agent, monkeypatch):
    from hermes_cli import lifecycle

    tool_release = threading.Event()
    hook_release = threading.Event()
    hook_started = threading.Event()
    messages: list[dict] = []

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")

    def _hook(name, **payload):
        if name == "post_tool_call" and payload.get("status") == "timeout":
            hook_started.set()
            hook_release.wait(timeout=5)

    monkeypatch.setattr(lifecycle, "invoke_hook", _hook)

    with patch(
        "run_agent.handle_function_call",
        side_effect=lambda *a, **kw: (tool_release.wait(timeout=5) or "late"),
    ):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        assert hook_started.wait(timeout=2)
        thread.join(timeout=1.5)

    assert not thread.is_alive()
    assert failure == []
    assert messages[0]["effect_disposition"] == "unknown"
    assert "timed out" in messages[0]["content"]
    hook_release.set()
    tool_release.set()


def test_human_tool_resumes_deadline_and_detaches_slow_success_hook(agent, monkeypatch):
    from hermes_cli import lifecycle

    agent.sequential_tool_timeout_s = 1.0
    callback_called = threading.Event()

    def _clarify_callback(*args, **kwargs):
        callback_called.set()
        return "answered"

    agent.clarify_callback = _clarify_callback
    hook_release = threading.Event()
    hook_started = threading.Event()
    events: list[str] = []
    messages: list[dict] = []

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")

    def _hook(name, **payload):
        if name != "post_tool_call":
            return
        hook_started.set()
        hook_release.wait(timeout=5)
        events.append(payload["status"])

    monkeypatch.setattr(lifecycle, "invoke_hook", _hook)

    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="clarify",
            arguments=json.dumps({"question": "Continue?"}),
        ),
    )
    thread, failure = _run_in_daemon(agent, call, messages)
    assert callback_called.wait(timeout=1)
    assert hook_started.wait(timeout=1)
    thread.join(timeout=1.5)

    assert not thread.is_alive()
    assert failure == []
    assert json.loads(messages[0]["content"])["user_response"] == "answered"
    assert "timed out" not in messages[0]["content"]
    hook_release.set()
    deadline = time.monotonic() + 1
    while events != ["ok"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events == ["ok"]


def test_unknown_late_effect_skips_remaining_calls_in_same_batch(agent):
    release = threading.Event()
    first_started = threading.Event()
    dispatched: list[str] = []
    messages: list[dict] = []
    assistant_message = SimpleNamespace(
        tool_calls=[
            _tool_call("web_search", call_id="first"),
            _tool_call("web_search", call_id="second"),
        ]
    )

    def _dispatch(*args, **kwargs):
        call_id = kwargs.get("tool_call_id", "")
        dispatched.append(call_id)
        if call_id == "first":
            first_started.set()
            release.wait(timeout=2)
        return "ok"

    failure: list[BaseException] = []

    def _target():
        try:
            agent._execute_tool_calls_sequential(
                assistant_message,
                messages,
                "task-deadline",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failure.append(exc)

    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        assert first_started.wait(timeout=1)
        thread.join(timeout=1.5)

    release.set()
    assert not thread.is_alive()
    assert failure == []
    assert dispatched == ["first"]
    assert [message["tool_call_id"] for message in messages] == ["first", "second"]
    assert messages[0]["effect_disposition"] == "unknown"
    assert messages[1]["effect_disposition"] == "none"
    assert "was not started" in messages[1]["content"]


def test_normal_registry_call_keeps_inner_post_hook_ownership(agent, monkeypatch):
    from hermes_cli import lifecycle
    from model_tools import _emit_post_tool_call_hook

    post_events: list[dict] = []
    messages: list[dict] = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")
    def _capture(name, **kwargs):
        if name == "post_tool_call":
            post_events.append(kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _capture)

    def _dispatch(*args, **kwargs):
        _emit_post_tool_call_hook(
            function_name="web_search",
            function_args={},
            result="raw result",
            task_id="task-deadline",
            tool_call_id=kwargs.get("tool_call_id", ""),
        )
        return "raw result"

    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        thread.join(timeout=1.5)

    assert failure == []
    assert not thread.is_alive()
    assert len(post_events) == 1
    assert post_events[0]["result"] == "raw result"
    assert messages[0]["content"] == "raw result"


def test_human_approval_wait_is_excluded_from_deadline(agent, monkeypatch):
    from tools import approval

    agent.sequential_tool_timeout_s = 0.3
    wait_started: list[float] = []
    messages: list[dict] = []

    def _human_wait_seconds(session_key=None):
        if not wait_started:
            return 0.0
        return time.monotonic() - wait_started[0]

    monkeypatch.setattr(approval, "human_wait_seconds", _human_wait_seconds)

    def _approved_after_wait(*args, **kwargs):
        wait_started.append(time.monotonic())
        time.sleep(0.6)
        return "approved result"

    with patch("run_agent.handle_function_call", side_effect=_approved_after_wait):
        thread, failure = _run_in_daemon(agent, _tool_call("web_search"), messages)
        thread.join(timeout=1.5)

    assert not thread.is_alive()
    assert failure == []
    assert messages[0]["content"] == "approved result"
    assert "effect_disposition" not in messages[0]


def test_unknown_effect_cancels_later_segment_without_dispatch(agent, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    dispatched: list[str] = []
    messages: list[dict] = []

    def _call(name: str, arguments: dict, call_id: str):
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments),
            ),
        )

    assistant_message = SimpleNamespace(
        tool_calls=[
            _call("web_search", {"query": "first"}, "first"),
            _call("web_search", {"query": "later one"}, "later-1"),
            _call("web_search", {"query": "later two"}, "later-2"),
        ]
    )
    monkeypatch.setattr(
        "agent.tool_executor._plan_tool_batch_segments",
        lambda tool_calls, **kwargs: [
            ("sequential", [tool_calls[0]]),
            ("parallel", list(tool_calls[1:])),
        ],
    )

    def _dispatch(*args, **kwargs):
        call_id = kwargs.get("tool_call_id", "")
        dispatched.append(call_id)
        if call_id == "first":
            started.set()
            release.wait(timeout=2)
        return "ok"

    failure: list[BaseException] = []

    def _target():
        try:
            from agent.tool_executor import execute_tool_calls_segmented

            execute_tool_calls_segmented(
                agent,
                assistant_message,
                messages,
                "task-deadline",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failure.append(exc)

    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        assert started.wait(timeout=1)
        thread.join(timeout=1.5)

    release.set()
    assert failure == []
    assert not thread.is_alive()
    assert dispatched == ["first"]
    assert [message["tool_call_id"] for message in messages] == [
        "first",
        "later-1",
        "later-2",
    ]
    assert messages[0]["effect_disposition"] == "unknown"
    assert all(message["effect_disposition"] == "none" for message in messages[1:])


def test_effectful_abandoned_worker_quarantines_later_mutations(agent):
    agent.max_abandoned_tool_workers = 2
    release = threading.Event()
    started = threading.Event()
    dispatched: list[str] = []

    def _call(name: str, arguments: dict, call_id: str):
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )

    calls = [
        _call("terminal", {"command": "pwd"}, "hung-effect"),
        _call("web_search", {"query": "diagnose"}, "safe-read"),
        _call("write_file", {"path": "later.txt", "content": "x"}, "mutation"),
    ]
    results: list[list[dict]] = [[], []]

    def _dispatch(*args, **kwargs):
        call_id = kwargs.get("tool_call_id", "")
        dispatched.append(call_id)
        if call_id == "hung-effect":
            started.set()
            release.wait(timeout=3)
        return "ok"

    with patch("run_agent.handle_function_call", side_effect=_dispatch):
        first, first_failure = _run_in_daemon(agent, calls[0], results[0])
        assert started.wait(timeout=1)
        first.join(timeout=1.5)
        assert not first.is_alive()

        agent._execute_tool_calls(
            SimpleNamespace(tool_calls=calls[1:]),
            results[1],
            "task-deadline",
        )

    release.set()
    assert first_failure == []
    assert dispatched == ["hung-effect", "safe-read"]
    assert results[0][0]["effect_disposition"] == "unknown"
    assert results[1][0]["content"] == "ok"
    assert results[1][1]["effect_disposition"] == "none"
    assert "still running after timeout" in results[1][1]["content"]
