"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution as workflow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  workflow:\n"
        "    remote_control_enabled: true\n"
        "    remote_control_principals:\n"
        "      'test:user:operator': [pause, resume]\n",
        encoding="utf-8",
    )
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()

    @app.middleware("http")
    async def _authenticated_operator(request, call_next):
        request.state.session = types.SimpleNamespace(
            provider="test",
            user_id="operator",
        )
        return await call_next(request)

    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


def test_workflow_reconnect_projection_and_typed_controller_are_remote(
    client, kanban_home
):
    initial = client.get("/api/plugins/kanban/workflow/projection")
    assert initial.status_code == 200
    projection = initial.json()
    assert projection["projection"] == "workflow-runtime-v1"
    assert projection["canonical_source"] == "github"
    assert projection["board"] == kb.DEFAULT_BOARD
    assert projection["controller"]["version"] == 0
    assert projection["controller"]["dispatch_enabled"] is False
    assert projection["controller"]["broker_ready"] is False
    assert projection["leaves"] == []

    unavailable = client.post(
        "/api/plugins/kanban/workflow/controller/resume",
        json={"expected_version": 0, "reason": "Desktop reconnect"},
    )
    assert unavailable.status_code == 409
    assert "broker" in unavailable.json()["detail"]

    with kb.connect_closing() as conn:
        controller = workflow.set_workflow_broker_ready(
            conn,
            ready=True,
            expected_version=0,
            actor="remote-controller-test",
            reason="isolated broker fixture",
        )
        task_id = kb.create_task(
            conn,
            title="remote protected leaf",
            body=json.dumps({
                "schema": "hermes.execution-capsule.v1",
                "spec": {
                    "repository": "org/repo",
                    "campaign_issue": 17,
                    "leaf_id": "remote-leaf",
                    "version": 1,
                },
                "capsule": {},
            }),
            assignee="coder",
            workspace_kind="worktree",
            workspace_path=str(kanban_home / "remote-worktree"),
            leaf_key="github:org/repo:issue-17:leaf-remote-leaf:v1",
            leaf_family_key="github:org/repo:issue-17:leaf-remote-leaf",
            spec_hash="a" * 64,
            pin_sha="b" * 40,
            capsule_hash="c" * 64,
            evidence_paths=("src/**",),
            lease_policy="evidence",
        )

    ordinary_board = client.get("/api/plugins/kanban/board").json()
    ordinary_ids = {
        task["id"]
        for column in ordinary_board["columns"]
        for task in column["tasks"]
    }
    assert task_id not in ordinary_ids
    assert ordinary_board["latest_event_id"] == 0
    assert client.get(f"/api/plugins/kanban/tasks/{task_id}").status_code == 409
    attachment = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/attachments",
        files={"file": ("note.txt", b"generic mutation")},
    )
    assert attachment.status_code == 409

    resumed = client.post(
        "/api/plugins/kanban/workflow/controller/resume",
        json={
            "expected_version": controller.version,
            "reason": "bounded test resume",
        },
    )
    assert resumed.status_code == 200
    resumed_controller = resumed.json()["controller"]
    assert resumed_controller["dispatch_enabled"] is True
    with kb.connect_closing() as conn:
        actor = conn.execute(
            "SELECT actor FROM workflow_controller_events "
            "WHERE kind = 'dispatch_resumed' ORDER BY id DESC LIMIT 1"
        ).fetchone()["actor"]
    assert actor == "test:user:operator"

    stale_pause = client.post(
        "/api/plugins/kanban/workflow/controller/pause",
        json={
            "expected_version": controller.version,
            "reason": "stale queued Desktop mutation",
        },
    )
    assert stale_pause.status_code == 409
    assert "stale" in stale_pause.json()["detail"]

    paused = client.post(
        "/api/plugins/kanban/workflow/controller/pause",
        json={
            "expected_version": resumed_controller["version"],
            "reason": "remote emergency stop",
        },
    )
    assert paused.status_code == 200
    assert paused.json()["controller"]["dispatch_enabled"] is False

    reconnected = client.get("/api/plugins/kanban/workflow/projection").json()
    assert (
        reconnected["controller"]["version"] == paused.json()["controller"]["version"]
    )
    assert reconnected["controller"]["dispatch_enabled"] is False
    assert len(reconnected["leaves"]) == 1
    leaf = reconnected["leaves"][0]
    assert leaf["id"] == task_id
    assert leaf["title"] == "remote protected leaf"
    assert leaf["canonical"] == {
        "source": "github",
        "repository": "org/repo",
        "campaign_issue": 17,
    }
    assert leaf["specification_version"] == "v1"
    assert leaf["current_run"] is None
    assert "workspace_path" not in leaf


