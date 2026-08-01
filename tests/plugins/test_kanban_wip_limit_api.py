"""FastAPI board WIP-limit contract and dashboard dispatch-owner coverage."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_router():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_wip_test", plugin_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    import hermes_constants

    hermes_constants._cached_default_hermes_root = None
    kb._INITIALIZED_PATHS.clear()
    app = FastAPI()
    app.include_router(_load_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_board_contract_set_preserve_clear_and_reject(client):
    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "api-board", "name": "API", "wip_limit": 2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["board"]["wip_limit"] == 2

    response = client.patch(
        "/api/plugins/kanban/boards/api-board", json={"name": "Renamed"}
    )
    assert response.status_code == 200
    assert response.json()["board"]["wip_limit"] == 2

    response = client.patch(
        "/api/plugins/kanban/boards/api-board", json={"wip_limit": None}
    )
    assert response.status_code == 200
    assert response.json()["board"]["wip_limit"] is None

    for value in (0, -1, True, "2", 1.5):
        response = client.patch(
            "/api/plugins/kanban/boards/api-board", json={"wip_limit": value}
        )
        assert response.status_code == 422, (value, response.text)
        assert kb.read_board_metadata("api-board")["name"] == "Renamed"
        assert kb.read_board_metadata("api-board")["wip_limit"] is None

    listed = client.get("/api/plugins/kanban/boards").json()
    board = next(item for item in listed["boards"] if item["slug"] == "api-board")
    assert board["wip_limit"] is None


def test_dashboard_dispatch_uses_shared_board_cap(client):
    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "nudge-board", "wip_limit": 1},
    )
    assert response.status_code == 200
    with kb.connect(board="nudge-board") as conn:
        running = kb.create_task(conn, title="running", assignee="default")
        ready = kb.create_task(conn, title="ready", assignee="default")
        assert kb.claim_task(conn, running, claimer="default") is not None

    response = client.post("/api/plugins/kanban/dispatch?board=nudge-board")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["spawned"] == []
    assert ready in result["skipped_wip_capped"]
