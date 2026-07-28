import sqlite3

import pytest

from hermes_cli.agents_os_commands import (
    CommandConflict,
    acknowledge_cancel,
    cancel_command,
    complete_command,
    confirm_command,
    create_command,
    create_feedback_candidate,
    mark_running,
    record_progress,
    resolve_command_approval,
)


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "commands.db")
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def test_safe_command_idempotent_create_to_result_and_feedback_candidate(conn):
    draft = create_command(
        conn, transcript="Show current tasks", idempotency_key="request-1",
        intent={"kind": "read_only_status"}, metadata={"source": "typed"},
    )
    retry = create_command(
        conn, transcript="Show current tasks", idempotency_key="request-1",
        intent={"kind": "read_only_status"}, metadata={"source": "typed"},
    )
    assert retry["id"] == draft["id"]
    assert retry["version"] == 1

    queued = confirm_command(conn, draft["id"], expected_version=1)
    running = mark_running(conn, draft["id"], expected_version=2, run_id="run-real-1")
    progress = record_progress(conn, draft["id"], expected_version=3, progress={"percent": 40})
    done = complete_command(
        conn, draft["id"], expected_version=4, succeeded=True,
        result={"answer": "Three tasks", "artifact_ids": ["artifact-1"]},
    )
    projected = create_feedback_candidate(
        conn, draft["id"], expected_version=5, verdict="accepted",
        candidate={"preference": "concise task summaries"}, metadata={"source": "jarvis_ui"},
    )

    assert queued["state"] == "queued"
    assert running["state"] == "running"
    assert progress["events"][-1]["payload"] == {"percent": 40}
    assert done["state"] == "succeeded"
    assert done["result"]["answer"] == "Three tasks"
    assert projected["version"] == 6
    assert projected["feedback_candidates"][0]["status"] == "candidate"
    assert projected["feedback_candidates"][0]["direct_memory_merge"] is False


def test_idempotency_key_rejects_different_request(conn):
    create_command(conn, transcript="First", idempotency_key="same")
    with pytest.raises(CommandConflict, match="different_request"):
        create_command(conn, transcript="Second", idempotency_key="same")


def test_gated_command_waits_for_matching_approval_then_queues(conn):
    draft = create_command(
        conn, transcript="Deploy", idempotency_key="gated-1",
        risk_class="public_gated", approval_required=True,
    )
    waiting = confirm_command(conn, draft["id"], expected_version=1, approval_id="approval-1")
    assert waiting["state"] == "awaiting_approval"
    with pytest.raises(CommandConflict, match="approval_id_mismatch"):
        resolve_command_approval(
            conn, draft["id"], expected_version=2, approved=True, approval_id="wrong",
        )
    queued = resolve_command_approval(
        conn, draft["id"], expected_version=2, approved=True, approval_id="approval-1",
    )
    assert queued["state"] == "queued"


def test_rejected_approval_is_terminal_cancelled(conn):
    draft = create_command(
        conn, transcript="Send email", idempotency_key="gated-2",
        risk_class="public_gated", approval_required=True,
    )
    confirm_command(conn, draft["id"], expected_version=1, approval_id="approval-2")
    rejected = resolve_command_approval(
        conn, draft["id"], expected_version=2, approved=False, approval_id="approval-2",
    )
    assert rejected["state"] == "cancelled"
    assert rejected["completed_at"]


def test_cancel_is_immediate_before_run_and_acknowledged_during_run(conn):
    early = create_command(conn, transcript="Status", idempotency_key="cancel-early")
    cancelled = cancel_command(conn, early["id"], expected_version=1, reason="changed mind")
    assert cancelled["state"] == "cancelled"

    active = create_command(conn, transcript="Build local report", idempotency_key="cancel-running")
    confirm_command(conn, active["id"], expected_version=1)
    mark_running(conn, active["id"], expected_version=2, run_id="run-2")
    cancelling = cancel_command(conn, active["id"], expected_version=3)
    assert cancelling["state"] == "cancelling"
    assert cancelling["completed_at"] is None
    final = acknowledge_cancel(conn, active["id"], expected_version=4)
    assert final["state"] == "cancelled"
    assert final["completed_at"]


def test_optimistic_version_and_transition_guards(conn):
    draft = create_command(conn, transcript="Status", idempotency_key="versioned")
    confirm_command(conn, draft["id"], expected_version=1)
    with pytest.raises(CommandConflict, match="version_conflict"):
        cancel_command(conn, draft["id"], expected_version=1)
    with pytest.raises(CommandConflict, match="invalid_transition"):
        confirm_command(conn, draft["id"], expected_version=2)


def test_feedback_requires_terminal_state_and_correction_text(conn):
    draft = create_command(conn, transcript="Status", idempotency_key="feedback-guard")
    with pytest.raises(CommandConflict, match="terminal"):
        create_feedback_candidate(conn, draft["id"], expected_version=1, verdict="accepted")
    cancelled = cancel_command(conn, draft["id"], expected_version=1)
    with pytest.raises(ValueError, match="correction is required"):
        create_feedback_candidate(
            conn, draft["id"], expected_version=cancelled["version"], verdict="corrected",
        )
