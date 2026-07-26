from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli.cost import ledger, turns_schema
from hermes_cli.programme import init as programme_init
from hermes_cli.programme import ingress
from hermes_cli.routing import bootstrap, facade, schema as routing_schema
from hermes_cli.smoke.cleanup import CleanupRefused, cleanup_smoke_rows
from hermes_cli.smoke.mocks import NoOpTelegramBucket
from hermes_cli.smoke.roundtrip import cli_command, command, run_smoke_turn
from hermes_cli.verdict import schema as verdict_schema


@pytest.fixture(scope="module")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cs13-template") / "kanban.db"
    kanban_db.init_db(path)
    programme_init.migrate(path)
    ingress.migrate(path)
    bootstrap.bootstrap_if_needed(path)
    routing_schema.ensure_migrated(path)
    verdict_schema.ensure_migrated(path)
    ledger.ensure_migrated(path)
    turns_schema.ensure_migrated(path)
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    path = tmp_path / "kanban.db"
    shutil.copy2(template_db, path)
    return path


def _state(db_path: Path, state: str) -> None:
    conn = programme_init.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE programme_state
               SET state = ?, reason = 'test', changed_by = 'test',
                   changed_at = '2026-07-26T00:00:00Z'
             WHERE id = 1
            """,
            (state,),
        )
    finally:
        conn.close()


def _run(
    db_path: Path,
    scenario: str = "success",
    lane: str = "default",
    *,
    commit: bool = True,
    **kwargs,
):
    return run_smoke_turn(
        scenario=scenario,
        lane=lane,
        db_path=db_path,
        commit=commit,
        **kwargs,
    )


def _args(
    db_path: Path,
    *,
    scenario: str = "success",
    lane: str = "default",
    commit: bool = False,
    json_output: bool = False,
    cleanup: bool = False,
    force: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        scenario=scenario,
        lane=lane,
        commit=commit,
        dry_run=not commit,
        json=json_output,
        cleanup=cleanup,
        force=force,
        db_path=str(db_path),
    )


def _rows(db_path: Path, sql: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _stage(result, name: str):
    return next(stage for stage in result.stages if stage.name == name)


def test_run_smoke_turn_success_returns_PASS(db_path: Path) -> None:
    assert _run(db_path).overall == "PASS"


def test_run_smoke_turn_writes_9_stages(db_path: Path) -> None:
    assert len(_run(db_path).stages) == 9


def test_run_smoke_turn_records_elapsed_ms_per_stage(db_path: Path) -> None:
    assert all(stage.elapsed_ms >= 0 for stage in _run(db_path).stages)


def test_run_smoke_turn_never_raises_captures_errors(tmp_path: Path) -> None:
    result = _run(tmp_path / "missing.db", commit=False)
    assert result.overall == "FAIL"
    assert result.errors[0].startswith("unexpected:")


def test_run_smoke_turn_uses_temp_db_when_commit_false(db_path: Path) -> None:
    before = db_path.read_bytes()
    result = _run(db_path, commit=False)
    assert Path(result.working_db_path) != db_path
    assert Path(result.working_db_path).parent == Path("/tmp")
    assert Path(result.working_db_path).name.startswith("hermes-smoke-")
    assert db_path.read_bytes() == before
    Path(result.working_db_path).unlink()


def test_scenario_success_all_stages_pass(db_path: Path) -> None:
    result = _run(db_path)
    assert result.overall == "PASS"
    assert _stage(result, "llm_call").outcome == "success"
    assert _stage(result, "route_context_flush").outcome == "success"


def test_scenario_fallback_success_records_failure_history(
    db_path: Path,
) -> None:
    result = _run(db_path, "fallback_success", "dayroute")
    stage = _stage(result, "llm_call")
    assert result.overall == "PASS"
    assert stage.details["failure_history"][0]["failure_class"] == "timeout"
    row = _rows(
        db_path,
        "SELECT chosen_provider, failure_history_json "
        "FROM routing_decisions WHERE profile='smoke_test'",
    )[0]
    assert row["chosen_provider"] == "openrouter"
    assert json.loads(row["failure_history_json"])


def test_scenario_cascade_exhausted_writes_sentinel_provider_model(
    db_path: Path,
) -> None:
    result = _run(db_path, "cascade_exhausted", "tihna")
    row = _rows(
        db_path,
        "SELECT chosen_provider, chosen_model FROM routing_decisions "
        "WHERE profile='smoke_test'",
    )[0]
    assert result.overall == "FAIL"
    assert tuple(row) == ("__all_failed__", "__none__")


def test_scenario_cap_hit_halts_at_stage_4_with_PerTaskCapExceeded(
    db_path: Path,
) -> None:
    result = _run(db_path, "cap_hit")
    assert len(result.stages) == 4
    assert result.stages[-1].outcome == "PerTaskCapExceeded"


def test_scenario_kill_switch_halts_at_stage_4_with_KillSwitchTripped(
    db_path: Path,
) -> None:
    result = _run(db_path, "kill_switch")
    assert len(result.stages) == 4
    assert result.stages[-1].outcome == "KillSwitchTripped"


def test_scenario_gate_paused_admission_blocks_next_admit_but_current_finishes(
    db_path: Path,
) -> None:
    result = _run(db_path, "gate_paused")
    call = _stage(result, "llm_call")
    assert result.overall == "PASS"
    assert len(result.stages) == 9
    assert call.details["next_admit_blocked"] is True
    assert call.details["current_turn_continued"] is True


def test_programme_PAUSED_returns_BLOCKED_at_stage_1(db_path: Path) -> None:
    _state(db_path, "PAUSED")
    result = _run(db_path)
    assert result.overall == "BLOCKED"
    assert len(result.stages) == 1


def test_programme_RUNNING_proceeds_to_stage_9(db_path: Path) -> None:
    _state(db_path, "RUNNING")
    result = _run(db_path)
    assert result.overall == "PASS"
    assert result.stages[-1].name == "route_context_flush"


def test_commit_never_modifies_non_smoke_rows(db_path: Path) -> None:
    before = {
        "tasks": len(_rows(db_path, "SELECT * FROM tasks")),
        "doctrine": len(_rows(db_path, "SELECT * FROM routing_doctrine")),
    }
    _run(db_path, commit=True)
    assert len(
        _rows(
            db_path,
            "SELECT * FROM tasks WHERE created_by != 'smoke_test' "
            "OR created_by IS NULL",
        )
    ) == before["tasks"]
    assert len(_rows(db_path, "SELECT * FROM routing_doctrine")) == before[
        "doctrine"
    ]


def test_all_cost_ledger_writes_carry_profile_smoke_test(
    db_path: Path,
) -> None:
    _run(db_path)
    rows = _rows(db_path, "SELECT profile, route FROM cost_ledger")
    assert rows and all(tuple(row) == ("smoke_test", "smoke_test") for row in rows)


def test_all_leaf_verdict_writes_carry_route_smoke_test(
    db_path: Path,
) -> None:
    _run(db_path)
    rows = _rows(db_path, "SELECT profile, route FROM leaf_verdicts")
    assert rows and all(tuple(row) == ("smoke_test", "smoke_test") for row in rows)


def test_all_dispatch_envelope_writes_carry_route_smoke_test(
    db_path: Path,
) -> None:
    _run(db_path)
    rows = _rows(db_path, "SELECT profile, route FROM dispatch_envelopes")
    assert rows and all(tuple(row) == ("smoke_test", "smoke_test") for row in rows)


def test_cleanup_deletes_only_smoke_test_rows(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(id,title,status,created_at,workspace_kind) "
            "VALUES('real-task','real','queued',1,'scratch')"
        )
        conn.commit()
    finally:
        conn.close()
    _run(db_path)
    _state(db_path, "PAUSED")
    cleanup_smoke_rows(db_path)
    assert len(_rows(db_path, "SELECT * FROM tasks WHERE id='real-task'")) == 1
    assert not _rows(db_path, "SELECT * FROM tasks WHERE id LIKE 'smoke-t-%'")


def test_cli_smoke_turn_default_is_dry_run(db_path: Path) -> None:
    assert command(_args(db_path)) == 0
    assert not _rows(
        db_path,
        "SELECT * FROM routing_decisions WHERE profile='smoke_test'",
    )


def test_cli_smoke_turn_exit_code_0_on_PASS(db_path: Path) -> None:
    assert command(_args(db_path, commit=True)) == 0


def test_cli_smoke_turn_exit_code_1_on_FAIL(db_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        cli_command(
            _args(db_path, scenario="cascade_exhausted", commit=True)
        )
    assert caught.value.code == 1


def test_cli_smoke_turn_exit_code_2_on_BLOCKED(db_path: Path) -> None:
    _state(db_path, "PAUSED")
    with pytest.raises(SystemExit) as caught:
        cli_command(_args(db_path, commit=True))
    assert caught.value.code == 2


def test_cli_smoke_turn_json_output_parses(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert command(_args(db_path, json_output=True)) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["overall"] == "PASS"
    assert len(parsed["stages"]) == 9


def test_cli_cleanup_refuses_when_programme_running_without_force(
    db_path: Path,
) -> None:
    _state(db_path, "RUNNING")
    with pytest.raises(CleanupRefused):
        cleanup_smoke_rows(db_path)


def test_cli_cleanup_reports_row_counts_per_table(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(db_path)
    _state(db_path, "PAUSED")
    assert command(_args(db_path, cleanup=True)) == 0
    output = capsys.readouterr().out
    for table in (
        "leaf_verdicts",
        "dispatch_envelopes",
        "cost_ledger",
        "subscription_turns_ledger",
        "routing_decisions",
        "tasks",
    ):
        assert f"{table}: deleted " in output


def test_cli_cleanup_idempotent_second_run_reports_zero(
    db_path: Path,
) -> None:
    _run(db_path)
    _state(db_path, "PAUSED")
    cleanup_smoke_rows(db_path)
    assert set(cleanup_smoke_rows(db_path).values()) == {0}


def test_telegram_bucket_writes_captured_in_stage_details(
    db_path: Path,
) -> None:
    bucket = NoOpTelegramBucket()
    result = _run(db_path, telegram_bucket=bucket)
    assert bucket.sends
    assert _stage(result, "route_context_flush").details["telegram_bucket"]


def test_no_real_telegram_send_during_smoke(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden(_message: str) -> None:
        raise AssertionError("real Telegram sender was called")

    monkeypatch.setattr(
        facade.telegram_alert,
        "send_bridge_alert",
        _forbidden,
    )
    assert _run(db_path).overall == "PASS"


def test_full_smoke_success_writes_expected_9_rows_across_5_tables(
    db_path: Path,
) -> None:
    result = _run(db_path)
    assert len(result.stages) == 9
    for table in (
        "routing_decisions",
        "leaf_verdicts",
        "cost_ledger",
        "dispatch_envelopes",
        "subscription_turns_ledger",
    ):
        assert _rows(db_path, f"SELECT * FROM {table}")


def test_full_smoke_fallback_success_updates_chosen_provider_and_failure_history(
    db_path: Path,
) -> None:
    result = _run(db_path, "fallback_success", "dayroute")
    row = _rows(
        db_path,
        "SELECT chosen_provider, chosen_model, failure_history_json, "
        "forced_legacy FROM routing_decisions",
    )[0]
    assert result.overall == "PASS"
    assert row["chosen_provider"] == "openrouter"
    assert row["chosen_model"] == "anthropic/claude-4.5-sonnet"
    assert json.loads(row["failure_history_json"])
    assert row["forced_legacy"] == 0
