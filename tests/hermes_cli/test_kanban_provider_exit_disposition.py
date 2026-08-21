"""Behavior contracts for structured Kanban provider-worker exits."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _prepare_isolated_kanban_home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "kanban-test.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert kb.kanban_db_path().resolve() == db_path.resolve()
    assert kb.kanban_db_path().resolve().is_relative_to(tmp_path.resolve())
    probe = sqlite3.connect(db_path)
    try:
        attached = Path(probe.execute("PRAGMA database_list").fetchone()[2]).resolve()
        assert attached == db_path.resolve()
        assert attached.is_relative_to(tmp_path.resolve())
    finally:
        probe.close()
    kb.init_db()
    return home, db_path


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home, db_path = _prepare_isolated_kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        attached = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        assert attached == db_path.resolve()
        assert attached.is_relative_to(tmp_path.resolve())
    return home


def test_ambient_kanban_db_cannot_capture_synthetic_tasks(tmp_path, monkeypatch):
    ambient_db = tmp_path / "ambient-live-board" / "kanban.db"
    kb.init_db(db_path=ambient_db)
    with kb.connect(db_path=ambient_db) as conn:
        sentinel_id = kb.create_task(conn, title="sentinel", assignee="operator")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(ambient_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "hermes-agent")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/live/worktree")
    _home, isolated_db = _prepare_isolated_kanban_home(tmp_path / "isolated", monkeypatch)

    with kb.connect() as conn:
        attached = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        assert attached == isolated_db.resolve()
        synthetic_id = kb.create_task(conn, title="synthetic", assignee="worker")
        assert kb.get_task(conn, synthetic_id) is not None

    with kb.connect(db_path=ambient_db) as conn:
        assert [task.id for task in kb.list_tasks(conn)] == [sentinel_id]
        assert kb.get_task(conn, synthetic_id) is None


def _exited_status(code: int) -> int:
    return code << 8


def _start_run(conn, *, provider="openrouter", model="model-a", assignee="worker"):
    host = kb._claimer_id().split(":", 1)[0]
    task_id = kb.create_task(
        conn,
        title="provider exit",
        assignee=assignee,
        model_override=model,
        provider_override=provider,
    )
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:test")
    assert claimed is not None
    pid = 87001
    kb._set_worker_pid(conn, task_id, pid)
    return task_id, claimed.current_run_id, pid


def _report_and_reap(
    conn,
    task_id,
    run_id,
    pid,
    *,
    disposition,
    classification,
    status_code,
    provider="openrouter",
    model="model-a",
    exit_code=0,
    session_id="session-safe",
):
    kb.record_provider_exit_disposition(
        conn,
        task_id,
        run_id=run_id,
        disposition=disposition,
        classification=classification,
        status_code=status_code,
        provider=provider,
        model=model,
        session_id=session_id,
    )
    kb._record_worker_exit(pid, _exited_status(exit_code))
    original_alive = kb._pid_alive
    kb._pid_alive = lambda _pid: False
    try:
        kb.detect_crashed_workers(conn)
    finally:
        kb._pid_alive = original_alive


def test_http_402_rc0_blocks_once_without_protocol_retry(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn)

        _report_and_reap(
            conn,
            task_id,
            run_id,
            pid,
            disposition="terminal",
            classification="billing",
            status_code=402,
        )

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.consecutive_failures == 0
        events = kb.list_events(conn, task_id)
        terminal = [event for event in events if event.kind == "provider_terminal"]
        assert len(terminal) == 1
        assert not [event for event in events if event.kind == "protocol_violation"]
        assert not [event for event in events if event.kind == "gave_up"]
        payload = terminal[0].payload
        assert payload["provider"] == "openrouter"
        assert payload["model"] == "model-a"
        assert payload["status_code"] == 402
        assert payload["classification"] == "billing"
        assert payload["failure_reason"] == "billing"
        assert payload["provider_terminal"] is True
        assert payload["session_id"] == "session-safe"
        assert payload["log_path"].endswith(f"{task_id}.log")
        runs = kb.list_runs(conn, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == "provider_terminal"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"


@pytest.mark.parametrize(
    ("status_code", "classification"),
    [(401, "auth"), (403, "auth_permanent"), (404, "model_not_found")],
)
def test_deterministic_provider_failures_are_terminal_on_first_observation(
    kanban_home, status_code, classification
):
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn)
        _report_and_reap(
            conn,
            task_id,
            run_id,
            pid,
            disposition="terminal",
            classification=classification,
            status_code=status_code,
        )
        assert kb.get_task(conn, task_id).status == "blocked"
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "provider_terminal"
        ) == 1


@pytest.mark.parametrize(
    ("status_code", "classification"),
    [(429, "rate_limit"), (503, "overloaded")],
)
def test_transient_provider_failures_use_bounded_retry_budget(
    kanban_home, status_code, classification
):
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn)
        _report_and_reap(
            conn,
            task_id,
            run_id,
            pid,
            disposition="transient",
            classification=classification,
            status_code=status_code,
            exit_code=75,
        )
        first = kb.get_task(conn, task_id)
        assert first.status == "ready"
        assert first.consecutive_failures == 1

        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        second_pid = pid + 1
        kb._set_worker_pid(conn, task_id, second_pid)
        _report_and_reap(
            conn,
            task_id,
            claimed.current_run_id,
            second_pid,
            disposition="transient",
            classification=classification,
            status_code=status_code,
            exit_code=75,
        )

        final = kb.get_task(conn, task_id)
        assert final.status == "blocked"
        assert final.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT
        events = kb.list_events(conn, task_id)
        assert len([event for event in events if event.kind == "provider_transient"]) == 2
        assert len([event for event in events if event.kind == "gave_up"]) == 1
        assert not [event for event in events if event.kind == "protocol_violation"]


@pytest.mark.parametrize(
    ("disposition", "classification", "status_code", "expected"),
    [
        ("terminal", "rate_limit", 429, "transient"),
        ("transient", "content_policy_blocked", 400, "safety_refusal"),
    ],
)
def test_inconsistent_provider_exit_envelopes_are_rejected(
    kanban_home, disposition, classification, status_code, expected
):
    with kb.connect() as conn:
        task_id, run_id, _pid = _start_run(conn)

        with pytest.raises(ValueError, match=f"expected {expected!r}"):
            kb.record_provider_exit_disposition(
                conn,
                task_id,
                run_id=run_id,
                disposition=disposition,
                classification=classification,
                status_code=status_code,
                provider="openrouter",
                model="model-a",
            )

        assert kb._provider_exit_for_run(conn, run_id) is None
        assert kb.get_task(conn, task_id).status == "running"

        fingerprint = kb._provider_route_fingerprint(
            "openrouter", "model-a", "worker"
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ? AND task_id = ?",
            (
                json.dumps(
                    {
                        kb._PROVIDER_EXIT_METADATA_KEY: {
                            "disposition": disposition,
                            "classification": classification,
                            "status_code": status_code,
                            "provider": "openrouter",
                            "model": "model-a",
                            "fingerprint": fingerprint,
                            "config_fingerprint": fingerprint,
                        }
                    }
                ),
                run_id,
                task_id,
            ),
        )
        assert kb._provider_exit_for_run(conn, run_id) is None

        kb._record_worker_exit(_pid, _exited_status(0))
        original_alive = kb._pid_alive
        kb._pid_alive = lambda _worker_pid: False
        try:
            kb.detect_crashed_workers(conn)
        finally:
            kb._pid_alive = original_alive

        events = kb.list_events(conn, task_id)
        assert [event for event in events if event.kind == "protocol_violation"]
        assert not [
            event
            for event in events
            if event.kind
            in {"provider_terminal", "provider_transient", "provider_safety_refusal"}
        ]


def test_provider_exit_record_rejects_run_from_another_task(kanban_home):
    with kb.connect() as conn:
        task_id, task_run_id, _pid = _start_run(conn)
        other_task_id, other_run_id, _other_pid = _start_run(conn)

        assert kb.record_provider_exit_disposition(
            conn,
            task_id,
            run_id=other_run_id,
            disposition="terminal",
            classification="billing",
            status_code=402,
            provider="openrouter",
            model="model-a",
        ) is False

        assert kb._provider_exit_for_run(conn, task_run_id) is None
        assert kb._provider_exit_for_run(conn, other_run_id) is None
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, other_task_id).status == "running"


def test_changed_task_provider_override_allows_one_new_run(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn)
        _report_and_reap(
            conn,
            task_id,
            run_id,
            pid,
            disposition="terminal",
            classification="billing",
            status_code=402,
        )
        assert kb.get_task(conn, task_id).status == "blocked"

        assert kb.unblock_task(conn, task_id) is False
        assert kb.get_task(conn, task_id).status == "blocked"

        assert kb.set_model_override(conn, task_id, "model-a", "openrouter")
        assert kb.get_task(conn, task_id).status == "blocked"

        assert kb.set_model_override(conn, task_id, "model-b", "anthropic")
        assert kb.get_task(conn, task_id).status == "ready"
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.current_run_id != run_id


def test_safety_refusal_is_separate_and_does_not_auto_recover_by_provider_change(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn)
        _report_and_reap(
            conn,
            task_id,
            run_id,
            pid,
            disposition="safety_refusal",
            classification="content_policy_blocked",
            status_code=400,
        )
        assert kb.get_task(conn, task_id).status == "blocked"
        events = kb.list_events(conn, task_id)
        assert len([event for event in events if event.kind == "provider_safety_refusal"]) == 1
        assert not [event for event in events if event.kind == "provider_terminal"]

        kb.set_model_override(conn, task_id, "other-model", "other-provider")
        assert kb.get_task(conn, task_id).status == "blocked"


def test_provider_exit_payload_is_allowlisted_redacted_and_role_safe(kanban_home):
    secret = "sk-test-secret-value"
    with kb.connect() as conn:
        task_id, run_id, pid = _start_run(conn, assignee="security-reviewer")
        with pytest.raises(TypeError):
            kb.record_provider_exit_disposition(
                conn,
                task_id,
                run_id=run_id,
                disposition="terminal",
                classification="auth_permanent",
                status_code=403,
                provider="vendor",
                model="private-model",
                session_id="safe-session",
                prompt=f"do not persist {secret}",
            )
        kb.record_provider_exit_disposition(
            conn,
            task_id,
            run_id=run_id,
            disposition="terminal",
            classification="auth_permanent",
            status_code=403,
            provider=f"vendor?api_key={secret}",
            model=f"private-model token={secret}",
            session_id="safe-session",
        )
        kb._record_worker_exit(pid, _exited_status(0))
        original_alive = kb._pid_alive
        kb._pid_alive = lambda _pid: False
        try:
            kb.detect_crashed_workers(conn)
        finally:
            kb._pid_alive = original_alive

        task = kb.get_task(conn, task_id)
        assert task.assignee == "security-reviewer"
        run = kb.list_runs(conn, task_id)[0]
        assert run.profile == "security-reviewer"
        serialized = json.dumps(
            {
                "events": [event.payload for event in kb.list_events(conn, task_id)],
                "metadata": run.metadata,
                "error": run.error,
            },
            sort_keys=True,
        )
        assert secret not in serialized
        assert "do not persist" not in serialized


def test_terminal_exit_code_is_only_a_hint_without_run_metadata(kanban_home):
    with kb.connect() as conn:
        task_id, _run_id, pid = _start_run(conn)
        kb._record_worker_exit(
            pid, _exited_status(kb.KANBAN_PROVIDER_TERMINAL_EXIT_CODE)
        )
        original_alive = kb._pid_alive
        kb._pid_alive = lambda _pid: False
        try:
            kb.detect_crashed_workers(conn)
        finally:
            kb._pid_alive = original_alive

        events = kb.list_events(conn, task_id)
        assert not [event for event in events if event.kind == "provider_terminal"]
        assert [event for event in events if event.kind == "crashed"]
