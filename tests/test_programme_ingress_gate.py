"""CS-01c universal conversation-ingress gate acceptance tests."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agent import conversation_loop
from hermes_cli.cost import ledger
from hermes_cli.cost.errors import ProgrammeGatePausedAtIngress
from hermes_cli.programme import cli as programme_cli
from hermes_cli.programme import gate
from hermes_cli.programme import ingress
from hermes_cli.programme import init as programme_init
from hermes_cli.verdict import api as verdict_api
from hermes_cli.verdict import schema as verdict_schema
from hermes_cli.verdict.types import DispatchEnvelope, LeafVerdict
from run_agent import AIAgent


@pytest.fixture
def ingress_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(verdict_schema, "DB_PATH", db_path)
    ledger._MIGRATED_PATHS.clear()
    verdict_schema._MIGRATED_PATHS.clear()
    ingress._MIGRATED_PATHS.clear()
    programme_init.migrate(db_path)
    ledger.migrate(db_path)
    verdict_schema.migrate(db_path)
    ingress.migrate(db_path)
    return db_path


def _set_programme_state(
    db_path: Path,
    state: str,
    reason: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE programme_state
               SET state = ?, reason = ?, changed_by = 'test',
                   changed_at = '2026-07-26T00:00:00Z',
                   task_count_at_change = 0
             WHERE id = 1
            """,
            (state, reason),
        )


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _envelope(task_id: str = "task-ingress") -> DispatchEnvelope:
    return DispatchEnvelope(
        task_id=task_id,
        attempt_number=1,
        rung_id="r0_baseline",
        model_slug="test/model",
        mode="single",
        strategy_payload={"model": "test/model", "mode": "single"},
        issued_by="test",
    )


def _verdict(
    task_id: str = "task-ingress",
    dispatch_id: int | None = None,
) -> LeafVerdict:
    return LeafVerdict(
        task_id=task_id,
        attempt_number=1,
        rung_id="r0_baseline",
        dispatch_envelope_id=dispatch_id,
        model_used="test/model",
        outcome="success",
        confidence=1.0,
        strategy_hash="strategy-ingress",
    )


def _write_expected_turn_rows(
    db_path: Path,
    *,
    profile: str | None,
    route: str | None,
    task_id: str = "task-ingress",
) -> None:
    dispatch_id = verdict_api.record_dispatch(
        _envelope(task_id),
        db_path,
        profile=profile,
        route=route,
    )
    ledger.record_call(
        task_id=task_id,
        lane="platform",
        vendor="openrouter",
        model_slug="openrouter/test-model",
        usd_amount=0.01,
        profile=profile,
        route=route,
        db_path=db_path,
    )
    verdict_api.record_verdict(
        _verdict(task_id, dispatch_id),
        db_path,
        profile=profile,
        route=route,
    )


class _DummyAgent:
    run_conversation = AIAgent.run_conversation

    def __init__(self, session_id: str = "session-ingress"):
        self.platform = "cli"
        self.session_id = session_id
        self._session_db = None
        self.lane = "platform"

    def _conversation_root_id(self):
        return self.session_id


def _call_public_funnel(
    monkeypatch,
    db_path: Path,
    *,
    write_rows: bool,
    entered: threading.Event | None = None,
    release: threading.Event | None = None,
):
    def fake_inner(_agent, *_args, **kwargs):
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(timeout=5)
        if write_rows:
            _write_expected_turn_rows(
                db_path,
                profile=kwargs.get("profile"),
                route=kwargs.get("route"),
            )
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(conversation_loop, "run_conversation", fake_inner)
    return _DummyAgent().run_conversation(
        "test turn",
        route="direct_cli",
        profile="forge",
    )


def test_admit_new_turn_returns_none_when_running(ingress_env):
    assert (
        ingress.admit_new_turn(
            route="direct_cli",
            profile="forge",
            db_path=ingress_env,
        )
        is None
    )


def test_admit_new_turn_raises_when_paused(ingress_env):
    _set_programme_state(ingress_env, "PAUSED", "test pause")
    with pytest.raises(ProgrammeGatePausedAtIngress, match="test pause"):
        ingress.admit_new_turn(
            route="direct_cli",
            profile="forge",
            db_path=ingress_env,
        )


