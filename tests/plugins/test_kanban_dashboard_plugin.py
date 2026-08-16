"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


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
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
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
    js = bundle.read_text(encoding="utf-8")

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


def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kb.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )


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


def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"


def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id)
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"


def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None


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


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert all(item["ok"] for item in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {task["id"] for task in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kb.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"


def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kb.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()


def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


def test_dashboard_done_actions_prompt_for_completion_summary():
    """Behavioral coverage for the migrated ``requestDialog`` flow.

    Replaces the prior bundle-string-only assertion (which only proved the
    rename landed). The dialog state machine at
    ``plugins/kanban/dashboard/dist/index.js`` resolves with
    ``{confirmed: true|false, summary?}``. Each migrated call site must
    gate the dispatch on the resolved ``confirmed`` flag. This test
    asserts that contract at two layers:

    1. **Bundle cancel guards**: every migrated site gates on ``r.confirmed``
       (or its subscripted alias ``r1.confirmed``/``r2.confirmed``) before
       dispatching. We verify by counting the cancel-guard patterns +
       cross-referencing against the 8 migrated sites listed in the PR
       description.
    2. **Visual affordance**: every destructive ``requestDialog`` call marks
       ``destructive: true`` so the host renders the destructive variant.

    The dispatch path itself (PATCH/DELETE actually firing on confirm, not
    on cancel) is covered by the backend behavioral tests
    ``test_dashboard_confirm_dispatches_expected_*`` and
    ``test_dashboard_cancel_keeps_task_in_old_status`` below — together
    they pin the contract end-to-end.
    """

    repo_root = Path(__file__).resolve().parents[2]
    js = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    import re

    # Match ``if (!r.confirmed)``, ``if (!r1.confirmed)``, ``if (r.confirmed)``
    # (positive-form gate). The bundle uses both polarities:
    # - negative ``if (!r.confirmed) return null;`` in dialog flow bodies
    # - positive ``if (r.confirmed) props.onDeleteBoard(...);`` in JSX handlers
    cancel_guard_pattern = re.compile(
        r"if\s*\(\s*!?\s*r\d?\.confirmed\s*\)",
        re.IGNORECASE,
    )
    guards = cancel_guard_pattern.findall(js)
    # 8 migrated sites per the PR description:
    # moveTask (1), moveSelected (1), applyBulk (1), deleteTask (1),
    # deleteSelected (1), archiveBoard (1), removeAttachment (1), doPatch (1).
    # Plus performMoveTask callers (moveTask/moveSelected each have
    # ``r1.confirmed`` + ``r2.confirmed`` for the two-stage flow) → up to
    # 10 guards. Loose lower bound to avoid brittleness.
    assert len(guards) >= 8, (
        f"expected >= 8 `if (r?.confirmed)` cancel guards in bundle (one "
        f"per migrated site, plus extras for two-stage flows); found {len(guards)}"
    )

    # Visual affordance: every destructive requestDialog call must mark
    # ``destructive: true`` so the host renders the destructive variant.
    # deleteTask, deleteSelected, archiveBoard → at least 3.
    destructive_call_count = js.count("destructive: true")
    assert destructive_call_count >= 3, (
        f"expected >= 3 `destructive: true` requestDialog calls (single "
        f"delete, bulk delete, archive-board); found {destructive_call_count}"
    )


def test_dashboard_cancel_keeps_task_dispatch_suppressed():
    """Execute the production-used dialog-owner cancel path."""
    js = _dashboard_bundle_source()
    helper_source = js[
        js.index("function settleDialogOwner"):
        js.index("function useKanbanDialogs")
    ]
    hook = js[
        js.index("function useKanbanDialogs"):
        js.index("const API =", js.index("function useKanbanDialogs"))
    ]
    assert (
        "return settleDialogOwner(resolverRef, setDialogState, confirmed, extras);"
        in hook
    )
    assert (
        "const onCancel = React.useCallback(function () { close(false, null); }"
        in hook
    )

    probe = helper_source + """
const resolverRef = {current: null};
let dialogState = {kind: "confirm"};
let requests = 0;
const pending = new Promise(function (resolve) { resolverRef.current = resolve; });
const settled = settleDialogOwner(
  resolverRef,
  function (next) { dialogState = next; },
  false,
  null
);
pending.then(function (result) {
  if (result.confirmed) requests += 1;
  console.log(JSON.stringify({
    settled,
    confirmed: result.confirmed,
    ownerReleased: resolverRef.current === null,
    dialogState,
    requests
  }));
});
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "settled": True,
        "confirmed": False,
        "ownerReleased": True,
        "dialogState": None,
        "requests": 0,
    }


def test_dashboard_confirm_dispatches_expected_patch_body(client):
    """Behavioral: the PATCH body shape the bundle produces on confirm
    (status + result + summary) must be accepted by the backend without
    rejection. The backend stores ``result`` as the human-readable
    completion summary (the bundle comments confirm ``summary`` is sent
    duplicatively so the backend can store the value under its preferred
    key while the wire format remains explicit).
    This is the contract the bundle's performMoveTask relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Bundle's performMoveTask on confirm with a summary produces:
    #   { status, result: summary, summary: summary }
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped", "summary": "shipped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "done"
    assert body.get("result") == "shipped"


def test_dashboard_confirm_dispatches_expected_delete(client):
    """Behavioral: the DELETE call the bundle issues on confirm
    (``fetchJSON(`${API}/tasks/${id}`, { method: 'DELETE' })``) must
    succeed and remove the task.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200, r.text
    # 404 on the now-deleted task confirms removal.
    r2 = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r2.status_code == 404


def test_dashboard_surfaces_ready_blocked_error_inline():
    """Regression for #26744: failed status transitions must be surfaced
    inline, not swallowed.  The drag/drop banner and the drawer's action
    row each render the parsed API ``detail`` so operators see *why*
    their click did nothing.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    # Helper that strips ``"409: {\"detail\":\"…\"}"`` down to the
    # human-readable message before it lands in any banner.
    assert "function parseApiErrorMessage(err)" in bundle
    assert "parsed.detail" in bundle

    # Drag/drop banner now uses the parsed message instead of raw
    # ``err.message`` so it no longer leaks HTTP plumbing.
    assert "setError(tx(t, \"moveFailed\", \"Move failed: \") + parseApiErrorMessage(err))" in bundle

    # Drawer action row has its own visible error surface and clears it
    # on success/refresh so stale failures don't follow the operator
    # around.
    assert "const [patchErr, setPatchErr] = useState(null);" in bundle
    assert "setPatchErr(parseApiErrorMessage(e))" in bundle
    assert "setPatchErr(null)" in bundle


def test_dashboard_dependency_selects_use_value_change_handler():
    """Regression for the dependency selects in the task drawer: the
    add-parent / add-child dropdowns must wire through the shared
    selectChangeHandler helper so their value actually lands on the
    underlying React state. Salvaged from #20019 @LeonSGP43.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    parent_select = (
        'value: newParent,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewParent))'
    )
    child_select = (
        'value: newChild,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewChild))'
    )

    assert parent_select in bundle
    assert child_select in bundle


def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "archive": True})
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids


def test_bulk_reassign(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "old"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks",
                    json={"title": "b", "assignee": "old"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "assignee": "new"})
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"


def test_bulk_unassign_via_empty_string(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "x"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"]], "assignee": ""})
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None


def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7


def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


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


def _dashboard_bundle_source():
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text(encoding="utf-8")


def test_dashboard_orchestration_policy_lifecycle_in_shipped_bundle():
    """Exercise the real registered bundle in headless Chromium."""
    repo_root = Path(__file__).resolve().parents[2]
    harness = (
        repo_root
        / "tests"
        / "plugins"
        / "kanban"
        / "dashboard_orchestration_bundle_harness.mjs"
    )
    node = shutil.which("node")
    assert node is not None, "declared Playwright runtime is absent: node not found"

    completed = subprocess.run(
        [node, str(harness)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"bundle harness failed ({completed.returncode})\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout) == {
        "ok": True,
        "board": "beta",
        "put_url": "/api/plugins/kanban/orchestration?board=beta",
        "put_body": {"allowed_profiles": []},
        "checks": 6,
    }


def test_dashboard_task_deletes_pin_the_board_captured_before_confirmation():
    js = _dashboard_bundle_source()
    single_start = js.index("const deleteTask = useCallback")
    bulk_start = js.index("const deleteSelected = useCallback", single_start)
    render_start = js.index("// --- render", bulk_start)
    single = js[single_start:bulk_start]
    bulk = js[bulk_start:render_start]

    assert single.index("const requestBoard = board;") < single.index(
        "kanbanDialogs.request({"
    )
    assert single.index(
        "const requestGeneration = boardActionGenerationRef.current;"
    ) < single.index("kanbanDialogs.request({")
    assert (
        "SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, "
        "requestBoard), {"
    ) in single
    assert "method: \"DELETE\"" in single
    assert single.count("isExactBoardRequestCurrent(") == 2

    assert bulk.index("const requestBoard = board;") < bulk.index(
        "kanbanDialogs.request({"
    )
    assert bulk.index(
        "const requestGeneration = boardActionGenerationRef.current;"
    ) < bulk.index("kanbanDialogs.request({")
    assert bulk.index("const ids = Array.from(selectedIds);") < bulk.index(
        "kanbanDialogs.request({"
    )
    assert (
        "withBoard(`${API}/tasks/${encodeURIComponent(id)}`, requestBoard)"
    ) in bulk
    assert "SDK.fetchJSON(`${API}/tasks/" not in single + bulk
    assert bulk.count("isExactBoardRequestCurrent(") == 3


def test_dashboard_board_delete_completion_is_generation_local():
    js = _dashboard_bundle_source()
    start = js.index("const deleteBoard = useCallback")
    end = js.index("const deleteTask = useCallback", start)
    delete_board = js[start:end]

    assert delete_board.index("const requestBoard = board;") < delete_board.index(
        "SDK.fetchJSON("
    )
    assert delete_board.index(
        "const requestGeneration = boardActionGenerationRef.current;"
    ) < delete_board.index("SDK.fetchJSON(")
    guard = delete_board.index("if (!isExactBoardRequestCurrent(")
    assert guard < delete_board.index("loadBoardList();")
    assert guard < delete_board.index('switchBoard("default");')
    assert "if (requestBoard === slug)" in delete_board


def test_dashboard_exact_board_request_helper_rejects_deferred_a_completion():
    js = _dashboard_bundle_source()
    helper_source = js[
        js.index("const ASSIGNEE_UNASSIGNED"):
        js.index("// The SDK's Select component")
    ]
    probe = helper_source + """
const boardRef = {current: "board-a"};
const generationRef = {current: 7};
const requestBoard = boardRef.current;
const requestGeneration = generationRef.current;
const mutations = {success: 0, catch: 0, finally: 0, load: 0};
function settle(kind) {
  if (!isExactBoardRequestCurrent(
    boardRef, generationRef, requestBoard, requestGeneration
  )) return;
  mutations[kind] += 1;
  mutations.load += 1;
}
boardRef.current = "board-b";
generationRef.current += 1;
settle("success");
settle("catch");
settle("finally");
const afterB = isExactBoardRequestCurrent(
  boardRef, generationRef, requestBoard, requestGeneration
);
boardRef.current = "board-a";
const afterReturnToA = isExactBoardRequestCurrent(
  boardRef, generationRef, requestBoard, requestGeneration
);
console.log(JSON.stringify({afterB, afterReturnToA, mutations}));
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "afterB": False,
        "afterReturnToA": False,
        "mutations": {"success": 0, "catch": 0, "finally": 0, "load": 0},
    }


def test_dashboard_exact_drawer_load_helper_rejects_stale_task_and_board():
    js = _dashboard_bundle_source()
    helper_source = js[
        js.index("const ASSIGNEE_UNASSIGNED"):
        js.index("// The SDK's Select component")
    ]
    probe = helper_source + """
const identityA = taskDrawerIdentity("board-a", "task-1");
const identityB = taskDrawerIdentity("board-a", "task-2");
const identityOtherBoard = taskDrawerIdentity("board-b", "task-2");
const state = createTaskRequestState(identityA);
const loadA = beginTaskLoad(state, identityA);
let rendered = null;
let staleWrites = 0;

// Linked navigation can batch close/open. Starting B must invalidate A even
// when the component has not unmounted yet.
const loadB = beginTaskLoad(state, identityB);
if (isTaskLoadCurrent(state, loadB)) rendered = "task-2";
if (isTaskLoadCurrent(state, loadA)) {
  rendered = "task-1";
  staleWrites += 1;
}
const oldIdentityActive = isTaskIdentityActive(state, identityA);
const currentIdentityActive = isTaskIdentityActive(state, identityB);
deactivateTaskRequests(state);
if (isTaskLoadCurrent(state, loadB)) staleWrites += 1;

console.log(JSON.stringify({
  identitiesDiffer: identityA !== identityB && identityB !== identityOtherBoard,
  rendered,
  staleWrites,
  oldIdentityActive,
  currentIdentityActive,
  activeAfterUnmount: isTaskIdentityActive(state, identityB)
}));
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "identitiesDiffer": True,
        "rendered": "task-2",
        "staleWrites": 0,
        "oldIdentityActive": False,
        "currentIdentityActive": True,
        "activeAfterUnmount": False,
    }


def test_dashboard_owned_operation_helper_serializes_and_is_stale_safe():
    js = _dashboard_bundle_source()
    helper_source = js[
        js.index("const ASSIGNEE_UNASSIGNED"):
        js.index("// The SDK's Select component")
    ]
    probe = helper_source + """
const owner = createOperationOwner();
let profileRequests = 0;
function beginProfileWrite(board, name, kind) {
  const token = claimOwnedOperation(owner, {board, name, kind});
  if (token === null) return null;
  profileRequests += 1;
  return token;
}
const saveA = beginProfileWrite("a", "alpha", "save");
const busyOnA = operationOwnerIsBusy(owner);
// A board transition does not touch the owner: profile endpoints are global.
const autoBWhileABusy = beginProfileWrite("b", "beta", "auto");
const requestsWhileABusy = profileRequests;
const busyOnB = operationOwnerIsBusy(owner);
const releaseA = releaseOwnedOperation(owner, saveA);
const nextSaveB = beginProfileWrite("b", "beta", "save");
const staleReleaseA = releaseOwnedOperation(owner, saveA);
const bStillOwned = ownsOperation(owner, nextSaveB);
const releaseB = releaseOwnedOperation(owner, nextSaveB);

const createOwner = createOperationOwner();
const form = {title: "retry me", workspace: "/repo", assignee: "builder"};
const firstCreate = claimOwnedOperation(createOwner, {kind: "create-task"});
const duplicateCreate = claimOwnedOperation(createOwner, {kind: "create-task"});
const resetAfterReject = settleInlineCreateSubmission(createOwner, firstCreate, false);
if (resetAfterReject) Object.keys(form).forEach((key) => { form[key] = ""; });
const retryCreate = claimOwnedOperation(createOwner, {kind: "create-task"});
const resetAfterSuccess = settleInlineCreateSubmission(createOwner, retryCreate, true);
const preservedForRetry = Object.assign({}, form);
if (resetAfterSuccess) Object.keys(form).forEach((key) => { form[key] = ""; });

console.log(JSON.stringify({
  saveClaimed: saveA !== null,
  busyOnA,
  secondBoardWriteBlocked: autoBWhileABusy === null,
  requestsWhileABusy,
  busyOnB,
  releaseA,
  nextBoardWriteAllowed: nextSaveB !== null,
  profileRequests,
  staleReleaseA,
  bStillOwned,
  releaseB,
  busyAfterB: operationOwnerIsBusy(owner),
  duplicateCreateBlocked: duplicateCreate === null,
  resetAfterReject,
  preservedForRetry,
  retryClaimed: retryCreate !== null,
  resetAfterSuccess,
  formAfterSuccess: form
}));
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "saveClaimed": True,
        "busyOnA": True,
        "secondBoardWriteBlocked": True,
        "requestsWhileABusy": 1,
        "busyOnB": True,
        "releaseA": True,
        "nextBoardWriteAllowed": True,
        "profileRequests": 2,
        "staleReleaseA": False,
        "bStillOwned": True,
        "releaseB": True,
        "busyAfterB": False,
        "duplicateCreateBlocked": True,
        "resetAfterReject": False,
        "preservedForRetry": {
            "title": "retry me",
            "workspace": "/repo",
            "assignee": "builder",
        },
        "retryClaimed": True,
        "resetAfterSuccess": True,
        "formAfterSuccess": {"title": "", "workspace": "", "assignee": ""},
    }


