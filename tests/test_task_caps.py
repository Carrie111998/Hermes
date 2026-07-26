"""CS-10a per-task cost cap, kill switch, CLI, and funnel acceptance tests."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from agent import conversation_loop
from hermes_cli import kanban_db as kb
from hermes_cli.cost import cli as cost_cli
from hermes_cli.cost import gate_integration, ledger, task_cap_schema
from hermes_cli.cost.kill_switch import (
    KillSwitchTripped,
    PerTaskCapExceeded,
    is_task_killed,
    kill_task,
    unkill_task,
)
from hermes_cli.cost.task_caps_config import (
    DEFAULT_TASK_CAPS,
    default_task_cap_for_lane,
)
from hermes_cli.programme import gate as programme_gate
from hermes_cli.programme import ingress
from hermes_cli.programme import init as programme_init
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.subcommands import kill as kill_cli
from hermes_cli.verdict import schema as verdict_schema
from run_agent import AIAgent


@pytest.fixture
def task_cap_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(task_cap_schema, "DB_PATH", db_path)
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(programme_gate, "HALT_SIGNAL_PATH", tmp_path / "halt")
    monkeypatch.setattr(side_effects_schema, "DB_PATH", db_path)
    monkeypatch.setattr(verdict_schema, "DB_PATH", db_path)
    ledger._MIGRATED_PATHS.clear()
    task_cap_schema._MIGRATED_PATHS.clear()
    side_effects_schema._MIGRATED_PATHS.clear()
    verdict_schema._MIGRATED_PATHS.clear()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path)
    conn.close()
    programme_init.migrate(db_path)
    ledger.migrate(db_path)
    task_cap_schema.migrate(db_path)
    side_effects_schema.migrate(db_path)
    verdict_schema.migrate(db_path)
    yield db_path
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _create_task(
    db_path: Path,
    *,
    assignee: str = "platform",
    status: str = "ready",
) -> str:
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn,
            title="CS-10a test task",
            assignee=assignee,
            initial_status="running",
        )
        if status != "ready":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status=? WHERE id=?",
                    (status, task_id),
                )
        return task_id
    finally:
        conn.close()


def _claim(
    db_path: Path,
    task_id: str,
    *,
    lane: str = "platform",
    cap: float | None = None,
):
    conn = kb.connect(db_path)
    try:
        return kb.claim_task(
            conn,
            task_id,
            lane=lane,
            task_cap_aud=cap,
            claimer="cs10a-test",
        )
    finally:
        conn.close()


def _record(
    db_path: Path,
    task_id: str,
    amount: float,
    *,
    enforce_task_cap: bool = True,
):
    return ledger.record_call(
        task_id=task_id,
        lane="platform",
        vendor="anthropic",
        model="anthropic/test",
        amount_aud=amount,
        profile="test",
        route="test",
        enforce_task_cap=enforce_task_cap,
        db_path=db_path,
    )


def _task_row(db_path: Path, task_id: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()


def _cost_count(db_path: Path, task_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM cost_ledger WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )


# Migration + config (5)


def test_migration_adds_task_cap_aud_column_idempotently(
    task_cap_env,
):
    task_cap_schema.migrate(task_cap_env)
    task_cap_schema.migrate(task_cap_env)
    with sqlite3.connect(task_cap_env) as conn:
        names = [
            row[1] for row in conn.execute("PRAGMA table_info(tasks)")
        ]
    assert names.count("task_cap_aud") == 1


def test_migration_adds_failure_reason_column_if_absent(
    tmp_path,
):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    task_cap_schema.migrate(path)
    with sqlite3.connect(path) as conn:
        names = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)")
        }
    assert "failure_reason" in names


def test_default_caps_config_has_all_lanes():
    assert {
        name
        for name in (
            "green_captains",
            "dayroute",
            "tihna",
            "ops",
            "platform",
            "reserve",
            "escalation",
            "default",
        )
        if getattr(DEFAULT_TASK_CAPS, name) > 0
    } == {
        "green_captains",
        "dayroute",
        "tihna",
        "ops",
        "platform",
        "reserve",
        "escalation",
        "default",
    }


def test_default_task_cap_for_lane_unknown_returns_default():
    assert default_task_cap_for_lane("unknown") == DEFAULT_TASK_CAPS.default


def test_task_kill_switch_table_and_indexes_present(task_cap_env):
    with sqlite3.connect(task_cap_env) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='task_kill_switch'"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='task_kill_switch'"
            )
        }
    assert table == ("task_kill_switch",)
    assert {"idx_kill_switch_ts", "idx_kill_switch_reason"} <= indexes


# Claim (5)


def test_claim_task_sets_task_cap_from_default_when_none_provided(
    task_cap_env,
):
    task_id = _create_task(task_cap_env)
    claimed = _claim(task_cap_env, task_id)
    assert claimed is not None
    assert _task_row(task_cap_env, task_id)["task_cap_aud"] == pytest.approx(
        DEFAULT_TASK_CAPS.platform
    )


def test_claim_task_uses_explicit_task_cap_when_provided(task_cap_env):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.37)
    assert _task_row(task_cap_env, task_id)["task_cap_aud"] == pytest.approx(
        0.37
    )


def test_claim_task_rejects_zero_cap(task_cap_env):
    task_id = _create_task(task_cap_env)
    with pytest.raises(ValueError, match="greater than zero"):
        _claim(task_cap_env, task_id, cap=0)


def test_claim_task_deletes_stale_kill_switch_row_on_reclaim(
    task_cap_env,
):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="test",
        reason="test",
        db_path=task_cap_env,
    )
    assert _claim(task_cap_env, task_id) is not None
    assert is_task_killed(task_id, db_path=task_cap_env) is None


def test_claim_review_task_also_sets_task_cap(task_cap_env):
    task_id = _create_task(task_cap_env, status="review")
    conn = kb.connect(task_cap_env)
    try:
        claimed = kb.claim_review_task(
            conn,
            task_id,
            lane="dayroute",
            task_cap_aud=None,
            claimer="review-test",
        )
    finally:
        conn.close()
    assert claimed is not None
    assert _task_row(task_cap_env, task_id)["task_cap_aud"] == pytest.approx(
        DEFAULT_TASK_CAPS.dayroute
    )


# Cost writer (7)


def test_record_call_below_cap_writes_normally(task_cap_env):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    entry = _record(task_cap_env, task_id, 0.05)
    assert entry.aud_amount == pytest.approx(0.05)
    assert _cost_count(task_cap_env, task_id) == 1


def test_record_call_at_cap_writes_last_row_but_no_kill(task_cap_env):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(task_cap_env, task_id, 0.10)
    assert _cost_count(task_cap_env, task_id) == 1
    assert is_task_killed(task_id, db_path=task_cap_env) is None


def test_record_call_default_task_cap_is_advisory(
    task_cap_env,
    monkeypatch,
):
    messages = []
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda message: messages.append(message),
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    entry = ledger.record_call(
        task_id=task_id,
        lane="platform",
        vendor="anthropic",
        model="anthropic/test",
        amount_aud=0.11,
        profile="test",
        route="test",
        db_path=task_cap_env,
    )
    assert entry.aud_amount == pytest.approx(0.11)
    assert _cost_count(task_cap_env, task_id) == 1
    assert is_task_killed(task_id, db_path=task_cap_env) is None
    assert _task_row(task_cap_env, task_id)["status"] == "running"
    assert len(messages) == 1
    assert "advisory_only: yes" in messages[0]


def test_record_call_legacy_enforcement_kwarg_is_advisory(
    task_cap_env,
    monkeypatch,
):
    messages = []
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        messages.append,
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    entry = _record(task_cap_env, task_id, 0.11)
    assert entry.aud_amount == pytest.approx(0.11)
    assert _cost_count(task_cap_env, task_id) == 1
    assert is_task_killed(task_id, db_path=task_cap_env) is None
    assert _task_row(task_cap_env, task_id)["status"] == "running"
    assert len(messages) == 1
    assert "advisory_only: yes" in messages[0]


def test_record_call_above_cap_always_inserts_cost_row(
    task_cap_env,
    monkeypatch,
):
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(task_cap_env, task_id, 0.11)
    assert _cost_count(task_cap_env, task_id) == 1


def test_record_call_above_cap_does_not_mark_task_failed(
    task_cap_env,
    monkeypatch,
):
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(task_cap_env, task_id, 0.11)
    row = _task_row(task_cap_env, task_id)
    assert row["status"] == "running"
    assert row["failure_reason"] is None


def test_record_call_legacy_enforcement_does_not_touch_task_state(
    task_cap_env,
):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    with sqlite3.connect(task_cap_env) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_cs10a_task_update
            BEFORE UPDATE OF failure_reason ON tasks
            BEGIN
                SELECT RAISE(ABORT, 'forced failure update rollback');
            END
            """
        )
    _record(task_cap_env, task_id, 0.11)
    assert _cost_count(task_cap_env, task_id) == 1
    assert is_task_killed(task_id, db_path=task_cap_env) is None
    assert _task_row(task_cap_env, task_id)["status"] == "running"


