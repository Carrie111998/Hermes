"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest


_MANAGED_EXEC_SURFACE_IMPORTS = {
    "tools.terminal_tool",
    "tools.file_tools",
    "tools.file_operations",
    "tools.environments.base",
    "tools.process_registry",
    "agent.lsp",
}


def _isolated_managed_lifecycle_call(call):
    """Run one real lifecycle handler with process/network/import canaries."""
    imported: list[str] = []
    real_import = __import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("managed lifecycle attempted detached execution")

    with (
        patch("builtins.__import__", side_effect=recording_import),
        patch.object(subprocess, "Popen", side_effect=forbidden),
        patch.object(threading.Thread, "start", side_effect=forbidden),
        patch.object(socket, "create_connection", side_effect=forbidden),
    ):
        result = call()

    assert not _MANAGED_EXEC_SURFACE_IMPORTS.intersection(imported)
    assert not any(name == "httpx" or name.startswith("httpx.") for name in imported)
    return result


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


def test_kanban_tools_visible_with_env_var(monkeypatch, tmp_path):
    """Worker sessions get task lifecycle tools, not board-routing tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_kanban_worker_env_overrides_profile_toolset_filter(monkeypatch, tmp_path):
    """Dispatcher-spawned workers must get lifecycle tools even when the
    assignee profile restricts enabled toolsets and does not list kanban.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    schema = get_tool_definitions(
        enabled_toolsets=["terminal"],
        quiet_mode=True,
    )
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_show" in names
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_list" not in names


def test_worker_with_kanban_toolset_still_hides_board_routing(monkeypatch, tmp_path):
    """Task scope wins over profile config for board-routing tools.

    Even if a worker process happens to also have ``toolsets: [kanban]``
    in its config, the HERMES_KANBAN_TASK env var means it's a focused
    worker and must not see kanban_list / kanban_unblock.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert {
        "kanban_list",
        "kanban_unblock",
    }.isdisjoint(kanban), (
        f"Board-routing tools leaked into worker schema: "
        f"{kanban & {'kanban_list', 'kanban_unblock'}}"
    )


def test_kanban_tools_visible_with_toolset_config(monkeypatch, tmp_path):
    """Orchestrator profiles with toolsets: [kanban] see all kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_list",
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_unblock",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


@pytest.mark.parametrize(
    ("policy", "expected_state"),
    [
        (
            {
                "agent": {"max_turns": 90},
                "kanban": {"short_task_handoff": {"enabled": False}},
                "terminal": {"backend": "local"},
                "toolsets": ["kanban"],
            },
            "disabled",
        ),
        (
            {
                "agent": {"max_turns": 90},
                "kanban": {
                    "short_task_handoff": {
                        "enabled": True,
                        "soft_iteration_limit": 90,
                        "max_handoffs": 8,
                    }
                },
                "terminal": {"backend": "local"},
                "toolsets": ["kanban"],
            },
            "unknown",
        ),
    ],
    ids=["disabled", "invalid"],
)
def test_kanban_control_model_surface_fails_closed_without_valid_policy(
    monkeypatch,
    tmp_path,
    policy,
    expected_state,
):
    """Disabled or invalid handoff policy must hide and reject model control.

    The direct handler check is deliberately paired with real database counts:
    even callers that bypass registry discovery cannot persist a receipt,
    comment, or event while the policy is unavailable.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    from hermes_cli import kanban_db as kb
    from agent import kanban_auto_handoff as handoff
    from agent import kanban_handoff_scope as handoff_scope
    from tools import kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache, registry

    monkeypatch.setattr(kt, "load_config", lambda: policy)
    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **_kwargs: handoff.build_dispatcher_policy_snapshot(policy),
    )
    _worker_snapshot = handoff.build_dispatcher_policy_snapshot(policy)
    monkeypatch.setattr(
        handoff_scope,
        "decide_current_gateway_origin",
        lambda: (
            {
                "authorized": False,
                "validation_error": _worker_snapshot["validation_error"],
            }
            if _worker_snapshot.get("validation_error")
            else {"authorized": False, "reason": "feature_disabled"}
        ),
    )
    monkeypatch.setattr(kt, "_is_delegated_child_context", lambda: False)
    # Reach the policy guard specifically; orchestrator authorization is a
    # separate boundary with its own tests.
    monkeypatch.setattr(kt, "_require_orchestrator_tool", lambda _name: None)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="control-policy-boundary")
        tables = ("task_handoff_controls", "task_comments", "task_events")
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        before_status = kb.get_task(conn, task_id).status
    finally:
        conn.close()

    invalidate_check_fn_cache()
    assert kt._kanban_control_mode_state() == expected_state
    assert kt._check_kanban_control_mode() is False
    entry = registry.get_entry("kanban_control")
    assert entry is not None
    assert entry.check_fn is kt._check_kanban_control_mode
    assert registry.get_definitions({"kanban_control"}, quiet=True) == []

    result = json.loads(
        kt._handle_control_locked(
            {"task_id": task_id, "kind": "stop", "message": "stop now"}
        )
    )
    assert "error" in result
    assert "short-task handoff enabled" in result["error"]

    conn = kb.connect()
    try:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        assert kb.get_task(conn, task_id).status == before_status
    finally:
        conn.close()
    assert after == before
    invalidate_check_fn_cache()


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _arm_managed_worker_env(monkeypatch, task_id):
    """Upgrade the fixture's claimed run to a dispatcher-managed identity."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (os.getpid(), task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, owner_node_id = 'node', "
            "owner_boot_id = 'boot', worker_start_token = 'start', "
            "worker_pgid = ?, handoff_safety_required = 1 WHERE id = ?",
            (os.getpid(), os.getpid(), task.current_run_id),
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return int(task.current_run_id)


def _short_task_policy_snapshot(*, enabled=True, workspace_root="/tmp"):
    workspace_root = os.path.realpath(workspace_root)
    return json.dumps(
        {
            "schema": 2,
            "enabled": enabled,
            "soft_iteration_limit": 36,
            "max_handoffs": 8,
            "max_iterations": 90,
            "failure_limit": 2,
            "allowed_workspace_roots": [workspace_root],
            "validation_error": None,
        }
    )


def _bind_short_task_policy(conn, task_id, worker_policy):
    """Attach one valid immutable test binding to an existing task."""
    from hermes_cli import kanban_db as kb

    identity = {
        "platform": "feishu",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "group-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "test-worker",
        "session_key": "test-session",
    }
    task_policy = {
        "schema": 1,
        "authorized": True,
        "origin": identity,
        "matched_origin": {
            key: identity[key]
            for key in ("platform", "chat_type", "chat_id", "user_id")
        },
        "worker_policy": dict(worker_policy),
    }
    assert kb.add_control_binding(
        conn,
        binding_id=f"b_{task_id}",
        task_id=task_id,
        short_handoff_policy=task_policy,
        **identity,
    ) is True


def test_managed_complete_confines_all_local_paths_to_worker_workspace(
    monkeypatch, worker_env, tmp_path
):
    """Artifacts and prose paths fail closed before a managed DB transition."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "proof.txt"
    inside.write_text("proof")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = workspace / "escaped-link.txt"
    link.symlink_to(outside)

    run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    policy = json.loads(
        _short_task_policy_snapshot(
            enabled=True, workspace_root=str(workspace.resolve())
        )
    )
    conn = kb.connect()
    try:
        _bind_short_task_policy(conn, worker_env, policy)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
            (str(workspace), worker_env),
        )
    finally:
        conn.close()
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", json.dumps(policy)
    )
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    rejected_calls = [
        {"summary": "done", "artifacts": [str(outside)]},
        {"summary": "done", "artifacts": ["../outside.txt"]},
        {"summary": "done", "artifacts": [str(link)]},
        {"summary": f"read {outside}"},
        {"summary": "read /privatefile"},
        {"summary": "done", "result": "see ../outside.txt"},
        {"summary": "done", "metadata": {"artifacts": [str(outside)]}},
    ]
    for payload in rejected_calls:
        output = json.loads(kt._handle_complete(payload))
        assert "error" in output, (payload, output)
        conn = kb.connect()
        try:
            assert kb.get_task(conn, worker_env).status == "running"
        finally:
            conn.close()

    accepted = json.loads(
        kt._handle_complete(
            {
                "summary": "完成 workspace/proof.txt 的受控交付",
                "metadata": {"artifacts": ["proof.txt"]},
            }
        )
    )
    assert accepted["ok"] is True
    assert accepted["status"] == "review"
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata["artifacts"] == [str(inside.resolve())]
    finally:
        conn.close()


def test_bound_task_rejects_plain_orchestrator_mutation_but_keeps_reads(
    monkeypatch, worker_env, tmp_path
):
    """A binding turns generic model tools read-only for every target write."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    policy = json.loads(_short_task_policy_snapshot(enabled=True))
    conn = kb.connect()
    try:
        _bind_short_task_policy(conn, worker_env, policy)
        child = kb.create_task(conn, title="ordinary child", assignee="peer")
        open_parent = kb.create_task(
            conn, title="open parent", assignee="peer"
        )
        managed_waiting = kb.create_task(
            conn,
            title="managed waiting child",
            assignee="peer",
            parents=[open_parent],
        )
        _bind_short_task_policy(conn, managed_waiting, policy)
        before_comments = len(kb.list_comments(conn, worker_env))
        before_attachments = len(kb.list_attachments(conn, worker_env))
    finally:
        conn.close()

    # Simulate CodingMan/a regular orchestrator: it can name the task but owns
    # no dispatcher run and no authenticated kanban_control binding.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
    )
    monkeypatch.setattr(
        kt,
        "_download_url_with_cap",
        lambda *_a, **_kw: pytest.fail("unauthorized URL was downloaded"),
    )
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)

    assert json.loads(kt._handle_show({"task_id": worker_env}))["task"]["id"] == worker_env
    assert json.loads(kt._handle_list({"limit": 50}))["promoted"] == 0
    conn = kb.connect()
    try:
        assert kb.get_task(conn, managed_waiting).status == "todo"
    finally:
        conn.close()
    denied = [
        kt._handle_comment({"task_id": worker_env, "body": "inject"}),
        kt._handle_heartbeat({"task_id": worker_env}),
        kt._handle_block({"task_id": worker_env, "reason": "stop"}),
        kt._handle_complete({"task_id": worker_env, "summary": "done"}),
        kt._handle_attach(
            {
                "task_id": worker_env,
                "filename": "x.txt",
                "content_base64": "eA==",
            }
        ),
        kt._handle_attach_url(
            {
                "task_id": worker_env,
                "url": "https://example.com/x.txt",
            }
        ),
        kt._handle_link({"parent_id": worker_env, "child_id": child}),
        kt._handle_create(
            {
                "title": "unauthorized child",
                "assignee": "peer",
                "parents": [worker_env],
            }
        ),
    ]
    for output in denied:
        assert "error" in json.loads(output), output

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run_id
        assert len(kb.list_comments(conn, worker_env)) == before_comments
        assert len(kb.list_attachments(conn, worker_env)) == before_attachments
        assert kb.parent_ids(conn, child) == []
        conn.execute(
            "UPDATE tasks SET status='blocked', current_run_id=NULL, "
            "worker_pid=NULL WHERE id=?",
            (worker_env,),
        )
    finally:
        conn.close()

    unblock = json.loads(kt._handle_unblock({"task_id": worker_env}))
    assert "error" in unblock
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" in d


