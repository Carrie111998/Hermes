"""Machine-enforced not-before safety invariants."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


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
                "SELECT lease_hash FROM kanban_not_before_dispatch_leases "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            assert len(leases) == 2
            assert len({row["lease_hash"] for row in leases}) == 2
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

        owner = kb._verified_kanban_owner(
            "alex",
            "local_tty_os_user",
        )
        override = kb.mint_owner_not_before_override(
            conn,
            task_id,
            owner=owner,
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
            "owner_identity": "alex",
            "reason": "approved emergency release",
            "authenticated_by": "local_tty_os_user",
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
