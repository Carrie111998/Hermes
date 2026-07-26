from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from tools import browser_camofox
from tools import browser_tool


def _execution_task(conn):
    task_id = kb.create_task(
        conn,
        title="protected external draft",
        body="GRACE_LOOP_CONTRACT_STAGE: execution",
        assignee="clawops-browser",
    )
    run = kb.claim_task(conn, task_id)
    assert run is not None
    return task_id, run


def test_browser_navigate_rejects_duplicate_external_create(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        kb.record_external_effect(
            conn,
            task_id,
            platform="facebook",
            state="verified",
            external_id="draft-123",
            expected_run_id=run.current_run_id,
        )

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))

    result = json.loads(
        browser_tool.browser_navigate(
            "https://www.facebook.com/marketplace/create/item",
        )
    )

    assert result["success"] is False
    assert "already verified" in result["error"]
    assert "draft-123" in result["error"]


def test_browser_navigate_reserves_first_create_for_current_run(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": True,
            "result": json.dumps({
                "href": "https://seller.shopee.tw/portal/product/new",
                "timeOrigin": 123.0,
            }),
        }),
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "url": "https://seller.shopee.tw/portal/product/new",
            "title": "Shopee",
            "snapshot": "",
        },
    )

    first = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )
    second = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )

    assert first["success"] is True
    assert second["success"] is False
    assert "already create_started" in second["error"]
    with kb.connect_closing(db_path) as conn:
        effects = kb.list_external_effects(conn, task_id)
    assert effects[0]["state"] == "create_started"
    assert effects[0]["run_id"] == run.current_run_id


def test_camofox_mutation_is_guarded_before_backend_call(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": True,
            "result": json.dumps({
                "href": "https://m.facebook.com/marketplace/create/item",
                "timeOrigin": 456.0,
            }),
        }),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "no active task-scoped reservation" in result["error"]
    assert "exact page load" in result["error"]
    assert called is False


def test_browser_mutation_fails_closed_without_page_identity(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": False,
            "error": "eval unavailable",
        }),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "failed closed" in result["error"]
    assert called is False


def test_protected_ref_action_is_blocked_even_with_bound_reservation(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    protected_url = "https://m.facebook.com/marketplace/create/item"
    page_identity = f"{protected_url}|789.0"
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        assert kb.reserve_external_create(
            conn,
            task_id,
            protected_url,
            expected_run_id=run.current_run_id,
        ) is None
        assert kb.bind_external_create_page(
            conn,
            task_id,
            protected_url,
            page_identity=page_identity,
            expected_run_id=run.current_run_id,
        ) is None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": True,
            "result": json.dumps({
                "href": protected_url,
                "timeOrigin": 789.0,
            }),
        }),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "cannot validate the exact page load atomically" in result["error"]
    assert called is False


def test_stale_worker_is_blocked_on_non_create_page(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id, run = _execution_task(conn)
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?",
            (task_id,),
        )
        conn.commit()

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: json.dumps({
            "success": True,
            "result": json.dumps({
                "href": "https://www.facebook.com/marketplace/you/selling",
                "timeOrigin": 999.0,
            }),
        }),
    )

    called = False

    def unexpected_click(ref, task_id=None):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(browser_camofox, "camofox_click", unexpected_click)
    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is False
    assert "not the active worker run" in result["error"]
    assert called is False


def test_required_create_binding_rejects_live_redirect_away(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_redirected")
    monkeypatch.setattr(
        browser_tool,
        "_browser_page_identity",
        lambda _task_id: (
            "https://seller.shopee.tw/portal/product/list/all",
            "https://seller.shopee.tw/portal/product/list/all|1000",
            None,
        ),
    )

    error = browser_tool._bind_external_create_page(
        "browser-1",
        require_protected=True,
    )

    assert error is not None
    assert "no longer a protected create route" in error


def test_non_grace_kanban_task_keeps_ordinary_ref_actions(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary browser task",
            body="Inspect a local dashboard",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("ordinary tasks must not require page identity"),
        ),
    )
    monkeypatch.setattr(
        browser_camofox,
        "camofox_click",
        lambda ref, task_id=None: json.dumps({
            "success": True,
            "clicked": ref,
        }),
    )

    result = json.loads(browser_tool.browser_click("@e1", task_id="browser-1"))

    assert result["success"] is True


def test_non_grace_navigation_skips_create_binding_probe(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary browser navigation",
            body="Inspect a route",
            assignee="clawops-browser",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(
        browser_tool,
        "_browser_eval",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("ordinary navigation must not bind a page"),
        ),
    )
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": True,
            "data": {
                "url": "https://seller.shopee.tw/portal/product/new",
                "title": "Route",
                "snapshot": "",
            },
        },
    )

    result = json.loads(
        browser_tool.browser_navigate(
            "https://seller.shopee.tw/portal/product/new",
        )
    )

    assert result["success"] is True
