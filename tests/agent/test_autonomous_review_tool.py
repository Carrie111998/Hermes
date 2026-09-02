"""Focused tests for the parent-only autonomous review invocation bridge."""

import json
from types import SimpleNamespace
from agent import review_engine as review_engine_module
from agent.tool_executor import execute_tool_calls_segmented, execute_tool_calls_sequential
from model_tools import get_tool_definitions
from tools import delegate_tool


def _call(name, arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call-{name}",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_review_tool_is_parent_visible_but_child_blocked():
    parent_names = {
        item["function"]["name"]
        for item in get_tool_definitions(quiet_mode=True)
    }
    assert "review_current_work" in parent_names

    for role in ("leaf", "orchestrator"):
        blocked = delegate_tool._blocked_toolsets_for_role(role)
        assert "review" in blocked
        child_names = {
            item["function"]["name"]
            for item in get_tool_definitions(
                disabled_toolsets=blocked,
                quiet_mode=True,
            )
        }
        assert "review_current_work" not in child_names

    from run_agent import AIAgent

    child = object.__new__(AIAgent)
    setattr(child, "_delegate_depth", 1)
    child_result = json.loads(
        AIAgent._dispatch_review_current_work(child, {}, [])
    )
    assert child_result["status"] == "error"
    assert "parent agent" in child_result["error"]


def test_review_dispatch_uses_auxiliary_review_model_only(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        review_engine_module,
        "_load_review_credentials_cfg",
        lambda: {
            "provider": "openai-codex",
            "model": "gpt-5.6-terra",
            "base_url": "",
            "api_key": "",
            "api_mode": "",
        },
    )

    def fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": "review-1"})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate)
    parent = SimpleNamespace(_delegate_depth=0)
    result = review_engine_module.start_review(
        parent,
        [{"role": "user", "content": "review this"}],
        autonomous=True,
    )

    assert result["status"] == "dispatched"
    assert captured["credentials_cfg"]["provider"] == "openai-codex"
    assert captured["credentials_cfg"]["model"] == "gpt-5.6-terra"
    assert "reasoning_effort" not in captured["credentials_cfg"]
    assert "toolsets" not in captured
    assert "review_current_work tool" in captured["context"]


def test_normal_delegate_task_does_not_receive_review_configuration(monkeypatch):
    captured = {}

    def fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched"})

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate)
    from run_agent import AIAgent

    parent = object.__new__(AIAgent)
    setattr(parent, "_delegate_depth", 0)
    AIAgent._dispatch_delegate_task(parent, {"goal": "ordinary subtask"})

    assert captured["goal"] == "ordinary subtask"
    assert captured["background"] is True
    assert captured["parent_agent"] is parent
    assert "credentials_cfg" not in captured
    assert "review_source_identity" not in captured


def test_async_review_sets_boundary_but_sync_fallback_does_not(monkeypatch):
    from run_agent import AIAgent

    def fake_start_review(*args, **kwargs):
        return {"status": "dispatched", "delegation_id": "review-async"}

    monkeypatch.setattr(review_engine_module, "start_review", fake_start_review)
    from run_agent import AIAgent

    async_parent = object.__new__(AIAgent)
    setattr(async_parent, "_delegate_depth", 0)
    async_result = json.loads(
        AIAgent._dispatch_review_current_work(
            async_parent,
            {"focus": "security"},
            [{"role": "user", "content": "review"}],
        )
    )
    assert async_result["status"] == "dispatched"
    assert async_parent._review_yield_requested is True

    monkeypatch.setattr(
        review_engine_module,
        "start_review",
        lambda *args, **kwargs: {
            "status": "completed",
            "results": [{"summary": "review complete"}],
        },
    )
    sync_parent = object.__new__(AIAgent)
    setattr(sync_parent, "_delegate_depth", 0)
    sync_result = json.loads(
        AIAgent._dispatch_review_current_work(
            sync_parent,
            {},
            [{"role": "user", "content": "review"}],
        )
    )
    assert sync_result["status"] == "completed"
    assert not hasattr(sync_parent, "_review_yield_requested")


def test_review_dispatch_converts_unexpected_start_errors_to_tool_error(monkeypatch):
    from run_agent import AIAgent

    def fail_start_review(*args, **kwargs):
        raise RuntimeError("review backend unavailable")

    monkeypatch.setattr(review_engine_module, "start_review", fail_start_review)

    parent = object.__new__(AIAgent)
    setattr(parent, "_delegate_depth", 0)
    result = json.loads(
        AIAgent._dispatch_review_current_work(
            parent,
            {},
            [{"role": "user", "content": "review"}],
        )
    )

    assert result == {
        "status": "error",
        "error": "review backend unavailable",
    }


def test_segmented_batch_cancels_every_segment_after_review(monkeypatch):
    from agent import tool_executor as executor
    from agent.tool_dispatch_helpers import _plan_tool_batch_segments

    review = _call("review_current_work")
    later = _call("write_file", '{"path":"later.txt","content":"x"}')
    planned = _plan_tool_batch_segments([review, later])
    assert len(planned) == 1
    assert planned[0][0] == "sequential"
    assert planned[0][1] == [review, later]
    agent = SimpleNamespace(_review_yield_requested=False, _incremental_persistence_failed=False)
    messages = []
    cancelled = []
    executed = []

    def fake_sequential(*args, **kwargs):
        executed.append("review")
        agent._review_yield_requested = True

    def fail_concurrent(*args, **kwargs):
        executed.append("later")

    monkeypatch.setattr(executor, "execute_tool_calls_sequential", fake_sequential)
    monkeypatch.setattr(executor, "execute_tool_calls_concurrent", fail_concurrent)
    monkeypatch.setattr(
        executor,
        "_append_review_yield_cancellations",
        lambda _agent, calls, _messages, _task_id: cancelled.extend(calls) or True,
    )

    execute_tool_calls_segmented(
        agent,
        SimpleNamespace(tool_calls=[review, later]),
        messages,
        "task",
        segments=[("sequential", [review]), ("parallel", [later])],
    )

    assert executed == ["review"]
    assert cancelled == [later]


def test_sequential_review_boundary_skips_later_calls(monkeypatch):
    from agent import tool_executor as executor

    review = _call("review_current_work")
    later = _call("patch", '{"path":"later.py","old_string":"a","new_string":"b"}')
    agent = SimpleNamespace(_review_yield_requested=True, _incremental_persistence_failed=False)
    messages = []
    cancelled = []

    monkeypatch.setattr(executor, "_budget_for_agent", lambda _agent: None)
    monkeypatch.setattr(executor, "_append_review_yield_cancellations", lambda _agent, calls, _messages, _task_id: cancelled.extend(calls) or True)

    execute_tool_calls_sequential(
        agent,
        SimpleNamespace(tool_calls=[review, later]),
        messages,
        "task",
    )

    assert cancelled == [review, later]


def test_default_review_briefing_remains_slash_review_compatible():
    _, context = review_engine_module.build_review_task(
        [{"role": "user", "text": "review"}],
    )
    assert "You were spawned by the /review command." in context
    assert "review_current_work tool" not in context


def test_autonomous_review_briefing_is_distinct_from_slash_review():
    _, context = review_engine_module.build_review_task(
        [{"role": "user", "text": "review"}],
        autonomous=True,
    )
    assert "You were spawned by the Hermes review_current_work tool." in context
    assert "You were spawned by the /review command." not in context