def test_record_call_enforcement_boolean_values_are_equally_advisory(
    task_cap_env,
):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(
        task_cap_env,
        task_id,
        0.11,
        enforce_task_cap=False,
    )
    _record(
        task_cap_env,
        task_id,
        0.11,
        enforce_task_cap=True,
    )
    assert _cost_count(task_cap_env, task_id) == 2
    assert is_task_killed(task_id, db_path=task_cap_env) is None


# Kill switch (6)


def test_record_call_killed_task_raises_KillSwitchTripped(task_cap_env):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=1.0)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    with pytest.raises(KillSwitchTripped, match="operator"):
        _record(task_cap_env, task_id, 0.01)


def test_record_call_killed_task_does_not_write_cost(task_cap_env):
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=1.0)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    with pytest.raises(KillSwitchTripped):
        _record(task_cap_env, task_id, 0.01)
    assert _cost_count(task_cap_env, task_id) == 0


def test_is_task_killed_returns_row_or_none(task_cap_env):
    task_id = _create_task(task_cap_env)
    assert is_task_killed(task_id, db_path=task_cap_env) is None
    kill_task(
        task_id=task_id,
        killed_by="test",
        reason="test",
        db_path=task_cap_env,
    )
    assert is_task_killed(task_id, db_path=task_cap_env)["killed_by"] == "test"