def test_show_explicit_task_id(worker_env):
    """Peek at a different task than the one in env."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other task", assignee="peer")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_show({"task_id": other})
    d = json.loads(out)
    assert d["task"]["id"] == other


@pytest.mark.parametrize(
    "snapshot",
    [_short_task_policy_snapshot(enabled=True), "{malformed-policy"],
    ids=["enabled", "malformed"],
)
def test_short_task_read_and_comment_tools_never_open_foreign_task(
    monkeypatch, worker_env, snapshot
):
    """Phase-1 read/comment isolation rejects before any DB connection."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        foreign = kb.create_task(conn, title="foreign secret", assignee="peer")
        before_comments = len(kb.list_comments(conn, foreign))
        before_attachments = len(kb.list_attachments(conn, foreign))
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)

    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("foreign task request reached the database")

    monkeypatch.setattr(kt, "_connect", forbidden_connect)
    calls = [
        kt._handle_show({"task_id": foreign}),
        kt._handle_comment({"task_id": foreign, "body": "must not land"}),
    ]
    for output in calls:
        error = json.loads(output).get("error", "")
        assert "Phase-1 worker is scoped only" in error
    attachment_error = json.loads(
        kt._handle_attachments({"task_id": foreign})
    ).get("error", "")
    assert "不能使用附件库" in attachment_error

    conn = kb.connect()
    try:
        assert len(kb.list_comments(conn, foreign)) == before_comments
        assert len(kb.list_attachments(conn, foreign)) == before_attachments
    finally:
        conn.close()


def test_short_task_read_comment_work_but_attachment_surface_is_closed(
    monkeypatch, worker_env
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _short_task_policy_snapshot(enabled=True),
    )
    shown = json.loads(kt._handle_show({"task_id": worker_env}))
    assert shown["task"]["id"] == worker_env
    commented = json.loads(
        kt._handle_comment({"task_id": worker_env, "body": "own evidence"})
    )
    assert commented["ok"] is True
    attached = json.loads(
        kt._handle_attach(
            {
                "task_id": worker_env,
                "filename": "proof.txt",
                "content_base64": "cHJvb2Y=",
            }
        )
    )
    assert "不能使用附件库" in attached["error"]
    monkeypatch.setattr(
        kt,
        "_download_url_with_cap",
        lambda *_a, **_kw: pytest.fail("managed worker reached network"),
    )
    remote = json.loads(
        kt._handle_attach_url(
            {
                "task_id": worker_env,
                "url": "https://example.com/proof.txt",
            }
        )
    )
    assert "不能使用附件库" in remote["error"]
    listed = json.loads(kt._handle_attachments({"task_id": worker_env}))
    assert "不能使用附件库" in listed["error"]
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_short_task_review_attachment_surface_is_closed_before_io(
    monkeypatch, worker_env
):
    from tools import kanban_tools as kt

    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _short_task_policy_snapshot(enabled=False),
    )
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1")
    monkeypatch.setattr(
        kt,
        "_download_url_with_cap",
        lambda *_a, **_kw: pytest.fail("reviewer reached network"),
    )
    outputs = [
        kt._handle_attach(
            {
                "task_id": worker_env,
                "filename": "proof.txt",
                "content_base64": "cHJvb2Y=",
            }
        ),
        kt._handle_attach_url(
            {
                "task_id": worker_env,
                "url": "https://example.com/proof.txt",
            }
        ),
        kt._handle_attachments({"task_id": worker_env}),
    ]
    for output in outputs:
        assert "不能使用附件库" in json.loads(output)["error"]


def test_disabled_short_task_policy_preserves_cross_task_collaboration(
    monkeypatch, worker_env
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        foreign = kb.create_task(conn, title="legacy peer", assignee="peer")
    finally:
        conn.close()
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _short_task_policy_snapshot(enabled=False),
    )
    assert json.loads(kt._handle_show({"task_id": foreign}))["task"]["id"] == foreign
    assert json.loads(
        kt._handle_comment({"task_id": foreign, "body": "legacy handoff"})
    )["ok"] is True
    assert json.loads(kt._handle_attachments({"task_id": foreign}))["ok"] is True


@pytest.mark.parametrize(
    "snapshot",
    [_short_task_policy_snapshot(enabled=True), "{malformed-policy"],
    ids=["enabled", "malformed"],
)
def test_short_task_lifecycle_tools_reject_cross_board_before_connect(
    monkeypatch, worker_env, snapshot
):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "frozen-board")

    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("cross-board request reached the database")

    monkeypatch.setattr(kt, "_connect", forbidden_connect)
    calls = [
        kt._handle_show,
        kt._handle_comment,
        kt._handle_heartbeat,
        kt._handle_block,
        kt._handle_complete,
    ]
    args = [
        {"task_id": worker_env, "board": "other-board"},
        {"task_id": worker_env, "body": "x", "board": "other-board"},
        {"task_id": worker_env, "note": "x", "board": "other-board"},
        {
            "task_id": worker_env,
            "reason": "x",
            "kind": "needs_input",
            "board": "other-board",
        },
        {"task_id": worker_env, "summary": "x", "board": "other-board"},
    ]
    for handler, payload in zip(calls, args):
        error = json.loads(handler(payload)).get("error", "")
        assert "pinned to board frozen-board" in error
    attachment_calls = [
        kt._handle_attach(
            {
                "task_id": worker_env,
                "filename": "x.txt",
                "content_base64": "eA==",
                "board": "other-board",
            }
        ),
        kt._handle_attachments(
            {"task_id": worker_env, "board": "other-board"}
        ),
    ]
    for output in attachment_calls:
        assert "不能使用附件库" in json.loads(output).get("error", "")


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert d["tasks"][0]["parent_count"] == 0
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_list_rejects_invalid_status(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"status": "not-a-state"})
    assert "status must be one of" in json.loads(out).get("error", "")


def test_list_rejects_bad_limit(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_list({"limit": "nope"})).get("error")
    assert json.loads(kt._handle_list({"limit": 0})).get("error")


