import json

import pytest

from tests.attempt_fence_helpers import (
    isolated_home,
    logical_board_snapshot,
    registered_current_process,
)
from tools import kanban_tools as tools


def _make_registered_attempt_stale(fixture) -> None:
    fixture.conn.execute(
        "UPDATE task_runs SET claim_lock = ? WHERE id = ?",
        ("lost-claim", fixture.claimed.current_run_id),
    )
    fixture.conn.commit()


def _remove_worker_authority_env(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)


@pytest.mark.macos_only
def test_stale_worker_tool_comment_has_zero_comment_event_delta(
    registered_current_process,
    monkeypatch,
):
    fixture = registered_current_process
    _make_registered_attempt_stale(fixture)
    _remove_worker_authority_env(monkeypatch)
    before = logical_board_snapshot(fixture.conn)

    result = json.loads(
        tools._handle_comment({"task_id": fixture.task_id, "body": "late"})
    )

    assert "error" in result
    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
def test_stale_worker_tool_create_has_zero_db_and_subscription_delta(
    registered_current_process,
    monkeypatch,
):
    fixture = registered_current_process
    _make_registered_attempt_stale(fixture)
    _remove_worker_authority_env(monkeypatch)
    before = logical_board_snapshot(fixture.conn)

    result = json.loads(
        tools._handle_create({"title": "late child", "assignee": "dor-coo"})
    )

    assert "error" in result
    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
def test_current_worker_tool_comment_succeeds_without_authority_env(
    registered_current_process,
    monkeypatch,
):
    fixture = registered_current_process
    _remove_worker_authority_env(monkeypatch)

    result = json.loads(
        tools._handle_comment({"task_id": fixture.task_id, "body": "current"})
    )

    assert result["ok"] is True