def test_admit_new_turn_raises_when_halted(ingress_env):
    _set_programme_state(ingress_env, "HALTED", "test halt")
    with pytest.raises(ProgrammeGatePausedAtIngress, match="test halt"):
        ingress.admit_new_turn(
            route="gateway",
            profile="atlas",
            db_path=ingress_env,
        )


def test_admit_new_turn_raises_when_state_row_missing(ingress_env):
    with sqlite3.connect(ingress_env) as conn:
        conn.execute("DELETE FROM programme_state WHERE id = 1")
    with pytest.raises(
        ProgrammeGatePausedAtIngress,
        match="programme state unreadable",
    ):
        ingress.admit_new_turn(
            route="direct_cli",
            db_path=ingress_env,
        )


def test_admit_new_turn_raises_when_db_read_fails(
    ingress_env,
    monkeypatch,
):
    def fail_read(*_args, **_kwargs):
        raise sqlite3.OperationalError("forced read failure")

    monkeypatch.setattr(ingress, "get_state", fail_read)
    with pytest.raises(
        ProgrammeGatePausedAtIngress,
        match="forced read failure",
    ):
        ingress.admit_new_turn(
            route="api_server",
            db_path=ingress_env,
        )


def test_rejection_log_row_written_on_paused_reject(ingress_env):
    _set_programme_state(ingress_env, "PAUSED", "paused row")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        ingress.admit_new_turn(
            route="direct_cli",
            profile="forge",
            db_path=ingress_env,
        )
    with sqlite3.connect(ingress_env) as conn:
        assert conn.execute(
            "SELECT state, reason FROM ingress_rejection_log"
        ).fetchone() == ("PAUSED", "paused row")


def test_rejection_log_row_written_on_halted_reject(ingress_env):
    _set_programme_state(ingress_env, "HALTED", "halted row")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        ingress.admit_new_turn(
            route="gateway",
            profile="atlas",
            db_path=ingress_env,
        )
    with sqlite3.connect(ingress_env) as conn:
        assert conn.execute(
            "SELECT state, reason FROM ingress_rejection_log"
        ).fetchone() == ("HALTED", "halted row")


def test_rejection_log_persists_route_and_profile_and_session(ingress_env):
    _set_programme_state(ingress_env, "PAUSED", "attribution")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        ingress.admit_new_turn(
            route="forge_direct",
            profile="forge",
            session_id="session-42",
            task_id_hint="turn-42",
            db_path=ingress_env,
        )
    with sqlite3.connect(ingress_env) as conn:
        row = conn.execute(
            """
            SELECT route, profile, session_id, task_id_hint
              FROM ingress_rejection_log
            """
        ).fetchone()
    assert row == ("forge_direct", "forge", "session-42", "turn-42")


def test_rejection_log_write_failure_still_raises_ingress_error(
    ingress_env,
    monkeypatch,
):
    _set_programme_state(ingress_env, "PAUSED", "remain closed")

    def fail_log(**_kwargs):
        raise sqlite3.OperationalError("forced log write failure")

    monkeypatch.setattr(ingress, "_write_rejection", fail_log)
    with pytest.raises(
        ProgrammeGatePausedAtIngress,
        match="remain closed",
    ):
        ingress.admit_new_turn(
            route="direct_cli",
            db_path=ingress_env,
        )


def test_no_rejection_row_written_on_admit(ingress_env):
    ingress.admit_new_turn(route="direct_cli", db_path=ingress_env)
    assert _count(ingress_env, "ingress_rejection_log") == 0


def test_record_call_accepts_profile_and_route_kwargs(ingress_env):
    row = ledger.record_call(
        task_id="cost-attribution",
        lane="platform",
        vendor="openrouter",
        model_slug="openrouter/test",
        usd_amount=0.01,
        profile="forge",
        route="forge_direct",
        db_path=ingress_env,
    )
    assert (row.profile, row.route) == ("forge", "forge_direct")


def test_record_verdict_accepts_profile_and_route_kwargs(ingress_env):
    verdict_id = verdict_api.record_verdict(
        _verdict("verdict-attribution"),
        ingress_env,
        profile="atlas",
        route="gateway",
    )
    with sqlite3.connect(ingress_env) as conn:
        row = conn.execute(
            "SELECT profile, route FROM leaf_verdicts WHERE id = ?",
            (verdict_id,),
        ).fetchone()
    assert row == ("atlas", "gateway")


