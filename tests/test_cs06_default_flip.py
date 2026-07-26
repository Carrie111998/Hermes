from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli.routing import bootstrap, drift, drift_schema, facade, schema
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.sqlite_util import retrying_write_txn
from hermes_cli.subcommands import doctrine


PLUGIN = Path(
    "/Users/genesis/.hermes/profiles/atlas/plugins/"
    "task-model-router/__init__.py"
)


@pytest.fixture
def cs06_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    doctrine_path = tmp_path / "doctrine_v1.json"
    doctrine_path.write_text(
        json.dumps(
            {
                "notes": "CS-06 tests",
                "created_by": "tests",
                "rules": [
                    {
                        "lane": "default",
                        "rung": "default",
                        "complexity": "default",
                        "primary_provider": "openai-codex",
                        "primary_model": "gpt-5-6-sol",
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
    sent: list[str] = []
    monkeypatch.setattr(
        facade.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    return db_path, sent


def _load_plugin():
    spec = importlib.util.spec_from_file_location("cs06_plugin_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fetchone(db_path, query, values=()):
    conn = schema.connect(db_path)
    try:
        row = conn.execute(query, values).fetchone()
        return tuple(row) if row is not None else None
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _rule_id(db_path) -> int:
    return int(
        _fetchone(
            db_path,
            "SELECT min(id) FROM routing_doctrine",
        )[0]
    )


def _insert_decision(
    db_path,
    *,
    kind: str,
    forced_legacy: int = 0,
    lane: str = "green_captains",
) -> None:
    if kind == "followed":
        used, overridden, matched = 1, 0, _rule_id(db_path)
    elif kind == "bypassed":
        used, overridden, matched = 0, 0, None
    else:
        raise ValueError(kind)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                INSERT INTO routing_decisions (
                    session_id, task_id, profile, route, lane, rung,
                    complexity, chosen_provider, chosen_model,
                    doctrine_version, matched_rule_id, match_specificity,
                    used_doctrine_reader, overridden_by_caller,
                    doctrine_suggested_provider, doctrine_suggested_model,
                    failure_history_json, chosen_at, forced_legacy
                ) VALUES (
                    NULL, NULL, 'atlas', 'single', ?, 'execute', 'standard',
                    'provider', 'model', 1, ?, 'default', ?, ?, ?, ?,
                    '[]', ?, ?
                )
                """,
                (
                    lane,
                    matched,
                    used,
                    overridden,
                    "provider" if used else None,
                    "model" if used else None,
                    _now(),
                    int(forced_legacy),
                ),
            )
    finally:
        conn.close()


def _refresh(db_path) -> None:
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            drift.refresh_bucket(conn, drift._hour_bucket(_now()))
    finally:
        conn.close()


def _alert(db_path):
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            return drift.maybe_alert(conn)
    finally:
        conn.close()


def _invoke_doctrine(argv):
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)
    doctrine.register_cli(children)
    args = parser.parse_args(["doctrine", *argv])
    return args.func(args)


def _plugin_route_until_selection(module, args):
    with mock.patch.object(
        module,
        "_route_selection",
        side_effect=ValueError("stop after audited selection"),
    ):
        return module._handle_task_model_route(args)


# Schema (2)


def test_forced_legacy_column_and_index_added_idempotently(cs06_env):
    db_path, _sent = cs06_env
    schema.migrate(db_path)
    schema.migrate(db_path)
    columns = _fetchone(
        db_path,
        "SELECT COUNT(*) FROM pragma_table_info('routing_decisions') "
        "WHERE name='forced_legacy'",
    )
    index = _fetchone(
        db_path,
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name='idx_decisions_forced_legacy'",
    )
    assert columns == (1,)
    assert index == (1,)


def test_existing_rows_forced_legacy_is_null_or_zero(cs06_env):
    db_path, _sent = cs06_env
    _insert_decision(db_path, kind="followed")
    schema.migrate(db_path)
    value = _fetchone(
        db_path,
        "SELECT forced_legacy FROM routing_decisions",
    )[0]
    assert value in (None, 0)


# Facade (7)


def test_route_for_turn_accepts_forced_legacy_kwarg(cs06_env):
    db_path, _sent = cs06_env
    result = facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        forced_legacy=True,
        db_path=db_path,
    )
    assert result["forced_legacy"] == 1


def test_route_for_turn_forced_legacy_requires_caller_provider_and_model(
    cs06_env,
):
    db_path, _sent = cs06_env
    with pytest.raises(ValueError, match="provider and model are required"):
        facade.route_for_turn(
            lane="green_captains",
            rung="execute",
            complexity="standard",
            use_doctrine_reader=True,
            forced_legacy=True,
            db_path=db_path,
        )


def test_route_for_turn_forced_legacy_writes_forced_legacy_one(cs06_env):
    db_path, _sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        forced_legacy=True,
        db_path=db_path,
    )
    assert _fetchone(
        db_path,
        "SELECT forced_legacy FROM routing_decisions",
    ) == (1,)


def test_route_for_turn_forced_legacy_writes_used_doctrine_zero(cs06_env):
    db_path, _sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        use_doctrine_reader=True,
        forced_legacy=True,
        db_path=db_path,
    )
    assert _fetchone(
        db_path,
        "SELECT used_doctrine_reader FROM routing_decisions",
    ) == (0,)


def test_route_for_turn_forced_legacy_writes_null_doctrine_columns(
    cs06_env,
):
    db_path, _sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        forced_legacy=True,
        db_path=db_path,
    )
    assert _fetchone(
        db_path,
        "SELECT doctrine_version, matched_rule_id, match_specificity, "
        "doctrine_suggested_provider, doctrine_suggested_model "
        "FROM routing_decisions",
    ) == (None, None, None, None, None)


def test_route_for_turn_forced_legacy_ignores_use_doctrine_true(cs06_env):
    db_path, _sent = cs06_env
    result = facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        use_doctrine_reader=True,
        forced_legacy=True,
        db_path=db_path,
    )
    assert result["used_doctrine_reader"] is False
    assert (result["provider"], result["model"]) == (
        "openrouter",
        "vendor/model",
    )


def test_route_for_turn_return_dict_includes_forced_legacy(cs06_env):
    db_path, _sent = cs06_env
    normal = facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=db_path,
    )
    assert normal["forced_legacy"] == 0


# Doctrine-live alert (4)


def test_first_doctrine_following_decision_emits_alert(cs06_env):
    db_path, sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        task_id="first",
        db_path=db_path,
    )
    assert len(sent) == 1
    assert "✅ DOCTRINE LIVE" in sent[0]


def test_second_doctrine_following_decision_same_day_deduped(cs06_env):
    db_path, sent = cs06_env
    for task_id in ("first", "second"):
        facade.route_for_turn(
            lane="green_captains",
            rung="execute",
            complexity="standard",
            use_doctrine_reader=True,
            task_id=task_id,
            db_path=db_path,
        )
    assert len(sent) == 1


def test_doctrine_live_alert_uses_side_effects_bucket(cs06_env):
    db_path, _sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        use_doctrine_reader=True,
        db_path=db_path,
    )
    key = _fetchone(
        db_path,
        "SELECT idempotency_key FROM side_effects",
    )[0]
    assert key == f"doctrine_live:{datetime.now(timezone.utc).date().isoformat()}"


def test_doctrine_live_alert_not_emitted_when_forced_legacy(cs06_env):
    db_path, sent = cs06_env
    facade.route_for_turn(
        lane="green_captains",
        rung="execute",
        complexity="standard",
        caller_provider="openrouter",
        caller_model="vendor/model",
        use_doctrine_reader=True,
        forced_legacy=True,
        db_path=db_path,
    )
    assert sent == []
    assert _fetchone(
        db_path,
        "SELECT COUNT(*) FROM side_effects",
    ) == (0,)


# Plugin (5)


def test_plugin_default_use_doctrine_reader_is_true(cs06_env):
    _db_path, _sent = cs06_env
    module = _load_plugin()
    prop = module.ROUTE_SCHEMA["parameters"]["properties"][
        "use_doctrine_reader"
    ]
    assert prop["default"] is True


def test_plugin_force_legacy_routing_kwarg_wired_to_facade(cs06_env):
    db_path, _sent = cs06_env
    module = _load_plugin()
    _plugin_route_until_selection(
        module,
        {
            "route": "single",
            "prompt": "test",
            "provider": "openrouter",
            "model": "vendor/model",
            "force_legacy_routing": True,
        },
    )
    assert _fetchone(
        db_path,
        "SELECT forced_legacy, used_doctrine_reader FROM routing_decisions",
    ) == (1, 0)


def test_plugin_inputSchema_reflects_new_defaults(cs06_env):
    _db_path, _sent = cs06_env
    module = _load_plugin()
    properties = module.ROUTE_SCHEMA["parameters"]["properties"]
    assert properties["use_doctrine_reader"]["default"] is True
    assert properties["force_legacy_routing"] == {
        "type": "boolean",
        "default": False,
        "description": (
            "Emergency escape hatch. When true, force pre-CS-06 legacy "
            "behavior (caller must supply provider+model)."
        ),
    }


def test_plugin_caller_supplied_provider_still_wins_when_use_doctrine_default_true(
    cs06_env,
):
    db_path, _sent = cs06_env
    module = _load_plugin()
    _plugin_route_until_selection(
        module,
        {
            "route": "single",
            "prompt": "test",
            "provider": "openrouter",
            "model": "vendor/model",
        },
    )
    assert _fetchone(
        db_path,
        "SELECT chosen_provider, chosen_model, used_doctrine_reader, "
        "overridden_by_caller FROM routing_decisions",
    ) == ("openrouter", "vendor/model", 1, 1)


def test_plugin_default_true_uses_doctrine_when_caller_provider_missing(
    cs06_env,
):
    db_path, _sent = cs06_env
    module = _load_plugin()
    _plugin_route_until_selection(
        module,
        {"route": "single", "prompt": "test"},
    )
    assert _fetchone(
        db_path,
        "SELECT chosen_provider, chosen_model, used_doctrine_reader "
        "FROM routing_decisions",
    ) == ("openai-codex", "gpt-5-6-sol", 1)


# Drift threshold + forced-legacy exclusion (7)


def test_bypass_alert_threshold_is_zero():
    assert drift._BYPASS_ALERT_PCT == 0.0


def test_bypass_alert_fires_on_any_non_forced_legacy_bypass_in_mature_window(
    cs06_env,
):
    db_path, sent = cs06_env
    for _index in range(19):
        _insert_decision(db_path, kind="followed")
    _insert_decision(db_path, kind="bypassed", forced_legacy=0)
    _refresh(db_path)
    assert _alert(db_path) == "bypass_high"
    assert len(sent) == 1


def test_bypass_alert_does_not_count_forced_legacy_rows(cs06_env):
    db_path, sent = cs06_env
    for _index in range(19):
        _insert_decision(db_path, kind="followed")
    _insert_decision(db_path, kind="bypassed", forced_legacy=1)
    _refresh(db_path)
    assert _alert(db_path) is None
    assert sent == []


def test_bypass_alert_does_not_fire_when_only_forced_legacy_present(
    cs06_env,
):
    db_path, sent = cs06_env
    for _index in range(20):
        _insert_decision(db_path, kind="bypassed", forced_legacy=1)
    _refresh(db_path)
    assert _alert(db_path) is None
    assert sent == []


def test_forced_legacy_count_and_pct_in_drift_window(cs06_env):
    db_path, _sent = cs06_env
    for _index in range(7):
        _insert_decision(db_path, kind="followed")
    for _index in range(3):
        _insert_decision(db_path, kind="bypassed", forced_legacy=1)
    _refresh(db_path)
    result = drift.compute_drift_window(hours=24, db_path=db_path)
    assert result["forced_legacy_count"] == 3
    assert result["forced_legacy_pct"] == 30.0


def test_forced_legacy_shown_in_hermes_doctrine_drift_output(
    cs06_env,
    capsys,
):
    db_path, _sent = cs06_env
    _insert_decision(db_path, kind="bypassed", forced_legacy=1)
    _refresh(db_path)
    assert _invoke_doctrine(["drift"]) == 0
    assert "forced_legacy: 1 (100.0%)" in capsys.readouterr().out


def test_forced_legacy_hidden_when_zero(cs06_env, capsys):
    db_path, _sent = cs06_env
    _insert_decision(db_path, kind="followed")
    _refresh(db_path)
    assert _invoke_doctrine(["drift"]) == 0
    assert "forced_legacy:" not in capsys.readouterr().out
