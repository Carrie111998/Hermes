"""Machine-enforced not-before safety invariants."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def _loopback_request():
    from hermes_cli import web_server
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/plugins/kanban/tasks",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (
                    web_server._SESSION_HEADER_NAME.lower().encode(),
                    web_server._SESSION_TOKEN.encode(),
                ),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 9119),
            "scheme": "http",
            "root_path": "",
            "app": web_server.app,
        }
    )


FUTURE = "2030-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, task_id: str, kind: str):
    return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


def test_future_task_is_scheduled_and_dependency_resolver_releases_only_when_due(
    kanban_home, monkeypatch
):
    clock = [1_700_000_000.0]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="wait for release",
            assignee="default",
            not_before=FUTURE,
        )
        assert kb.get_task(conn, task_id).status == "scheduled"

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "scheduled"
        blocked = _events(conn, task_id, "not_before_blocked")
        assert len(blocked) == 1
        assert blocked[0].payload["operation"] == "dependency_resolver"

        # Repeated dispatcher ticks do not flood the audit stream.
        assert kb.recompute_ready(conn) == 0
        assert len(_events(conn, task_id, "not_before_blocked")) == 1

        clock[0] = 1_900_000_000.0
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, task_id).status == "ready"


def test_child_inherits_latest_parent_deadline_but_keeps_later_explicit_gate(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        first_parent = kb.create_task(
            conn,
            title="first parent",
            assignee="worker",
            not_before="2030-01-01T00:00:00Z",
        )
        second_parent = kb.create_task(
            conn,
            title="second parent",
            assignee="worker",
            not_before="2031-01-01T00:00:00Z",
        )
        inherited = kb.create_task(
            conn,
            title="inherited child",
            assignee="worker",
            parents=[first_parent, second_parent],
            not_before="2029-01-01T00:00:00Z",
        )
        explicit_later = kb.create_task(
            conn,
            title="later child",
            assignee="worker",
            parents=[first_parent],
            not_before="2032-01-01T00:00:00Z",
        )

        assert kb.get_task(conn, inherited).not_before == "2031-01-01T00:00:00Z"
        assert kb.get_task(conn, inherited).status == "scheduled"
        assert kb.get_task(conn, explicit_later).not_before == "2032-01-01T00:00:00Z"


def test_direct_claim_and_completion_bypasses_are_blocked_without_state_mutation(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="protected", assignee="default")
        conn.execute(
            "UPDATE tasks SET not_before = ?, status = 'ready' WHERE id = ?",
            (FUTURE, task_id),
        )

        assert kb.claim_task(conn, task_id, claimer="bypass") is None
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.claim_lock is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0

        # Simulate an already-running drain/root-drain worker that gets gated
        # after claim. Completion must leave task and run evidence untouched.
        conn.execute(
            "UPDATE tasks SET not_before = NULL WHERE id = ?", (task_id,)
        )
        claimed = kb.claim_task(conn, task_id, claimer="worker")
        assert claimed is not None
        run_id = kb.get_task(conn, task_id).current_run_id
        conn.execute(
            "UPDATE tasks SET not_before = ? WHERE id = ?", (FUTURE, task_id)
        )

        assert kb.complete_task(
            conn,
            task_id,
            summary="unsafe early completion",
            expected_run_id=run_id,
        ) is False
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.current_run_id == run_id
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert tuple(run) == ("running", None, None)
        complete_block = _events(conn, task_id, "not_before_blocked")[-1]
        assert complete_block.payload["operation"] == "complete"


def test_completion_guard_and_terminal_mutation_are_atomic_across_connections(
    kanban_home, monkeypatch,
):
    """A gate installed after preflight must invalidate the completion CAS."""
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="race completion", assignee="worker")
        db_path = kb.kanban_db_path()

    preflight_finished = threading.Event()
    allow_terminal_phase = threading.Event()
    original_merge = kb._merge_completion_prose_artifacts

    def pause_after_preflight(*args, **kwargs):
        # This helper is reached only after the current implementation's
        # separate guard transaction has committed, but before its terminal
        # mutation transaction begins.
        preflight_finished.set()
        assert allow_terminal_phase.wait(5)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(kb, "_merge_completion_prose_artifacts", pause_after_preflight)
    outcome: list[bool] = []

    def complete_from_worker_connection():
        with kb.connect(db_path=db_path) as worker_conn:
            outcome.append(kb.complete_task(worker_conn, task_id, result="too early"))

    worker = threading.Thread(target=complete_from_worker_connection)
    worker.start()
    assert preflight_finished.wait(5)

    with kb.connect(db_path=db_path) as racing_conn:
        with kb.write_txn(racing_conn):
            racing_conn.execute(
                "UPDATE tasks SET not_before = ? WHERE id = ?",
                (FUTURE, task_id),
            )

    allow_terminal_phase.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome == [False]

    with kb.connect(db_path=db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.not_before == FUTURE
        assert task.result is None


def test_tool_dispatch_revalidates_same_task_gate_before_provider_and_keeps_other_task_live(
    kanban_home, monkeypatch,
):
    """A concurrent gate cancels only the stale task's provider dispatch."""
    from agent import relay_tools, tool_executor

    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)
    with kb.connect() as conn:
        same_task = kb.create_task(conn, title="same task", assignee="worker")
        other_task = kb.create_task(conn, title="other task", assignee="worker")
        db_path = kb.kanban_db_path()

    agent = SimpleNamespace(
        quiet_mode=True,
        tool_progress_mode="off",
        _tool_guardrails=SimpleNamespace(
            before_call=lambda _name, _args: SimpleNamespace(
                allows_execution=True,
            )
        ),
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_args, **_kwargs: None,
    )
    provider_mutations: list[str] = []
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda _name, args, callback, **_kwargs: (callback(args), args),
    )

    preflight_finished = threading.Event()
    allow_execution_boundary = threading.Event()
    original_preflight = tool_executor._kanban_not_before_tool_block

    def pause_after_preflight(function_name):
        result = original_preflight(function_name)
        if (tool_executor.os.getenv("HERMES_KANBAN_TASK") or "") == same_task:
            preflight_finished.set()
            assert allow_execution_boundary.wait(5)
        return result

    monkeypatch.setattr(tool_executor, "_kanban_not_before_tool_block", pause_after_preflight)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", same_task)

    same_outcome: list = []

    def run_same_task():
        same_outcome.append(
            tool_executor._run_agent_tool_execution_middleware(
                agent,
                function_name="terminal",
                function_args={"command": "provider-mutation"},
                effective_task_id=same_task,
                tool_call_id="same-call",
                execute=lambda _args: provider_mutations.append(same_task) or "same",
            )
        )

    worker = threading.Thread(target=run_same_task)
    worker.start()
    assert preflight_finished.wait(5)

    # Install the gate while the same task is between preflight and provider
    # execution. This is the race the execution-boundary lease must close.
    with kb.connect(db_path=db_path) as racing_conn:
        with kb.write_txn(racing_conn):
            racing_conn.execute(
                "UPDATE tasks SET not_before = ? WHERE id = ?",
                (FUTURE, same_task),
            )

    # An unrelated task must remain dispatchable while the stale same-task
    # call is parked at its deterministic boundary.
    monkeypatch.setenv("HERMES_KANBAN_TASK", other_task)
    other = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={"command": "unrelated-provider-mutation"},
        effective_task_id=other_task,
        tool_call_id="other-call",
        execute=lambda _args: provider_mutations.append(other_task) or "other",
    )
    assert other.blocked is False
    assert provider_mutations == [other_task]

    monkeypatch.setenv("HERMES_KANBAN_TASK", same_task)
    allow_execution_boundary.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert same_outcome and same_outcome[0].blocked is True
    assert provider_mutations == [other_task]


