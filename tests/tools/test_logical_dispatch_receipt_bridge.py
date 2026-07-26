"""Task 3 RED contract: logical dispatch -> durable Kanban receipt bridge.

These tests intentionally describe the public API Task 3 must add.  They use
only disposable ``HERMES_HOME`` / Kanban SQLite files and inspect committed
state rather than mocking either persistence layer.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from hermes_cli import kanban_db as kb
from tools import async_delegation as ad
from tools.process_registry import process_registry


TERMINAL = {"PASS", "REWORK", "REPLAN", "BLOCKED", "FALLBACK_REQUIRED", "FAILED"}


@pytest.fixture(autouse=True)
def isolated_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes"
    kanban_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_path))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    kb.init_db(kanban_path)
    yield {"hermes_home": hermes_home, "kanban_path": kanban_path}
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


@pytest.fixture
def board_task(isolated_bridge):
    with kb.connect_closing(isolated_bridge["kanban_path"]) as conn:
        task_id = kb.create_task(
            conn,
            title="Task 3 receipt destination",
            assignee="forge",
            created_by="gauge",
            initial_status="running",
        )
        assert kb.claim_task(conn, task_id, claimer="bridge-claim") is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET workflow_template_id='workflow-v1', current_step_key='implement' WHERE id=?",
            (task_id,),
        )
        conn.commit()
    return ad.BridgeAuthorityContext(
        kanban_db_path=str(isolated_bridge["kanban_path"].resolve()),
        workflow_id="workflow-v1",
        step_key="implement",
        step_attempt_id=f"workflow-v1/implement/{task.current_run_id}",
        task_id=task_id,
        run_id=task.current_run_id,
        claim_token="bridge-claim",
        lane="forge",
        route="test-model",
        owner_id="parent-session",
    )


def _public(name: str) -> Callable:
    value = getattr(ad, name, None)
    assert callable(value), f"Task 3 public API tools.async_delegation.{name} is missing"
    return value


def _dispatch(
    logical_key: str,
    runner: Callable,
    authority_context: ad.BridgeAuthorityContext,
    *,
    goal: str = "verify logical receipt",
    context: str = "Task 3 RED",
    role: str = "leaf",
    **extra,
):
    digest = ad.canonical_json_digest(
        ad.canonical_dispatch_input(goal, context, role)
    )
    return _public("dispatch_logical_delegation")(
        logical_key=logical_key,
        input_digest=digest,
        goal=goal,
        context=context,
        toolsets=None,
        role=role,
        model="test-model",
        session_key="source-session",
        parent_session_id="parent-session",
        runner=runner,
        max_async_children=8,
        authority_context=authority_context,
        **extra,
    )


def _get(logical_key: str):
    return _public("get_logical_delegation")(logical_key)


def _wait_state(logical_key: str, expected: set[str], timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _get(logical_key)
        if record and record["state"] in expected:
            return record
        time.sleep(0.01)
    pytest.fail(f"logical dispatch {logical_key!r} did not reach {sorted(expected)}")


def _terminal_runner(summary: str = "done"):
    return {
        "status": "PASS",
        "summary": summary,
        "artifact_digest": "sha256:terminal-evidence",
    }


def _attach(
    logical_key: str,
    isolated_bridge,
    board_task: ad.BridgeAuthorityContext,
    **extra,
):
    return _public("attach_logical_dispatch_receipt")(
        logical_key=logical_key,
        kanban_db_path=isolated_bridge["kanban_path"],
        task_id=board_task.task_id,
        **extra,
    )


def _bridge_counts(kanban_path: Path, task_id: str, logical_key: str) -> dict[str, int]:
    with sqlite3.connect(kanban_path) as conn:
        receipt = conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?", (logical_key,)
        ).fetchone()[0]
        continuation = conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?", (logical_key,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (task_id,),
        ).fetchall()
    event = sum(json.loads(payload)["logical_key"] == logical_key for (payload,) in events)
    return {"receipt": receipt, "event": event, "continuation": continuation}


def test_stable_logical_key_reserves_before_launch_and_reuses_one_execution(board_task):
    gate = threading.Event()
    entered = threading.Event()
    calls = {"count": 0}
    observed = {}

    def runner():
        calls["count"] += 1
        observed["reservation"] = _get("workflow/step/attempt")
        entered.set()
        gate.wait(timeout=5)
        return _terminal_runner()

    first = _dispatch("workflow/step/attempt", runner, board_task)
    assert first["state"] in {"reserved", "running"}
    assert entered.wait(timeout=2)
    second = _dispatch("workflow/step/attempt", runner, board_task)

    assert second["delegation_id"] == first["delegation_id"]
    assert second["execution_id"] == first["execution_id"]
    assert calls["count"] == 1
    assert observed["reservation"]["delegation_id"] == first["delegation_id"]
    assert observed["reservation"]["execution_id"] == first["execution_id"]
    assert observed["reservation"]["state"] in {"reserved", "running"}
    gate.set()
    terminal = _wait_state("workflow/step/attempt", {"terminal_unattached"})
    assert terminal["terminal_status"] in TERMINAL


def test_terminal_source_evidence_is_durable_before_kanban_attachment(
    isolated_bridge, board_task
):
    dispatched = _dispatch("durable/source", _terminal_runner, board_task)
    terminal = _wait_state("durable/source", {"terminal_unattached"})
    ad._reset_for_tests()  # discard process memory; the next read must come from SQLite
    terminal = _get("durable/source")

    assert terminal["delegation_id"] == dispatched["delegation_id"]
    assert terminal["terminal_status"] == "PASS"
    assert terminal["result"]["summary"] == "done"
    assert terminal["receipt_id"] is None
    assert terminal["source_acknowledged_at"] is None
    assert isolated_bridge["kanban_path"].exists()
    assert _bridge_counts(
        isolated_bridge["kanban_path"], board_task.task_id, "durable/source"
    ) == {
        "receipt": 0,
        "event": 0,
        "continuation": 0,
    }


@pytest.mark.parametrize(
    "boundary",
    ["before_attach", "after_kanban_commit", "before_source_ack", "after_source_ack_commit"],
)
def test_replay_at_attach_and_source_ack_boundaries_is_exactly_once(
    boundary, isolated_bridge, board_task
):
    logical_key = f"replay/{boundary}"
    _dispatch(logical_key, _terminal_runner, board_task)
    _wait_state(logical_key, {"terminal_unattached"})

    if boundary == "before_attach":
        _attach(logical_key, isolated_bridge, board_task, acknowledge_source=False)
    elif boundary == "after_kanban_commit":
        with pytest.raises(ad.LogicalDispatchBridgeFault, match="kanban_commit"):
            _attach(
                logical_key,
                isolated_bridge,
                board_task,
                acknowledge_source=False,
                fault_after="kanban_commit",
            )
    elif boundary == "before_source_ack":
        _attach(logical_key, isolated_bridge, board_task, acknowledge_source=False)
    else:
        _attach(logical_key, isolated_bridge, board_task, acknowledge_source=False)
        with pytest.raises(ad.LogicalDispatchBridgeFault, match="source_ack_commit"):
            _public("acknowledge_logical_dispatch")(
                logical_key=logical_key, fault_after="source_ack_commit"
            )

    # Replay attach and acknowledgement after every boundary. Both are idempotent.
    _attach(logical_key, isolated_bridge, board_task, acknowledge_source=False)
    _public("acknowledge_logical_dispatch")(logical_key=logical_key)
    _public("acknowledge_logical_dispatch")(logical_key=logical_key)

    assert _bridge_counts(isolated_bridge["kanban_path"], board_task.task_id, logical_key) == {
        "receipt": 1,
        "event": 1,
        "continuation": 1,
    }
    record = _get(logical_key)
    assert record["state"] == "acknowledged"
    assert record["source_ack_count"] == 1


def test_conflicting_digest_quarantines_logical_key_and_cannot_continue(
    isolated_bridge, board_task
):
    calls = {"count": 0}

    def runner():
        calls["count"] += 1
        return _terminal_runner()

    first = _dispatch("conflict/key", runner, board_task)
    conflict = _dispatch(
        "conflict/key",
        runner,
        board_task,
        context="Task 3 RED changed input",
    )

    assert conflict["status"] == "quarantined"
    assert conflict["delegation_id"] == first["delegation_id"]
    assert conflict["expected_digest"] == ad.canonical_json_digest(
        ad.canonical_dispatch_input("verify logical receipt", "Task 3 RED", "leaf")
    )
    assert conflict["observed_digest"] == ad.canonical_json_digest(
        ad.canonical_dispatch_input(
            "verify logical receipt", "Task 3 RED changed input", "leaf"
        )
    )
    assert calls["count"] == 1
    replay = _attach("conflict/key", isolated_bridge, board_task)
    assert replay["status"] == "quarantined"
    assert _get("conflict/key")["state"] == "quarantined"
    assert _bridge_counts(
        isolated_bridge["kanban_path"], board_task.task_id, "conflict/key"
    ) == {
        "receipt": 0,
        "event": 0,
        "continuation": 0,
    }


def test_retention_protects_unfinished_bridge_evidence_and_overflow_backpressures(
    isolated_bridge, board_task
):
    keys = ["retain/unattached", "retain/pending-receipt", "retain/unacknowledged"]
    for key in keys:
        _dispatch(key, _terminal_runner, board_task)
        _wait_state(key, {"terminal_unattached"})

    pending = _attach(
        keys[1],
        isolated_bridge,
        board_task,
        acknowledge_source=False,
        prepare_only=True,
    )
    assert pending["state"] == "terminal_pending_receipt"
    _attach(keys[2], isolated_bridge, board_task, acknowledge_source=False)
    assert _get(keys[2])["state"] == "receipted_unacknowledged"

    result = _public("prune_logical_delegations")(
        retention_seconds=0,
        max_records=1,
        max_pending_records=1,
    )
    assert result["status"] == "backpressure"
    assert result["deleted"] == 0
    assert result["protected"] >= 3
    assert {key: _get(key)["state"] for key in keys} == {
        keys[0]: "terminal_unattached",
        keys[1]: "terminal_pending_receipt",
        keys[2]: "receipted_unacknowledged",
    }

    rejected = _dispatch(
        "retain/overflow",
        _terminal_runner,
        board_task,
        max_pending_records=1,
    )
    assert rejected["status"] == "backpressure"
    assert _get("retain/overflow") is None