def test_list_parses_include_archived_string_false(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "false",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived not in ids


def test_list_parses_include_archived_string_true(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "true",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived in ids


def test_list_rejects_bad_include_archived(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"include_archived": "sometimes"})
    assert "include_archived must be" in json.loads(out).get("error", "")


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
    finally:
        conn.close()


@pytest.mark.parametrize(
    "postcommit_helper",
    [
        "_scan_prose_for_phantom_ids",
        "_clear_failure_counter",
        "recompute_ready",
        "_fire_kanban_lifecycle_hook",
    ],
)
def test_managed_complete_stays_successful_when_postcommit_maintenance_fails(
    monkeypatch, worker_env, postcommit_helper
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    run_id = _arm_managed_worker_env(monkeypatch, worker_env)

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError(f"injected {postcommit_helper} failure")

    monkeypatch.setattr(kb, postcommit_helper, injected_failure)
    output = json.loads(kt._handle_complete({"summary": "durably done"}))

    assert output["ok"] is True
    assert output["run_id"] == run_id
    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "done"
        assert task.worker_pid == os.getpid()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ? "
            "AND parent_task_id = ? AND released_at IS NULL",
            (worker_env, worker_env),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_short_task_complete_submits_for_review_then_fresh_reviewer_completes(
    monkeypatch, worker_env, tmp_path
):
    """Implementation completion is non-terminal and reviewer-only thereafter."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = tmp_path / "shared-project"
    workspace.mkdir()
    candidate = workspace / "candidate.txt"
    candidate.write_text("phase-one-candidate\n", encoding="utf-8")
    run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    implementation_policy = json.loads(
        _short_task_policy_snapshot(
            enabled=True, workspace_root=str(workspace.resolve())
        )
    )
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace.resolve()))
    conn = kb.connect()
    try:
        _bind_short_task_policy(conn, worker_env, implementation_policy)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=?, "
            "validation_class='text_mechanism' "
            "WHERE id=?",
            (str(workspace), worker_env),
        )
        child_id = kb.create_task(
            conn,
            title="must wait for real review completion",
            assignee="test-worker",
            parents=[worker_env],
        )
        kb.add_notify_sub(
            conn,
            task_id=worker_env,
            platform="feishu",
            chat_id="group-1",
            thread_id="thread-1",
        )

        before_events = len(kb.list_events(conn, worker_env))
        assert kb.submit_task_for_review(
            conn,
            worker_env,
            summary="wrong run",
            expected_run_id=run_id + 1,
            expected_worker_pid=os.getpid(),
        ) is False
        assert kb.submit_task_for_review(
            conn,
            worker_env,
            summary="wrong pid",
            expected_run_id=run_id,
            expected_worker_pid=os.getpid() + 1,
        ) is False
        assert kb.get_task(conn, worker_env).status == "running"
        assert len(kb.list_events(conn, worker_env)) == before_events
        assert kb.short_task_completion_authorization(
            conn,
            worker_env,
            expected_run_id=run_id,
            policy_snapshot_raw=json.dumps(implementation_policy),
        ) == "implementation"
    finally:
        conn.close()

    for invalid_snapshot in (None, "{malformed", _short_task_policy_snapshot(enabled=False)):
        if invalid_snapshot is None:
            monkeypatch.delenv(
                "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
            )
        else:
            monkeypatch.setenv(
                "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", invalid_snapshot
            )
        refused = json.loads(
            kt._handle_complete({"summary": "must remain in flight"})
        )
        assert "error" in refused
        conn = kb.connect()
        try:
            assert kb.get_task(conn, worker_env).status == "running"
            assert len(kb.list_events(conn, worker_env)) == before_events
        finally:
            conn.close()

    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(implementation_policy),
    )

    submitted = json.loads(
        _isolated_managed_lifecycle_call(
            lambda: kt._handle_complete(
                {
                    "summary": "implementation ready; tests deferred to reviewer",
                    "result": "candidate output",
                    "metadata": {"changed_files": ["app.py"]},
                }
            )
        )
    )
    assert submitted == {
        "ok": True,
        "task_id": worker_env,
        "run_id": run_id,
        "status": "review",
        "review_required": True,
        "message": "Implementation submitted for independent review.",
    }

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        run = kb.latest_run(conn, worker_env)
        assert task.status == "review"
        assert task.result is None
        assert task.completed_at is None
        assert task.assignee == "test-worker"
        assert task.workspace_kind == "dir"
        assert task.workspace_path == str(workspace)
        assert task.current_run_id is None
        assert task.worker_pid == os.getpid()
        assert run.id == run_id
        assert run.status == "review_requested"
        assert run.outcome == "review_requested"
        assert run.summary == "implementation ready; tests deferred to reviewer"
        assert run.metadata == {
            "changed_files": ["app.py"],
            "_hermes_review_submission_result": "candidate output",
        }
        kinds = [event.kind for event in kb.list_events(conn, worker_env)]
        assert kinds.count("review_requested") == 1
        assert "completed" not in kinds
        assert kb.get_task(conn, child_id).status == "todo"
        assert kb.claim_review_task(conn, worker_env) is None
        _cursor, notifications = kb.unseen_events_for_sub(
            conn,
            task_id=worker_env,
            platform="feishu",
            chat_id="group-1",
            thread_id="thread-1",
            kinds=["completed"],
        )
        assert notifications == []
    finally:
        conn.close()

    # Retrying the same terminal tool acknowledgement is idempotent and does
    # not manufacture a second run, event, or gate.
    replay = json.loads(
        kt._handle_complete(
            {
                "summary": "implementation ready; tests deferred to reviewer",
                "result": "candidate output",
                "metadata": {"changed_files": ["app.py"]},
            }
        )
    )
    assert replay["ok"] is True
    assert replay["status"] == "review"
    conflict = json.loads(
        kt._handle_complete(
            {
                "summary": "a conflicting replay must not replace evidence",
                "result": "candidate output",
                "metadata": {"changed_files": ["app.py"]},
            }
        )
    )
    assert "error" in conflict
    conn = kb.connect()
    try:
        assert len(kb.list_runs(conn, worker_env)) == 1
        assert [e.kind for e in kb.list_events(conn, worker_env)].count(
            "review_requested"
        ) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id=? "
            "AND released_at IS NULL",
            (worker_env,),
        ).fetchone()[0] == 1

        monkeypatch.setattr(
            kb, "_exit_gate_release_reason", lambda _row: "test_worker_exited"
        )
        assert kb.release_handoff_exit_gates(conn) == 0
        assert kb.get_task(conn, worker_env).status == "review"
        assert kb.get_task(conn, worker_env).worker_pid is None

        dispatched = []

        def capture_review(
            task,
            task_workspace,
            *,
            failure_limit,
            allow_short_handoff=True,
            require_exit_safety=False,
            review_mode=False,
        ):
            dispatched.append(
                (
                    task,
                    task_workspace,
                    failure_limit,
                    allow_short_handoff,
                    require_exit_safety,
                    review_mode,
                )
            )
            return None

        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _profile: True
        )
        monkeypatch.setattr(
            kb, "_short_task_handoff_dispatch_enabled", lambda: True
        )
        dispatch_result = kb.dispatch_once(
            conn, spawn_fn=capture_review, max_spawn=1, failure_limit=2
        )
        assert [item[0] for item in dispatch_result.spawned] == [worker_env]
        assert len(dispatched) == 1
        (
            review_task,
            review_workspace,
            failure_limit,
            allow_handoff,
            require_exit_safety,
            review_mode,
        ) = dispatched[0]
        assert review_task.skills is None
        assert review_workspace == str(workspace)
        assert failure_limit == 2
        assert allow_handoff is False
        assert require_exit_safety is True
        assert review_mode is True
    finally:
        conn.close()

    # The fresh reviewer receives an explicit disabled snapshot and therefore
    # reaches the ordinary terminal completion path.
    reviewer_run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run_id))
    review_policy = dict(implementation_policy)
    review_policy["enabled"] = False
    review_policy["inactive_reason"] = (
        "goal/review workers are outside Phase-1 handoff scope"
    )
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(review_policy),
    )
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", "review")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace.resolve()))
    conn = kb.connect()
    try:
        assert kb.short_task_completion_authorization(
            conn,
            worker_env,
            expected_run_id=reviewer_run_id,
            policy_snapshot_raw=json.dumps(review_policy),
        ) == "review"
    finally:
        conn.close()
    from tools import managed_file_tools

    managed_file_tools._REVIEW_READ_EVIDENCE.clear()
    assert "phase-one-candidate" in managed_file_tools.read_file_tool(
        "candidate.txt"
    )
    reviewed = json.loads(
        _isolated_managed_lifecycle_call(
            lambda: kt._handle_complete(
                {"summary": "independent review passed"}
            )
        )
    )
    assert reviewed["ok"] is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
        assert kb.get_task(conn, child_id).status == "todo"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id=? "
            "AND released_at IS NULL",
            (worker_env,),
        ).fetchone()[0] == 1
        events = kb.list_events(conn, worker_env)
        assert [event.kind for event in events].count("completed") == 1
        _cursor, notifications = kb.unseen_events_for_sub(
            conn,
            task_id=worker_env,
            platform="feishu",
            chat_id="group-1",
            thread_id="thread-1",
            kinds=["completed"],
        )
        assert [event.kind for event in notifications] == ["completed"]
        monkeypatch.setattr(
            kb, "_exit_gate_release_reason", lambda _row: "reviewer_exited"
        )
        assert kb.release_handoff_exit_gates(conn) == 1
        assert kb.get_task(conn, child_id).status == "ready"
    finally:
        conn.close()


def test_complete_readback_failure_cannot_erase_committed_acknowledgement(
    monkeypatch, worker_env
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    real_connect = kt._connect

    class ReadbackFailingKB:
        def __getattr__(self, name):
            if name == "latest_run":
                raise RuntimeError("injected readback failure")
            return getattr(kb, name)

    def connect_with_proxy(*args, **kwargs):
        _module, conn = real_connect(*args, **kwargs)
        return ReadbackFailingKB(), conn

    monkeypatch.setattr(kt, "_connect", connect_with_proxy)
    output = json.loads(kt._handle_complete({"summary": "durably done"}))

    assert output == {"ok": True, "task_id": worker_env, "run_id": run_id}


def test_block_readback_failure_cannot_erase_committed_acknowledgement(
    monkeypatch, worker_env
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    run_id = _arm_managed_worker_env(monkeypatch, worker_env)
    real_connect = kt._connect

    class ReadbackFailingKB:
        def __getattr__(self, name):
            if name == "latest_run":
                raise RuntimeError("injected readback failure")
            return getattr(kb, name)

    def connect_with_proxy(*args, **kwargs):
        _module, conn = real_connect(*args, **kwargs)
        return ReadbackFailingKB(), conn

    monkeypatch.setattr(kt, "_connect", connect_with_proxy)
    output = json.loads(kt._handle_block({"reason": "durably blocked"}))

    assert output["ok"] is True
    assert output["run_id"] == run_id
    assert output["status"] == "blocked"


def test_complete_metadata_round_trips_through_show(worker_env):
    """Structured completion metadata should be visible to downstream agents."""
    from tools import kanban_tools as kt

    handoff = {
        "changed_files": ["hermes_cli/kanban.py"],
        "verification": ["pytest tests/tools/test_kanban_tools.py -q"],
        "dependencies": [],
        "blocked_reason": None,
        "retry_notes": "none",
        "residual_risk": ["dashboard rendering not exercised"],
    }

    complete_out = kt._handle_complete({
        "summary": "finished with structured evidence",
        "metadata": handoff,
    })
    assert json.loads(complete_out)["ok"] is True

    show_out = kt._handle_show({"task_id": worker_env})
    shown = json.loads(show_out)
    assert shown["task"]["status"] == "done"
    assert shown["runs"][-1]["summary"] == "finished with structured evidence"
    assert shown["runs"][-1]["metadata"] == handoff


def test_complete_stamps_worker_session_id_from_env(monkeypatch, worker_env):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")
    metadata = {"files": 2, "worker_session_id": "user-spoof"}

    out = kt._handle_complete({
        "summary": "done by scoped worker",
        "metadata": metadata,
    })
    assert json.loads(out)["ok"] is True
    assert metadata["worker_session_id"] == "user-spoof"

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "session-trusted",
        }
    finally:
        conn.close()


def test_complete_does_not_stamp_worker_session_id_without_scoped_task(
    monkeypatch, worker_env
):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")

    out = kt._handle_complete({
        "task_id": worker_env,
        "summary": "done outside worker scope",
        "metadata": {"files": 2, "worker_session_id": "user-provided"},
    })
    assert json.loads(out)["ok"] is True

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "user-provided",
        }
    finally:
        conn.close()


def test_complete_with_result_only(worker_env):
    """`result` alone (without summary) is accepted for legacy compat."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({"result": "legacy result"})
    d = json.loads(out)
    assert d["ok"] is True


def test_complete_with_artifacts_lands_in_event_payload(worker_env):
    """``artifacts=[...]`` rides into the completed event payload so the
    gateway notifier can upload them as native attachments. See the
    kanban notifier in gateway/run.py for the consumer side."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "rendered the chart",
        "artifacts": ["/tmp/q3-revenue.png", "/tmp/q3-report.pdf"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        events = kb.list_events(conn, worker_env)
        # Find the completion event
        completed = [e for e in events if e.kind == "completed"]
        assert len(completed) == 1
        payload = completed[0].payload or {}
        assert payload.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
        # And the artifacts also live on metadata for downstream workers
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
    finally:
        conn.close()


def test_complete_artifacts_accepts_single_string(worker_env):
    """A bare string is auto-promoted to a single-element list for convenience."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "one chart",
        "artifacts": "/tmp/chart.png",
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == ["/tmp/chart.png"]
    finally:
        conn.close()


def test_complete_artifacts_merges_with_explicit_metadata_field(worker_env):
    """If the worker passes metadata.artifacts AND the top-level artifacts
    param, merge the two without duplicates."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "merged",
        "metadata": {"artifacts": ["/tmp/a.png"], "other": "fact"},
        "artifacts": ["/tmp/b.pdf", "/tmp/a.png"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        # Order: existing entries first, then new ones, deduplicated.
        assert run.metadata.get("artifacts") == ["/tmp/a.png", "/tmp/b.pdf"]
        assert run.metadata.get("other") == "fact"
    finally:
        conn.close()


def test_complete_rejects_non_list_artifacts(worker_env):
    """Non-list, non-string artifacts should be rejected with a clear error."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "bad shape",
        "artifacts": {"not": "a list"},
    })
    err = json.loads(out).get("error", "")
    assert "artifacts must be a list" in err