def test_dashboard_drawer_identity_and_accessibility_wiring():
    js = _dashboard_bundle_source()
    page = js[
        js.index("function KanbanPage()"):
        js.index("function collectDiagTasks")
    ]
    drawer = js[js.index("function TaskDrawer"):js.index("function _fmtBytes")]

    assert "key: taskDrawerIdentity(board, selectedTaskId)" in page
    assert drawer.index("setData(null);") < drawer.index("SDK.fetchJSON(")
    assert "const loadToken = beginTaskLoad(" in drawer
    assert "taskDrawerIdentity(request.board, d.task.id) !== request.identity" in drawer
    assert "const displayedData = dataIdentity === drawerIdentity ? data : null;" in drawer
    assert "if (props.onOpenTask) props.onOpenTask(taskId);" in drawer
    linked_open = drawer[drawer.index("onOpenTask: function (taskId)"):]
    linked_open = linked_open[:linked_open.index("requestDialog:")]
    assert "props.onClose()" not in linked_open

    for contract in (
        'role: "dialog"',
        '"aria-modal": true',
        '"aria-labelledby": drawerHeadingId',
        'id: drawerHeadingId',
        "ref: closeRef",
        "closeRef.current.focus();",
        'if (e.key === "Escape") closeDrawer();',
        "previousFocus.focus();",
    ):
        assert contract in drawer


