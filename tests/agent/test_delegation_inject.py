"""Behavioral contracts for same-turn async delegation injection."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import threading
import time
from typing import Any
import uuid

import pytest

from agent.delegation_inject import (
    acknowledge_pending_injects,
    drain_ready_injects,
    reconcile_provisional_final,
    release_pending_injects,
)
from tools import async_delegation as ad
from tools import delegate_tool
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_async_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _record(*, goals=("audit",), turn_id="turn-current", delivery="inject"):
    delegation_id = f"deleg_test_{uuid.uuid4().hex}"
    record = {
        "delegation_id": delegation_id,
        "goal": goals[0] if len(goals) == 1 else f"{len(goals)} tasks",
        "goals": list(goals),
        "context": "parent context",
        "toolsets": ["file"],
        "role": "leaf",
        "model": "child-model",
        "session_key": "agent:main:cli:dm:local",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": "parent-session",
        "parent_turn_id": turn_id,
        "status": "running",
        "dispatched_at": time.time(),
        "completed_at": None,
        "is_batch": True,
        "result_delivery": delivery,
    }
    with ad._records_lock:
        ad._records[delegation_id] = record
    ad._persist_dispatch(record)
    return delegation_id


def _child(index: int, summary: str, *, status="completed", error=None):
    return {
        "task_index": index,
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": 2,
        "duration_seconds": 0.25,
    }


def _queue_contents():
    items = []
    while not process_registry.completion_queue.empty():
        items.append(process_registry.completion_queue.get_nowait())
    for item in items:
        process_registry.completion_queue.put(item)
    return items


def _event_state(delegation_id: str, event_key: str):
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            "SELECT delivery_state, delivery_attempts FROM async_delegation_events "
            "WHERE delegation_id=? AND event_key=?",
            (delegation_id, event_key),
        ).fetchone()


def _parent_state(delegation_id: str):
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            "SELECT state, delivery_state FROM async_delegations "
            "WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()


def _durable_event_keys(delegation_id: str):
    with ad._DB_LOCK, ad._transaction() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT event_key FROM async_delegation_events "
                "WHERE delegation_id=? ORDER BY event_key",
                (delegation_id,),
            ).fetchall()
        ]


def test_inject_drain_rotates_unrelated_queue_items_and_coalesces_ready_children():
    delegation_id = _record(goals=("audit A", "audit B"))
    unrelated = {"type": "completion", "session_id": "process-1"}
    legacy = {
        "type": "async_delegation",
        "delegation_id": "legacy-after-turn",
        "result_delivery": "after_turn",
    }
    process_registry.completion_queue.put(unrelated)
    assert ad.publish_batch_child_completion(delegation_id, 0, _child(0, "finding A"))
    assert ad.publish_batch_child_completion(delegation_id, 1, _child(1, "finding B"))
    process_registry.completion_queue.put(legacy)

    messages = [
        {"role": "assistant", "tool_calls": [{"id": "tc1", "function": {}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "done"},
    ]
    agent = SimpleNamespace(_active_turn_id="turn-current")
    count = drain_ready_injects(agent, messages, "turn-current")

    assert count == 2
    assert [m["role"] for m in messages] == ["assistant", "tool", "user"]
    assert "finding A" in messages[-1]["content"]
    assert "finding B" in messages[-1]["content"]
    assert "TASK 1/2" in messages[-1]["content"]
    assert "TASK 2/2" in messages[-1]["content"]
    assert messages[-1]["_delegation_event_ids"] == [
        f"{delegation_id}:task:0",
        f"{delegation_id}:task:1",
    ]
    assert _queue_contents() == [unrelated, legacy]
    assert _event_state(delegation_id, "task:0") == ("pending", 1)
    assert _event_state(delegation_id, "task:1") == ("pending", 1)
    assert acknowledge_pending_injects(agent, turn_id="turn-current") == 2
    assert _event_state(delegation_id, "task:0") == ("delivered", 1)
    assert _event_state(delegation_id, "task:1") == ("delivered", 1)


def test_inject_drain_is_exactly_once_when_queue_contains_duplicate_event():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(delegation_id, 0, _child(0, "once"))
    event = process_registry.completion_queue.get_nowait()
    process_registry.completion_queue.put(event)
    process_registry.completion_queue.put(deepcopy(event))
    messages = [{"role": "tool", "tool_call_id": "tc", "content": "done"}]

    agent = SimpleNamespace()
    assert drain_ready_injects(agent, messages, "turn-current") == 1
    assert messages[-1]["content"].count("once") == 1
    assert process_registry.completion_queue.empty()
    assert drain_ready_injects(agent, messages, "turn-current") == 0
    assert _event_state(delegation_id, "task:0") == ("pending", 1)
    assert acknowledge_pending_injects(agent, turn_id="turn-current") == 1
    assert _event_state(delegation_id, "task:0") == ("delivered", 1)


def test_wrong_turn_and_missing_mode_are_not_injected():
    delegation_id = _record(turn_id="old-turn")
    assert ad.publish_batch_child_completion(delegation_id, 0, _child(0, "late"))
    legacy = {
        "type": "async_delegation",
        "delegation_id": "legacy",
        "parent_turn_id": "turn-current",
    }
    process_registry.completion_queue.put(legacy)
    messages = [{"role": "tool", "tool_call_id": "tc", "content": "done"}]

    assert drain_ready_injects(SimpleNamespace(), messages, "turn-current") == 0
    assert messages == [{"role": "tool", "tool_call_id": "tc", "content": "done"}]
    assert [e.get("delegation_id") for e in _queue_contents()] == [
        delegation_id,
        "legacy",
    ]
    assert _event_state(delegation_id, "task:0") == ("pending", 0)


def test_provisional_final_is_append_only_and_gets_budgeted_reconciliation():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "review changed the answer")
    )
    agent = SimpleNamespace(
        _active_turn_id="turn-current",
        _budget_grace_call=False,
        _api_call_count=1,
        max_iterations=1,
        iteration_budget=SimpleNamespace(remaining=0),
    )
    final_message = {"role": "assistant", "content": "provisional answer"}
    final_snapshot = deepcopy(final_message)
    messages = [{"role": "tool", "tool_call_id": "tc", "content": "done"}]

    assert reconcile_provisional_final(
        agent, messages, final_message, turn_id="turn-current"
    ) is True
    assert [message["role"] for message in messages] == ["tool", "assistant", "user"]
    assert messages[-2] is final_message
    assert final_message == final_snapshot
    assert "review changed the answer" in messages[-1]["content"]
    assert agent._budget_grace_call is True


def test_ready_inject_uses_normal_budget_when_available():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "ready")
    )
    agent = SimpleNamespace(
        _active_turn_id="turn-current",
        _budget_grace_call=False,
        _api_call_count=1,
        max_iterations=5,
        iteration_budget=SimpleNamespace(remaining=4),
    )
    messages = [{"role": "assistant", "content": "provisional"}]
    assert drain_ready_injects(agent, messages, "turn-current") == 1
    assert agent._budget_grace_call is False


def test_retry_tail_user_defers_new_inject_without_rewriting_history():
    delegation_id = _record(goals=("first", "second"))
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "first result")
    )
    agent = SimpleNamespace(_active_turn_id="turn-current")
    messages = [{"role": "assistant", "content": "working"}]
    assert drain_ready_injects(agent, messages, "turn-current") == 1
    snapshot = deepcopy(messages)

    assert ad.publish_batch_child_completion(
        delegation_id, 1, _child(1, "second result")
    )
    assert drain_ready_injects(agent, messages, "turn-current") == 0
    assert messages == snapshot
    assert [event["delivery_event_key"] for event in _queue_contents()] == ["task:1"]


def test_exhausted_budget_grants_only_one_reconciliation_per_turn():
    delegation_id = _record(goals=("first", "second"))
    agent = SimpleNamespace(
        _active_turn_id="turn-current",
        _budget_grace_call=False,
        _api_call_count=1,
        max_iterations=1,
        iteration_budget=SimpleNamespace(remaining=0),
    )
    messages = [{"role": "assistant", "content": "working"}]

    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "first result")
    )
    assert drain_ready_injects(agent, messages, "turn-current") == 1
    assert agent._budget_grace_call is True

    # Simulate the conversation loop consuming the one reconciliation request
    # and producing an assistant response. A child that finishes afterwards
    # must stay pending for the late/after-turn delivery path instead of being
    # acknowledged without any model request left to read it.
    agent._budget_grace_call = False
    messages.append({"role": "assistant", "content": "reconciled first result"})
    assert ad.publish_batch_child_completion(
        delegation_id, 1, _child(1, "second result")
    )

    snapshot = deepcopy(messages)
    assert drain_ready_injects(agent, messages, "turn-current") == 0
    assert messages == snapshot
    assert [event["delivery_event_key"] for event in _queue_contents()] == ["task:1"]
    assert _event_state(delegation_id, "task:1") == ("pending", 0)


def test_inject_batch_publishes_first_child_without_waiting_for_sibling():
    gate = threading.Event()
    ready = threading.Event()
    delegation_id = f"deleg_partial_{uuid.uuid4().hex}"

    def runner():
        assert ad.publish_batch_child_completion(
            delegation_id, 0, _child(0, "fast result")
        )
        ready.set()
        gate.wait(timeout=5)
        return {
            "results": [_child(0, "fast result"), _child(1, "slow result")],
            "total_duration_seconds": 0.5,
        }

    dispatched = ad.dispatch_async_delegation_batch(
        goals=["fast", "slow"],
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="agent:main:cli:dm:local",
        parent_session_id="parent",
        parent_turn_id="turn-current",
        runner=runner,
        max_async_children=1,
        delegation_id=delegation_id,
        result_delivery="inject",
    )
    assert dispatched["status"] == "dispatched"
    assert ready.wait(timeout=2)
    first = process_registry.completion_queue.get(timeout=2)
    assert first["delivery_event_key"] == "task:0"
    assert first["results"][0]["summary"] == "fast result"
    assert ad.get_durable_delegation(delegation_id)["state"] == "running"

    gate.set()
    deadline = time.monotonic() + 3
    second = None
    while time.monotonic() < deadline:
        try:
            candidate = process_registry.completion_queue.get(timeout=0.05)
        except Exception:
            continue
        if candidate.get("delivery_event_key") == "task:1":
            second = candidate
            break
    assert second is not None
    assert second["results"][0]["summary"] == "slow result"
    assert "delivery_event_key" not in {
        e.get("delivery_event_key") for e in _queue_contents()
    }
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"

    restored_queue = __import__("queue").Queue()
    assert ad.restore_undelivered_completions(restored_queue) == 2
    restored = [restored_queue.get_nowait(), restored_queue.get_nowait()]
    assert {event.get("delivery_event_key") for event in restored} == {
        "task:0",
        "task:1",
    }
    assert all(event.get("delivery_event_key") for event in restored)


def test_after_turn_remains_default_and_emits_only_combined_batch_event():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {
            "results": [_child(0, "A"), _child(1, "B")],
            "total_duration_seconds": 0.1,
        }

    dispatched = ad.dispatch_async_delegation_batch(
        goals=["A", "B"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert process_registry.completion_queue.empty()
    gate.set()
    event = process_registry.completion_queue.get(timeout=3)
    assert event["delegation_id"] == dispatched["delegation_id"]
    assert event["result_delivery"] == "after_turn"
    assert "delivery_event_key" not in event
    assert [r["summary"] for r in event["results"]] == ["A", "B"]


def test_inject_batch_finalization_rolls_back_children_if_parent_update_fails(
    monkeypatch,
):
    delegation_id = _record(goals=("A", "B"))
    with ad._records_lock:
        event_record = dict(ad._records[delegation_id])
    combined = {"results": [_child(0, "A"), _child(1, "B")]}
    parent_event = {
        **event_record,
        "type": "async_delegation",
        "status": "completed",
        "completed_at": time.time(),
    }

    def crash_before_parent_update(*_args, **_kwargs):
        raise RuntimeError("simulated crash before parent terminal update")

    monkeypatch.setattr(ad, "_update_completion_row", crash_before_parent_update)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ad._persist_inject_batch_finalization(
            event_record, parent_event, combined, "completed"
        )

    assert _durable_event_keys(delegation_id) == []
    assert _parent_state(delegation_id) == ("running", "pending")


def test_recovery_with_complete_inject_children_suppresses_parent_aggregate():
    delegation_id = _record(goals=("A", "B"))
    assert ad.publish_batch_child_completion(delegation_id, 0, _child(0, "A"))
    assert ad.publish_batch_child_completion(delegation_id, 1, _child(1, "B"))
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=99999999, owner_started_at=0 "
            "WHERE delegation_id=?",
            (delegation_id,),
        )

    assert ad.recover_abandoned_delegations() == 1

    assert _parent_state(delegation_id) == ("completed", "delivered")
    assert _durable_event_keys(delegation_id) == ["task:0", "task:1"]
    restored = __import__("queue").Queue()
    assert ad.restore_undelivered_completions(restored) == 2
    assert {restored.get_nowait()["delivery_event_key"] for _ in range(2)} == {
        "task:0",
        "task:1",
    }


def test_recovery_with_partial_inject_children_emits_only_terminal_gap_event():
    delegation_id = _record(goals=("A", "B"))
    assert ad.publish_batch_child_completion(delegation_id, 0, _child(0, "A"))
    process_registry.completion_queue.get_nowait()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=99999999, owner_started_at=0 "
            "WHERE delegation_id=?",
            (delegation_id,),
        )

    assert ad.recover_abandoned_delegations() == 1

    assert _parent_state(delegation_id) == ("unknown", "delivered")
    assert _durable_event_keys(delegation_id) == ["task:0", "terminal"]
    restored = __import__("queue").Queue()
    assert ad.restore_undelivered_completions(restored) == 2
    events = [restored.get_nowait() for _ in range(2)]
    assert {event["delivery_event_key"] for event in events} == {
        "task:0",
        "terminal",
    }
    terminal = next(event for event in events if event["delivery_event_key"] == "terminal")
    assert "1/2 batch child results" in terminal["error"]


def test_child_timeout_error_is_injectable_and_durable():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id,
        0,
        _child(0, "", status="timeout", error="child exceeded 10s"),
    )
    messages = [{"role": "tool", "tool_call_id": "tc", "content": "done"}]
    agent = SimpleNamespace()
    assert drain_ready_injects(agent, messages, "turn-current") == 1
    assert "timeout" in messages[-1]["content"]
    assert "child exceeded 10s" in messages[-1]["content"]
    assert _event_state(delegation_id, "task:0") == ("pending", 1)
    assert acknowledge_pending_injects(agent, turn_id="turn-current") == 1
    assert _event_state(delegation_id, "task:0") == ("delivered", 1)


def test_unconsumed_ram_inject_is_removed_released_and_requeued():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "retry after interrupt")
    )
    original = {"role": "tool", "tool_call_id": "tc", "content": "done"}
    messages = [original]
    agent = SimpleNamespace()

    assert drain_ready_injects(agent, messages, "turn-current") == 1
    assert _event_state(delegation_id, "task:0") == ("pending", 1)

    assert release_pending_injects(agent, messages, turn_id="turn-current") == 1
    assert messages == [original]
    assert [event["delegation_id"] for event in _queue_contents()] == [delegation_id]
    assert _event_state(delegation_id, "task:0") == ("pending", 1)


def test_restart_dedups_inject_already_persisted_in_active_history():
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "persisted before crash")
    )
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "tc", "content": "done"}
    ]
    crashed_agent = SimpleNamespace()

    assert drain_ready_injects(crashed_agent, messages, "turn-current") == 1
    persisted = messages[-1]
    persisted["_db_persisted"] = True
    pending = crashed_agent._pending_delegation_inject_claims
    crashed_event = deepcopy(pending[0]["event"])
    # Simulate process loss and expiry of the dead consumer's durable lease.
    del crashed_agent._pending_delegation_inject_claims
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegation_events SET delivery_claimed_at=0 "
            "WHERE delegation_id=? AND event_key='task:0'",
            (delegation_id,),
        )
    process_registry.completion_queue.put(crashed_event)

    assert drain_ready_injects(SimpleNamespace(), messages, "turn-current") == 0
    assert messages[-1] is persisted
    assert sum("persisted before crash" in m.get("content", "") for m in messages) == 1
    assert _event_state(delegation_id, "task:0") == ("pending", 1)

    # The resumed provider consumes the durable tail user message. At the next
    # append-only boundary, the queued duplicate is acknowledged without append.
    messages.append({"role": "assistant", "content": "consumed"})
    assert drain_ready_injects(SimpleNamespace(), messages, "turn-current") == 0
    assert sum("persisted before crash" in m.get("content", "") for m in messages) == 1
    assert _event_state(delegation_id, "task:0") == ("delivered", 2)


def test_formatter_failure_does_not_consume_delivery_attempts(monkeypatch):
    delegation_id = _record()
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "format me")
    )

    def broken_formatter(_event):
        raise ValueError("broken spill")

    monkeypatch.setattr(
        "tools.process_registry._format_async_delegation", broken_formatter
    )
    messages = [{"role": "tool", "tool_call_id": "tc", "content": "done"}]
    for _ in range(ad._MAX_DELIVERY_ATTEMPTS + 2):
        assert drain_ready_injects(SimpleNamespace(), messages, "turn-current") == 0

    assert _event_state(delegation_id, "task:0") == ("pending", 0)
    assert len(_queue_contents()) == 1


def test_pending_child_event_restores_with_same_durable_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    delegation_id = _record(goals=("restore me",))
    assert ad.publish_batch_child_completion(
        delegation_id, 0, _child(0, "restored result")
    )
    process_registry.completion_queue.get_nowait()

    restored_queue = __import__("queue").Queue()
    assert ad.restore_undelivered_completions(restored_queue) == 1
    event = restored_queue.get_nowait()
    assert event["restored"] is True
    assert event["delegation_id"] == delegation_id
    assert event["delivery_event_key"] == "task:0"

    claim = ad.claim_event_delivery(event, "restart-consumer")
    assert claim
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(restored_queue) == 0


def test_model_schema_defaults_after_turn_and_dispatch_forwards_explicit_mode(monkeypatch):
    delivery_schema = delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"][
        "result_delivery"
    ]
    assert delivery_schema["enum"] == ["inject", "after_turn"]
    assert delivery_schema["default"] == "after_turn"

    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    from run_agent import AIAgent

    result = AIAgent._dispatch_delegate_task(
        SimpleNamespace(_delegate_depth=0),
        {"goal": "audit", "result_delivery": "inject"},
    )
    assert result == "ok"
    assert captured["background"] is True
    assert captured["result_delivery"] == "inject"

    # The registry fallback is a distinct model-facing dispatch path. It must
    # preserve the same delivery choice if the run_agent intercept is bypassed.
    captured.clear()
    from tools.registry import registry

    entry = registry.get_entry("delegate_task")
    assert entry is not None
    result = entry.handler(
        {"goal": "audit", "result_delivery": "inject"},
        parent_agent=SimpleNamespace(_delegate_depth=0),
    )
    assert result == "ok"
    assert captured["background"] is True
    assert captured["result_delivery"] == "inject"