def test_complete_missing_scratch_artifact_stays_in_flight(worker_env):
    """A false deliverable claim must return retry guidance, not mark Done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, worker_env, workspace)

    output = kt._handle_complete({
        "summary": "report complete",
        "artifacts": [str(workspace / "missing-report.md")],
    })
    error = json.loads(output).get("error", "")

    assert "could not preserve" in error
    assert "still in-flight" in error
    assert "retry kanban_complete" in error
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"
    assert workspace.exists()


def test_complete_rejects_no_handoff(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({})
    assert json.loads(out).get("error"), "should have errored"


def test_complete_rejects_non_dict_metadata(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "x", "metadata": [1, 2, 3]})
    assert json.loads(out).get("error")


def test_complete_phantom_card_message_advertises_retry(worker_env):
    """A phantom-card rejection must surface a tool_error that explicitly
    tells the worker the task is still in-flight and how to retry — the
    worker has no other channel to discover that. Regression for #22923,
    where the previous wording read like a terminal failure and workers
    routinely abandoned the run instead of trying again.
    """
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "oops claimed a phantom",
        "created_cards": ["t_phantomdeadbeef"],
    })
    err = json.loads(out).get("error", "")
    assert err, f"expected an error, got {out!r}"
    # Phantom id surfaced verbatim.
    assert "t_phantomdeadbeef" in err
    # The retry-is-supported phrasing — these are the literal cues a
    # worker reads to decide whether to retry vs block/abandon. If a
    # future change rewords the message, these checks will catch the
    # regression. See #22923 for the failure mode.
    assert "still in-flight" in err
    assert "Retry kanban_complete" in err
    assert "created_cards=[]" in err

    # Critically: the task is genuinely still in-flight — the gate
    # rejection did not mutate state, so the worker's retry can land.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_retry_with_corrected_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with a
    corrected created_cards list (phantom ids removed) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Create a real child via the tool so it gets the worker-profile
    # attribution the gate trusts.
    child = json.loads(kt._handle_create({
        "title": "real child", "assignee": "peer",
    }))
    assert child["ok"]
    real_id = child["task_id"]

    # First attempt mixes real + phantom — gate rejects.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": [real_id, "t_phantomdeadbeef"],
    }))
    assert rejected.get("error")
    assert "t_phantomdeadbeef" in rejected["error"]

    # Retry with corrected list.
    ok = json.loads(kt._handle_complete({
        "summary": "retry with corrected list",
        "created_cards": [real_id],
    }))
    assert ok.get("ok") is True


def test_complete_goal_mode_rejected_by_judge(monkeypatch, tmp_path):
    """Goal-mode tasks must pass the auxiliary judge before completion.
    Regression for #38367: workers bypassing the judge via early kanban_complete."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Set up isolated HERMES_HOME
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # Mock the judge to reject the completion. The gate only runs when a
    # judge is reachable, so force the availability probe True as well.
    def mock_judge_goal(goal, last_response, *, timeout=30.0, subgoals=None):
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        return "continue", "missing verification evidence", False, None, False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", mock_judge_goal)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)

    # Attempt to complete should be rejected
    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]
    assert f"parents=[{goal_task_id}]" in d["error"]

    # Verify the task is NOT completed in the DB
    conn2 = kb.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "running"  # Should still be running, not done
    finally:
        conn2.close()


def test_complete_goal_mode_allows_when_judge_unavailable(monkeypatch, tmp_path):
    """Fail-open: an unreachable judge must not wedge a goal_mode worker.

    judge_goal returns a "continue" verdict when no auxiliary model is
    configured, which is indistinguishable from a real "not done" judgment.
    The gate probes availability first, so completion proceeds rather than
    being rejected forever when no judge can be reached."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # No judge reachable. judge_goal must not even be consulted; if it were,
    # this stub would reject — so reaching "done" proves the probe short-circuit.
    def fail_if_called(goal, last_response, *, timeout=30.0, subgoals=None):
        raise AssertionError("judge_goal must not run when no judge is available")

    monkeypatch.setattr("tools.kanban_tools.judge_goal", fail_if_called)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: False)

    out = kt._handle_complete({"summary": "done enough"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn2 = kb.connect()
    try:
        assert kb.get_task(conn2, goal_task_id).status == "done"
    finally:
        conn2.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_block_rejects_empty_reason(worker_env):
    from tools import kanban_tools as kt
    for bad in ["", "   ", None]:
        out = kt._handle_block({"reason": bad})
        assert json.loads(out).get("error")


def _make_goal_mode_worker_env(monkeypatch, tmp_path):
    """Set up an isolated HERMES_HOME with one claimed goal_mode task,
    matching the pattern used by the kanban_complete judge gate tests."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-block-test", assignee="test-worker",
            body="Must achieve X.", goal_mode=True,
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    return goal_task_id