def test_same_task_dispatches_hold_independent_leases_until_each_callback_finishes(
    kanban_home, monkeypatch,
):
    """Parallel callbacks coexist; either live lease still protects the gate."""
    from agent import relay_tools, tool_executor

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="parallel task", assignee="worker")
        db_path = kb.kanban_db_path()

    clock = [int(tool_executor.time.time())]
    monkeypatch.setattr(tool_executor.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        tool_executor._KanbanNotBeforeDispatchLease, "_LEASE_SECONDS", 2
    )
    monkeypatch.setattr(
        tool_executor._KanbanNotBeforeDispatchLease, "_RENEW_INTERVAL_SECONDS", 0.01
    )

    agent = SimpleNamespace(
        quiet_mode=True,
        tool_progress_mode="off",
        _tool_guardrails=SimpleNamespace(
            before_call=lambda _name, _args: SimpleNamespace(allows_execution=True)
        ),
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda _name, args, callback, **_kwargs: (callback(args), args),
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    entered = {name: threading.Event() for name in ("first", "second")}
    release = {name: threading.Event() for name in ("first", "second")}
    provider_mutations: list[str] = []
    outcomes = {}

    def run(name: str):
        def execute(_args):
            provider_mutations.append(name)
            entered[name].set()
            assert release[name].wait(5)
            return name

        outcomes[name] = tool_executor._run_agent_tool_execution_middleware(
            agent,
            function_name="terminal",
            function_args={"command": name},
            effective_task_id=task_id,
            tool_call_id=f"{name}-call",
            execute=execute,
        )

    workers = {
        name: threading.Thread(target=run, args=(name,))
        for name in ("first", "second")
    }
    try:
        for worker in workers.values():
            worker.start()
        assert all(event.wait(5) for event in entered.values())

        with kb.connect(db_path=db_path) as conn:
            leases = conn.execute(
                "SELECT lease_hash, expires_at "
                "FROM kanban_not_before_dispatch_leases "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            assert len(leases) == 2
            assert len({row["lease_hash"] for row in leases}) == 2
            assert {row["expires_at"] for row in leases} == {clock[0] + 2}

        # Advance beyond both original deadlines while the callbacks remain
        # active. The renewal threads must extend both authorizations.
        clock[0] += 3
        renewed = False
        for _ in range(100):
            threading.Event().wait(0.01)
            with kb.connect(db_path=db_path) as conn:
                expiries = {
                    row["expires_at"]
                    for row in conn.execute(
                        "SELECT expires_at FROM kanban_not_before_dispatch_leases "
                        "WHERE task_id = ?",
                        (task_id,),
                    )
                }
            if expiries == {clock[0] + 2}:
                renewed = True
                break
        assert renewed

        with kb.connect(db_path=db_path) as conn:
            conn.create_function("unixepoch", 0, lambda: clock[0])
            with pytest.raises(sqlite3.IntegrityError, match="dispatch lease is active"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET not_before = ? WHERE id = ?",
                        (FUTURE, task_id),
                    )

        release["first"].set()
        workers["first"].join(timeout=5)
        assert not workers["first"].is_alive()
        with kb.connect(db_path=db_path) as conn:
            conn.create_function("unixepoch", 0, lambda: clock[0])
            assert conn.execute(
                "SELECT COUNT(*) FROM kanban_not_before_dispatch_leases "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError, match="dispatch lease is active"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET not_before = ? WHERE id = ?",
                        (FUTURE, task_id),
                    )

        release["second"].set()
        workers["second"].join(timeout=5)
        assert not workers["second"].is_alive()
    finally:
        for event in release.values():
            event.set()
        for worker in workers.values():
            worker.join(timeout=5)

    with kb.connect(db_path=db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_not_before_dispatch_leases "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET not_before = ? WHERE id = ?",
                (FUTURE, task_id),
            )
        assert kb.get_task(conn, task_id).not_before == FUTURE

    assert sorted(provider_mutations) == ["first", "second"]
    assert all(not outcome.blocked for outcome in outcomes.values())


def test_task_scoped_dispatch_fails_closed_without_pinned_database(monkeypatch):
    from agent import relay_tools, tool_executor

    agent = SimpleNamespace(
        quiet_mode=True,
        tool_progress_mode="off",
        _tool_guardrails=SimpleNamespace(
            before_call=lambda _name, _args: SimpleNamespace(allows_execution=True)
        ),
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda _name, args, callback, **_kwargs: (callback(args), args),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing_board")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    provider_mutations = []

    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={"command": "must-not-run"},
        effective_task_id="t_missing_board",
        tool_call_id="missing-board-call",
        execute=lambda _args: provider_mutations.append("ran"),
    )

    assert outcome.blocked is True
    assert "pinned Kanban database is missing" in outcome.result
    assert provider_mutations == []


def test_dispatcher_and_default_assignment_make_no_mutation_before_release(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)
    spawned = []

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="future unassigned", not_before=FUTURE)
        # Simulate a stale/external writer bypassing the scheduled status. The
        # dispatcher backstop must run before default-assignee mutation.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args: spawned.append(args),
            default_assignee="default",
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert result.skipped_not_before == [task_id]
        assert spawned == []
        assert task.status == "ready"
        assert task.assignee is None
        assert _events(conn, task_id, "not_before_blocked")[-1].payload[
            "operation"
        ] == "dispatcher"


def test_manual_promotion_requires_sealed_human_override_and_audits_it(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="operator release",
            assignee="default",
            not_before=FUTURE,
        )

        ok, reason = kb.promote_task(
            conn,
            task_id,
            actor="worker",
            force=True,
        )
        assert ok is False
        assert "not-before" in reason
        assert kb.get_task(conn, task_id).status == "scheduled"

        override = kb.mint_owner_not_before_override(
            conn,
            task_id,
            request=_loopback_request(),
            reason="approved emergency release",
        )
        ok, reason = kb.promote_task(
            conn,
            task_id,
            actor="alex",
            reason="approved emergency release",
            force=True,
            not_before_override=override,
        )
        assert (ok, reason) == (True, None)
        promoted = kb.get_task(conn, task_id)
        assert promoted.status == "ready"
        assert promoted.not_before is None
        event = _events(conn, task_id, "not_before_overridden")[-1]
        assert event.payload == {
            "operation": "manual_promotion",
            "not_before": FUTURE,
            "owner_identity": f"local:{os.getuid()}",
            "reason": "approved emergency release",
            "authenticated_by": "dashboard_session_token",
        }


def test_legacy_and_forged_not_before_overrides_fail_closed(kanban_home):
    with pytest.raises(ValueError, match="owner-authenticated"):
        kb.authenticated_human_not_before_override(
            actor="alex",
            reason="self asserted",
            authenticated_by="worker supplied string",
        )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="forged release",
            assignee="worker",
            not_before=FUTURE,
        )
        forged = SimpleNamespace(
            actor="alex",
            reason="approved",
            authenticated_by="owner",
            _seal=object(),
        )
        ok, reason = kb.promote_task(
            conn,
            task_id,
            actor="worker",
            force=True,
            not_before_override=forged,
        )
        assert ok is False
        assert "not-before" in reason
        assert kb.get_task(conn, task_id).not_before == FUTURE


