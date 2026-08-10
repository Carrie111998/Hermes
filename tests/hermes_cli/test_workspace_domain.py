from dataclasses import replace

import pytest

from hermes_cli.workspace.domain import (
    PushApprovalDecision,
    PushRequest,
    RunState,
    WorkspaceRunEvent,
    WorkspaceRunProjection,
    approval_is_current,
    can_transition,
    normalize_binding_relative_path,
    reduce_run_event,
)


def _projection(**overrides):
    value = WorkspaceRunProjection(
        run_id="run-1",
        state=RunState.QUEUED,
        last_sequence=0,
        sync_status="current",
    )
    return replace(value, **overrides)


def _event(sequence: int, state: RunState) -> WorkspaceRunEvent:
    return WorkspaceRunEvent(
        schema_version=1,
        event_id=f"event-{sequence}",
        project_id="project-1",
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=sequence,
        occurred_at="2026-08-05T00:00:00Z",
        state=state,
    )


def test_run_transitions_and_ordered_replay_are_fail_closed():
    assert can_transition(RunState.QUEUED, RunState.OFFERED)
    assert can_transition(RunState.RUNNING, RunState.UNCERTAIN)
    assert not can_transition(RunState.QUEUED, RunState.COMPLETED)
    assert not can_transition(RunState.COMPLETED, RunState.RUNNING)

    offered = reduce_run_event(_projection(), _event(1, RunState.OFFERED))
    assert offered == WorkspaceRunProjection(
        "run-1", RunState.OFFERED, 1, "current", "event-1"
    )
    assert reduce_run_event(offered, _event(1, RunState.OFFERED)) is offered

    gap = reduce_run_event(_projection(), _event(2, RunState.OFFERED))
    assert gap == WorkspaceRunProjection("run-1", RunState.QUEUED, 0, "needs_replay")

    with pytest.raises(ValueError, match="run-2"):
        reduce_run_event(_projection(), replace(_event(1, RunState.OFFERED), run_id="run-2"))

    conflict = reduce_run_event(
        offered,
        replace(_event(1, RunState.ACCEPTED), event_id="event-conflict"),
    )
    assert conflict.sync_status == "needs_replay"


def test_push_approval_is_bound_to_immutable_snapshot_and_expiry():
    request = PushRequest(
        request_id="push-1",
        run_id="run-1",
        commit_sha="abc123",
        diff_digest="sha256:diff",
        remote="origin",
        remote_url="https://github.com/example/project.git",
        remote_url_digest="url-digest",
        destination_ref="refs/heads/hermes/task-1",
        expires_at="2026-08-06T00:00:00Z",
    )
    approval = PushApprovalDecision(
        request_id=request.request_id,
        approved=True,
        commit_sha=request.commit_sha,
        diff_digest=request.diff_digest,
        remote=request.remote,
        remote_url=request.remote_url,
        remote_url_digest=request.remote_url_digest,
        destination_ref=request.destination_ref,
        decided_at="2026-08-05T12:00:00Z",
    )

    assert approval_is_current(
        request=request,
        approval=approval,
        current_commit_sha=request.commit_sha,
        current_diff_digest=request.diff_digest,
        now="2026-08-05T13:00:00Z",
    )
    assert not approval_is_current(
        request=request,
        approval=approval,
        current_commit_sha=request.commit_sha,
        current_diff_digest="sha256:changed",
        now="2026-08-05T13:00:00Z",
    )
    assert not approval_is_current(
        request=request,
        approval=approval,
        current_commit_sha=request.commit_sha,
        current_diff_digest=request.diff_digest,
        now=request.expires_at,
    )


@pytest.mark.parametrize(
    "unsafe",
    ["", ".", "..", "../secret", "nested/../../secret", "/etc/passwd", "C:\\secret", "//server/share"],
)
def test_binding_relative_paths_reject_escape_and_absolute_forms(unsafe: str):
    with pytest.raises(ValueError):
        normalize_binding_relative_path(unsafe)


def test_binding_relative_paths_are_normalized_without_disclosing_a_host_root():
    assert normalize_binding_relative_path("src/./app/../app/main.py") == "src/app/main.py"
    assert normalize_binding_relative_path("assets\\image.png") == "assets/image.png"