def test_kill_task_idempotent_on_double_kill(task_cap_env):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="first",
        reason="test",
        notes="first",
        db_path=task_cap_env,
    )
    kill_task(
        task_id=task_id,
        killed_by="second",
        reason="runaway",
        notes="second",
        db_path=task_cap_env,
    )
    row = is_task_killed(task_id, db_path=task_cap_env)
    assert row["killed_by"] == "first"
    assert row["notes"] == "first"


def test_unkill_task_removes_row(task_cap_env):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="test",
        reason="test",
        db_path=task_cap_env,
    )
    unkill_task(task_id=task_id, db_path=task_cap_env)
    assert is_task_killed(task_id, db_path=task_cap_env) is None


def test_unkill_task_does_not_change_tasks_status(task_cap_env):
    task_id = _create_task(task_cap_env)
    with sqlite3.connect(task_cap_env) as conn:
        conn.execute(
            "UPDATE tasks SET status='failed' WHERE id=?",
            (task_id,),
        )
    kill_task(
        task_id=task_id,
        killed_by="test",
        reason="test",
        db_path=task_cap_env,
    )
    unkill_task(task_id=task_id, db_path=task_cap_env)
    assert _task_row(task_cap_env, task_id)["status"] == "failed"


# Telegram (3)


def test_per_task_threshold_emits_advisory_via_side_effects_bucket(
    task_cap_env,
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(task_cap_env, task_id, 0.11)
    assert len(sent) == 1
    assert "TASK SPEND ADVISORY" in sent[0]
    assert "advisory_only: yes" in sent[0]
    with sqlite3.connect(task_cap_env) as conn:
        key = conn.execute(
            "SELECT idempotency_key FROM side_effects"
        ).fetchone()[0]
    assert key.startswith(f"task_cost_advisory:{task_id}:")


def test_operator_kill_does_not_alert(task_cap_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    assert sent == []


def test_repeated_threshold_hits_dedupe_per_task_alert(
    task_cap_env,
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    task_id = _create_task(task_cap_env)
    _claim(task_cap_env, task_id, cap=0.10)
    _record(task_cap_env, task_id, 0.11)
    _record(task_cap_env, task_id, 0.11)
    assert len(sent) == 1
    assert _cost_count(task_cap_env, task_id) == 2
    assert is_task_killed(task_id, db_path=task_cap_env) is None
    with sqlite3.connect(task_cap_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM side_effects"
        ).fetchone()[0] == 1


# CLI (4)


def test_hermes_kill_task_without_confirm_is_noop(
    task_cap_env,
    capsys,
):
    task_id = _create_task(task_cap_env)
    args = argparse.Namespace(task_id=task_id, reason="pause it", confirm=False)
    assert kill_cli._cmd_task(args) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert is_task_killed(task_id, db_path=task_cap_env) is None


def test_hermes_kill_task_with_confirm_writes_row(task_cap_env):
    task_id = _create_task(task_cap_env)
    args = argparse.Namespace(task_id=task_id, reason="pause it", confirm=True)
    assert kill_cli._cmd_task(args) == 0
    assert is_task_killed(task_id, db_path=task_cap_env)["reason"] == "operator"


def test_hermes_kill_list_prints_expected_columns(
    task_cap_env,
    capsys,
):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    args = argparse.Namespace(limit=50, lane=None, profile=None)
    assert kill_cli._cmd_list(args) == 0
    output = capsys.readouterr().out
    assert "task_id" in output
    assert "killed_ts" in output
    assert task_id in output


def test_hermes_kill_unkill_removes_row(task_cap_env):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    args = argparse.Namespace(task_id=task_id, confirm=True)
    assert kill_cli._cmd_unkill(args) == 0
    assert is_task_killed(task_id, db_path=task_cap_env) is None


# Cost today (2)


def test_hermes_cost_today_includes_killed_section_when_present(
    task_cap_env,
    capsys,
):
    task_id = _create_task(task_cap_env)
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        db_path=task_cap_env,
    )
    assert cost_cli._cmd_today(argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "Per-task killed today: 1" in output
    assert task_id in output


def test_hermes_cost_today_omits_killed_section_when_empty(
    task_cap_env,
    capsys,
):
    assert cost_cli._cmd_today(argparse.Namespace()) == 0
    assert "Per-task killed today:" not in capsys.readouterr().out


# Public conversation funnel (3)


class _DummyAgent:
    run_conversation = AIAgent.run_conversation

    def __init__(self):
        self.platform = "cli"
        self.session_id = "session-cs10a"
        self._session_db = None
        self.lane = "platform"
        self.model = "test/model"

    def _conversation_root_id(self):
        return self.session_id


def _run_fenced_conversation(monkeypatch, exception):
    import hermes_cli.verdict as verdict

    recorded = []
    monkeypatch.setattr(
        ingress,
        "resolve_turn_attribution",
        lambda **_kwargs: ("test-route", "test-profile"),
    )
    monkeypatch.setattr(ingress, "admit_new_turn", lambda **_kwargs: None)
    monkeypatch.setattr(
        conversation_loop,
        "prepare_session_rotation",
        lambda _agent, **kwargs: kwargs["conversation_history"],
    )

    def raise_fence(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(conversation_loop, "run_conversation", raise_fence)
    monkeypatch.setattr(
        verdict,
        "attempts_at_current_rung",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        verdict,
        "record_verdict",
        lambda value, **_kwargs: recorded.append(value) or 1,
    )
    with pytest.raises(type(exception)) as raised:
        _DummyAgent().run_conversation(
            "test",
            task_id=exception.task_id,
            route="test-route",
            profile="test-profile",
        )
    return recorded, raised.value


def test_conversation_loop_terminates_on_KillSwitchTripped_with_leaf_verdict_killed_by_operator(
    monkeypatch,
):
    exception = KillSwitchTripped(task_id="operator-task", reason="operator")
    recorded, raised = _run_fenced_conversation(monkeypatch, exception)
    assert raised is exception
    assert len(recorded) == 1
    assert recorded[0].outcome == "killed_by_operator"
    assert recorded[0].failure_class == "operator"


def test_conversation_wrapper_does_not_record_legacy_cap_exception_as_termination(
    monkeypatch,
):
    exception = PerTaskCapExceeded(
        task_id="cap-task",
        current_total=0.05,
        projected_total=0.11,
        cap=0.10,
    )
    recorded, raised = _run_fenced_conversation(monkeypatch, exception)
    assert raised is exception
    assert recorded == []


def test_conversation_wrapper_does_not_swallow_operator_kill(
    monkeypatch,
):
    exception = KillSwitchTripped(
        task_id="operator-task",
        reason="operator",
    )
    _recorded, raised = _run_fenced_conversation(monkeypatch, exception)
    assert raised is exception