def test_workflow_remote_control_defaults_to_denied(client, kanban_home):
    (kanban_home / "config.yaml").write_text("{}\n", encoding="utf-8")

    denied = client.post(
        "/api/plugins/kanban/workflow/controller/pause",
        json={"expected_version": 0, "reason": "must be authorized"},
    )

    assert denied.status_code == 403
    assert "workflow control" in denied.json()["detail"].lower()


def test_workflow_remote_control_denies_unlisted_authenticated_principal(
    client, kanban_home
):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  workflow:\n"
        "    remote_control_enabled: true\n"
        "    remote_control_principals:\n"
        "      'test:user:someone-else': [pause, resume]\n",
        encoding="utf-8",
    )

    denied = client.post(
        "/api/plugins/kanban/workflow/controller/pause",
        json={"expected_version": 0, "reason": "principal must be granted"},
    )

    assert denied.status_code == 403
    assert "authorized" in denied.json()["detail"].lower()


def test_workflow_remote_control_denies_operation_not_granted_to_principal(
    client, kanban_home
):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  workflow:\n"
        "    remote_control_enabled: true\n"
        "    remote_control_principals:\n"
        "      'test:user:operator': [pause]\n",
        encoding="utf-8",
    )

    denied = client.post(
        "/api/plugins/kanban/workflow/controller/resume",
        json={"expected_version": 0, "reason": "resume is not granted"},
    )

    assert denied.status_code == 403
    assert "resume" in denied.json()["detail"].lower()


def test_generic_sibling_routes_and_aggregates_confine_protected_runtime(
    client, kanban_home
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="protected sibling surface",
            body=json.dumps({
                "schema": "hermes.execution-capsule.v1",
                "spec": {
                    "repository": "org/repo",
                    "campaign_issue": 18,
                    "leaf_id": "sibling-surface",
                    "version": 1,
                },
                "capsule": {},
            }),
            assignee="protected-only-profile",
            workspace_kind="worktree",
            workspace_path=str(kanban_home / "protected-worktree"),
            leaf_key="github:org/repo:issue-18:leaf-sibling-surface:v1",
            leaf_family_key="github:org/repo:issue-18:leaf-sibling-surface",
            spec_hash="d" * 64,
            pin_sha="e" * 40,
            capsule_hash="f" * 64,
            evidence_paths=("src/**",),
            lease_policy="evidence",
        )
        raw_token = "must-never-cross-generic-rest"
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, worker_pid, started_at, leaf_key, leaf_family_key, "
            "spec_hash, pin_sha, capsule_hash) "
            "VALUES (?, 'coder', 'running', ?, ?, 424242, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                raw_token,
                int(time.time()) + 600,
                int(time.time()),
                "github:org/repo:issue-18:leaf-sibling-surface:v1",
                "github:org/repo:issue-18:leaf-sibling-surface",
                "d" * 64,
                "e" * 40,
                "f" * 64,
            ),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, "
            "worker_pid = 424242, current_run_id = ? WHERE id = ?",
            (raw_token, int(time.time()) + 600, run_id, task_id),
        )
        attachment_id = conn.execute(
            "INSERT INTO task_attachments (task_id, filename, stored_path, size, created_at) "
            "VALUES (?, 'protected.txt', ?, 1, ?)",
            (task_id, str(kanban_home / "protected.txt"), int(time.time())),
        ).lastrowid
        conn.commit()

        assert kb.get_generic_task(conn, task_id) is None
        assert task_id not in {task.id for task in kb.list_tasks(conn)}
        with pytest.raises(PermissionError, match="controller-only"):
            kb.list_attachments(conn, task_id)
        with pytest.raises(PermissionError, match="controller-only"):
            kb.delete_attachment(conn, attachment_id)

        with pytest.raises(PermissionError, match="controller-only"):
            kb.add_comment(conn, task_id, "generic", "forbidden")
        with pytest.raises(PermissionError, match="controller-only"):
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="test",
                chat_id="test-chat",
            )
        with pytest.raises(PermissionError, match="controller-only"):
            kb.remove_notify_sub(
                conn,
                task_id=task_id,
                platform="test",
                chat_id="test-chat",
            )

    log_path = kb.worker_log_path(task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("protected-log-sentinel", encoding="utf-8")

    protected_requests = (
        ("post", f"/api/plugins/kanban/tasks/{task_id}/comments", {"json": {"body": "x"}}, 409),
        ("post", f"/api/plugins/kanban/tasks/{task_id}/estimate", {}, 409),
        ("get", f"/api/plugins/kanban/tasks/{task_id}/log", {}, 409),
        ("get", f"/api/plugins/kanban/home-channels?task_id={task_id}", {}, 409),
        ("post", f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/test", {}, 409),
        ("delete", f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/test", {}, 409),
        ("get", f"/api/plugins/kanban/runs/{run_id}", {}, 404),
        ("get", f"/api/plugins/kanban/runs/{run_id}/inspect", {}, 404),
        (
            "post",
            f"/api/plugins/kanban/runs/{run_id}/terminate",
            {"json": {"reason": "generic bypass"}},
            404,
        ),
    )
    for method, path, kwargs, expected_status in protected_requests:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == expected_status, (method, path, response.text)
        assert raw_token not in response.text
        assert "protected-log-sentinel" not in response.text

    workers = client.get("/api/plugins/kanban/workers/active").json()
    assert workers["workers"] == []
    diagnostics = client.get("/api/plugins/kanban/diagnostics").json()
    assert diagnostics == {"diagnostics": [], "count": 0}
    stats = client.get("/api/plugins/kanban/stats").json()
    assert sum(stats["by_status"].values()) == 0
    boards = client.get("/api/plugins/kanban/boards").json()["boards"]
    default_board = next(board for board in boards if board["slug"] == kb.DEFAULT_BOARD)
    assert default_board["total"] == 0
    assignees = client.get("/api/plugins/kanban/assignees").json()["assignees"]
    assert all(
        sum(entry["counts"].values()) == 0
        for entry in assignees
        if entry["name"] == "protected-only-profile"
    )


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


def test_generic_desktop_routes_cannot_mutate_evidence_fenced_leaf(client, kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="protected runtime leaf",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path=str(kanban_home / "protected-worktree"),
            leaf_key="github:org/repo:issue-1:leaf-a:v1",
            leaf_family_key="github:org/repo:issue-1:leaf-a",
            spec_hash="a" * 64,
            pin_sha="b" * 40,
            capsule_hash="c" * 64,
            evidence_paths=("src/**",),
            lease_policy="evidence",
        )

    patch_response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"title": "local intent rewrite", "status": "archived"},
    )
    assert patch_response.status_code == 409
    assert "Workflow controller" in patch_response.json()["detail"]

    bulk_response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [task_id], "status": "done", "priority": 99},
    )
    assert bulk_response.status_code == 200
    assert bulk_response.json()["results"] == [
        {
            "id": task_id,
            "ok": False,
            "error": "Evidence-fenced leaves are read-only in generic Kanban; use the Workflow controller",
        }
    ]

    for method, path, payload in (
        ("delete", f"/api/plugins/kanban/tasks/{task_id}", None),
        ("post", f"/api/plugins/kanban/tasks/{task_id}/reclaim", {}),
        (
            "post",
            f"/api/plugins/kanban/tasks/{task_id}/reassign",
            {"profile": "other"},
        ),
        ("post", f"/api/plugins/kanban/tasks/{task_id}/specify", {}),
        ("post", f"/api/plugins/kanban/tasks/{task_id}/decompose", {}),
    ):
        response = client.request(method, path, json=payload)
        assert response.status_code == 409, response.text

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "protected runtime leaf"
        assert task.status == "ready"
        assert task.assignee == "coder"
        assert task.priority != 99


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


