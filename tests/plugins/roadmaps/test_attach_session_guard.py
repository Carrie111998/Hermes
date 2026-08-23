"""RED: runtime_session_id must be rejected by the REST plugin."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import projects_db


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_file = repo_root / "plugins" / "roadmaps" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_roadmaps_guard", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def roadmaps_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = tmp_path / "projects.db"
    conn = projects_db.connect(db)
    conn.execute("INSERT INTO projects(id, slug, name, created_at) VALUES (?, ?, ?, 1)", ("p1", "p1", "p1"))
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def client(roadmaps_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/roadmaps")
    return TestClient(app)


PROFILE = "default"
PROJECT = "p1"


def _create_roadmap(client):
    r = client.post(
        f"/api/plugins/roadmaps/roadmaps?profile={PROFILE}&project_id={PROJECT}",
        json={"actor": "pierre", "title": "guard check"},
    )
    return r.json()["roadmap_id"]


def test_runtime_session_id_is_rejected(client):
    """runtime_session_id is an ephemeral field that must never reach the writer.

    Pydantic v2's default extra='ignore' silently drops unknown fields, so the
    explicit guard in attach_session never fires. This test proves the bug:
    the request currently succeeds (200) when it should be rejected (422).
    """
    rid = _create_roadmap(client)
    r = client.post(
        f"/api/plugins/roadmaps/roadmaps/{rid}/sessions?profile={PROFILE}&project_id={PROJECT}",
        json={"actor": "pierre", "stored_session_id": "sess-1", "expected_version": 0, "runtime_session_id": "ephemeral"},
    )
    # With the bug, this returns 200 because the guard is dead code.
    # After the fix, this must return 422 (validation error).
    assert r.status_code == 422, (
        f"runtime_session_id should be rejected with 422, got {r.status_code} body={r.text}"
    )