def test_block_goal_mode_rejects_missing_kind(monkeypatch, tmp_path):
    """A goal_mode worker calling kanban_block with no kind must not be able
    to use it as an unguarded escape from the goal loop (Issue #38696,
    sibling of the kanban_complete judge gate / Issue #38367)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "giving up"})
    d = json.loads(out)
    assert "error" in d
    assert "goal_mode" in d["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_rejects_disallowed_kind(monkeypatch, tmp_path):
    """`capability` / `transient` are valid kinds in general but must not
    let a goal_mode worker exit the loop without going through the judge."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    for kind in ("capability", "transient"):
        out = kt._handle_block({"reason": "blocked", "kind": kind})
        d = json.loads(out)
        assert "error" in d, f"kind={kind} should be rejected for goal_mode"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_allows_dependency_kind(monkeypatch, tmp_path):
    """`dependency` and `needs_input` represent a genuine external blocker
    the worker cannot resolve itself — these remain ungated.

    `dependency` routes to status='todo' (not 'blocked') per block_task's
    own kind-routing — the goal loop still treats anything outside
    running/ready/done/blocked as a stop, so this is still a legitimate,
    judge-free exit; it's just not the literal 'blocked' status."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "waiting on another task", "kind": "dependency"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "todo"
    finally:
        conn.close()


def test_block_goal_mode_allows_needs_input_kind(monkeypatch, tmp_path):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "need a decision from the user", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_block_non_goal_mode_task_unaffected_by_new_gate(worker_env):
    """The new gate only applies to goal_mode tasks — plain tasks must keep
    blocking freely with no kind, exactly as before this fix."""
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    assert json.loads(out).get("ok") is True


def test_heartbeat_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "progress"})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_without_note(worker_env):
    """note is optional."""
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({})
    d = json.loads(out)
    assert d["ok"] is True


def test_plain_task_can_complete_when_it_mentions_waiting_control(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            (
                "实现 waiting_for_user_control=true 支持并完成代码。",
                worker_env,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    out = kt._handle_complete({"summary": "implemented parameter support"})
    assert json.loads(out).get("ok") is True
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "done"


def test_waiting_control_note_cannot_impersonate_boolean_arg(
    monkeypatch, worker_env
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _arm_managed_worker_env(monkeypatch, worker_env)
    policy = json.loads(_short_task_policy_snapshot(enabled=True))
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(policy),
    )
    conn = kb.connect()
    try:
        _bind_short_task_policy(conn, worker_env, policy)
        conn.execute(
            "UPDATE tasks SET validation_class='text_mechanism', body = ? "
            "WHERE id = ?",
            (
                "必须调用 kanban_heartbeat 并明确设置 waiting_for_user_control=true；"
                "不要调用 kanban_complete，不要调用 kanban_block，等待后续人工停止指令。",
                worker_env,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    out = kt._handle_heartbeat({"note": "waiting_for_user_control=true"})
    err = json.loads(out).get("error", "")

    assert "note text cannot set waiting_for_user_control" in err
    with kb.connect() as conn:
        events = [
            event for event in kb.list_events(conn, worker_env)
            if event.kind == "heartbeat"
        ]
    assert events == []


def test_waiting_control_task_rejects_completion(monkeypatch, worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _arm_managed_worker_env(monkeypatch, worker_env)
    policy = json.loads(_short_task_policy_snapshot(enabled=True))
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(policy),
    )
    conn = kb.connect()
    try:
        _bind_short_task_policy(conn, worker_env, policy)
        conn.execute(
            "UPDATE tasks SET validation_class='text_mechanism', body = ? "
            "WHERE id = ?",
            (
                "必须调用 kanban_heartbeat 并明确设置 waiting_for_user_control=true；"
                "不要调用 kanban_complete，不要调用 kanban_block，等待后续人工停止指令。",
                worker_env,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    out = kt._handle_complete({"summary": "done after fake waiting note"})
    err = json.loads(out).get("error", "")

    assert "requires an explicit kanban_heartbeat" in err
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"


def test_allowed_workspace_waiting_control_task_rejects_note_and_completion(
    monkeypatch, worker_env, tmp_path,
):
    from agent import kanban_auto_handoff as handoff
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = tmp_path / "allowed-wait"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_POLICY_HOME", "/dispatcher-home")
    policy_homes: list[str | None] = []
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=?, body=? "
            "WHERE id=?",
            (
                str(workspace),
                "必须调用 kanban_heartbeat 并明确设置 waiting_for_user_control=true；"
                "不要调用 kanban_complete，不要调用 kanban_block，等待后续人工停止指令。",
                worker_env,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **kwargs: (
            policy_homes.append(kwargs.get("policy_home"))
            or {
                "enabled": True,
                "validation_error": None,
                "allowed_workspace_roots": [str(workspace)],
            }
        ),
    )

    heartbeat = json.loads(
        kt._handle_heartbeat({"note": "waiting_for_user_control=true"})
    )
    assert "note text cannot set waiting_for_user_control" in heartbeat.get(
        "error", ""
    )

    complete = json.loads(kt._handle_complete({"summary": "done anyway"}))
    assert "requires an explicit kanban_heartbeat" in complete.get("error", "")
    assert policy_homes and set(policy_homes) == {"/dispatcher-home"}
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"


def test_allowed_workspace_plain_parameter_task_can_complete(
    monkeypatch, worker_env, tmp_path,
):
    from agent import kanban_auto_handoff as handoff
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = tmp_path / "allowed-ordinary"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_POLICY_HOME", "/dispatcher-home")
    policy_homes: list[str | None] = []
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=?, body=? "
            "WHERE id=?",
            (
                str(workspace),
                "实现 waiting_for_user_control=true 支持并正常完成代码。",
                worker_env,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **kwargs: (
            policy_homes.append(kwargs.get("policy_home"))
            or {
                "enabled": True,
                "validation_error": None,
                "allowed_workspace_roots": [str(workspace)],
            }
        ),
    )

    out = kt._handle_complete({"summary": "implemented parameter support"})
    assert json.loads(out).get("ok") is True
    assert policy_homes == []
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "done"


def test_run_marked_waiting_control_rejects_completion(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_heartbeat({"waiting_for_user_control": True})
    assert json.loads(out).get("waiting_for_user_control") is True

    out = kt._handle_complete({"summary": "done after waiting"})
    err = json.loads(out).get("error", "")

    assert "already entered waiting_for_user_control=true" in err
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kb.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_rejects_empty_body(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({"task_id": worker_env, "body": "   "})
    assert json.loads(out).get("error")


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_comment_schema_omits_author_override():
    """The ``author`` property must not appear on KANBAN_COMMENT_SCHEMA;
    exposing it to the LLM would re-introduce the forgery surface this
    handler is hardened against.
    """
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA
    props = KANBAN_COMMENT_SCHEMA["parameters"]["properties"]
    assert "author" not in props


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_create_default_child_isolates_materialized_scratch_workspace(
    monkeypatch, worker_env,
):
    """A worker-created default-scratch child must not reuse its parent's path."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent is not None
        parent_workspace = kb.resolve_workspace(parent)
        kb.set_workspace_path(conn, worker_env, parent_workspace)
    finally:
        conn.close()

    # This file represents immutable evidence produced by the parent review.
    evidence = parent_workspace / "review-evidence.txt"
    evidence.write_text("parent-only", encoding="utf-8")

    d = json.loads(kt._handle_create({
        "title": "remediation", "assignee": "peer", "parents": [worker_env],
    }))
    assert d["ok"] is True
    assert d["workspace_kind"] == "scratch"
    assert d["workspace_path"] is None
    assert d["project_id"] is None
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child is not None
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
        child_workspace = kb.resolve_workspace(child)
    finally:
        conn.close()

    assert child_workspace != parent_workspace
    (child_workspace / "child-write.txt").write_text("child", encoding="utf-8")
    assert not (parent_workspace / "child-write.txt").exists()
    assert evidence.read_text(encoding="utf-8") == "parent-only"


def test_create_default_child_does_not_implicitly_share_worker_dir(
    monkeypatch, worker_env,
):
    """Persistent directory sharing requires explicit child workspace args."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    proj = "/home/teknium/myproject"
    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path=proj,
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({"title": "follow-up", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child is not None
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_explicit_dir_workspace_shares_parent_path(monkeypatch, worker_env):
    """An explicit dir workspace remains the intentional sharing escape hatch."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    proj = "/home/teknium/proj"
    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path=proj,
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({
        "title": "shared child", "assignee": "peer",
        "workspace_kind": "dir", "workspace_path": proj,
    }))
    assert d["ok"] is True
    assert d["workspace_kind"] == "dir"
    assert d["workspace_path"] == proj
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child is not None
        assert child.workspace_kind == "dir"
        assert child.workspace_path == proj
        created = next(
            event for event in kb.list_events(conn, child.id)
            if event.kind == "created"
        )
        assert created.payload is not None
        assert created.payload["workspace_kind"] == "dir"
        assert created.payload["workspace_path"] == proj
        assert created.payload["project_id"] is None
    finally:
        conn.close()


def test_create_explicit_scratch_beats_parent_workspace(monkeypatch, worker_env):
    """Explicit scratch remains isolated even when the parent uses a directory."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path="/home/teknium/proj",
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({
        "title": "scratch child", "assignee": "peer",
        "workspace_kind": "scratch",
    }))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child is not None
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_nested_default_scratch_children_each_get_own_workspace(
    monkeypatch, worker_env,
):
    """Isolation remains stable throughout a worker-created task graph."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent is not None
        parent_workspace = kb.resolve_workspace(parent)
        kb.set_workspace_path(conn, worker_env, parent_workspace)
    finally:
        conn.close()

    child_result = json.loads(kt._handle_create({
        "title": "child", "assignee": "peer", "parents": [worker_env],
    }))
    monkeypatch.setenv("HERMES_KANBAN_TASK", child_result["task_id"])
    grandchild_result = json.loads(kt._handle_create({
        "title": "grandchild", "assignee": "reviewer",
        "parents": [child_result["task_id"]],
    }))

    conn = kb.connect()
    try:
        child = kb.get_task(conn, child_result["task_id"])
        grandchild = kb.get_task(conn, grandchild_result["task_id"])
        assert child is not None
        assert grandchild is not None
        assert child.workspace_path is None
        assert grandchild.workspace_path is None
        workspaces = {
            kb.resolve_workspace(parent),
            kb.resolve_workspace(child),
            kb.resolve_workspace(grandchild),
        }
    finally:
        conn.close()
    assert len(workspaces) == 3


def test_create_default_child_inherits_project_without_reusing_worktree(
    monkeypatch, worker_env, tmp_path,
):
    """Project context propagates while each task keeps its own worktree path."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn, name="Isolated Project", folders=[str(repo)],
        )

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn, title="implementation", assignee="test-worker",
            project_id=project_id,
        )
        kb.claim_task(conn, parent_id)
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)

    result = json.loads(kt._handle_create({
        "title": "independent review", "assignee": "reviewer",
        "parents": [parent_id],
    }))
    assert result["ok"] is True
    assert result["workspace_kind"] == "worktree"
    assert result["workspace_path"] == str(
        repo / ".worktrees" / result["task_id"]
    )
    assert result["project_id"] == parent.project_id

    conn = kb.connect()
    try:
        child = kb.get_task(conn, result["task_id"])
        assert child is not None
        assert child.project_id == parent.project_id
        assert child.workspace_kind == "worktree"
        assert child.workspace_path != parent.workspace_path
        assert child.workspace_path == str(repo / ".worktrees" / child.id)
        assert child.branch_name != parent.branch_name
    finally:
        conn.close()


def test_create_cross_profile_project_children_keep_isolated_worktree_routing(
    monkeypatch, tmp_path,
):
    """A shared-board worker need not duplicate the creator's projects.db."""
    from pathlib import Path as _Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb
    from tools import kanban_tools as kt

    profile_a = tmp_path / "profiles" / "creator"
    profile_b = tmp_path / "profiles" / "worker"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    shared_db = tmp_path / "shared-kanban.db"

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    monkeypatch.setenv("HERMES_PROFILE", "creator")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn, name="Cross Profile Project", folders=[str(repo)],
        )
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="parent implementation",
            assignee="worker",
            project_id=project_id,
        )
        kb.claim_task(conn, parent_id)
        parent = kb.get_task(conn, parent_id)
        assert parent is not None

    # Dispatcher switches to profile B but pins the shared board DB. Profile B
    # intentionally has no copy of profile A's first-class Project row.
    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    assert not (profile_b / "projects.db").exists()

    def create_child(index: int) -> dict:
        return json.loads(kt._handle_create({
            "title": f"parallel child {index}",
            "assignee": "peer",
            "parents": [parent_id],
        }))

    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(pool.map(create_child, range(2)))

    assert all(result["ok"] is True for result in children)
    child_ids = [result["task_id"] for result in children]
    with kb.connect() as conn:
        child_tasks = [kb.get_task(conn, task_id) for task_id in child_ids]
    for task in child_tasks:
        assert task is not None
        assert task.project_id == project_id
        assert task.workspace_kind == "worktree"
        assert task.workspace_path == str(repo / ".worktrees" / task.id)
        assert task.workspace_path != parent.workspace_path
        assert task.branch_name is not None
        assert task.branch_name.startswith(f"cross-profile-project/{task.id}")
    assert len({task.workspace_path for task in child_tasks}) == 2
    assert len({task.branch_name for task in child_tasks}) == 2

    # Nested fan-out must route from the persisted child context too, without
    # requiring the worker profile to learn or duplicate the Project record.
    monkeypatch.setenv("HERMES_KANBAN_TASK", child_ids[0])
    grandchild_result = json.loads(kt._handle_create({
        "title": "nested review",
        "assignee": "reviewer",
        "parents": [child_ids[0]],
    }))
    assert grandchild_result["ok"] is True
    with kb.connect() as conn:
        grandchild = kb.get_task(conn, grandchild_result["task_id"])
    assert grandchild is not None
    assert grandchild.project_id == project_id
    assert grandchild.workspace_kind == "worktree"
    assert grandchild.workspace_path == str(repo / ".worktrees" / grandchild.id)
    assert grandchild.workspace_path not in {
        parent.workspace_path,
        *(task.workspace_path for task in child_tasks),
    }
    assert grandchild.branch_name is not None
    assert grandchild.branch_name.startswith(
        f"cross-profile-project/{grandchild.id}"
    )