def test_dashboard_count_changing_task_actions_refresh_board_counts():
    js = _dashboard_bundle_source()
    page = js[
        js.index("function KanbanPage()"):
        js.index("function collectDiagTasks")
    ]
    drawer = js[js.index("function TaskDrawer"):js.index("function _fmtBytes")]

    create = page[page.index("const createTask"):page.index("const toggleSelected")]
    assert create.index("if (!isExactBoardRequestCurrent(") < create.index(
        "loadBoardList();"
    )

    move = page[page.index("const performMoveTask"):page.index(
        "// Pre-dispatch dialog step"
    )]
    assert move.count('if (newStatus === "archived") loadBoardList();') == 2

    bulk = page[page.index("const applyBulk"):page.index("// --- board switching")]
    assert "finalPatch.archive === true || finalPatch.status === \"archived\"" in bulk
    assert bulk.index("if (!actionIsCurrent()) return res;") < bulk.index(
        "loadBoardList();"
    )

    single_delete = page[page.index("const deleteTask"):page.index(
        "const deleteSelected"
    )]
    bulk_delete = page[page.index("const deleteSelected"):page.index("// --- render")]
    for delete_path in (single_delete, bulk_delete):
        assert delete_path.index("if (!isExactBoardRequestCurrent(") < (
            delete_path.index("loadBoardList();")
        )

    assert "onCountsRefresh: loadBoardList" in page
    assert 'finalPatch.status === "archived" && props.onCountsRefresh' in drawer
    assert "if (props.onCountsRefresh) props.onCountsRefresh();" in drawer


