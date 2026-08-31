"""Containment custody probes for Kanban dashboard lifecycle mutations."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_containment as containment
from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    module_name = "hermes_dashboard_plugin_kanban_containment_lifecycle_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app, raise_server_exceptions=False)


def _contained_running_task() -> tuple[str, int, str]:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="contained dashboard worker", assignee="ops")
        task = kb.claim_task(conn, task_id, claimer="host:dashboard-test")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=424242,
            cgroup_path=f"/sys/fs/cgroup/hermes-{task_id}",
            cgroup_inode=8181,
        )
        return task_id, task.current_run_id, task.claim_lock


def _assert_still_owned(task_id: str, run_id: int, claim_lock: str) -> None:
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == run_id
        assert task.claim_lock == claim_lock
        run = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["ended_at"] is None


def test_direct_requeue_fails_closed_when_containment_cannot_be_certified(
    client, monkeypatch
):
    task_id, run_id, claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": False,
            "containment_certified": False,
        },
    )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"status": "ready"}
    )

    assert response.status_code == 409
    assert "containment" in response.json()["detail"].lower()
    _assert_still_owned(task_id, run_id, claim_lock)


def test_direct_requeue_uses_durable_reclaim_before_status_mutation(client, monkeypatch):
    task_id, run_id, _claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": True,
            "containment_certified": True,
        },
    )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"status": "ready"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.claim_lock is None
        row = conn.execute(
            "SELECT termination_certified_at FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None and row["termination_certified_at"] is not None


def test_parent_reopen_prepares_contained_descendant_before_atomic_invalidation(
    client, monkeypatch
):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="done parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="contained child",
            assignee="builder",
            parents=[parent_id],
        )
        child = kb.claim_task(conn, child_id, claimer="host:dashboard-child")
        assert child is not None and child.current_run_id is not None
        assert child.claim_lock is not None
        run_id = int(child.current_run_id)
        kb._register_worker_containment(
            conn,
            child_id,
            run_id=run_id,
            claim_lock=child.claim_lock,
            worker_pid=434343,
            cgroup_path=f"/sys/fs/cgroup/hermes-{child_id}",
            cgroup_inode=8282,
        )

    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": True,
            "containment_certified": True,
        },
    )
    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}", json={"status": "todo"}
    )

    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        parent = kb.get_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        assert parent is not None and parent.status == "todo"
        assert child is not None and child.status == "todo"
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "ancestor_reopened"
        assert row["termination_certified_at"] is not None


@pytest.mark.parametrize("status", ["done", "blocked", "review", "scheduled", "archived"])
def test_structured_patch_retires_containment_before_lifecycle_mutation(
    client, monkeypatch, status
):
    task_id, run_id, _claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": True,
            "containment_certified": True,
        },
    )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"status": status}
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == status
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT termination_certified_at FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None and row["termination_certified_at"] is not None


def test_structured_patch_fails_closed_when_containment_is_uncertified(
    client, monkeypatch
):
    task_id, run_id, claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": False,
            "containment_certified": False,
        },
    )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"status": "done"}
    )

    assert response.status_code == 409
    assert "containment" in response.json()["detail"].lower()
    _assert_still_owned(task_id, run_id, claim_lock)


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"status": "done"}, "done"),
        ({"status": "blocked"}, "blocked"),
        ({"status": "review"}, "review"),
        ({"status": "scheduled"}, "scheduled"),
        ({"archive": True}, "archived"),
    ],
)
def test_bulk_lifecycle_retires_containment_before_mutation(
    client, monkeypatch, payload, expected_status
):
    task_id, run_id, _claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": True,
            "containment_certified": True,
        },
    )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk", json={"ids": [task_id], **payload}
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"] == [{"id": task_id, "ok": True}]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == expected_status
        row = conn.execute(
            "SELECT termination_certified_at FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None and row["termination_certified_at"] is not None


def test_bulk_lifecycle_fails_closed_for_uncertified_containment(client, monkeypatch):
    task_id, run_id, claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": False,
            "containment_certified": False,
        },
    )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [task_id], "status": "done"},
    )

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["id"] == task_id and result["ok"] is False
    assert "containment" in result["error"].lower()
    _assert_still_owned(task_id, run_id, claim_lock)


def test_delete_fails_closed_without_releasing_uncertified_containment(
    client, monkeypatch
):
    task_id, run_id, claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": False,
            "containment_certified": False,
        },
    )

    response = client.delete(f"/api/plugins/kanban/tasks/{task_id}")

    assert response.status_code == 409
    assert "containment" in response.json()["detail"].lower()
    _assert_still_owned(task_id, run_id, claim_lock)


def test_delete_retires_and_cleans_containment_before_hard_delete(client, monkeypatch):
    task_id, run_id, _claim_lock = _contained_running_task()
    monkeypatch.setattr(
        containment,
        "kill_cgroup",
        lambda *_args, **_kwargs: {
            "terminated": True,
            "containment_certified": True,
        },
    )
    monkeypatch.setattr(containment, "cgroup_absent", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(containment, "cleanup_cgroup", lambda *_args, **_kwargs: True)

    response = client.delete(f"/api/plugins/kanban/tasks/{task_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True, "task_id": task_id}
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id) is None
        row = conn.execute(
            "SELECT termination_certified_at, unlink_intent_at, cleaned_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert all(row[column] is not None for column in row.keys())


def test_legacy_running_requeue_keeps_dashboard_compatibility(client, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy dashboard worker", assignee="ops")
        task = kb.claim_task(conn, task_id, claimer="host:legacy-test")
        assert task is not None
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_args: {})

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"status": "ready"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