def test_create_no_worker_task_stays_scratch(monkeypatch, worker_env):
    """Orchestrator/CLI callers keep the same isolated scratch default."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    d = json.loads(kt._handle_create({"title": "orch child", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_stamps_session_id_from_env(monkeypatch, worker_env):
    """When the agent loop runs under ACP, the server propagates the
    originating chat session id via HERMES_SESSION_ID. ``kanban_create``
    reads it and stamps the new task so clients can render a per-session
    board (issue: ACP session linkage on kanban tasks)."""
    monkeypatch.setenv("HERMES_SESSION_ID", "acp-sess-abc")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "from chat",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "acp-sess-abc"
    finally:
        conn.close()


def test_create_session_id_arg_overrides_env(monkeypatch, worker_env):
    """An explicit ``session_id`` arg from the model wins over the env
    propagation. Edge case but exercised: a tool call could carry a
    different session id (e.g. cross-session linking) and the explicit
    arg should not be silently overwritten."""
    monkeypatch.setenv("HERMES_SESSION_ID", "from-env")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "explicit override",
        "assignee": "peer",
        "parents": [worker_env],
        "session_id": "explicit-arg",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "explicit-arg"
    finally:
        conn.close()


def test_create_session_id_absent_when_env_unset(monkeypatch, worker_env):
    """No env var, no arg → session_id stays NULL. Important for backwards
    compatibility: pre-ACP-propagation hosts and CLI-driven creates must
    not accidentally inherit a stale id."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "no session",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id is None
    finally:
        conn.close()


def test_create_rejects_no_title(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"assignee": "x"})).get("error")
    assert json.loads(kt._handle_create({"title": "   ", "assignee": "x"})).get("error")


def test_create_rejects_no_assignee(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"title": "t"})).get("error")


def test_create_rejects_non_list_parents(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "t", "assignee": "a", "parents": 42})
    assert json.loads(out).get("error")