def test_owner_capability_construction_is_not_a_public_issuer():
    with pytest.raises(TypeError):
        kb.OwnerNotBeforeOverride(
            task_id="t_deadbeef",
            owner_identity="alex",
            reason="self asserted",
            token="forged",
            _seal=object(),
        )
    assert not hasattr(kb, "VerifiedKanbanOwner")


def test_gated_dashboard_auth_can_issue_owner(kanban_home, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware as auth_middleware
    from hermes_cli.dashboard_auth.base import Session
    from starlette.responses import Response

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated owner", not_before=FUTURE)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(
        auth_middleware,
        "_verify_bearer",
        lambda request, *, access_token: Session(
            user_id="alex", email="alex@example.com", display_name="Alex",
            org_id="org", provider="test-owner-auth", expires_at=2_000_000_000,
            access_token=access_token, refresh_token="refresh",
        ),
    )
    request = _loopback_request()
    request.scope["headers"] = [
        (b"host", b"127.0.0.1"),
        (b"authorization", b"Bearer gated-token"),
    ]
    issued = []

    async def endpoint(authenticated_request):
        with kb.connect() as conn:
            issued.append(kb.mint_owner_not_before_override(
                conn, task_id, request=authenticated_request, reason="approved"
            ))
        return Response(status_code=200)


    response = asyncio.run(web_server._dashboard_auth_gate(request, endpoint))
    assert response.status_code == 200
    assert len(issued) == 1
    with kb.connect() as conn:
        ok, reason = kb.promote_task(
            conn, task_id, actor="alex", force=True, not_before_override=issued[0]
        )
        assert (ok, reason) == (True, None)
        assert kb.get_task(conn, task_id).not_before is None


