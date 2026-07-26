"""CS-02d Pro-bridge turns ledger and health-watch acceptance tests."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.cost import (
    bridge_config,
    bridge_state,
    gate_integration,
    ledger,
    turns_ledger,
    turns_schema,
)
from hermes_cli.cost.errors import SubscriptionBridgeHaltedError
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.subcommands import bridge as bridge_cli
from hermes_cli.verdict import api as verdict_api
from hermes_cli.verdict import schema as verdict_schema


@pytest.fixture
def bridge_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(turns_schema, "DB_PATH", db_path)
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(verdict_schema, "DB_PATH", db_path)
    monkeypatch.setattr(side_effects_schema, "DB_PATH", db_path)
    for module in (turns_schema, ledger, verdict_schema, side_effects_schema):
        module._MIGRATED_PATHS.discard(str(db_path.resolve()))
    turns_schema.migrate(db_path)
    return db_path


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def _turn_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM subscription_turns_ledger ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _response(
    text: str = "bridge ok",
    model: str = "gpt-5.6-sol",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp-test",
        model=model,
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text))
        ],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
    )


def _caps(soft: int, hard: int) -> bridge_config.BridgeCaps:
    return bridge_config.BridgeCaps(
        soft_turns_daily=soft,
        hard_turns_daily=hard,
        degraded_latency_ms=15_000,
        nightly_probe_hour_utc=14,
    )


# Schema (3)


def test_migration_creates_turns_ledger_and_bridge_health_and_bridge_state(
    bridge_db,
):
    assert {
        "subscription_turns_ledger",
        "bridge_health_log",
        "bridge_state",
    } <= _table_names(bridge_db)


def test_all_indexes_exist(bridge_db):
    expected = {
        "idx_turns_ledger_ts",
        "idx_turns_ledger_task",
        "idx_turns_ledger_lane_ts",
        "idx_turns_ledger_outcome",
        "idx_turns_ledger_tier",
        "idx_bridge_health_ts",
        "idx_bridge_health_source",
    }
    with sqlite3.connect(bridge_db) as conn:
        actual = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert expected <= actual


def test_lazy_migration_on_isolated_home(tmp_path, monkeypatch):
    isolated_home = tmp_path / "isolated-hermes"
    monkeypatch.setenv("HERMES_HOME", str(isolated_home))
    monkeypatch.setattr(turns_schema, "DB_PATH", turns_schema._DEFAULT_DB_PATH)
    expected_db = isolated_home / "kanban.db"
    turns_schema._MIGRATED_PATHS.discard(str(expected_db.resolve()))
    assert turns_ledger.turns_today() == 0
    assert expected_db.exists()
    assert "subscription_turns_ledger" in _table_names(expected_db)


# Ledger writes (7)


def test_record_turn_writes_row(bridge_db):
    row_id = turns_ledger.record_turn(
        task_id="t1",
        lane="platform",
        outcome="success",
        latency_ms=12,
        db_path=bridge_db,
    )
    assert row_id == 1
    assert _turn_rows(bridge_db)[0]["task_id"] == "t1"


def test_record_turn_rejects_unknown_lane(bridge_db):
    with pytest.raises(ValueError, match="Unknown lane"):
        turns_ledger.record_turn(
            task_id="t",
            lane="mystery",
            outcome="success",
            db_path=bridge_db,
        )


def test_record_turn_rejects_unknown_outcome(bridge_db):
    with pytest.raises(ValueError, match="unknown bridge outcome"):
        turns_ledger.record_turn(
            task_id="t",
            lane="platform",
            outcome="maybe",
            db_path=bridge_db,
        )


def test_record_turn_rejects_unknown_bridge_tier(bridge_db):
    with pytest.raises(ValueError, match="unknown bridge tier"):
        turns_ledger.record_turn(
            task_id="t",
            lane="platform",
            outcome="success",
            bridge_tier="enterprise",
            db_path=bridge_db,
        )


def test_record_turn_default_vendor_is_openai_codex(bridge_db):
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    assert _turn_rows(bridge_db)[0]["vendor"] == "openai-codex"


def test_record_turn_multi_turn_consumed_persisted(bridge_db):
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        turns_consumed=3,
        db_path=bridge_db,
    )
    assert _turn_rows(bridge_db)[0]["turns_consumed"] == 3


def test_record_turn_uses_retrying_write_txn(bridge_db, monkeypatch):
    original = turns_ledger.retrying_write_txn
    calls = []

    @contextmanager
    def observed(conn):
        calls.append(conn)
        with original(conn):
            yield

    monkeypatch.setattr(turns_ledger, "retrying_write_txn", observed)
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    assert len(calls) == 1


# Aggregation (5)


def test_turns_today_zero_when_empty(bridge_db):
    assert turns_ledger.turns_today(bridge_db) == 0


def test_turns_today_sums_only_today_utc(bridge_db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)
    with sqlite3.connect(bridge_db) as conn:
        conn.executemany(
            """
            INSERT INTO subscription_turns_ledger
                (ts, task_id, lane, bridge_tier, turns_consumed, outcome)
            VALUES (?, 't', 'platform', 'pro', ?, 'success')
            """,
            [
                (now.isoformat().replace("+00:00", "Z"), 4),
                (old.isoformat().replace("+00:00", "Z"), 7),
            ],
        )
    assert turns_ledger.turns_today(bridge_db) == 4


def test_turns_today_by_lane_grouped_correctly(bridge_db):
    for lane, turns in (("platform", 2), ("tihna", 3)):
        turns_ledger.record_turn(
            task_id="t",
            lane=lane,
            outcome="success",
            turns_consumed=turns,
            db_path=bridge_db,
        )
    grouped = turns_ledger.turns_today_by_lane(bridge_db)
    assert grouped["platform"] == 2
    assert grouped["tihna"] == 3
    assert grouped["green_captains"] == 0
    assert len(grouped) == 6


def test_turns_by_outcome_last_hours_window_correct(bridge_db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=3)
    with sqlite3.connect(bridge_db) as conn:
        conn.executemany(
            """
            INSERT INTO subscription_turns_ledger
                (ts, task_id, lane, bridge_tier, turns_consumed, outcome)
            VALUES (?, 't', 'platform', 'pro', ?, ?)
            """,
            [
                (now.isoformat().replace("+00:00", "Z"), 2, "degraded"),
                (old.isoformat().replace("+00:00", "Z"), 5, "success"),
            ],
        )
    grouped = turns_ledger.turns_by_outcome_last_hours(1, bridge_db)
    assert grouped["degraded"] == 2
    assert grouped["success"] == 0


def test_check_bridge_caps_returns_expected_shape(bridge_db):
    assert set(turns_ledger.check_bridge_caps(bridge_db)) == {
        "turns_used",
        "soft_cap",
        "hard_cap",
        "soft_hit",
        "hard_hit",
        "degraded_rate_pct",
    }


# Cap thresholds (4)


def test_soft_cap_hit_but_not_hard(bridge_db, monkeypatch):
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(2, 4))
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        turns_consumed=2,
        db_path=bridge_db,
    )
    caps = turns_ledger.check_bridge_caps(bridge_db)
    assert caps["soft_hit"] is True
    assert caps["hard_hit"] is False


def test_hard_cap_hit_implies_soft_hit(bridge_db, monkeypatch):
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(2, 4))
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        turns_consumed=4,
        db_path=bridge_db,
    )
    caps = turns_ledger.check_bridge_caps(bridge_db)
    assert caps["hard_hit"] is True
    assert caps["soft_hit"] is True


def test_below_both_caps_reports_neither(bridge_db, monkeypatch):
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(2, 4))
    turns_ledger.record_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    caps = turns_ledger.check_bridge_caps(bridge_db)
    assert caps["soft_hit"] is False
    assert caps["hard_hit"] is False


def test_cap_thresholds_come_from_bridge_caps_config(bridge_db, monkeypatch):
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(7, 9))
    caps = turns_ledger.check_bridge_caps(bridge_db)
    assert caps["soft_cap"] == 7
    assert caps["hard_cap"] == 9


# Fallthrough state (4)


def test_set_fallthrough_disabled_persists(bridge_db):
    bridge_state.set_fallthrough_disabled(
        True,
        reason="daily turns cap hit",
        db_path=bridge_db,
    )
    assert bridge_state.is_fallthrough_disabled(bridge_db) == (
        True,
        "daily turns cap hit",
    )


def test_is_fallthrough_disabled_defaults_false(bridge_db):
    assert bridge_state.is_fallthrough_disabled(bridge_db) == (False, None)


def test_hard_cap_hit_sets_fallthrough_disabled(
    bridge_db, monkeypatch
):
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(1, 1))
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    gate_integration.record_bridge_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    assert bridge_state.is_fallthrough_disabled(bridge_db)[0] is True


def test_healthy_nightly_probe_clears_fallthrough_disabled(bridge_db):
    bridge_state.set_fallthrough_disabled(
        True,
        reason="test",
        db_path=bridge_db,
    )
    result = bridge_cli._run_probe(
        source="nightly",
        db_path=bridge_db,
        call=lambda: _response(),
    )
    assert result.outcome == "healthy"
    assert bridge_state.is_fallthrough_disabled(bridge_db) == (False, None)


# Telegram dedup (3)


def test_soft_warn_telegram_uses_hourly_bucket(
    bridge_db, monkeypatch
):
    messages = []
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(1, 9))
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        messages.append,
    )
    gate_integration.record_bridge_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    with sqlite3.connect(bridge_db) as conn:
        key = conn.execute(
            "SELECT idempotency_key FROM side_effects"
        ).fetchone()[0]
    assert key.startswith("bridge_warn:")
    assert len(messages) == 1


def test_hard_alert_telegram_uses_hourly_bucket(
    bridge_db, monkeypatch
):
    messages = []
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(1, 1))
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        messages.append,
    )
    gate_integration.record_bridge_turn(
        task_id="t",
        lane="platform",
        outcome="success",
        db_path=bridge_db,
    )
    with sqlite3.connect(bridge_db) as conn:
        key = conn.execute(
            "SELECT idempotency_key FROM side_effects"
        ).fetchone()[0]
    assert key.startswith("bridge_hard:")
    assert "Atlas fallthrough disabled" in messages[0]


def test_second_soft_warn_in_same_hour_is_deduped(
    bridge_db, monkeypatch
):
    messages = []
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(1, 99))
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        messages.append,
    )
    for _ in range(2):
        gate_integration.record_bridge_turn(
            task_id="t",
            lane="platform",
            outcome="success",
            db_path=bridge_db,
        )
    assert len(messages) == 1
    with sqlite3.connect(bridge_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM side_effects").fetchone()[0] == 1


# Gate integration (4)


def test_record_call_openai_codex_also_writes_turn_row(bridge_db):
    entry = ledger.record_call(
        task_id="t-codex",
        lane="platform",
        vendor="openai-codex",
        model="openai-codex/gpt-5.6-sol",
        db_path=bridge_db,
    )
    assert entry.is_subscription_bridge is True
    assert _turn_rows(bridge_db)[0]["outcome"] == "success"


def test_record_call_openai_codex_turn_write_failure_does_not_break_cost_write(
    bridge_db, monkeypatch, caplog
):
    def fail(**_kwargs):
        raise RuntimeError("turn ledger unavailable")

    monkeypatch.setattr(gate_integration, "record_bridge_turn", fail)
    entry = ledger.record_call(
        task_id="t-codex",
        lane="platform",
        vendor="openai-codex",
        model="openai-codex/gpt-5.6-sol",
        db_path=bridge_db,
    )
    assert entry.id == 1
    assert "turn write failed" in caplog.text


def test_conversation_loop_raises_when_fallthrough_disabled(bridge_db):
    from agent.conversation_loop import _execute_recorded_leaf_call

    bridge_state.set_fallthrough_disabled(
        True,
        reason="daily turns cap hit",
        db_path=bridge_db,
    )
    called = []
    with pytest.raises(
        SubscriptionBridgeHaltedError,
        match="daily turns cap hit",
    ):
        _execute_recorded_leaf_call(
            lambda: called.append(True),
            task_id="t-halted",
            attempt_number=1,
            rung_id="r0_baseline",
            model="gpt-5.6-sol",
            provider="openai-codex",
            prompt="hello",
            db_path=bridge_db,
        )
    assert called == []


def test_conversation_loop_raise_writes_budget_failure_verdict_and_rate_limited_turn(
    bridge_db,
):
    from agent.conversation_loop import _execute_recorded_leaf_call

    bridge_state.set_fallthrough_disabled(
        True,
        reason="daily turns cap hit",
        db_path=bridge_db,
    )
    with pytest.raises(SubscriptionBridgeHaltedError):
        _execute_recorded_leaf_call(
            lambda: _response(),
            task_id="t-budget",
            attempt_number=1,
            rung_id="r0_baseline",
            model="gpt-5.6-sol",
            provider="openai-codex",
            prompt="hello",
            db_path=bridge_db,
        )
    verdict = verdict_api.list_verdicts_for_task("t-budget", bridge_db)[0]
    assert verdict.failure_class == "budget"
    assert verdict.escalation_recommended is False
    assert _turn_rows(bridge_db)[0]["outcome"] == "rate_limited"


# CLI (4)


def test_bridge_status_prints_expected_fields(bridge_db, capsys):
    assert bridge_cli._cmd_status(argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "turns used today: 0" in output
    assert "per-lane turns:" in output
    assert "last probe: never" in output
    assert "fallthrough disabled: no" in output
    assert "last 1h degraded rate: 0.00%" in output


def test_bridge_probe_without_confirm_does_not_call(
    bridge_db, monkeypatch, capsys
):
    called = []
    monkeypatch.setattr(
        bridge_cli,
        "_perform_pro_bridge_call",
        lambda: called.append(True),
    )
    assert bridge_cli._cmd_probe(argparse.Namespace(confirm=False)) == 0
    assert called == []
    assert _turn_rows(bridge_db) == []
    assert "Would make ONE real" in capsys.readouterr().out


def test_bridge_probe_with_confirm_makes_mocked_call_and_writes_rows(
    bridge_db, monkeypatch
):
    calls = []

    def mocked_call():
        calls.append(True)
        return _response()

    monkeypatch.setattr(
        bridge_cli,
        "_perform_pro_bridge_call",
        mocked_call,
    )
    assert bridge_cli._cmd_probe(argparse.Namespace(confirm=True)) == 0
    assert calls == [True]
    assert len(_turn_rows(bridge_db)) == 1
    with sqlite3.connect(bridge_db) as conn:
        assert conn.execute(
            "SELECT source, outcome FROM bridge_health_log"
        ).fetchone() == ("probe", "healthy")


def test_bridge_nightly_check_exit_codes_match_outcome(
    bridge_db, monkeypatch
):
    cases = (
        (lambda: _response(), 0),
        (lambda: _response("not exact"), 1),
        (lambda: (_ for _ in ()).throw(RuntimeError("429 rate limit")), 2),
        (lambda: (_ for _ in ()).throw(RuntimeError("network down")), 3),
    )
    for call, expected in cases:
        result = bridge_cli._run_probe(
            source="nightly",
            db_path=bridge_db,
            call=call,
        )
        assert result.exit_code == expected
    monkeypatch.setattr(bridge_config, "BRIDGE_CAPS", _caps(1, 1))
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    exhausted = bridge_cli._run_probe(
        source="nightly",
        db_path=bridge_db,
        call=lambda: _response(),
    )
    assert exhausted.exit_code == 2
