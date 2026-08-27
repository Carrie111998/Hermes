"""The dashboard must not release a human approval gate.

SECURITY-CRITICAL. The board offers many ways to move a card — drag to any
column, the bulk multi-select, archive, complete. Each is a distinct code path
in plugin_api, so each is tested rather than assumed covered.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

REPO = Path(__file__).resolve().parents[2]


def _load_plugin_router():
    spec = importlib.util.spec_from_file_location(
        "kanban_plugin_api_gate",
        REPO / "plugins" / "kanban" / "dashboard" / "plugin_api.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    return tmp_path


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def gated(kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    tid = kb.create_task(conn, title="gated work", assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is True
    conn.close()
    return tid


def _state(kanban_home, tid):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    try:
        t = kb.get_task(conn, tid)
        return t.status, kb.gate_state_of(conn, tid)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "status", ["ready", "todo", "triage", "scheduled", "done", "blocked", "archived"]
)
def test_patch_to_any_status_is_refused_while_gated(client, kanban_home, gated, status):
    r = client.patch(
        f"/api/plugins/kanban/tasks/{gated}", json={"status": status}
    )
    assert r.status_code == 409, (status, r.status_code, r.text)
    assert "approval" in r.text.lower()
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_bulk_status_change_is_refused_while_gated(client, kanban_home, gated):
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [gated], "status": "ready"},
    )
    assert r.status_code == 200
    entry = next(e for e in r.json()["results"] if e["id"] == gated)
    assert entry["ok"] is False
    assert "approval" in entry["error"].lower()
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_bulk_archive_is_refused_while_gated(client, kanban_home, gated):
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [gated], "archive": True},
    )
    assert r.status_code == 200
    entry = next(e for e in r.json()["results"] if e["id"] == gated)
    assert entry["ok"] is False
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_the_gate_survives_every_route_in_sequence(client, kanban_home, gated):
    for status in ("ready", "todo", "done", "archived"):
        client.patch(f"/api/plugins/kanban/tasks/{gated}", json={"status": status})
    client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [gated], "status": "ready"})
    client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [gated], "archive": True})
    assert _state(kanban_home, gated) == ("scheduled", "plan")


# --- the guard must not break ordinary board use -------------------------

def test_ungated_task_still_moves_normally(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    tid = kb.create_task(conn, title="normal", assignee="a")
    conn.close()
    r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "todo"})
    assert r.status_code == 200
    assert _state(kanban_home, tid)[0] == "todo"


def test_ordinary_scheduled_task_still_moves_normally(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    tid = kb.create_task(conn, title="normal", assignee="a")
    kb.schedule_task(conn, tid, reason="later")
    conn.close()
    r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "ready"})
    assert r.status_code == 200
    assert _state(kanban_home, tid)[1] is None


def test_bulk_still_works_on_ungated_tasks(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    a = kb.create_task(conn, title="a", assignee="a")
    b = kb.create_task(conn, title="b", assignee="a")
    conn.close()
    r = client.post(
        "/api/plugins/kanban/tasks/bulk", json={"ids": [a, b], "status": "todo"}
    )
    assert r.status_code == 200
    assert all(e["ok"] for e in r.json()["results"])


def test_board_still_renders_a_gated_task(client, gated):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    ids = [t["id"] for c in r.json()["columns"] for t in c["tasks"]]
    assert gated in ids


# ---------------------------------------------------------------------------
# Independent review findings 2, 4, 5
# ---------------------------------------------------------------------------


def _assignee(kanban_home, tid):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    try:
        return kb.get_task(conn, tid).assignee
    finally:
        conn.close()


def _refusal_vias(kanban_home, tid):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    try:
        return {
            (e.payload or {}).get("via")
            for e in kb.list_events(conn, tid)
            if e.kind == "gate_release_refused"
        }
    finally:
        conn.close()


# --- finding 4: combined patch must be atomic ----------------------------

def test_combined_assignee_and_status_patch_changes_nothing(client, kanban_home, gated):
    """The assignee must not be applied before the status is refused."""
    before = _assignee(kanban_home, gated)
    r = client.patch(
        f"/api/plugins/kanban/tasks/{gated}",
        json={"assignee": "worker-x", "status": "ready"},
    )
    assert r.status_code == 409
    assert _assignee(kanban_home, gated) == before
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_combined_patch_with_other_fields_also_changes_nothing(client, kanban_home, gated):
    before = _assignee(kanban_home, gated)
    r = client.patch(
        f"/api/plugins/kanban/tasks/{gated}",
        json={"assignee": "worker-x", "priority": 9, "status": "done"},
    )
    assert r.status_code == 409
    assert _assignee(kanban_home, gated) == before
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_assignee_only_patch_is_still_allowed_on_a_gated_task(client, kanban_home, gated):
    """Renaming the owner of a gated card does not release the gate."""
    r = client.patch(
        f"/api/plugins/kanban/tasks/{gated}", json={"assignee": "worker-x"}
    )
    assert r.status_code == 200
    assert _assignee(kanban_home, gated) == "worker-x"
    assert _state(kanban_home, gated) == ("scheduled", "plan")


# --- finding 2: DELETE returns a conflict, not a 404 ---------------------

def test_delete_endpoint_returns_409_for_a_gated_task(client, kanban_home, gated):
    r = client.delete(f"/api/plugins/kanban/tasks/{gated}")
    assert r.status_code == 409, r.text
    assert "approval" in r.text.lower()
    assert _state(kanban_home, gated) == ("scheduled", "plan")


def test_delete_endpoint_still_404s_for_a_missing_task(client):
    r = client.delete("/api/plugins/kanban/tasks/t_missing")
    assert r.status_code == 404


def test_delete_endpoint_still_deletes_an_ungated_task(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    tid = kb.create_task(conn, title="normal", assignee="a")
    conn.close()
    r = client.delete(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200


# --- finding 5: dashboard refusals are audited ---------------------------

def test_dashboard_patch_refusal_is_audited(client, kanban_home, gated):
    client.patch(f"/api/plugins/kanban/tasks/{gated}", json={"status": "ready"})
    assert "dashboard_patch" in _refusal_vias(kanban_home, gated)


def test_dashboard_bulk_refusal_is_audited(client, kanban_home, gated):
    client.post(
        "/api/plugins/kanban/tasks/bulk", json={"ids": [gated], "status": "ready"}
    )
    assert "dashboard_bulk" in _refusal_vias(kanban_home, gated)


def test_dashboard_delete_refusal_is_audited(client, kanban_home, gated):
    client.delete(f"/api/plugins/kanban/tasks/{gated}")
    assert "dashboard_delete" in _refusal_vias(kanban_home, gated)


def test_all_dashboard_routes_audit_with_a_route_tag(client, kanban_home, gated):
    client.patch(f"/api/plugins/kanban/tasks/{gated}", json={"status": "ready"})
    client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [gated], "status": "todo"})
    client.delete(f"/api/plugins/kanban/tasks/{gated}")
    vias = _refusal_vias(kanban_home, gated)
    assert {"dashboard_patch", "dashboard_bulk", "dashboard_delete"} <= vias
    assert _state(kanban_home, gated) == ("scheduled", "plan")


# --- round-2 finding 9: dashboard DELETE /links ---------------------------

def _link_edge(kanban_home, parent, child):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    try:
        return kb.parent_ids(conn, child) == [parent]
    finally:
        conn.close()


@pytest.fixture
def gated_parent_child(kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    assert kb.park_for_plan_approval(conn, parent, project_id="p1", revision=1) is True
    conn.close()
    return parent, child


def test_dashboard_unlink_returns_409_for_a_gated_parent(client, gated_parent_child):
    parent, child = gated_parent_child
    r = client.delete(
        f"/api/plugins/kanban/links?parent_id={parent}&child_id={child}"
    )
    assert r.status_code == 409, r.text
    assert "approval" in r.text.lower()


def test_dashboard_unlink_refusal_preserves_the_edge(client, kanban_home, gated_parent_child):
    parent, child = gated_parent_child
    client.delete(f"/api/plugins/kanban/links?parent_id={parent}&child_id={child}")
    assert _link_edge(kanban_home, parent, child)
    assert _state(kanban_home, child)[0] == "todo"


def test_dashboard_unlink_refusal_is_audited(client, kanban_home, gated_parent_child):
    parent, child = gated_parent_child
    client.delete(f"/api/plugins/kanban/links?parent_id={parent}&child_id={child}")
    assert "dashboard_unlink" in _refusal_vias(kanban_home, parent)


def test_dashboard_unlink_still_works_for_an_ungated_parent(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    conn.close()
    r = client.delete(
        f"/api/plugins/kanban/links?parent_id={parent}&child_id={child}"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not _link_edge(kanban_home, parent, child)
    assert _state(kanban_home, child)[0] == "ready"


def test_dashboard_unlink_of_a_missing_edge_is_unchanged(client, kanban_home):
    conn = kb.connect(db_path=kanban_home / "kanban.db")
    a = kb.create_task(conn, title="a", assignee="a")
    b = kb.create_task(conn, title="b", assignee="a")
    conn.close()
    r = client.delete(f"/api/plugins/kanban/links?parent_id={a}&child_id={b}")
    assert r.status_code == 200 and r.json()["ok"] is False