def test_dashboard_inline_create_waits_for_success_and_surfaces_rejection():
    js = _dashboard_bundle_source()
    column = js[js.index("function BoardColumn"):js.index("function TaskCard")]
    inline = js[js.index("function InlineCreate"):js.index("function TaskDrawer")]

    assert "return props.onCreate(body);" in column
    assert "onSuccess: function () { setShowCreate(false); }" in column
    assert "const operationToken = claimOwnedOperation(" in inline
    assert "if (operationToken === null) return Promise.resolve(null);" in inline
    assert inline.index("return props.onSubmit(body);") < inline.index(
        "setTitle(\"\")"
    )
    assert "settleInlineCreateSubmission(" in inline
    assert "setSubmitError(tx(t, \"taskCreateFailed\"" in inline
    assert 'role: "alert"' in inline
    assert "disabled: submitting || !title.trim()" in inline


def test_dashboard_task_and_bulk_completions_use_exact_board_identity():
    js = _dashboard_bundle_source()
    page = js[
        js.index("function KanbanPage()"):
        js.index("function collectDiagTasks")
    ]
    move = page[
        page.index("const performMoveTask"):
        page.index("// Pre-dispatch dialog step")
    ]
    apply_bulk = page[
        page.index("const applyBulk"):
        page.index("// --- board switching")
    ]

    assert "withBoard(`${API}/tasks/bulk`, requestBoard)" in move
    assert "`${API}/tasks/${encodeURIComponent(taskId)}`, requestBoard" in move
    assert move.count("if (!actionIsCurrent())") >= 3
    assert "if (!b || !actionIsCurrent()) return b;" in move

    assert "const requestBoard = board;" in apply_bulk
    assert "const requestGeneration = boardActionGenerationRef.current;" in apply_bulk
    assert "withBoard(`${API}/tasks/bulk`, requestBoard)" in apply_bulk
    assert apply_bulk.count("if (!actionIsCurrent())") == 2
    assert "if (!b || !actionIsCurrent()) return b;" in apply_bulk


def test_dashboard_create_and_nudge_callbacks_pin_exact_board_identity():
    js = _dashboard_bundle_source()
    page = js[
        js.index("function KanbanPage()"):
        js.index("function collectDiagTasks")
    ]
    create = page[
        page.index("const createTask = useCallback"):
        page.index("const toggleSelected", page.index("const createTask = useCallback"))
    ]
    toolbar = page[
        page.index("onNudgeDispatch: function ()"):
        page.index("onRefresh: loadBoard", page.index("onNudgeDispatch: function ()"))
    ]

    # Both requests capture identity before dispatch and pin the HTTP URL to it.
    for callback in (create, toolbar):
        assert callback.index("const requestBoard = board;") < callback.index(
            "SDK.fetchJSON("
        )
        assert callback.index(
            "const requestGeneration = boardActionGenerationRef.current;"
        ) < callback.index("SDK.fetchJSON(")
        assert (
            "boardRef, boardActionGenerationRef, requestBoard, requestGeneration,"
        ) in callback
    assert "withBoard(`${API}/tasks`, requestBoard)" in create
    assert "withBoard(`${API}/dispatch?max=8`, requestBoard)" in toolbar

    # Create success cannot publish A's warning or reload callbacks onto B.
    create_guard = create.index("if (!isExactBoardRequestCurrent(")
    assert create_guard < create.index("setError(")
    assert create_guard < create.index("loadBoard();")
    assert create_guard < create.index("loadBoardList();")

    # Nudge success and error each independently reject stale A completion.
    assert toolbar.count("if (!isExactBoardRequestCurrent(") == 2
    success_guard = toolbar.index("if (!isExactBoardRequestCurrent(")
    error_guard = toolbar.index("if (!isExactBoardRequestCurrent(", success_guard + 1)
    assert success_guard < toolbar.index("loadBoard()")
    assert error_guard < toolbar.index("setError(")


def test_dashboard_board_load_and_websocket_are_generation_local():
    js = _dashboard_bundle_source()
    page = js[
        js.index("function KanbanPage()"):
        js.index("function collectDiagTasks")
    ]

    # Full-board and page-level profile loads reject stale success/error/finally.
    assert "const boardActionGenerationRef = useRef(0);" in page
    assert "const boardLoadGenerationRef = useRef(0);" in page
    assert "const profilesGenerationRef = useRef(0);" in page
    assert page.count(
        "boardRef, boardLoadGenerationRef, requestBoard, requestGeneration,"
    ) == 3
    assert "SDK.fetchJSON(withBoard(`${API}/profiles`, requestBoard))" in page
    assert page.count(
        "boardRef, profilesGenerationRef, requestBoard, requestGeneration,"
    ) == 2

    # Each WS effect owns its close flag, socket, timer, and backoff.
    for contract in (
        "let closed = false;",
        "let socket = null;",
        "let reconnectTimer = null;",
        "let backoff = 1000;",
        "if (closed || boardRef.current !== socketBoard) return;",
        "if (reconnectTimer) clearTimeout(reconnectTimer);",
        "try { ws.close(); } catch (_e) { /* noop */ }",
    ):
        assert contract in page
    assert "wsClosedRef" not in page
    assert "wsBackoffRef" not in page
    assert "wsRef" not in page

    # The visible error-only state retries through the production exact-board
    # helper. A callback retained from A cannot start loading after B is active.
    error_render = page[
        page.index("if (error && !boardData)"):
        page.index("if (!filteredBoard)")
    ]
    assert "h(Button, {" in error_render
    assert "onClick: retryBoardLoad," in error_render
    assert 'tx(t, "retry", "Retry")' in error_render
    assert (
        "return retryExactBoardLoad(boardRef, board, setLoading, loadBoard);"
        in page
    )

    helper_source = js[
        js.index("const ASSIGNEE_UNASSIGNED"):
        js.index("// The SDK's Select component")
    ]
    probe = helper_source + """
const retryBoardRef = {current: "board-a"};
const loadingFor = [];
const requests = [];
function retry(capturedBoard) {
  return retryExactBoardLoad(
    retryBoardRef,
    capturedBoard,
    function () { loadingFor.push(capturedBoard); },
    function () {
      requests.push(capturedBoard);
      return Promise.resolve({board: capturedBoard});
    }
  );
}
const staleRetryA = function () { return retry("board-a"); };
retryBoardRef.current = "board-b";
Promise.all([staleRetryA(), retry("board-b")]).then(function (results) {
  console.log(JSON.stringify({loadingFor, requests, results}));
});
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "loadingFor": ["board-b"],
        "requests": ["board-b"],
        "results": [None, {"board": "board-b"}],
    }

    # A switch invalidates work and clears every board-local interaction surface.
    switch = page[page.index("const switchBoard"):page.index("const createNewBoard")]
    for contract in (
        "boardActionGenerationRef.current += 1;",
        "boardLoadGenerationRef.current += 1;",
        "profilesGenerationRef.current += 1;",
        "setSelectedTaskId(null);",
        "setShowNewBoard(false);",
        "setShowBoardSettings(false);",
        "setTaskEventTick({});",
        "kanbanDialogs.cancel();",
        "clearSelected();",
    ):
        assert contract in switch
    assert "key: board" in page


def test_dashboard_effective_roster_drives_every_assignment_surface():
    js = _dashboard_bundle_source()

    # Execute the production-used pure roster/sentinel helpers, rather than
    # only proving their names are present in the bundle.
    helper_source = js[
        js.index("const ASSIGNEE_UNASSIGNED"):
        js.index("// The SDK's Select component")
    ]
    probe = helper_source + """
