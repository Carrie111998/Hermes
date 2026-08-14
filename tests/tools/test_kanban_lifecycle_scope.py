"""Fail-closed contracts for lifecycle-only Kanban worker processes."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def lifecycle_task(monkeypatch: pytest.MonkeyPatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "dashboardcontrol")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "lifecycle-only")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="lifecycle task",
            assignee="dashboardcontrol",
            workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        foreign_id = kb.create_task(
            conn,
            title="foreign task",
            assignee="other",
        )
        kb.claim_task(conn, task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    return task_id, foreign_id, workspace


def test_lifecycle_scope_forces_exact_surface_and_narrows_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_schema")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "lifecycle-only")

    from model_tools import _clear_tool_defs_cache, get_tool_definitions

    _clear_tool_defs_cache()
    try:
        definitions = get_tool_definitions(
            enabled_toolsets=["hermes-cli", "terminal", "kanban"],
            disabled_toolsets=[],
            quiet_mode=True,
        )
    finally:
        _clear_tool_defs_cache()

    by_name = {item["function"]["name"]: item["function"] for item in definitions}
    assert set(by_name) == {
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
    }
    for function in by_name.values():
        properties = function["parameters"].get("properties", {})
        assert "task_id" not in properties
        assert "board" not in properties
    complete_properties = by_name["kanban_complete"]["parameters"]["properties"]
    assert "created_cards" not in complete_properties
    assert "artifacts" not in complete_properties


@pytest.mark.parametrize(
    "name,args",
    [
        ("terminal", {"command": "pwd"}),
        ("kanban_create", {"title": "forged"}),
        ("kanban_comment", {"task_id": "t_deadbeef", "body": "forged"}),
        ("tool_call", {"name": "terminal", "arguments": {"command": "pwd"}}),
    ],
)
def test_forged_dispatch_is_denied_before_any_tool_path(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict,
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_dispatch")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "lifecycle-only")

    from model_tools import handle_function_call

    result = json.loads(
        handle_function_call(
            name,
            args,
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    )
    assert "unavailable in lifecycle-only" in result["error"]


def test_unknown_nonempty_scope_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_dispatch")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "typo-broad")

    from model_tools import handle_function_call

    result = json.loads(handle_function_call("kanban_show", {}))
    assert "invalid worker scope" in result["error"]


def test_lifecycle_handler_rejects_foreign_task_board_and_attachment_fields(
    lifecycle_task,
) -> None:
    task_id, foreign_id, _workspace = lifecycle_task
    from tools import kanban_tools as kt

    foreign = json.loads(kt._handle_show({"task_id": foreign_id}))
    assert "scoped to task" in foreign["error"]

    wrong_board = json.loads(kt._handle_show({"board": "other-board"}))
    assert "refusing board override" in wrong_board["error"]

    for forbidden in (
        {"task_id": task_id},
        {"board": "default"},
        {"created_cards": []},
        {"artifacts": []},
    ):
        result = json.loads(
            kt._handle_complete({"summary": "bounded", **forbidden})
        )
        assert "unavailable in lifecycle-only" in result["error"]

    metadata_artifact = json.loads(
        kt._handle_complete(
            {"summary": "bounded", "metadata": {"artifacts": []}}
        )
    )
    assert "metadata.artifacts is unavailable" in metadata_artifact["error"]


def test_lifecycle_completion_persists_structured_output_without_artifacts(
    lifecycle_task,
) -> None:
    task_id, _foreign_id, workspace = lifecycle_task
    decoy = workspace / "decoy.json"
    decoy.write_text('{"not":"worker output"}\n', encoding="utf-8")

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    payload = {"schema": "control-output/v1", "status": "ready"}
    result = json.loads(
        kt._handle_complete(
            {
                "summary": f"done; legacy path {decoy}",
                "metadata": {"control_output": payload},
            }
        )
    )
    assert result["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, task_id)
        assert run.outcome == "completed"
        assert run.metadata == {"control_output": payload}
        assert all(event.kind != "attached" for event in kb.list_events(conn, task_id))
    finally:
        conn.close()
