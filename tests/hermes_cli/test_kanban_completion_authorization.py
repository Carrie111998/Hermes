from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import lifecycle
from hermes_cli import plugins as plugin_runtime


def _ready_task(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = kb.connect(db_path)
    task_id = kb.create_task(conn, title="governed", initial_status="blocked")
    assert kb.promote_task(conn, task_id, actor="test")[0]
    return conn, task_id


def test_central_completion_veto_prevents_db_transition(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: event == "pre_kanban_task_complete")
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda event, **kwargs: [{"allow": False, "reason": "missing reviewer proof"}],
    )

    assert kb.complete_task(conn, task_id, result="attempted") is False
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb.list_runs(conn, task_id) == []
    conn.close()


def test_central_completion_preserves_governance_lifecycle_reason(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: True)
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda event, **kwargs: [{
            "allow": False,
            "reason": "Waiting for Marrow: verified completion contract unavailable.",
        }],
    )

    assert kb.complete_task(
        conn,
        task_id,
        result="attempted",
        with_reason=True,
    ) == (
        False,
        "Waiting for Marrow: verified completion contract unavailable.",
    )
    assert kb.get_task(conn, task_id).status == "ready"
    conn.close()


def test_central_completion_reports_dependency_wait(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = kb.connect(db_path)
    parent = kb.create_task(conn, title="parent", initial_status="blocked")
    child = kb.create_task(
        conn,
        title="child",
        parents=[parent],
        initial_status="blocked",
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: False)

    assert kb.complete_task(conn, child, with_reason=True) == (
        False,
        "Waiting dependency: one or more parent tasks are not done.",
    )
    conn.close()


def test_central_completion_requires_explicit_policy_response(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: event == "pre_kanban_task_complete")
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda event, **kwargs: [])

    assert kb.complete_task(conn, task_id, result="attempted") is False
    assert kb.get_task(conn, task_id).status == "ready"
    conn.close()


def test_central_completion_fails_closed_when_a_policy_callback_is_missing(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: event == "pre_kanban_task_complete")
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(), object()))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda event, **kwargs: [{"allow": True}])

    assert kb.complete_task(conn, task_id, result="attempted") is False
    assert kb.get_task(conn, task_id).status == "ready"
    conn.close()


def test_central_completion_persists_authorized_metadata(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: event == "pre_kanban_task_complete")
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))
    stamped = {
        "review_approved": True,
        "proof": {"tests_passed": True},
        "governance": {"implementation_proof_digest": "sha256:opaque"},
    }
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda event, **kwargs: [{"allow": True, "metadata": stamped}],
    )

    assert kb.complete_task(conn, task_id, result="approved", metadata={"untrusted": True})
    assert kb.get_task(conn, task_id).status == "done"
    completed = kb.list_runs(conn, task_id)[-1]
    assert completed.metadata == stamped
    conn.close()


def test_central_completion_executes_registered_policy_hook(tmp_path):
    conn, task_id = _ready_task(tmp_path)
    manager = plugin_runtime.get_plugin_manager()
    saved = list(manager._hooks.get("pre_kanban_task_complete", ()))
    seen = []

    def policy(**kwargs):
        seen.append(kwargs["task_id"])
        return {"allow": True, "metadata": {"authorized_by": "registered-policy"}}

    manager._hooks["pre_kanban_task_complete"] = [policy]
    try:
        assert kb.complete_task(conn, task_id, result="approved")
    finally:
        manager._hooks["pre_kanban_task_complete"] = saved
        conn.close()
    assert seen == [task_id]


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("title", "changed title"),
        ("body", "changed body"),
        ("priority", 99),
        ("assignee", "other-reviewer"),
        ("model_override", "different-model"),
        ("provider_override", "different-provider"),
        ("reasoning_effort", "low"),
    ),
)
def test_central_completion_rejects_task_edit_after_authorization(
    tmp_path, monkeypatch, column, value
):
    conn, task_id = _ready_task(tmp_path)
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: True)
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))

    def authorize_then_race(event, **kwargs):
        racing = kb.connect(db_path)
        try:
            with kb.write_txn(racing):
                racing.execute(
                    f"UPDATE tasks SET {column} = ? WHERE id = ?",
                    (value, task_id),
                )
        finally:
            racing.close()
        return [{"allow": True}]

    monkeypatch.setattr(lifecycle, "invoke_hook", authorize_then_race)
    assert kb.complete_task(conn, task_id, result="attempted") is False
    assert kb.get_task(conn, task_id).status == "ready"
    conn.close()


def test_central_completion_rejects_stale_expected_run_id(tmp_path, monkeypatch):
    conn, task_id = _ready_task(tmp_path)
    claimed = kb.claim_task(conn, task_id, claimer="test")
    assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda event: True)
    monkeypatch.setattr(plugin_runtime, "iter_hook_callbacks", lambda event: (object(),))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda event, **kwargs: [{"allow": True}])

    assert kb.complete_task(
        conn,
        task_id,
        result="attempted",
        expected_run_id=claimed.current_run_id + 1,
    ) is False
    assert kb.get_task(conn, task_id).status == "running"
    conn.close()