def test_record_dispatch_accepts_profile_and_route_kwargs(ingress_env):
    dispatch_id = verdict_api.record_dispatch(
        _envelope("dispatch-attribution"),
        ingress_env,
        profile="shield",
        route="api_server",
    )
    with sqlite3.connect(ingress_env) as conn:
        row = conn.execute(
            "SELECT profile, route FROM dispatch_envelopes WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
    assert row == ("shield", "api_server")


def test_existing_rows_remain_null_profile_and_route(ingress_env):
    ledger.record_call(
        task_id="historical",
        lane="platform",
        vendor="openrouter",
        model_slug="openrouter/test",
        usd_amount=0.01,
        db_path=ingress_env,
    )
    verdict_api.record_dispatch(_envelope("historical"), ingress_env)
    verdict_api.record_verdict(_verdict("historical"), ingress_env)
    with sqlite3.connect(ingress_env) as conn:
        rows = [
            conn.execute(
                "SELECT profile, route FROM cost_ledger"
            ).fetchone(),
            conn.execute(
                "SELECT profile, route FROM dispatch_envelopes"
            ).fetchone(),
            conn.execute(
                "SELECT profile, route FROM leaf_verdicts"
            ).fetchone(),
        ]
    assert rows == [(None, None), (None, None), (None, None)]


def test_paused_direct_turn_writes_no_cost_row(ingress_env, monkeypatch):
    _set_programme_state(ingress_env, "PAUSED", "no direct spend")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        _call_public_funnel(
            monkeypatch,
            ingress_env,
            write_rows=True,
        )
    assert _count(ingress_env, "cost_ledger") == 0


def test_paused_direct_turn_writes_no_verdict_row(ingress_env, monkeypatch):
    _set_programme_state(ingress_env, "PAUSED", "no direct verdict")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        _call_public_funnel(
            monkeypatch,
            ingress_env,
            write_rows=True,
        )
    assert _count(ingress_env, "leaf_verdicts") == 0


def test_paused_direct_turn_writes_no_dispatch_row(ingress_env, monkeypatch):
    _set_programme_state(ingress_env, "PAUSED", "no direct dispatch")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        _call_public_funnel(
            monkeypatch,
            ingress_env,
            write_rows=True,
        )
    assert _count(ingress_env, "dispatch_envelopes") == 0


def test_paused_direct_turn_writes_rejection_log_row(
    ingress_env,
    monkeypatch,
):
    _set_programme_state(ingress_env, "PAUSED", "logged refusal")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        _call_public_funnel(
            monkeypatch,
            ingress_env,
            write_rows=True,
        )
    assert _count(ingress_env, "ingress_rejection_log") == 1


def test_running_direct_turn_writes_expected_rows(ingress_env, monkeypatch):
    result = _call_public_funnel(
        monkeypatch,
        ingress_env,
        write_rows=True,
    )
    assert result["final_response"] == "ok"
    assert _count(ingress_env, "cost_ledger") == 1
    assert _count(ingress_env, "leaf_verdicts") == 1
    assert _count(ingress_env, "dispatch_envelopes") == 1
    with sqlite3.connect(ingress_env) as conn:
        rows = [
            conn.execute(
                "SELECT profile, route FROM cost_ledger"
            ).fetchone(),
            conn.execute(
                "SELECT profile, route FROM leaf_verdicts"
            ).fetchone(),
            conn.execute(
                "SELECT profile, route FROM dispatch_envelopes"
            ).fetchone(),
        ]
    assert rows == [
        ("forge", "direct_cli"),
        ("forge", "direct_cli"),
        ("forge", "direct_cli"),
    ]


def test_kanban_claim_still_admits_when_running(ingress_env):
    assert gate.admit_task("kanban-running") == (True, "admitted")


def test_kanban_claim_still_rejects_when_paused_at_admit_task(ingress_env):
    _set_programme_state(ingress_env, "PAUSED", "kanban pause")
    assert gate.admit_task("kanban-paused") == (
        False,
        "programme paused: kanban pause",
    )


def test_kanban_claim_and_ingress_double_check_is_harmless(
    ingress_env,
    monkeypatch,
):
    assert gate.admit_task("kanban-double") == (True, "admitted")
    result = _call_public_funnel(
        monkeypatch,
        ingress_env,
        write_rows=False,
    )
    assert result["final_response"] == "ok"
    assert _count(ingress_env, "ingress_rejection_log") == 0


def test_in_flight_turn_completes_when_state_transitions_to_paused_mid_turn(
    ingress_env,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def run_turn():
        try:
            _call_public_funnel(
                monkeypatch,
                ingress_env,
                write_rows=True,
                entered=entered,
                release=release,
            )
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    worker = threading.Thread(target=run_turn)
    worker.start()
    assert entered.wait(timeout=5)
    _set_programme_state(ingress_env, "PAUSED", "mid-turn pause")
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert _count(ingress_env, "cost_ledger") == 1


def test_next_turn_after_transition_is_rejected(ingress_env, monkeypatch):
    _call_public_funnel(
        monkeypatch,
        ingress_env,
        write_rows=False,
    )
    _set_programme_state(ingress_env, "PAUSED", "next turn pause")
    with pytest.raises(ProgrammeGatePausedAtIngress, match="next turn pause"):
        _call_public_funnel(
            monkeypatch,
            ingress_env,
            write_rows=False,
        )


def test_migration_adds_profile_and_route_columns_idempotently(ingress_env):
    with sqlite3.connect(ingress_env) as conn:
        for table in (
            "cost_ledger",
            "leaf_verdicts",
            "dispatch_envelopes",
        ):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN profile")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN route")
    ledger.migrate(ingress_env)
    verdict_schema.migrate(ingress_env)
    ledger.migrate(ingress_env)
    verdict_schema.migrate(ingress_env)
    with sqlite3.connect(ingress_env) as conn:
        for table in (
            "cost_ledger",
            "leaf_verdicts",
            "dispatch_envelopes",
        ):
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            assert columns.count("profile") == 1
            assert columns.count("route") == 1


def test_migration_preserves_existing_rows_exactly(ingress_env):
    _write_expected_turn_rows(
        ingress_env,
        profile=None,
        route=None,
        task_id="preserve-me",
    )
    with sqlite3.connect(ingress_env) as conn:
        for table in (
            "cost_ledger",
            "leaf_verdicts",
            "dispatch_envelopes",
        ):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN profile")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN route")
        before = {
            table: conn.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall()
            for table in (
                "cost_ledger",
                "leaf_verdicts",
                "dispatch_envelopes",
            )
        }
    ledger.migrate(ingress_env)
    verdict_schema.migrate(ingress_env)
    with sqlite3.connect(ingress_env) as conn:
        for table, expected in before.items():
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})")
                if row[1] not in {"profile", "route"}
            ]
            actual = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"
            ).fetchall()
            assert actual == expected
            assert conn.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                 WHERE profile IS NOT NULL OR route IS NOT NULL
                """
            ).fetchone()[0] == 0


def test_ingress_rejection_log_migration_lazy_and_indexed(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "lazy" / "kanban.db"
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    ingress._MIGRATED_PATHS.clear()
    programme_init.migrate(db_path)
    assert ingress.list_recent_rejections(db_path=db_path) == []
    with sqlite3.connect(db_path) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                 WHERE type = 'index'
                   AND tbl_name = 'ingress_rejection_log'
                """
            )
        }
    assert indexes == {
        "idx_ingress_rejection_route",
        "idx_ingress_rejection_ts",
    }


def test_hermes_gate_status_prints_state(ingress_env, capsys):
    assert programme_cli.main(["gate", "status"]) == 0
    output = capsys.readouterr().out
    assert "programme state: RUNNING" in output
    assert "last N=20 rejected ingress attempts:" in output


def test_hermes_gate_status_lists_recent_rejections(
    ingress_env,
    capsys,
):
    _set_programme_state(ingress_env, "PAUSED", "cli visibility")
    with pytest.raises(ProgrammeGatePausedAtIngress):
        ingress.admit_new_turn(
            route="forge_direct",
            profile="forge",
            session_id="cli-session",
            db_path=ingress_env,
        )
    assert programme_cli.main(["gate", "status"]) == 0
    output = capsys.readouterr().out
    assert "route=forge_direct" in output
    assert "profile=forge" in output
    assert "session=cli-session" in output
    assert "reason=cli visibility" in output