def test_websocket_events_exclude_protected_workflow_rows(
    client, monkeypatch, kanban_home
):
    from hermes_cli import web_server

    monkeypatch.setattr(
        web_server,
        "_ws_auth_ok",
        lambda ws: ws.query_params.get("token") == "ws-secret",
    )
    with kb.connect_closing() as conn:
        ordinary_id = kb.create_task(conn, title="ordinary event")
        protected_id = kb.create_task(
            conn,
            title="protected event",
            body=json.dumps({
                "schema": "hermes.execution-capsule.v1",
                "spec": {"repository": "org/repo", "campaign_issue": 9},
                "capsule": {},
            }),
            assignee="coder",
            workspace_kind="worktree",
            workspace_path=str(kanban_home / "protected-worktree"),
            leaf_key="github:org/repo:issue-9:leaf-ws:v1",
            leaf_family_key="github:org/repo:issue-9:leaf-ws",
            spec_hash="a" * 64,
            pin_sha="b" * 40,
            capsule_hash="c" * 64,
            evidence_paths=("src/**",),
            lease_policy="evidence",
        )

    with client.websocket_connect(
        "/api/plugins/kanban/events?token=ws-secret&since=0"
    ) as ws:
        frame = ws.receive_json()

    event_task_ids = {event["task_id"] for event in frame["events"]}
    assert ordinary_id in event_task_ids
    assert protected_id not in event_task_ids


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(f"/api/plugins/kanban/tasks/{tid}",
                     json={"status": "blocked", "block_reason": "wait"})

    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert all(r["ok"] for r in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {t["id"] for t in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


