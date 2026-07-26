"""CS-01 programme-control acceptance tests."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.programme import cli as programme_cli
from hermes_cli.programme import gate
from hermes_cli.programme import init as programme_init
from hermes_cli.programme import log as programme_log


@pytest.fixture
def programme_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    halt_path = tmp_path / "halt"
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(gate, "HALT_SIGNAL_PATH", halt_path)
    programme_init.migrate()
    return db_path, halt_path


def test_migration_creates_default_running_state(programme_env):
    assert gate.get_state().state == "RUNNING"


def test_admit_task_when_running(programme_env):
    assert gate.admit_task("t_test") == (True, "admitted")


def test_admit_task_when_paused(programme_env):
    gate.set_state("PAUSED", "operator hold", "adrian")
    assert gate.admit_task("t_test") == (
        False,
        "programme paused: operator hold",
    )


def test_admit_task_when_draining(programme_env):
    gate.set_state("DRAINING", "finish current work", "adrian")
    assert gate.admit_task("t_test") == (
        False,
        "programme draining: finish current work",
    )


def test_admit_task_when_halted(programme_env):
    gate.set_state("HALTED", "kill switch", "adrian")
    assert gate.admit_task("t_test") == (
        False,
        "programme halted: kill switch",
    )


def test_drain_transitions_to_paused_when_zero_inflight(
    programme_env, monkeypatch
):
    monkeypatch.setattr(gate, "inflight_count", lambda: 0)
    gate.set_state("DRAINING", "finish current work", "adrian")
    assert gate.check_drain().state == "PAUSED"


def test_drain_stays_when_inflight_positive(programme_env, monkeypatch):
    monkeypatch.setattr(gate, "inflight_count", lambda: 3)
    gate.set_state("DRAINING", "finish current work", "adrian")
    assert gate.check_drain().state == "DRAINING"


def test_halt_creates_signal_file(programme_env):
    _, halt_path = programme_env
    gate.set_state("HALTED", "kill switch", "adrian")
    assert halt_path.is_file()


def test_resume_clears_signal_file(programme_env):
    _, halt_path = programme_env
    gate.set_state("HALTED", "kill switch", "adrian")
    gate.set_state("RUNNING", "ok", "adrian")
    assert not halt_path.exists()


def test_pause_does_not_touch_signal_file(programme_env):
    _, halt_path = programme_env
    halt_path.write_text("existing\n", encoding="utf-8")
    gate.set_state("PAUSED", "operator hold", "adrian")
    assert halt_path.read_text(encoding="utf-8") == "existing\n"
    gate.set_state("RUNNING", "ok", "adrian")
    assert not halt_path.exists()


def test_state_log_append_only(programme_env):
    db_path, _ = programme_env
    previous_rows: list[tuple] = []
    changes = [
        ("PAUSED", "one"),
        ("RUNNING", "two"),
        ("DRAINING", "three"),
        ("HALTED", "four"),
    ]
    for state, reason in changes:
        gate.set_state(state, reason, "adrian")
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, state, reason, changed_by, changed_at,
                       task_count_at_change
                  FROM programme_state_log
                 ORDER BY id
                """
            ).fetchall()
        assert rows[: len(previous_rows)] == previous_rows
        previous_rows = rows

    assert len(previous_rows) == 4
    assert len({row[0] for row in previous_rows}) == 4


def test_cli_status_prints_state(programme_env, capsys):
    state = gate.get_state()
    assert programme_cli.main(["status"]) == 0
    stdout = capsys.readouterr().out
    assert state.state in stdout
    assert state.changed_at in stdout


def test_invalid_state_rejected(programme_env):
    with pytest.raises(ValueError):
        gate.set_state("BOGUS", "invalid", "adrian")


def test_atomic_transaction_on_failure(programme_env, monkeypatch):
    def fail_log(*_args, **_kwargs):
        raise RuntimeError("forced log failure")

    monkeypatch.setattr(programme_log, "append_state_log", fail_log)
    with pytest.raises(RuntimeError, match="forced log failure"):
        gate.set_state("PAUSED", "operator hold", "adrian")
    assert gate.get_state().state == "RUNNING"
