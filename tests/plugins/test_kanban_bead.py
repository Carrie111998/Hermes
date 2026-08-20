"""Mandatory bead reference on kanban tasks — DB layer + dashboard API.

Every kanban card must carry the id of the upstream issue-tracker item it
captures (estate: beads) so the card can render a clickable link. This test
covers the three enforcement surfaces:

  * ``kanban_db.create_task`` — refuses a create with no ``bead_id``;
    children inherit the first parent's bead when none is given;
    rejects malformed ids (``<tracker>-<digits>`` required).
  * ``kanban_db.set_bead_id`` — lets a legacy card (created before the
    field existed) be linked post-hoc.
  * the dashboard REST surface — ``POST /tasks`` returns 400 without a
    bead, and ``PATCH /tasks/:id`` links a legacy card.

The plugin router is attached to a bare FastAPI app — same approach as
``test_kanban_model_override.py`` — so the real HTTP path is exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

VALID = "worktracker-676.4.36.8.6.2"


# ---------------------------------------------------------------------------
# Fixtures (mirror test_kanban_model_override.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kb.connect()
    yield c
    c.close()


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_bead_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# DB layer — create_task gate + inheritance + set_bead_id
# ---------------------------------------------------------------------------


def test_create_refuses_missing_bead(conn):
    with pytest.raises(ValueError, match="bead_id is required"):
        kb.create_task(conn, title="no bead")


def test_create_refuses_malformed_bead(conn):
    # Empty/blank reads as "missing" (the required-message path); genuinely
    # malformed ids get the shape error.
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="bead_id is required"):
            kb.create_task(conn, title="bad", bead_id=bad)
    for bad in ("worktracker", "worktracker-", "Worktracker-1",
                "123-worktracker", "worktracker-a"):
        with pytest.raises(ValueError, match="bead_id must look like"):
            kb.create_task(conn, title="bad", bead_id=bad)


def test_create_accepts_valid_bead(conn):
    tid = kb.create_task(conn, title="ok", bead_id=VALID)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.bead_id == VALID


def test_child_inherits_parent_bead(conn):
    parent = kb.create_task(conn, title="parent", bead_id=VALID)
    child = kb.create_task(conn, title="child", parents=[parent])
    assert kb.get_task(conn, child).bead_id == VALID


def test_child_with_explicit_bead_keeps_own(conn):
    parent = kb.create_task(conn, title="parent", bead_id=VALID)
    other = "worktracker-999"
    child = kb.create_task(conn, title="child", parents=[parent], bead_id=other)
    assert kb.get_task(conn, child).bead_id == other


def test_set_bead_id_links_legacy_card(conn):
    # Simulate a legacy card: created before the mandatory field existed
    # (via a direct DB write, bypassing the current create gate) by
    # creating one with the gate satisfied, then clearing the field.
    tid = kb.create_task(conn, title="legacy", bead_id=VALID)
    conn.execute("UPDATE tasks SET bead_id = NULL WHERE id = ?", (tid,))
    conn.commit()
    assert kb.get_task(conn, tid).bead_id is None

    assert kb.set_bead_id(conn, tid, VALID) is True
    assert kb.get_task(conn, tid).bead_id == VALID


def test_set_bead_id_rejects_malformed(conn):
    tid = kb.create_task(conn, title="legacy", bead_id=VALID)
    with pytest.raises(ValueError, match="bead_id must look like"):
        kb.set_bead_id(conn, tid, "not-a-bead")


def test_set_bead_id_missing_task_returns_false(conn):
    assert kb.set_bead_id(conn, "t_nonexistent", VALID) is False


# ---------------------------------------------------------------------------
# Dashboard API — POST /tasks gate + PATCH link
# ---------------------------------------------------------------------------


def test_api_create_without_bead_400(client):
    r = client.post("/api/plugins/kanban/tasks", json={"title": "no bead"})
    assert r.status_code == 400
    assert "bead_id is required" in r.json()["detail"]


def test_api_create_with_malformed_bead_400(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "bad", "bead_id": "nope"},
    )
    assert r.status_code == 400
    assert "bead_id must look like" in r.json()["detail"]


def test_api_create_with_bead_ok(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "ok", "bead_id": VALID},
    )
    assert r.status_code == 200
    task = r.json().get("task") or {}
    assert task.get("bead_id") == VALID


def test_api_patch_links_legacy_card(client):
    tid = kb.create_task(
        kb.connect(), title="legacy", bead_id=VALID,
    )
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET bead_id = NULL WHERE id = ?", (tid,))
        conn.commit()
    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"bead_id": "worktracker-42"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["bead_id"] == "worktracker-42"