def test_create_parses_triage_string_false(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "not triage",
        "assignee": "peer",
        "triage": "false",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "ready"
    finally:
        conn.close()


def test_create_parses_triage_string_true(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "needs triage",
        "assignee": "peer",
        "triage": "true",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "triage"
    finally:
        conn.close()


def test_create_rejects_bad_triage(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "bad triage",
        "assignee": "peer",
        "triage": "sometimes",
    })
    assert "triage must be" in json.loads(out).get("error", "")


def test_create_accepts_string_parent(worker_env):
    """Convenience: a single parent id as string is coerced to [id]."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "parents": worker_env,
    })
    assert json.loads(out)["ok"]


def test_create_accepts_skills_list(worker_env):
    """Tool writes the per-task skills through to the kernel."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "skilled",
        "assignee": "linguist",
        "skills": ["translation", "github-code-review"],
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation", "github-code-review"]


def test_create_accepts_skills_string(worker_env):
    """Convenience: a single skill name as string is coerced to [name]."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "one-skill",
        "assignee": "a",
        "skills": "translation",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation"]


def test_create_rejects_non_list_skills(worker_env):
    """skills: 42 must be rejected, not silently dropped."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "skills": 42,
    })
    assert json.loads(out).get("error")


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_link_rejects_self_reference(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": worker_env, "child_id": worker_env})
    assert json.loads(out).get("error")


def test_link_rejects_missing_args(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_link({"parent_id": "x"})).get("error")
    assert json.loads(kt._handle_link({"child_id": "y"})).get("error")


def test_link_rejects_cycle(worker_env):
    """A → B, then try to link B → A."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x", parents=[a])
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": b, "child_id": a})
    assert json.loads(out).get("error")


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_with_pending_parents_returns_todo(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": child})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "todo"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, child).status == "todo"
    finally:
        conn.close()


def test_unblock_rejects_non_blocked_task(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": worker_env})
    assert json.loads(out).get("error")


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------

def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban task execution protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt


def test_kanban_guidance_prompt_size_bounded(monkeypatch, tmp_path):
    """Sanity: the guidance block stays lean so it doesn't blow up the
    cached prompt.

    The ceiling guards against unbounded growth, not against any growth.
    The block absorbed the load-bearing worker/orchestrator reference
    details (workspace kinds, deliverable artifacts, created-card claims,
    profile discovery) when the standalone kanban-worker / kanban-orchestrator
    skills were removed and folded into this always-injected guidance, so the
    ceiling is sized to fit that content with a little headroom.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from agent.prompt_builder import KANBAN_GUIDANCE
    assert 1_500 < len(KANBAN_GUIDANCE) < 6_200, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_block_rejects_foreign_task_id(worker_env):
    """A worker cannot block a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_block({"task_id": other, "reason": "evil"})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_heartbeat_rejects_foreign_task_id(worker_env):
    """A worker cannot heartbeat a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        # Put sibling in running state so heartbeat would otherwise succeed.
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"task_id": other})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_worker_complete_own_task_still_works(worker_env):
    """The ownership check doesn't break the normal own-task happy path."""
    from tools import kanban_tools as kt
    # Both implicit (no task_id arg) and explicit (matching env) must work.
    out = kt._handle_complete({"task_id": worker_env, "summary": "explicit own"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == worker_env


def test_worker_complete_rejects_stale_run_id(worker_env, monkeypatch):
    """A retried worker cannot complete the task using an old run token."""
    from hermes_cli import kanban_db as kb
    import hermes_cli.kanban_db as _kb

    # detect_crashed_workers now gates each running task behind a
    # launch-window grace period (c002668ff) so a freshly-spawned worker
    # whose PID isn't yet visible on /proc isn't reclaimed. The fixture
    # creates the task moments before this assertion, so the grace
    # period (default 30s) would skip the liveness check. Zero it out
    # for this test — we WANT immediate reclamation here.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    conn = kb.connect()
    try:
        run1 = kb.latest_run(conn, worker_env)
        kb._set_worker_pid(conn, worker_env, 98765)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [worker_env]

        kb.claim_task(conn, worker_env)
        run2 = kb.latest_run(conn, worker_env)
        assert run2.id != run1.id
    finally:
        conn.close()

    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.id))
    out = kt._handle_complete({"summary": "late stale completion"})
    d = json.loads(out)
    assert d.get("ok") is not True

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run2.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run2.id))
    out = kt._handle_complete({"summary": "current completion"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid


# ---------------------------------------------------------------------------
# Optional ``board`` parameter — per-call DB override
# ---------------------------------------------------------------------------
#
# The dispatcher pins the active board via HERMES_KANBAN_BOARD env var,
# but a Telegram-side orchestrator handling multiple boards needs to be
# able to route a single tool call to a specific board's DB without
# restarting Hermes. These tests pin that ``board=<slug>`` argument
# routes each handler to that board's sqlite file, and that omitting
# ``board`` preserves the legacy env-driven resolution.


@pytest.fixture
def multi_board_env(monkeypatch, tmp_path):
    """Isolated Hermes home with two distinct kanban boards seeded.

    Returns ``("default", "alt")`` slugs. The default board has one
    pre-existing task ``seed_default``; ``alt`` has ``seed_alt``. No
    HERMES_KANBAN_TASK is pinned (orchestrator context) — workers test
    the env-task case via the existing ``worker_env`` fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make sure neither HERMES_KANBAN_DB nor HERMES_KANBAN_BOARD pin a
    # board — the test is specifically about the per-call override.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Default board — implicit
    conn = kb.connect()
    try:
        seed_default = kb.create_task(
            conn, title="seed-default", assignee="worker-d"
        )
    finally:
        conn.close()
    # Alt board — explicit slug routes the connection to a separate DB
    conn = kb.connect(board="alt")
    try:
        seed_alt = kb.create_task(
            conn, title="seed-alt", assignee="worker-a"
        )
    finally:
        conn.close()
    return {
        "default_seed": seed_default,
        "alt_seed": seed_alt,
        "default_db": kb.kanban_db_path(),
        "alt_db": kb.kanban_db_path(board="alt"),
    }


def test_board_param_routes_create_to_alt_board(multi_board_env):
    """kanban_create with ``board="alt"`` must write into the alt board's DB,
    not the default one."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-only",
        "assignee": "worker",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    new_tid = d["task_id"]

    # Lands on alt board.
    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, new_tid).title == "alt-only"
    # Does NOT land on default board.
    with kb.connect() as conn:
        assert kb.get_task(conn, new_tid) is None


def test_board_param_routes_list_to_alt_board(multi_board_env):
    """kanban_list filters by the board parameter, not env-active."""
    from tools import kanban_tools as kt

    # Default — sees seed-default, not seed-alt.
    default_out = json.loads(kt._handle_list({}))
    default_titles = {t["title"] for t in default_out["tasks"]}
    assert "seed-default" in default_titles
    assert "seed-alt" not in default_titles

    # Alt — sees seed-alt, not seed-default.
    alt_out = json.loads(kt._handle_list({"board": "alt"}))
    alt_titles = {t["title"] for t in alt_out["tasks"]}
    assert "seed-alt" in alt_titles
    assert "seed-default" not in alt_titles


def test_board_param_routes_show_to_alt_board(multi_board_env):
    """kanban_show reads from the board parameter, not env-active.

    Tasks across boards may share ids (the id space is per-DB) but the
    seed task ids in this fixture are distinct, so a cross-board show
    must return the matching task only when board is correct.
    """
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Without board override, the alt task is invisible.
    bad = json.loads(kt._handle_show({"task_id": alt_seed}))
    assert "not found" in bad.get("error", "")

    # With board override, it's readable.
    good = json.loads(kt._handle_show({"task_id": alt_seed, "board": "alt"}))
    assert good["task"]["id"] == alt_seed
    assert good["task"]["title"] == "seed-alt"


def test_board_param_routes_assign_via_create_to_alt(multi_board_env):
    """Workflow test for the 'assign' UX — create with assignee on a
    specific board. (The CLI has a separate ``kanban assign`` verb; the
    MCP surface assigns at task creation time.)"""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-assigned",
        "assignee": "linguist",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect(board="alt") as conn:
        task = kb.get_task(conn, d["task_id"])
        assert task is not None
        assert task.assignee == "linguist"


def test_board_param_routes_comment_to_alt_board(multi_board_env):
    """kanban_comment routes the insert to the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    out = kt._handle_comment({
        "task_id": alt_seed,
        "body": "alt comment",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        comments = kb.list_comments(conn, alt_seed)
        assert len(comments) == 1
        assert comments[0].body == "alt comment"
    # Default board does not have this task at all, so no rogue comment.
    with kb.connect() as conn:
        assert kb.get_task(conn, alt_seed) is None


def test_board_param_routes_complete_to_alt_board(multi_board_env):
    """kanban_complete on the alt board closes the alt task, leaving
    the default seed untouched."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Make alt task running so complete is valid.
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_complete({
        "task_id": alt_seed,
        "summary": "alt close",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "done"
    # Default seed is unchanged.
    with kb.connect() as conn:
        default_seed = multi_board_env["default_seed"]
        assert kb.get_task(conn, default_seed).status == "ready"


def test_board_param_routes_block_to_alt_board(multi_board_env):
    """kanban_block targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_block({
        "task_id": alt_seed,
        "reason": "need input on alt board",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "blocked"


def test_board_param_routes_unblock_to_alt_board(multi_board_env):
    """kanban_unblock targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.block_task(conn, alt_seed, reason="waiting")
        assert kb.get_task(conn, alt_seed).status == "blocked"

    out = kt._handle_unblock({"task_id": alt_seed, "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "ready"


def test_board_param_routes_heartbeat_to_alt_board(monkeypatch, tmp_path):
    """kanban_heartbeat targets the alt board's DB. Worker-scoped, so we
    use the worker-env style fixture inline (pinning HERMES_KANBAN_TASK
    to a task that exists in the alt board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "alt-worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Seed the alt board with a claimed task.
    with kb.connect(board="alt") as conn:
        tid = kb.create_task(conn, title="alt hb", assignee="alt-worker")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "alive on alt", "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True

    # Heartbeat event landed in the alt DB.
    with kb.connect(board="alt") as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "heartbeat"]
        assert len(events) == 1


def test_board_param_routes_link_to_alt_board(multi_board_env):
    """kanban_link operates on the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect(board="alt") as conn:
        a = kb.create_task(conn, title="A-alt", assignee="x")
        b = kb.create_task(conn, title="B-alt", assignee="x")

    out = kt._handle_link({
        "parent_id": a,
        "child_id": b,
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert b in kb.child_ids(conn, a)


def test_board_param_none_falls_back_to_env(worker_env):
    """When ``board`` is omitted or None, behaviour is unchanged from
    before this feature — calls land on whatever the env resolves to.
    Regression guard against accidentally rewiring default resolution."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_show({})  # no board, no task_id
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    out = kt._handle_show({"task_id": worker_env, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    # Sanity: the env-resolved path is the legacy default DB, NOT an
    # 'alt' board path. Confirms the override path was not silently
    # forced.
    assert kb.kanban_db_path() == kb.kanban_db_path(board="default")


def test_board_param_rejects_invalid_slug(multi_board_env):
    """A board slug that fails ``_normalize_board_slug`` surfaces as a
    structured tool_error rather than a 500 / unhandled exception."""
    from tools import kanban_tools as kt

    out = kt._handle_list({"board": "Has Spaces"})
    err = json.loads(out).get("error", "")
    assert "invalid board slug" in err, f"got {err!r}"


def test_board_param_in_all_schemas():
    """Every kanban_* tool schema must expose an optional ``board``
    parameter. This pins the contract surfaced to the LLM — adding a
    new kanban tool without ``board`` will fail CI immediately."""
    from tools import kanban_tools as kt

    schemas = [
        kt.KANBAN_SHOW_SCHEMA,
        kt.KANBAN_LIST_SCHEMA,
        kt.KANBAN_COMPLETE_SCHEMA,
        kt.KANBAN_BLOCK_SCHEMA,
        kt.KANBAN_HEARTBEAT_SCHEMA,
        kt.KANBAN_COMMENT_SCHEMA,
        kt.KANBAN_CREATE_SCHEMA,
        kt.KANBAN_UNBLOCK_SCHEMA,
        kt.KANBAN_LINK_SCHEMA,
        kt.KANBAN_ATTACH_SCHEMA,
        kt.KANBAN_ATTACH_URL_SCHEMA,
        kt.KANBAN_ATTACHMENTS_SCHEMA,
    ]
    for schema in schemas:
        props = schema["parameters"]["properties"]
        assert "board" in props, (
            f"{schema['name']} is missing the 'board' property"
        )
        assert props["board"]["type"] == "string"
        # board is optional everywhere — never in required.
        assert "board" not in schema["parameters"].get("required", []), (
            f"{schema['name']} marks board as required; must be optional"
        )


def test_waiting_for_user_control_is_only_on_heartbeat_schema():
    from tools import kanban_tools as kt

    heartbeat_props = kt.KANBAN_HEARTBEAT_SCHEMA["parameters"]["properties"]
    show_props = kt.KANBAN_SHOW_SCHEMA["parameters"]["properties"]

    assert heartbeat_props["waiting_for_user_control"]["type"] == "boolean"
    assert "waiting_for_user_control" not in show_props
    assert "waiting_for_user_control" not in kt.KANBAN_HEARTBEAT_SCHEMA[
        "parameters"
    ].get("required", [])


# ---------------------------------------------------------------------------
# kanban_create auto-subscribe behaviour
#
# When a worker calls kanban_create from inside a session that has a
# persistent delivery channel, the originating session should be
# subscribed to the new task's completion/block events automatically.
# - Gateway sessions: HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID set.
# - TUI sessions: HERMES_SESSION_KEY (or HERMES_SESSION_ID) set, with
#   the platform/chat_id ContextVars intentionally empty.
# - CLI / cron / test sessions: no delivery channel -> no subscription.
# - Config gate kanban.auto_subscribe_on_create: false -> no subscription
#   even when the session has a delivery channel.
# ---------------------------------------------------------------------------


def test_short_task_mode_rejects_model_supplied_global_idempotency_key(
    monkeypatch, worker_env
):
    """Enabled multi-channel governance must not reuse a globally keyed task."""
    from tools import kanban_tools as kt
    from agent import kanban_auto_handoff as handoff

    policy = {
        "agent": {"max_turns": 90},
        "kanban": {
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 36,
                "max_handoffs": 8,
            }
        },
    }
    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **_kwargs: handoff.build_dispatcher_policy_snapshot(policy),
    )
    result = json.loads(
        kt._handle_create(
            {
                "title": "must not cross-subscribe",
                "assignee": "peer",
                "idempotency_key": "model-key",
            }
        )
    )
    assert "error" in result
    assert "idempotency_key is unavailable" in result["error"]


def test_disabled_short_task_mode_preserves_legacy_idempotent_create(
    monkeypatch, worker_env
):
    """The new protection is opt-in; disabled mode keeps baseline semantics."""
    from tools import kanban_tools as kt
    from agent import kanban_auto_handoff as handoff

    policy = {
        "agent": {"max_turns": 90},
        "kanban": {"short_task_handoff": {"enabled": False}},
    }
    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **_kwargs: handoff.build_dispatcher_policy_snapshot(policy),
    )
    args = {
        "title": "legacy retry-safe create",
        "assignee": "peer",
        "idempotency_key": "legacy-key",
    }
    first = json.loads(kt._handle_create(dict(args)))
    second = json.loads(kt._handle_create(dict(args)))
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["task_id"] == second["task_id"]

def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return list(kb.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _sub_index(subs):
    """Normalise a list of notify-subs (dicts or objects) into dicts
    keyed by platform+chat_id, so assertions work regardless of the
    return shape."""
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "platform": getattr(s, "platform", None),
                "chat_id": getattr(s, "chat_id", None),
                "thread_id": getattr(s, "thread_id", None),
                "user_id": getattr(s, "user_id", None),
            })
    return out


def test_create_subscribes_gateway_session(monkeypatch, worker_env):
    """A gateway session (platform + chat_id set) gets auto-subscribed
    to its own kanban_create result, and the response surfaces the
    ``subscribed`` flag so the orchestrator can react."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-9")

    out = kt._handle_create({
        "title": "auto-sub gateway",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "telegram"
    assert s["chat_id"] == "chat-42"
    assert s["thread_id"] == "thread-7"
    assert s["user_id"] == "user-9"


def test_create_subscribes_tui_session_via_session_key(monkeypatch, worker_env):
    """TUI / desktop sessions don't have a platform/chat_id (single
    local channel), but the parent process exports HERMES_SESSION_KEY.
    We should still auto-subscribe, with platform='tui' and
    chat_id=<key>."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "auto-sub tui",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"


def test_create_does_not_subscribe_in_cli_session(monkeypatch, worker_env):
    """CLI / cron / test sessions have no persistent delivery channel.
    _maybe_auto_subscribe returns False and no row is written."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub cli",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_respects_auto_subscribe_on_create_false(monkeypatch, worker_env, tmp_path):
    """The config gate kanban.auto_subscribe_on_create=false must
    suppress auto-subscription even when the session has a delivery
    channel. This is the knob that addresses the upstream design
    concern from PR #19718 (reverted in #19721) — users who want
    explicit kanban_notify-subscribe calls per task get that."""
    # worker_env already created <tmp>/.hermes; use a fresh sibling
    # home to avoid mkdir() colliding with the worker's directory.
    home = tmp_path / "gate-home" / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "channel-1")

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no sub gated",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_partial_session_context_no_subscribe(monkeypatch, worker_env):
    """Only one of (platform, chat_id) set -> no implicit subscribe.
    Either both are set (gateway) or neither (TUI / CLI); partial is
    ambiguous and the safe default is to skip."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub partial",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d


def test_maybe_auto_subscribe_swallows_add_notify_sub_failure(monkeypatch, worker_env):
    """If add_notify_sub itself raises (e.g. DB locked, schema drift),
    _maybe_auto_subscribe must NOT bubble that up and fail the parent
    kanban_create. The function returns False and the parent create
    still succeeds with subscribed=False."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")

    from hermes_cli import kanban_db as kb

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    out = kt._handle_create({
        "title": "auto-sub tolerates add_notify_sub failure",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    assert d["subscribed"] is False, d


# ---------------------------------------------------------------------------
# Attachments — kanban_attach / kanban_attach_url / kanban_attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_private_urls(monkeypatch):
    """Opt the SSRF guard into private/loopback targets for local fixtures.

    Mirrors a user setting HERMES_ALLOW_PRIVATE_URLS on a private network.
    Resets the url_safety process-lifetime cache on both sides so the
    override neither leaks in nor out of the test.
    """
    from tools import url_safety

    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def test_attach_roundtrips_bytes_to_row_and_disk(worker_env):
    """kanban_attach decodes base64, writes the blob, and records the row."""
    import base64
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    content = b"hello attachment from a tool"
    out = kt._handle_attach({
        "filename": "notes.txt",
        "content_base64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
    })
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(content)
    att_id = d["attachment_id"]

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["notes.txt"]
        a = atts[0]
        assert a.id == att_id
        assert a.content_type == "text/plain"
        assert a.uploaded_by == "agent"
        # Blob is on disk under the task's attachments dir with the bytes.
        assert Path(a.stored_path).read_bytes() == content
        assert Path(a.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(worker_env).resolve()
        )
    finally:
        conn.close()


def test_attach_rejects_oversize(worker_env, monkeypatch):
    """A decoded payload over the cap returns a clean tool error, no row."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Shrink the cap so we don't have to build a 25 MB payload.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 8)
    out = kt._handle_attach({
        "filename": "big.bin",
        "content_base64": base64.b64encode(b"0123456789").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_rejects_bad_base64(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach({"filename": "x.txt", "content_base64": "not base64!!!"})
    d = json.loads(out)
    assert "error" in d and "base64" in d["error"]


def test_attach_requires_filename_and_content(worker_env):
    from tools import kanban_tools as kt

    assert "error" in json.loads(kt._handle_attach({"content_base64": "QQ=="}))
    assert "error" in json.loads(kt._handle_attach({"filename": "x.txt"}))


def test_attach_enforces_worker_task_ownership(worker_env):
    """A worker scoped to its own task can't attach to a foreign task."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="someone else's task", assignee="peer")
    finally:
        conn.close()

    out = kt._handle_attach({
        "task_id": other,
        "filename": "x.txt",
        "content_base64": base64.b64encode(b"x").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "scoped to task" in d["error"]


def test_attachments_lists_uploaded_files(worker_env):
    import base64

    from tools import kanban_tools as kt

    kt._handle_attach({
        "filename": "a.txt",
        "content_base64": base64.b64encode(b"aaa").decode(),
    })
    kt._handle_attach({
        "filename": "b.txt",
        "content_base64": base64.b64encode(b"bbbb").decode(),
    })
    out = kt._handle_attachments({})
    d = json.loads(out)
    assert d.get("ok") is True
    names = sorted(a["filename"] for a in d["attachments"])
    assert names == ["a.txt", "b.txt"]
    sizes = {a["filename"]: a["size"] for a in d["attachments"]}
    assert sizes == {"a.txt": 3, "b.txt": 4}


def test_attachments_unknown_task_errors(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attachments({"task_id": "t_nope"})
    assert "error" in json.loads(out)


def test_attach_url_fetches_local_fixture(worker_env, allow_private_urls):
    """kanban_attach_url downloads from an http(s) URL and stores the bytes.

    The fixture server lives on loopback, which the SSRF guard blocks by
    default — opted in via the allow_private_urls fixture exactly like a
    user on a private network would.
    """
    import http.server
    import threading
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    payload = b"downloaded-by-url body"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({
            "url": f"http://127.0.0.1:{port}/files/report.bin",
        })
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        # Filename derived from the URL path leaf.
        assert atts[0].filename == "report.bin"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def test_attach_url_rejects_oversize_stream(worker_env, monkeypatch, allow_private_urls):
    """An oversize response body is rejected during download, no row written."""
    import http.server
    import threading

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    big = b"x" * (64 * 1024)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *a):
            pass

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({"url": f"http://127.0.0.1:{port}/big.bin"})
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_rejects_non_http_scheme(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": "file:///etc/passwd"})
    d = json.loads(out)
    assert "error" in d
    assert "scheme" in d["error"]


# ---------------------------------------------------------------------------
# kanban_attach_url — SSRF guard (tools/url_safety.is_safe_url per hop)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_url_guard(monkeypatch):
    """Force the SSRF guard to its secure default for this test.

    Clears HERMES_ALLOW_PRIVATE_URLS and resets url_safety's process-lifetime
    cache on both sides so a prior test's opt-in can't leak in.
    """
    from tools import url_safety

    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _assert_attach_url_blocked(worker_env, url):
    """Call kanban_attach_url with ``url`` and assert the SSRF guard fired
    (clean tool error, no attachment row, no network fetch needed)."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": url})
    d = json.loads(out)
    assert "error" in d, out
    assert "SSRF" in d["error"] or "blocked" in d["error"].lower(), out
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_blocks_loopback(worker_env, default_url_guard):
    """http://127.0.0.1/ is rejected before any connection is made."""
    _assert_attach_url_blocked(worker_env, "http://127.0.0.1/")


def test_attach_url_blocks_cloud_metadata(worker_env, default_url_guard):
    """The cloud metadata endpoint is rejected — the #1 SSRF target."""
    _assert_attach_url_blocked(
        worker_env, "http://169.254.169.254/latest/meta-data/"
    )


def test_attach_url_blocks_private_range(worker_env, default_url_guard):
    """RFC1918 addresses (http://10.0.0.1/) are rejected."""
    _assert_attach_url_blocked(worker_env, "http://10.0.0.1/")


def _fake_public_dns(monkeypatch, mapping):
    """Patch url_safety's getaddrinfo so hostnames in ``mapping`` resolve to
    the given (public) IPs and literal IPs resolve to themselves — no real
    DNS or network traffic."""
    import ipaddress
    import socket as _socket

    real_af, real_sock = _socket.AF_INET, _socket.SOCK_STREAM

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            # Literal IPs pass through; unknown hostnames fail like NXDOMAIN.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise _socket.gaierror(f"fake DNS: unknown host {host!r}")
            ip = host
        return [(real_af, real_sock, 6, "", (ip, 0))]

    from tools import url_safety
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in {
            k.lower() for k in self.headers
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_attach_url_blocks_redirect_to_loopback(worker_env, default_url_guard, monkeypatch):
    """A public host 302ing to loopback is caught on the redirect hop.

    The pre-flight check passes (public IP), then the mocked response
    redirects to http://127.0.0.1/ — the guard must re-validate the
    Location target and refuse to follow it.
    """
    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    requested = []

    def fake_stream(method, url, **kwargs):
        requested.append(url)
        assert kwargs.get("follow_redirects") is False
        return _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1/latest/secrets"},
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/report.pdf"})
    d = json.loads(out)
    assert "error" in d, out
    assert "127.0.0.1" in d["error"], out
    # Only the public hop was ever fetched; the loopback target never was.
    assert requested == ["http://files.example.com/report.pdf"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_happy_path_public_host(worker_env, default_url_guard, monkeypatch):
    """A public URL passes the guard and the bytes are stored (mocked fetch)."""
    from pathlib import Path

    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    payload = b"public fetch body"

    def fake_stream(method, url, **kwargs):
        assert url == "http://files.example.com/docs/spec.pdf"
        return _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "application/pdf; charset=binary"},
            body=payload,
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/docs/spec.pdf"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["spec.pdf"]
        assert atts[0].content_type == "application/pdf"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()
