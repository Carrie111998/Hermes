from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from hermes_cli.cost import cli as cost_cli
from hermes_cli.cost import kill_switch
from hermes_cli.routing import bootstrap, drift, drift_schema, facade, schema
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.sqlite_util import retrying_write_txn
from hermes_cli.subcommands import doctrine


@pytest.fixture
def drift_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    doctrine_path = tmp_path / "doctrine_v1.json"
    doctrine_path.write_text(
        json.dumps(
            {
                "notes": "drift test doctrine",
                "created_by": "tests",
                "rules": [
                    {
                        "lane": "default",
                        "rung": "default",
                        "complexity": "default",
                        "primary_provider": "doctrine-provider",
                        "primary_model": "doctrine-model",
                        "fallback_chain": [],
                        "forbid_paths": [],
                        "priority": 0,
                        "notes": "test default",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DOCTRINE_V1_PATH", str(doctrine_path))
    facade._READERS.clear()
    bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    drift_schema.migrate(db_path)
    side_effects_schema.migrate(db_path)
    return db_path


def _now(*, hours_ago: int = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _rule_id(db_path) -> int:
    conn = schema.connect(db_path)
    try:
        return int(
            conn.execute("SELECT min(id) FROM routing_doctrine").fetchone()[0]
        )
    finally:
        conn.close()


def _insert_decision(
    db_path,
    *,
    kind: str = "followed",
    lane: str = "green_captains",
    profile: str | None = "atlas",
    chosen_at: str | None = None,
    chosen_provider: str = "doctrine-provider",
    chosen_model: str = "doctrine-model",
) -> int:
    if kind == "followed":
        used, overridden, matched = 1, 0, _rule_id(db_path)
    elif kind == "overridden":
        used, overridden, matched = 1, 1, _rule_id(db_path)
    elif kind == "bypassed":
        used, overridden, matched = 0, 0, None
    elif kind == "no_rule":
        used, overridden, matched = 1, 0, None
    else:
        raise ValueError(kind)
    suggested_provider = (
        "doctrine-provider" if used else None
    )
    suggested_model = "doctrine-model" if used else None
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO routing_decisions (
                    session_id, task_id, profile, route, lane, rung,
                    complexity, chosen_provider, chosen_model,
                    doctrine_version, matched_rule_id, match_specificity,
                    used_doctrine_reader, overridden_by_caller,
                    doctrine_suggested_provider, doctrine_suggested_model,
                    failure_history_json, chosen_at
                ) VALUES (
                    NULL, NULL, ?, 'single', ?, 'execute', 'standard',
                    ?, ?, 1, ?, 'default', ?, ?, ?, ?, '[]', ?
                )
                """,
                (
                    profile,
                    lane,
                    chosen_provider,
                    chosen_model,
                    matched,
                    used,
                    overridden,
                    suggested_provider,
                    suggested_model,
                    chosen_at or _now(),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _refresh_current(db_path) -> None:
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            row = conn.execute(
                "SELECT max(chosen_at) AS chosen_at FROM routing_decisions"
            ).fetchone()
            chosen_at = row["chosen_at"] if row is not None else None
            drift.refresh_bucket(
                conn,
                drift._hour_bucket(chosen_at or _now()),
            )
    finally:
        conn.close()


def _fetchone(db_path, query, values=()):
    conn = schema.connect(db_path)
    try:
        row = conn.execute(query, values).fetchone()
        return tuple(row) if row is not None else None
    finally:
        conn.close()


def _invoke_doctrine(argv):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctrine.register_cli(subparsers)
    args = parser.parse_args(["doctrine", *argv])
    return args.func(args)


def _invoke_cost_today():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cost_cli.register_cli(subparsers)
    args = parser.parse_args(["cost", "today"])
    return args.func(args)


def _stub_cost_display(monkeypatch):
    monkeypatch.setattr(kill_switch, "list_killed_tasks", lambda **_kw: [])
    monkeypatch.setattr(
        cost_cli.config,
        "LANE_DAILY_CAPS_AUD",
        {"platform": 2.0},
    )
    monkeypatch.setattr(cost_cli.config, "ESCALATION_DAILY_CAP_AUD", 3.0)
    monkeypatch.setattr(cost_cli.config, "GLOBAL_DAILY_CAP_AUD", 20.0)
    monkeypatch.setattr(cost_cli.caps, "daily_spend_aud", lambda *_a: 0.0)
    monkeypatch.setattr(
        cost_cli.caps,
        "escalation_spend_today_aud",
        lambda: 0.0,
    )


# Schema (3)


def test_drift_rollup_table_and_indexes_present(drift_env):
    conn = drift_schema.connect(drift_env)
    try:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        columns = tuple(
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(routing_drift_rollup)"
            )
        )
    finally:
        conn.close()
    assert columns == drift_schema.EXPECTED_COLUMNS
    assert "idx_drift_rollup_updated" in names


def test_migration_idempotent(drift_env):
    drift_schema.migrate(drift_env)
    drift_schema.migrate(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*) FROM routing_drift_rollup",
    ) == (0,)


def test_bucket_ts_is_iso_hour():
    assert drift._hour_bucket("2026-07-26T02:41:59.123456+00:00") == (
        "2026-07-26T02:00:00Z"
    )


# Refresh bucket (7)


def test_refresh_bucket_inserts_first_time(drift_env):
    _insert_decision(drift_env)
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT total_decisions FROM routing_drift_rollup",
    ) == (1,)


def test_refresh_bucket_upserts_on_second_call(drift_env):
    _insert_decision(drift_env)
    _refresh_current(drift_env)
    _insert_decision(drift_env)
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*), total_decisions FROM routing_drift_rollup",
    ) == (1, 2)


def test_refresh_bucket_counts_followed_class_correctly(drift_env):
    _insert_decision(drift_env, kind="followed")
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT followed_count, followed_pct FROM routing_drift_rollup",
    ) == (1, 100.0)


def test_refresh_bucket_counts_overridden_class_correctly(drift_env):
    _insert_decision(
        drift_env,
        kind="overridden",
        chosen_provider="caller",
        chosen_model="caller-model",
    )
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT overridden_count, overridden_pct FROM routing_drift_rollup",
    ) == (1, 100.0)


def test_refresh_bucket_counts_bypassed_class_correctly(drift_env):
    _insert_decision(drift_env, kind="bypassed")
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT bypassed_count, bypassed_pct FROM routing_drift_rollup",
    ) == (1, 100.0)


def test_refresh_bucket_computes_top_override_lane(drift_env):
    _insert_decision(drift_env, kind="overridden", lane="dayroute")
    _insert_decision(drift_env, kind="overridden", lane="dayroute")
    _insert_decision(drift_env, kind="overridden", lane="tihna")
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT top_override_lane, top_override_count "
        "FROM routing_drift_rollup",
    ) == ("dayroute", 2)


def test_refresh_bucket_handles_empty_bucket_gracefully(drift_env):
    _refresh_current(drift_env)
    assert _fetchone(
        drift_env,
        "SELECT total_decisions, followed_pct FROM routing_drift_rollup",
    ) == (0, 0.0)


# Facade integration (5)


def test_route_for_turn_refreshes_bucket_on_insert(drift_env):
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=drift_env,
    )
    assert _fetchone(
        drift_env,
        "SELECT total_decisions, followed_count "
        "FROM routing_drift_rollup",
    ) == (1, 1)


def test_route_for_turn_refresh_and_insert_are_single_transaction(
    drift_env,
    monkeypatch,
):
    observed = []
    original = drift.refresh_bucket

    def wrapped(conn, bucket):
        observed.append(bool(conn.in_transaction))
        return original(conn, bucket)

    monkeypatch.setattr(drift, "refresh_bucket", wrapped)
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=drift_env,
    )
    assert observed == [True]


def test_route_for_turn_survives_when_drift_rollup_write_fails(
    drift_env,
    monkeypatch,
):
    def fail_refresh(_conn, _bucket):
        raise RuntimeError("synthetic rollup failure")

    monkeypatch.setattr(drift, "refresh_bucket", fail_refresh)
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=drift_env,
    )
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*) FROM routing_decisions",
    ) == (1,)
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*) FROM routing_drift_rollup",
    ) == (0,)


def test_route_for_turn_bucket_ts_matches_row_hour(drift_env):
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=drift_env,
    )
    chosen_at, bucket = _fetchone(
        drift_env,
        """
        SELECT d.chosen_at, r.window_bucket_ts
          FROM routing_decisions AS d
          JOIN routing_drift_rollup AS r ON 1 = 1
        """,
    )
    assert bucket == drift._hour_bucket(chosen_at)


def test_route_for_turn_multiple_calls_same_hour_all_land_in_same_bucket(
    drift_env,
):
    for _index in range(3):
        facade.route_for_turn(
            lane="green_captains",
            rung="execute",
            complexity="standard",
            use_doctrine_reader=True,
            db_path=drift_env,
        )
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*), SUM(total_decisions) FROM routing_drift_rollup",
    ) == (1, 3)


# Drift window (6)


def test_compute_drift_window_default_24h(drift_env):
    _insert_decision(drift_env, kind="followed")
    _insert_decision(drift_env, kind="overridden")
    _refresh_current(drift_env)
    result = drift.compute_drift_window(db_path=drift_env)
    assert (result["window_hours"], result["total_decisions"]) == (24, 2)


def test_compute_drift_window_custom_hours(drift_env):
    _insert_decision(drift_env, chosen_at=_now())
    _insert_decision(drift_env, chosen_at=_now(hours_ago=2))
    drift.refresh_all_buckets(db_path=drift_env)
    assert drift.compute_drift_window(
        hours=1,
        db_path=drift_env,
    )["total_decisions"] == 1
    assert drift.compute_drift_window(
        hours=3,
        db_path=drift_env,
    )["total_decisions"] == 2


def test_compute_drift_window_filter_by_lane(drift_env):
    _insert_decision(drift_env, lane="green_captains")
    _insert_decision(drift_env, lane="dayroute")
    _refresh_current(drift_env)
    result = drift.compute_drift_window(
        hours=24,
        lane="dayroute",
        db_path=drift_env,
    )
    assert result["total_decisions"] == 1


def test_compute_drift_window_filter_by_profile(drift_env):
    _insert_decision(drift_env, profile="atlas")
    _insert_decision(drift_env, profile="forge")
    _refresh_current(drift_env)
    result = drift.compute_drift_window(
        hours=24,
        profile="forge",
        db_path=drift_env,
    )
    assert result["total_decisions"] == 1


def test_compute_drift_window_top_override_lanes_ranked(drift_env):
    _insert_decision(drift_env, kind="overridden", lane="dayroute")
    _insert_decision(drift_env, kind="overridden", lane="dayroute")
    _insert_decision(drift_env, kind="overridden", lane="tihna")
    _refresh_current(drift_env)
    result = drift.compute_drift_window(hours=24, db_path=drift_env)
    assert result["top_override_lanes"][0] == ("dayroute", 2)


def test_compute_drift_window_top_overridden_pairs_ranked(drift_env):
    _insert_decision(
        drift_env,
        kind="overridden",
        chosen_provider="caller",
        chosen_model="caller-model",
    )
    _insert_decision(
        drift_env,
        kind="overridden",
        chosen_provider="caller",
        chosen_model="caller-model",
    )
    _refresh_current(drift_env)
    result = drift.compute_drift_window(hours=24, db_path=drift_env)
    assert result["top_overridden_pairs"][0] == (
        ("doctrine-provider", "doctrine-model"),
        ("caller", "caller-model"),
        2,
    )


# Alert thresholds (7)


def _populate_alert_window(db_path, *, followed=0, overridden=0, bypassed=0, no_rule=0):
    for _index in range(followed):
        _insert_decision(db_path, kind="followed")
    for _index in range(overridden):
        _insert_decision(db_path, kind="overridden")
    for _index in range(bypassed):
        _insert_decision(db_path, kind="bypassed")
    for _index in range(no_rule):
        _insert_decision(db_path, kind="no_rule")
    _refresh_current(db_path)


def _call_alert(db_path):
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            return drift.maybe_alert(conn)
    finally:
        conn.close()


def test_alert_fires_when_override_pct_over_40(
    drift_env,
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, followed=11, overridden=9)
    assert _call_alert(drift_env) == "override_high"
    assert len(sent) == 1


def test_alert_fires_when_bypass_pct_over_20(drift_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, followed=15, bypassed=5)
    assert _call_alert(drift_env) == "bypass_high"
    assert len(sent) == 1


def test_alert_fires_when_no_rule_present(drift_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, followed=19, no_rule=1)
    assert _call_alert(drift_env) == "no_rule_present"
    assert len(sent) == 1


def test_alert_does_not_fire_below_min_sample_size(drift_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, overridden=19)
    assert _call_alert(drift_env) is None
    assert sent == []


def test_alert_dedupes_via_side_effects_bucket(drift_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, followed=11, overridden=9)
    assert _call_alert(drift_env) == "override_high"
    assert _call_alert(drift_env) is None
    assert len(sent) == 1
    key = _fetchone(
        drift_env,
        "SELECT idempotency_key FROM side_effects",
    )[0]
    assert key.startswith("doctrine_drift:override_high:")


def test_alert_class_labels_match_spec(drift_env, monkeypatch):
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    _populate_alert_window(drift_env, followed=11, overridden=9)
    result = _call_alert(drift_env)
    assert result in {"override_high", "bypass_high", "no_rule_present"}


def test_alert_returns_none_when_all_healthy(drift_env, monkeypatch):
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    _populate_alert_window(drift_env, followed=20)
    assert _call_alert(drift_env) is None
    assert sent == []


# Refresh-all (3)


def test_refresh_all_rebuilds_from_scratch(drift_env):
    _insert_decision(drift_env)
    assert drift.refresh_all_buckets(db_path=drift_env) == 1
    assert _fetchone(
        drift_env,
        "SELECT SUM(total_decisions) FROM routing_drift_rollup",
    ) == (1,)


def test_refresh_all_is_idempotent(drift_env):
    _insert_decision(drift_env)
    drift.refresh_all_buckets(db_path=drift_env)
    first = _fetchone(
        drift_env,
        "SELECT * FROM routing_drift_rollup",
    )
    drift.refresh_all_buckets(db_path=drift_env)
    second = _fetchone(
        drift_env,
        "SELECT * FROM routing_drift_rollup",
    )
    updated_index = drift_schema.EXPECTED_COLUMNS.index("updated_ts")
    assert (
        first[:updated_index] + first[updated_index + 1 :]
        == second[:updated_index] + second[updated_index + 1 :]
    )


def test_refresh_all_matches_incremental_refresh(drift_env):
    _insert_decision(drift_env, kind="followed")
    _insert_decision(drift_env, kind="overridden")
    _refresh_current(drift_env)
    incremental = _fetchone(
        drift_env,
        "SELECT total_decisions, followed_count, overridden_count "
        "FROM routing_drift_rollup",
    )
    drift.refresh_all_buckets(db_path=drift_env)
    rebuilt = _fetchone(
        drift_env,
        "SELECT total_decisions, followed_count, overridden_count "
        "FROM routing_drift_rollup",
    )
    assert rebuilt == incremental


# CLI (7)


def test_hermes_doctrine_drift_default_prints_columns(
    drift_env,
    capsys,
):
    _insert_decision(drift_env)
    _refresh_current(drift_env)
    assert _invoke_doctrine(["drift"]) == 0
    output = capsys.readouterr().out
    assert "Total decisions: 1" in output
    assert "followed: 1 (100.0%)" in output


def test_hermes_doctrine_drift_lane_filter_narrows_output(
    drift_env,
    capsys,
):
    _insert_decision(drift_env, lane="green_captains")
    _insert_decision(drift_env, lane="dayroute")
    _refresh_current(drift_env)
    assert _invoke_doctrine(
        ["drift", "--lane", "dayroute"]
    ) == 0
    assert "Total decisions: 1" in capsys.readouterr().out


def test_hermes_doctrine_drift_refresh_all_without_confirm_is_noop(
    drift_env,
    capsys,
):
    _insert_decision(drift_env)
    assert _invoke_doctrine(["drift", "--refresh-all"]) == 0
    assert "Dry run: would rebuild 1 buckets" in capsys.readouterr().out
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*) FROM routing_drift_rollup",
    ) == (0,)


def test_hermes_doctrine_drift_refresh_all_with_confirm_rebuilds(
    drift_env,
    capsys,
):
    _insert_decision(drift_env)
    assert _invoke_doctrine(
        ["drift", "--refresh-all", "--confirm"]
    ) == 0
    assert "1 buckets rebuilt" in capsys.readouterr().out
    assert _fetchone(
        drift_env,
        "SELECT COUNT(*) FROM routing_drift_rollup",
    ) == (1,)


def test_hermes_doctrine_drift_explain_prints_bucket_rows(
    drift_env,
    capsys,
):
    row_id = _insert_decision(drift_env)
    assert _invoke_doctrine(
        ["drift", "--explain", drift._hour_bucket(_now())]
    ) == 0
    output = capsys.readouterr().out
    assert f'"id": {row_id}' in output


def test_hermes_cost_today_includes_drift_summary_line(
    drift_env,
    monkeypatch,
    capsys,
):
    _stub_cost_display(monkeypatch)
    _insert_decision(drift_env)
    _refresh_current(drift_env)
    assert _invoke_cost_today() == 0
    assert (
        "Doctrine drift 24h: followed 100.0% / "
        "overridden 0.0% / bypassed 0.0%"
    ) in capsys.readouterr().out


def test_hermes_cost_today_drift_line_says_no_decisions_when_empty(
    drift_env,
    monkeypatch,
    capsys,
):
    _stub_cost_display(monkeypatch)
    assert _invoke_cost_today() == 0
    assert "Doctrine drift 24h: no decisions" in capsys.readouterr().out