const names = effectiveProfileNames([
  {name: "alpha", effective_allowed: true},
  {name: "historical", effective_allowed: false}
]);
console.log(JSON.stringify({
  names,
  allowed: canSubmitAssignee("alpha", names, true),
  historical: canSubmitAssignee("historical", names, true),
  unassigned: canSubmitAssignee(ASSIGNEE_UNASSIGNED, names, true),
  patchNone: assignmentPatchValue(ASSIGNEE_UNASSIGNED),
  createAuto: assignmentCreateValue(ASSIGNEE_DISPATCHER),
  normalizedAllowed: normalizeCreateAssignee("alpha", names),
  normalizedStale: normalizeCreateAssignee("historical", names),
  allowedPayload: {assignee: createTaskAssigneeValue("alpha", names)},
  stalePayload: {assignee: createTaskAssigneeValue("historical", names)},
  dispatcherPayload: {assignee: createTaskAssigneeValue(ASSIGNEE_DISPATCHER, names)},
  sentinelsDiffer: ASSIGNEE_UNASSIGNED !== ASSIGNEE_DISPATCHER
}));
"""
    completed = subprocess.run(
        ["node", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "names": ["alpha"],
        "allowed": True,
        "historical": False,
        "unassigned": True,
        "patchNone": "",
        "createAuto": None,
        "normalizedAllowed": "alpha",
        "normalizedStale": "__kanban_dispatcher__",
        "allowedPayload": {"assignee": "alpha"},
        "stalePayload": {"assignee": None},
        "dispatcherPayload": {"assignee": None},
        "sentinelsDiffer": True,
    }

    # Page roster flows through bulk, drawer/diagnostics, and create columns.
    assert "return profilesBoard === board ? effectiveProfileNames(profiles) : [];" in js
    assert js.count("effectiveAssignees: effectiveAssignees") >= 3
    assert "effectiveAssignees: props.effectiveAssignees" in js

    inline = js[js.index("function InlineCreate"):js.index("function TaskDrawer")]
    assert "useState(ASSIGNEE_DISPATCHER)" in inline
    assert "const controlledAssignee = normalizeCreateAssignee(" in inline
    assert "if (assignee !== controlledAssignee) setAssignee(controlledAssignee);" in inline
    assert "value: controlledAssignee" in inline
    assert "const payloadAssignee = createTaskAssigneeValue(" in inline
    assert "assignee, props.effectiveAssignees || []," in inline
    assert "assignee: payloadAssignee" in inline
    assert "props.effectiveAssignees || []" in inline
    assert "assignee.trim()" not in inline

    diagnostic = js[js.index("function DiagnosticCard"):js.index("function DiagnosticsSection")]
    assert "value: ASSIGNEE_UNASSIGNED" in diagnostic
    assert "profile: nextProfile || null" in diagnostic
    assert "canSubmitAssignee(reassignProfile, effectiveAssignees, true)" in diagnostic

    drawer = js[js.index("function AssigneeEditor"):js.index("function PriorityEditor")]
    assert "value: ASSIGNEE_UNASSIGNED" in drawer
    assert "disabled: true" in drawer
    assert "assignee: assignmentPatchValue(v)" in drawer
    assert "h(Input" not in drawer


# ---------------------------------------------------------------------------
# Board-scoped profile policy and orchestration settings
# ---------------------------------------------------------------------------


@pytest.fixture
def policy_boards(kanban_home):
    profiles_root = kanban_home / "profiles"
    for name in ("alpha", "beta", "blocked"):
        (profiles_root / name).mkdir(parents=True)

    kb.create_board("board-a", name="Board A")
    kb.create_board("board-b", name="Board B")
    kb.write_board_metadata("board-a", allowed_profiles=["alpha"])
    kb.write_board_metadata("board-b", allowed_profiles=["beta"])

    config_path = kanban_home / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "kanban": {
                    "allowed_profiles": ["default", "alpha", "beta"],
                    "orchestrator_profile": "alpha",
                    "default_assignee": "beta",
                    "auto_decompose": True,
                    "auto_promote_children": False,
                },
                "policy_test_sentinel": {"preserve": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "home": kanban_home,
        "config_path": config_path,
        "board_a_path": kb.board_metadata_path("board-a"),
        "board_b_path": kb.board_metadata_path("board-b"),
    }


def _profile_policy_flags(response):
    assert response.status_code == 200, response.text
    return {
        profile["name"]: {
            "machine_allowed": profile["machine_allowed"],
            "board_selected": profile["board_selected"],
            "effective_allowed": profile["effective_allowed"],
        }
        for profile in response.json()["profiles"]
    }


def test_profiles_are_all_visible_with_board_scoped_policy_flags(
    client, policy_boards,
):
    # A raw board subset can select a machine-blocked profile. It remains
    # visible and selected, but can never become effectively assignable.
    kb.write_board_metadata(
        "board-a",
        allowed_profiles=["alpha", "blocked"],
    )

    assert _profile_policy_flags(
        client.get("/api/plugins/kanban/profiles?board=board-a")
    ) == {
        "default": {
            "machine_allowed": True,
            "board_selected": False,
            "effective_allowed": False,
        },
        "alpha": {
            "machine_allowed": True,
            "board_selected": True,
            "effective_allowed": True,
        },
        "beta": {
            "machine_allowed": True,
            "board_selected": False,
            "effective_allowed": False,
        },
        "blocked": {
            "machine_allowed": False,
            "board_selected": True,
            "effective_allowed": False,
        },
    }
    assert _profile_policy_flags(
        client.get("/api/plugins/kanban/profiles?board=board-b")
    ) == {
        "default": {
            "machine_allowed": True,
            "board_selected": False,
            "effective_allowed": False,
        },
        "alpha": {
            "machine_allowed": True,
            "board_selected": False,
            "effective_allowed": False,
        },
        "beta": {
            "machine_allowed": True,
            "board_selected": True,
            "effective_allowed": True,
        },
        "blocked": {
            "machine_allowed": False,
            "board_selected": False,
            "effective_allowed": False,
        },
    }

    # Omitting board follows the current board selected by the connection.
    kb.set_current_board("board-b")
    assert _profile_policy_flags(
        client.get("/api/plugins/kanban/profiles")
    ) == _profile_policy_flags(
        client.get("/api/plugins/kanban/profiles?board=board-b")
    )


def test_get_orchestration_reports_raw_effective_and_resolved_per_board(
    client, policy_boards,
):
    board_a = client.get(
        "/api/plugins/kanban/orchestration?board=board-a"
    )
    assert board_a.status_code == 200, board_a.text
    assert board_a.json() == {
        "board": "board-a",
        "orchestrator_profile": "alpha",
        "default_assignee": "beta",
        "auto_decompose": True,
        "auto_promote_children": False,
        "resolved_orchestrator_profile": "alpha",
        "resolved_default_assignee": "alpha",
        "active_profile": "default",
        "board_allowed_profiles": ["alpha"],
        "effective_allowed_profiles": ["alpha"],
    }

    board_b = client.get(
        "/api/plugins/kanban/orchestration?board=board-b"
    )
    assert board_b.status_code == 200, board_b.text
    assert board_b.json()["board"] == "board-b"
    assert board_b.json()["board_allowed_profiles"] == ["beta"]
    assert board_b.json()["effective_allowed_profiles"] == ["beta"]
    assert board_b.json()["resolved_orchestrator_profile"] == "beta"
    assert board_b.json()["resolved_default_assignee"] == "beta"

    kb.write_board_metadata("board-a", allowed_profiles=[])
    empty = client.get(
        "/api/plugins/kanban/orchestration?board=board-a"
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["board_allowed_profiles"] == []
    assert empty.json()["effective_allowed_profiles"] == []
    assert empty.json()["resolved_orchestrator_profile"] is None
    assert empty.json()["resolved_default_assignee"] is None

    kb.set_current_board("board-b")
    current = client.get("/api/plugins/kanban/orchestration")
    assert current.status_code == 200, current.text
    assert current.json()["board"] == "board-b"
    assert current.json()["effective_allowed_profiles"] == ["beta"]


def test_get_policy_endpoints_fail_closed_for_non_mapping_kanban_config(
    client, policy_boards,
):
    config_path = policy_boards["config_path"]
    board_path = policy_boards["board_a_path"]
    config_path.write_text(
        json.dumps(
            {
                "kanban": ["not", "a", "mapping"],
                "policy_test_sentinel": {"preserve": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    board_before = board_path.read_bytes()

    roster = client.get("/api/plugins/kanban/profiles?board=board-a")
    assert roster.status_code == 200, roster.text
    assert all(
        not profile["machine_allowed"] and not profile["effective_allowed"]
        for profile in roster.json()["profiles"]
    )

    settings = client.get(
        "/api/plugins/kanban/orchestration?board=board-a"
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["orchestrator_profile"] == ""
    assert settings.json()["default_assignee"] == ""
    assert settings.json()["auto_decompose"] is True
    assert settings.json()["auto_promote_children"] is True
    assert settings.json()["board_allowed_profiles"] == ["alpha"]
    assert settings.json()["effective_allowed_profiles"] == []
    assert settings.json()["resolved_orchestrator_profile"] is None
    assert settings.json()["resolved_default_assignee"] is None
    assert config_path.read_bytes() == config_before
    assert board_path.read_bytes() == board_before


def test_get_orchestration_uses_safe_defaults_for_malformed_config_leaves(
    client, policy_boards,
):
    config_path = policy_boards["config_path"]
    board_path = policy_boards["board_a_path"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["kanban"].update(
        {
            "orchestrator_profile": [],
            "default_assignee": {"profile": "beta"},
            "auto_decompose": "false",
        }
    )
    config_path.write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    board_before = board_path.read_bytes()
    other_board_before = policy_boards["board_b_path"].read_bytes()

    response = client.get(
        "/api/plugins/kanban/orchestration?board=board-a"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "board": "board-a",
        "orchestrator_profile": "",
        "default_assignee": "",
        "auto_decompose": True,
        "auto_promote_children": False,
        "resolved_orchestrator_profile": "alpha",
        "resolved_default_assignee": "alpha",
        "active_profile": "default",
        "board_allowed_profiles": ["alpha"],
        "effective_allowed_profiles": ["alpha"],
    }
    assert config_path.read_bytes() == config_before
    assert board_path.read_bytes() == board_before
    assert policy_boards["board_b_path"].read_bytes() == other_board_before


def test_put_preserves_unrelated_malformed_config_leaves(
    client, policy_boards,
):
    from hermes_cli.config import read_raw_config

    config_path = policy_boards["config_path"]
    board_path = policy_boards["board_a_path"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["kanban"].update(
        {
            "orchestrator_profile": [],
            "default_assignee": {"profile": "beta"},
            "auto_decompose": "false",
        }
    )
    config_path.write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    board_before = board_path.read_bytes()
    other_board_before = policy_boards["board_b_path"].read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"auto_promote_children": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["orchestrator_profile"] == ""
    assert response.json()["default_assignee"] == ""
    assert response.json()["auto_decompose"] is True
    assert response.json()["auto_promote_children"] is True
    assert response.json()["resolved_orchestrator_profile"] == "alpha"
    assert response.json()["resolved_default_assignee"] == "alpha"
    assert response.json()["effective_allowed_profiles"] == ["alpha"]

    config["kanban"]["auto_promote_children"] = True
    saved_config = read_raw_config()
    assert isinstance(saved_config.pop("_config_version"), int)
    assert saved_config == config
    assert board_path.read_bytes() == board_before
    assert policy_boards["board_b_path"].read_bytes() == other_board_before


def test_put_allowed_profiles_tri_state_isolated_and_config_write_free(
    client, policy_boards,
):
    config_path = policy_boards["config_path"]
    board_b_path = policy_boards["board_b_path"]
    original_config = config_path.read_bytes()
    original_board_b = board_b_path.read_bytes()

    selected = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": [" Alpha ", "alpha"]},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["board_allowed_profiles"] == ["alpha"]
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == ["alpha"]
    assert config_path.read_bytes() == original_config
    assert board_b_path.read_bytes() == original_board_b

    inherited = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": None},
    )
    assert inherited.status_code == 200, inherited.text
    assert inherited.json()["board_allowed_profiles"] is None
    assert inherited.json()["effective_allowed_profiles"] == [
        "alpha",
        "beta",
        "default",
    ]
    assert config_path.read_bytes() == original_config
    assert board_b_path.read_bytes() == original_board_b

    empty = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": []},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["board_allowed_profiles"] == []
    assert config_path.read_bytes() == original_config
    assert board_b_path.read_bytes() == original_board_b

    # Omitting allowed_profiles preserves the explicit empty board policy.
    global_update = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"auto_decompose": False},
    )
    assert global_update.status_code == 200, global_update.text
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == []
    assert kb.read_board_metadata("board-b")["allowed_profiles"] == ["beta"]

    # A no-board PUT follows the connection/current board instead of default.
    config_after_global_update = config_path.read_bytes()
    kb.set_current_board("board-b")
    current_update = client.put(
        "/api/plugins/kanban/orchestration",
        json={"allowed_profiles": ["alpha"]},
    )
    assert current_update.status_code == 200, current_update.text
    assert current_update.json()["board"] == "board-b"
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == []
    assert kb.read_board_metadata("board-b")["allowed_profiles"] == ["alpha"]
    assert config_path.read_bytes() == config_after_global_update


def test_put_keeps_global_writes_and_scopes_new_policy_to_requested_board(
    client, policy_boards,
):
    board_b_before = policy_boards["board_b_path"].read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={
            "allowed_profiles": [" Beta ", "beta"],
            "orchestrator_profile": "BETA",
            "default_assignee": "beta",
            "auto_decompose": False,
            "auto_promote_children": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["board_allowed_profiles"] == ["beta"]
    assert response.json()["resolved_orchestrator_profile"] == "beta"
    assert response.json()["resolved_default_assignee"] == "beta"
    assert response.json()["auto_decompose"] is False
    assert response.json()["auto_promote_children"] is True
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == ["beta"]
    assert policy_boards["board_b_path"].read_bytes() == board_b_before

    from hermes_cli.config import load_config

    kanban_config = load_config()["kanban"]
    assert kanban_config["allowed_profiles"] == ["default", "alpha", "beta"]
    assert kanban_config["orchestrator_profile"] == "beta"
    assert kanban_config["default_assignee"] == "beta"
    assert kanban_config["auto_decompose"] is False
    assert kanban_config["auto_promote_children"] is True


@pytest.mark.parametrize(
    ("allowed_profiles", "detail_fragment"),
    [
        (["bad/name"], "invalid"),
        ([123], "invalid"),
        (["missing"], "does not exist"),
        (["blocked"], "machine ceiling"),
    ],
)
def test_put_rejects_invalid_uninstalled_or_machine_blocked_board_profiles(
    client, policy_boards, allowed_profiles, detail_fragment,
):
    board_path = policy_boards["board_a_path"]
    config_path = policy_boards["config_path"]
    board_before = board_path.read_bytes()
    config_before = config_path.read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": allowed_profiles},
    )

    assert response.status_code == 400, response.text
    assert detail_fragment in response.json()["detail"].lower()
    assert board_path.read_bytes() == board_before
    assert config_path.read_bytes() == config_before


@pytest.mark.parametrize("selector", ["orchestrator_profile", "default_assignee"])
def test_put_rejects_selector_disallowed_by_prospective_board_policy_atomically(
    client, policy_boards, selector,
):
    board_path = policy_boards["board_a_path"]
    config_path = policy_boards["config_path"]
    board_before = board_path.read_bytes()
    config_before = config_path.read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": ["beta"], selector: "alpha"},
    )

    assert response.status_code == 400, response.text
    assert "board" in response.json()["detail"].lower()
    assert board_path.read_bytes() == board_before
    assert config_path.read_bytes() == config_before


def test_policy_endpoints_reject_unknown_board_without_mutation(
    client, policy_boards,
):
    config_path = policy_boards["config_path"]
    config_before = config_path.read_bytes()

    for method, path, kwargs in (
        ("get", "/api/plugins/kanban/profiles?board=unknown", {}),
        ("get", "/api/plugins/kanban/orchestration?board=unknown", {}),
        (
            "put",
            "/api/plugins/kanban/orchestration?board=unknown",
            {"json": {"allowed_profiles": ["alpha"]}},
        ),
    ):
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, response.text

    assert not kb.board_exists("unknown")
    assert config_path.read_bytes() == config_before


@pytest.mark.parametrize(
    ("malformed_config", "detail_fragment"),
    [
        (["not", "a", "mapping"], "root"),
        ({"kanban": ["not", "a", "mapping"]}, "kanban"),
    ],
)
def test_put_refuses_malformed_global_config_before_any_mixed_write(
    client, policy_boards, malformed_config, detail_fragment,
):
    config_path = policy_boards["config_path"]
    board_path = policy_boards["board_a_path"]
    config_path.write_text(
        json.dumps(malformed_config, indent=2) + "\n",
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    board_before = board_path.read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": [], "auto_decompose": False},
    )

    assert response.status_code == 500, response.text
    detail = response.json()["detail"].lower()
    assert "malformed config" in detail
    assert detail_fragment in detail
    assert config_path.read_bytes() == config_before
    assert board_path.read_bytes() == board_before


def test_policy_only_put_remains_config_write_free_with_malformed_global_config(
    client, policy_boards,
):
    config_path = policy_boards["config_path"]
    config_path.write_text('["malformed-root"]\n', encoding="utf-8")
    config_before = config_path.read_bytes()

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": []},
    )

    assert response.status_code == 200, response.text
    assert response.json()["board_allowed_profiles"] == []
    assert config_path.read_bytes() == config_before
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == []


def test_malformed_board_policy_is_reported_and_validated_fail_closed(
    client, policy_boards,
):
    board_path = policy_boards["board_a_path"]
    config_path = policy_boards["config_path"]
    malformed_metadata = {
        "slug": "board-a",
        "name": "Board A",
        "allowed_profiles": "alpha",
        "policy_test_sentinel": {"preserve": True},
    }
    board_path.write_text(
        json.dumps(malformed_metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    roster = client.get("/api/plugins/kanban/profiles?board=board-a")
    assert roster.status_code == 200, roster.text
    assert all(
        not profile["board_selected"] and not profile["effective_allowed"]
        for profile in roster.json()["profiles"]
    )

    settings = client.get(
        "/api/plugins/kanban/orchestration?board=board-a"
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["board_allowed_profiles"] == []
    assert settings.json()["effective_allowed_profiles"] == []

    board_before = board_path.read_bytes()
    config_before = config_path.read_bytes()
    rejected = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"orchestrator_profile": "alpha"},
    )
    assert rejected.status_code == 400, rejected.text
    assert "effective policy" in rejected.json()["detail"].lower()
    assert board_path.read_bytes() == board_before
    assert config_path.read_bytes() == config_before

    repaired = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={"allowed_profiles": ["alpha"]},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["board_allowed_profiles"] == ["alpha"]
    assert kb.read_board_metadata("board-a")["policy_test_sentinel"] == {
        "preserve": True,
    }
    assert config_path.read_bytes() == config_before


def test_mixed_put_saves_config_before_board_and_leaves_board_on_config_failure(
    client, policy_boards, monkeypatch,
):
    from hermes_cli import config as config_mod

    config_path = policy_boards["config_path"]
    board_path = policy_boards["board_a_path"]
    config_before = config_path.read_bytes()
    board_before = board_path.read_bytes()
    calls = []

    def fail_config_save(_cfg):
        calls.append("config")
        raise OSError("config save exploded")

    def unexpected_board_write(*_args, **_kwargs):
        calls.append("board")
        raise AssertionError("board write ran before config save completed")

    monkeypatch.setattr(config_mod, "save_config", fail_config_save)
    monkeypatch.setattr(kb, "write_board_metadata", unexpected_board_write)

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={
            "allowed_profiles": ["beta"],
            "orchestrator_profile": "beta",
        },
    )

    assert response.status_code == 500, response.text
    assert "failed to save config" in response.json()["detail"].lower()
    assert calls == ["config"]
    assert config_path.read_bytes() == config_before
    assert board_path.read_bytes() == board_before


def test_mixed_put_rolls_config_back_when_board_write_fails(
    client, policy_boards, monkeypatch,
):
    from hermes_cli import config as config_mod

    original_save_config = config_mod.save_config
    original_config = config_mod.load_config()
    board_path = policy_boards["board_a_path"]
    board_before = board_path.read_bytes()
    calls = []

    def recording_save(cfg):
        calls.append("config")
        original_save_config(cfg)

    def fail_board_write(*_args, **_kwargs):
        calls.append("board")
        assert config_mod.load_config()["kanban"]["orchestrator_profile"] == "beta"
        raise OSError("board write exploded")

    monkeypatch.setattr(config_mod, "save_config", recording_save)
    monkeypatch.setattr(kb, "write_board_metadata", fail_board_write)

    response = client.put(
        "/api/plugins/kanban/orchestration?board=board-a",
        json={
            "allowed_profiles": ["beta"],
            "orchestrator_profile": "beta",
        },
    )

    assert response.status_code == 500, response.text
    assert "config restored" in response.json()["detail"].lower()
    assert calls == ["config", "board", "config"]
    assert config_mod.load_config() == original_config
    assert board_path.read_bytes() == board_before


def test_mixed_put_reports_config_rollback_failure(
    client, policy_boards, monkeypatch, caplog,
):
    from hermes_cli import config as config_mod

    original_save_config = config_mod.save_config
    board_path = policy_boards["board_a_path"]
    board_before = board_path.read_bytes()
    save_calls = 0

    def fail_second_save(cfg):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("rollback exploded")
        original_save_config(cfg)

    def fail_board_write(*_args, **_kwargs):
        raise OSError("board write exploded")

    monkeypatch.setattr(config_mod, "save_config", fail_second_save)
    monkeypatch.setattr(kb, "write_board_metadata", fail_board_write)

    with caplog.at_level("ERROR"):
        response = client.put(
            "/api/plugins/kanban/orchestration?board=board-a",
            json={
                "allowed_profiles": ["beta"],
                "orchestrator_profile": "beta",
            },
        )

    assert response.status_code == 500, response.text
    detail = response.json()["detail"].lower()
    assert "board write exploded" in detail
    assert "rollback failed" in detail
    assert "rollback exploded" in detail
    assert "rollback exploded" in caplog.text
    assert save_calls == 2
    assert board_path.read_bytes() == board_before


def test_mixed_put_transaction_serializes_rollback_before_later_success(
    client, policy_boards, monkeypatch,
):
    """A failed mixed write cannot roll stale config over a later success."""
    from hermes_cli import config as config_mod

    original_save_config = config_mod.save_config
    original_write_board_metadata = kb.write_board_metadata
    first_save_entered = threading.Event()
    release_first_save = threading.Event()
    second_save_entered = threading.Event()
    second_start = threading.Barrier(2)
    responses = {}
    save_calls = []
    board_calls = []

    def controlled_save(cfg):
        kanban_cfg = cfg["kanban"]
        profile = kanban_cfg["orchestrator_profile"]
        auto_decompose = kanban_cfg["auto_decompose"]
        save_calls.append((profile, auto_decompose))
        if profile == "beta":
            first_save_entered.set()
            assert release_first_save.wait(5), "timed out releasing first config save"
        elif profile == "alpha" and auto_decompose is False:
            second_save_entered.set()
        original_save_config(cfg)

    def fail_first_board_write(*args, **kwargs):
        allowed = kwargs.get("allowed_profiles")
        board_calls.append(tuple(allowed) if allowed is not None else None)
        if allowed == ["beta"]:
            raise OSError("first board write exploded")
        return original_write_board_metadata(*args, **kwargs)

    def run_first_request():
        responses["first"] = TestClient(client.app).put(
            "/api/plugins/kanban/orchestration?board=board-a",
            json={
                "allowed_profiles": ["beta"],
                "orchestrator_profile": "beta",
            },
        )

    def run_second_request():
        second_start.wait()
        responses["second"] = TestClient(client.app).put(
            "/api/plugins/kanban/orchestration?board=board-a",
            json={
                "allowed_profiles": ["alpha"],
                "orchestrator_profile": "alpha",
                "auto_decompose": False,
            },
        )

    monkeypatch.setattr(config_mod, "save_config", controlled_save)
    monkeypatch.setattr(kb, "write_board_metadata", fail_first_board_write)

    first_thread = threading.Thread(target=run_first_request, name="first-mixed-put")
    second_thread = threading.Thread(target=run_second_request, name="second-mixed-put")
    first_thread.start()
    try:
        assert first_save_entered.wait(5), "first request never entered config save"
        second_thread.start()
        second_start.wait()
        assert not second_save_entered.wait(0.5), (
            "second request entered config save while the first mixed transaction "
            "was blocked"
        )
    finally:
        release_first_save.set()
        first_thread.join(5)
        if second_thread.ident is not None:
            second_thread.join(5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert responses["first"].status_code == 500, responses["first"].text
    assert "config restored" in responses["first"].json()["detail"].lower()
    assert responses["second"].status_code == 200, responses["second"].text
    assert save_calls == [
        ("beta", True),
        ("alpha", True),
        ("alpha", False),
    ]
    assert board_calls == [("beta",), ("alpha",)]
    assert config_mod.load_config()["kanban"]["orchestrator_profile"] == "alpha"
    assert config_mod.load_config()["kanban"]["auto_decompose"] is False
    assert kb.read_board_metadata("board-a")["allowed_profiles"] == ["alpha"]

    plugin_module = sys.modules["hermes_dashboard_plugin_kanban_test"]
    transaction_lock = plugin_module._ORCHESTRATION_MUTATION_LOCK
    assert transaction_lock.acquire(blocking=False), (
        "orchestration transaction lock remained held after the board-write exception"
    )
    transaction_lock.release()