def test_forged_request_state_cannot_issue_owner(kanban_home, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    request = _loopback_request()
    request.scope["headers"] = [(b"host", b"127.0.0.1")]
    request.state.session = SimpleNamespace(
        user_id="forged-user",
        provider="forged-provider",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="forged request", not_before=FUTURE)
        with pytest.raises(PermissionError, match="owner-authenticated"):
            kb.mint_owner_not_before_override(
                conn, task_id, request=request, reason="forged"
            )


def test_gated_owner_capability_rejects_wrong_user_session(kanban_home, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware as auth_middleware
    from hermes_cli.dashboard_auth.base import Session
    from starlette.responses import Response

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="wrong session", not_before=FUTURE)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(
        auth_middleware,
        "_verify_bearer",
        lambda request, *, access_token: Session(
            user_id="authenticated-user", email="", display_name="Authenticated",
            org_id="", provider="test-owner-auth", expires_at=2_000_000_000,
            access_token=access_token, refresh_token="refresh",
        ),
    )
    request = _loopback_request()
    request.scope["headers"] = [
        (b"host", b"127.0.0.1"),
        (b"authorization", b"Bearer gated-token"),
    ]
    outcome = []

    async def endpoint(authenticated_request):
        authenticated_request.state.session = SimpleNamespace(
            user_id="different-user",
            provider="test-owner-auth",
        )
        with kb.connect() as conn:
            with pytest.raises(PermissionError, match="owner-authenticated"):
                kb.mint_owner_not_before_override(
                    conn, task_id, request=authenticated_request, reason="wrong"
                )
        outcome.append(True)
        return Response(status_code=200)

    response = asyncio.run(web_server._dashboard_auth_gate(request, endpoint))
    assert response.status_code == 200
    assert outcome == [True]


def test_ordinary_worker_without_control_plane_cannot_issue_owner(kanban_home):
    from hermes_cli import web_server

    request = _loopback_request()
    request.scope["headers"] = [(b"host", b"127.0.0.1")]
    failures = []

    def worker():
        try:
            with kb.connect() as conn:
                task_id = kb.create_task(conn, title="worker request", not_before=FUTURE)
                kb.mint_owner_not_before_override(
                    conn, task_id, request=request, reason="worker"
                )
        except PermissionError:
            failures.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == [True]
    assert web_server.verified_dashboard_owner(request) is None


def test_exposed_capability_forgery_cannot_issue_owner(kanban_home, monkeypatch):
    """The former module capability objects cannot authenticate a request."""
    from hermes_cli import web_server
    from starlette.responses import Response

    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    request = _loopback_request()
    request.state.session = SimpleNamespace(
        user_id="forged-user",
        provider="forged-provider",
    )

    # Exercise the exact in-process attack against the old implementation.
    # The objects are no longer exported after the repair; either way, the
    # ordinary plugin/worker path must not obtain owner provenance.
    capability_type = getattr(web_server, "_DashboardAuthCapability", None)
    capability_context = getattr(web_server, "_DASHBOARD_AUTH_CAPABILITY", None)
    if capability_type is not None and capability_context is not None:
        marker = capability_context.set(
            capability_type(
                app=web_server.app,
                scope=request.scope,
                identity="forged-user",
                authenticated_by="dashboard_session:forged-provider",
            )
        )
        try:
            assert web_server.verified_dashboard_owner(request) is None
        finally:
            capability_context.reset(marker)
    else:
        assert web_server.verified_dashboard_owner(request) is None

    # Public auth-bootstrap paths bypass credential verification by design;
    # they must not turn caller-supplied request state into a lease either.
    request.scope["path"] = "/login"

    async def public_endpoint(public_request):
        assert web_server.verified_dashboard_owner(public_request) is None
        return Response(status_code=200)

    response = asyncio.run(web_server._dashboard_auth_gate(request, public_endpoint))
    assert response.status_code == 200


def test_gated_owner_lease_is_revoked_from_copied_context(kanban_home, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import middleware as auth_middleware
    from hermes_cli.dashboard_auth.base import Session
    from starlette.responses import Response

    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(
        auth_middleware,
        "_verify_bearer",
        lambda request, *, access_token: Session(
            user_id="copy-context-user", email="", display_name="Copy Context",
            org_id="", provider="test-copy-context", expires_at=2_000_000_000,
            access_token=access_token, refresh_token="refresh",
        ),
    )
    request = _loopback_request()
    request.scope["headers"] = [
        (b"host", b"127.0.0.1"),
        (b"authorization", b"Bearer copied-context-token"),
    ]
    copied_contexts = []

    async def endpoint(authenticated_request):
        copied_contexts.append(copy_context())
        assert web_server.verified_dashboard_owner(authenticated_request) == (
            "copy-context-user",
            "dashboard_session:test-copy-context",
        )
        return Response(status_code=200)

    response = asyncio.run(web_server._dashboard_auth_gate(request, endpoint))
    assert response.status_code == 200
    assert len(copied_contexts) == 1
    assert copied_contexts[0].run(
        web_server.verified_dashboard_owner, request
    ) is None


def test_mounted_basic_auth_sync_patch_can_issue_owner(kanban_home):
    """The production mounted sync route keeps gated owner provenance.

    This follows the real password-login flow through the production
    ``web_server.app`` and its bundled Kanban router.  The PATCH handler is
    synchronous, so FastAPI executes it in AnyIO's worker thread after the
    auth middleware has wrapped the ASGI request.
    """
    from fastapi.testclient import TestClient

    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from plugins.dashboard_auth.basic import BasicAuthProvider, hash_password

    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_port = getattr(web_server.app.state, "bound_port", None)
    clear_providers()
    register_provider(
        BasicAuthProvider(
            username="admin",
            password_hash=hash_password("correct horse battery staple"),
            secret=b"test-basic-auth-secret-1234",
        )
    )
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "dashboard.example.test"
    web_server.app.state.bound_port = 443
    try:
        with TestClient(
            web_server.app,
            base_url="https://dashboard.example.test",
        ) as dashboard:
            with kb.connect() as conn:
                unauthenticated_task = kb.create_task(
                    conn, title="unauthenticated release", not_before=FUTURE
                )
            unauthenticated = dashboard.patch(
                f"/api/plugins/kanban/tasks/{unauthenticated_task}",
                json={
                    "status": "ready",
                    "not_before_override_reason": "unauthenticated",
                },
            )
            assert unauthenticated.status_code == 401

            login = dashboard.post(
                "/auth/password-login",
                json={
                    "provider": "basic",
                    "username": "admin",
                    "password": "correct horse battery staple",
                },
            )
            assert login.status_code == 200, login.text

            created = dashboard.post(
                "/api/plugins/kanban/tasks",
                json={
                    "title": "mounted gated release",
                    "assignee": "worker",
                    "not_before": FUTURE,
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["id"]

            released = dashboard.patch(
                f"/api/plugins/kanban/tasks/{task_id}",
                json={
                    "status": "ready",
                    "not_before_override_reason": "approved",
                },
            )
            assert released.status_code == 200, released.text
            assert released.json()["task"]["not_before"] is None
    finally:
        clear_providers()
        web_server.app.state.auth_required = previous_required
        web_server.app.state.bound_host = previous_host
        web_server.app.state.bound_port = previous_port


def test_synthetic_completed_evidence_is_blocked_before_release(
    kanban_home, monkeypatch


):
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="no synthetic proof",
            assignee="default",
            not_before=FUTURE,
        )
        with kb.write_txn(conn):
            synthetic_id = kb._synthesize_ended_run(
                conn,
                task_id,
                outcome="completed",
                summary="fabricated early proof",
            )
        assert synthetic_id == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0
        event = _events(conn, task_id, "not_before_blocked")[-1]
        assert event.payload["operation"] == "synthetic_evidence_creation"


def test_tool_execution_backstop_blocks_before_relay_or_provider_mutation(
    kanban_home, monkeypatch
):
    from agent import relay_tools, tool_executor

    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="no provider mutation",
            assignee="default",
            not_before=FUTURE,
        )
        db_path = kb.kanban_db_path()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    relay_called = []
    execute_called = []
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda *args, **kwargs: relay_called.append((args, kwargs)),
    )

    outcome = tool_executor._run_agent_tool_execution_middleware(
        object(),
        function_name="terminal",
        function_args={"command": "vercel remove production"},
        effective_task_id="session-task",
        tool_call_id="call-1",
        execute=lambda args: execute_called.append(args),
    )

    assert outcome.blocked is True
    assert outcome.dispatched is False
    assert relay_called == []
    assert execute_called == []
    assert "not-before deadline" in json.loads(outcome.result)["error"]

    with kb.connect(db_path=db_path) as conn:
        event = _events(conn, task_id, "not_before_blocked")[-1]
        assert event.payload["operation"] == "side_effect_execution:terminal"

def test_override_is_reusable_when_completion_cas_fails(kanban_home, monkeypatch):
    """A failed terminal CAS must not burn a valid owner release."""
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="rollback completion", assignee="worker",
            not_before=FUTURE,
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,)
        )
        override = kb.mint_owner_not_before_override(
            conn, task_id, request=_loopback_request(), reason="approved"
        )

        assert kb.complete_task(
            conn,
            task_id,
            expected_run_id=999,
            not_before_override=override,
        ) is False
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.not_before == FUTURE
        used = conn.execute(
            "SELECT used_at FROM kanban_not_before_overrides WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert used["used_at"] is None
        assert _events(conn, task_id, "not_before_overridden") == []

        assert kb.complete_task(
            conn, task_id, not_before_override=override
        ) is True


def test_unblock_override_is_consumed_after_success(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unblock release", not_before=FUTURE)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        override = kb.mint_owner_not_before_override(
            conn, task_id, request=_loopback_request(), reason="approved"
        )
        assert kb.unblock_task(conn, task_id, not_before_override=override) is True
        task = kb.get_task(conn, task_id)
        assert task.status == "ready" and task.not_before is None
        used = conn.execute(
            "SELECT used_at FROM kanban_not_before_overrides WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert used["used_at"] is not None
