from __future__ import annotations

import argparse
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli.routing import bootstrap, facade, schema
from hermes_cli.routing.reader import DoctrineReader
from hermes_cli.sqlite_util import retrying_write_txn
from hermes_cli.subcommands import doctrine


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    doctrine_path = tmp_path / "doctrine_v1.json"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DOCTRINE_V1_PATH", str(doctrine_path))
    facade._READERS.clear()
    return db_path, doctrine_path


def _rule(
    lane="default",
    rung="default",
    complexity="default",
    provider="provider",
    model="model",
    *,
    priority=0,
    fallbacks=None,
    forbid_paths=None,
    notes="test rule",
):
    return {
        "lane": lane,
        "rung": rung,
        "complexity": complexity,
        "primary_provider": provider,
        "primary_model": model,
        "fallback_chain": list(fallbacks or []),
        "forbid_paths": list(forbid_paths or []),
        "priority": priority,
        "notes": notes,
    }


def _payload(rules=None, *, bootstrap_document=True):
    value = {
        "notes": "test doctrine",
        "rules": list(rules or [_rule()]),
    }
    if bootstrap_document:
        value["created_by"] = "tests"
    return value


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _bootstrap(routing_env):
    db_path, doctrine_path = routing_env
    _write_json(doctrine_path, _payload())
    bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    return db_path


def _install_version(db_path, rules, *, version=2, previous=1):
    schema.ensure_migrated(db_path)
    now = bootstrap.utc_now()
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            for rule in rules:
                bootstrap._insert_rule(
                    conn,
                    version=version,
                    rule=rule,
                    created_ts=now,
                )
            conn.execute(
                """
                UPDATE routing_doctrine_meta
                   SET active_version = ?, previous_version = ?,
                       last_activated_ts = ?, last_activated_by = 'tests'
                 WHERE singleton = 1
                """,
                (version, previous, now),
            )
    finally:
        conn.close()


def _fetch_one(db_path, query, params=()):
    conn = schema.connect(db_path)
    try:
        row = conn.execute(query, params).fetchone()
        return tuple(row) if row is not None else None
    finally:
        conn.close()


def _invoke_cli(argv):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctrine.register_cli(subparsers)
    args = parser.parse_args(["doctrine", *argv])
    return args.func(args)


def _plan_file(path: Path, rules=None) -> Path:
    _write_json(path, _payload(rules, bootstrap_document=False))
    return path


def test_migration_creates_all_tables_and_indexes_idempotently(routing_env):
    db_path, _ = routing_env
    schema.migrate(db_path)
    schema.migrate(db_path)
    conn = schema.connect(db_path)
    try:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
    finally:
        conn.close()
    assert set(schema.EXPECTED_COLUMNS) <= names
    assert {
        "idx_doctrine_version",
        "idx_doctrine_specific",
        "idx_activations_when",
        "idx_activations_ver",
        "idx_decisions_session",
        "idx_decisions_task",
        "idx_decisions_when",
        "idx_decisions_profile",
        "idx_decisions_route",
    } <= names


def test_bootstrap_creates_doctrine_v1_json_if_missing(routing_env):
    db_path, doctrine_path = routing_env
    result = bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    assert result == {
        "created": True,
        "rule_count": 4,
        "version": 1,
        "path": str(doctrine_path),
    }
    assert json.loads(doctrine_path.read_text()) == bootstrap.STUB_PAYLOAD


def test_bootstrap_reads_existing_doctrine_v1_json(routing_env):
    db_path, doctrine_path = routing_env
    _write_json(
        doctrine_path,
        _payload([_rule(provider="custom", model="chosen")]),
    )
    bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    row = _fetch_one(
        db_path,
        "SELECT primary_provider, primary_model FROM routing_doctrine",
    )
    assert tuple(row) == ("custom", "chosen")


def test_bootstrap_writes_version_one_rules_and_meta_and_activation_row(
    routing_env,
):
    db_path = _bootstrap(routing_env)
    assert _fetch_one(
        db_path,
        "SELECT count(*), min(version), max(version) FROM routing_doctrine",
    ) == (1, 1, 1)
    assert _fetch_one(
        db_path,
        "SELECT active_version, previous_version FROM routing_doctrine_meta",
    ) == (1, None)
    assert _fetch_one(
        db_path,
        "SELECT activated_version, activation_type "
        "FROM routing_doctrine_activations",
    ) == (1, "bootstrap")


def test_bootstrap_is_a_noop_if_meta_already_populated(routing_env):
    db_path = _bootstrap(routing_env)
    before = _fetch_one(
        db_path,
        "SELECT count(*) FROM routing_doctrine",
    )[0]
    assert bootstrap.bootstrap_if_needed(db_path) == {"created": False}
    after = _fetch_one(
        db_path,
        "SELECT count(*) FROM routing_doctrine",
    )[0]
    assert after == before


def test_bootstrap_rejects_malformed_json(routing_env):
    db_path, doctrine_path = routing_env
    doctrine_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid doctrine_v1 JSON"):
        bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    assert _fetch_one(
        db_path,
        "SELECT count(*) FROM routing_doctrine_meta",
    )[0] == 0


def test_bootstrap_rejects_unknown_keys_strictly(routing_env):
    db_path, doctrine_path = routing_env
    payload = _payload()
    payload["surprise"] = True
    _write_json(doctrine_path, payload)
    with pytest.raises(ValueError, match="keys must be exact"):
        bootstrap.bootstrap_if_needed(db_path, doctrine_path)


def test_reader_exact_match_beats_partial(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [
            _rule("lane", "default", "default", model="lane"),
            _rule("lane", "rung", "default", model="rung"),
            _rule("lane", "rung", "complex", model="exact"),
        ],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="rung",
        complexity="complex",
    )
    assert (choice["model"], choice["match_specificity"]) == ("exact", "exact")


def test_reader_lane_rung_default_complexity_matches(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [
            _rule(model="fallback"),
            _rule("lane", "rung", "default", model="lane-rung"),
        ],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="rung",
        complexity="complex",
    )
    assert choice["model"] == "lane-rung"
    assert choice["match_specificity"] == "lane+rung"


def test_reader_lane_only_matches_when_no_rung_rule(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [_rule(), _rule("lane", "default", "default", model="lane")],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="unknown",
        complexity="complex",
    )
    assert choice["model"] == "lane"
    assert choice["match_specificity"] == "lane"


def test_reader_falls_back_to_default_default_default(routing_env):
    db_path = _bootstrap(routing_env)
    choice = DoctrineReader(db_path).choose(
        lane="unknown",
        rung="unknown",
        complexity="unknown",
    )
    assert choice["model"] == "model"
    assert choice["match_specificity"] == "default"


def test_reader_forbid_paths_filter_excludes_rule(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [
            _rule(model="allowed"),
            _rule(
                "lane",
                "rung",
                "complex",
                model="forbidden",
                forbid_paths=[{"lane": "lane", "rung": "rung"}],
            ),
        ],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="rung",
        complexity="complex",
    )
    assert choice["model"] == "allowed"


def test_reader_priority_breaks_specificity_tie(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [
            _rule("lane", "rung", "complex", model="low", priority=1),
            _rule("lane", "rung", "complex", model="high", priority=9),
        ],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="rung",
        complexity="complex",
    )
    assert choice["model"] == "high"


def test_reader_deterministic_tie_break_by_rule_id(routing_env):
    db_path = _bootstrap(routing_env)
    _install_version(
        db_path,
        [
            _rule("lane", "rung", "complex", model="first"),
            _rule("lane", "rung", "complex", model="second"),
        ],
    )
    choice = DoctrineReader(db_path).choose(
        lane="lane",
        rung="rung",
        complexity="complex",
    )
    assert choice["model"] == "first"


def test_reader_returns_full_resolution_dict_shape(routing_env):
    db_path = _bootstrap(routing_env)
    choice = DoctrineReader(db_path).choose(
        lane="anything",
        rung="anything",
        complexity="anything",
    )
    assert set(choice) == {
        "provider",
        "model",
        "fallbacks",
        "doctrine_version",
        "matched_rule_id",
        "match_specificity",
    }


def test_reader_reloads_when_active_version_changes(routing_env):
    db_path = _bootstrap(routing_env)
    reader = DoctrineReader(db_path)
    assert reader.choose(
        lane="x",
        rung="x",
        complexity="x",
    )["model"] == "model"
    _install_version(db_path, [_rule(model="version-two")])
    assert reader.choose(
        lane="x",
        rung="x",
        complexity="x",
    )["model"] == "version-two"


def test_reader_threadsafe_under_concurrent_choose(routing_env):
    db_path = _bootstrap(routing_env)
    reader = DoctrineReader(db_path)

    def choose():
        return reader.choose(
            lane="x",
            rung="y",
            complexity="z",
        )["model"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert set(pool.map(lambda _index: choose(), range(100))) == {"model"}


def test_failure_history_kwarg_accepted_and_persisted_in_decisions_row(
    routing_env,
):
    db_path = _bootstrap(routing_env)
    history = [{"provider": "x", "rung": "execute", "failed": True}]
    facade.route_for_turn(
        lane="x",
        rung="execute",
        complexity="complex",
        failure_history=history,
        use_doctrine_reader=True,
        db_path=db_path,
    )
    stored = _fetch_one(
        db_path,
        "SELECT failure_history_json FROM routing_decisions",
    )[0]
    assert json.loads(stored) == history


def test_failure_history_kwarg_does_not_change_choice(routing_env):
    db_path = _bootstrap(routing_env)
    reader = DoctrineReader(db_path)
    first = reader.choose(lane="x", rung="y", complexity="z")
    second = reader.choose(
        lane="x",
        rung="y",
        complexity="z",
        failure_history=[{"provider": "provider", "failed": True}],
    )
    assert first == second


def test_facade_legacy_path_requires_caller_provider_and_model(routing_env):
    db_path = _bootstrap(routing_env)
    with pytest.raises(ValueError, match="provider and model are required"):
        facade.route_for_turn(
            lane="x",
            rung="y",
            complexity="z",
            use_doctrine_reader=False,
            db_path=db_path,
        )


def test_facade_legacy_path_writes_used_doctrine_reader_zero(routing_env):
    db_path = _bootstrap(routing_env)
    facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="caller",
        caller_model="caller-model",
        db_path=db_path,
    )
    assert _fetch_one(
        db_path,
        "SELECT used_doctrine_reader FROM routing_decisions",
    )[0] == 0


def test_facade_legacy_path_writes_nullable_doctrine_columns(routing_env):
    db_path = _bootstrap(routing_env)
    result = facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="caller",
        caller_model="caller-model",
        db_path=db_path,
    )
    assert result["doctrine_version"] is None
    assert _fetch_one(
        db_path,
        "SELECT doctrine_version, matched_rule_id, match_specificity "
        "FROM routing_decisions",
    ) == (None, None, None)


def test_facade_doctrine_path_without_caller_uses_doctrine(routing_env):
    db_path = _bootstrap(routing_env)
    result = facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        use_doctrine_reader=True,
        db_path=db_path,
    )
    assert (result["provider"], result["model"]) == ("provider", "model")
    assert result["overridden_by_caller"] is False


def test_facade_doctrine_path_with_caller_uses_caller_and_records_suggestion(
    routing_env,
):
    db_path = _bootstrap(routing_env)
    result = facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="caller",
        caller_model="caller-model",
        use_doctrine_reader=True,
        db_path=db_path,
    )
    assert (result["provider"], result["model"]) == (
        "caller",
        "caller-model",
    )
    assert result["doctrine_suggested_provider"] == "provider"
    assert result["doctrine_suggested_model"] == "model"
    assert result["overridden_by_caller"] is True


def test_facade_writes_profile_and_route_to_decisions_row(routing_env):
    db_path = _bootstrap(routing_env)
    facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="p",
        caller_model="m",
        profile="atlas",
        route="source-label",
        db_path=db_path,
    )
    assert _fetch_one(
        db_path,
        "SELECT profile, route FROM routing_decisions",
    ) == ("atlas", "source-label")


def test_facade_writes_session_id_and_task_id_to_decisions_row(routing_env):
    db_path = _bootstrap(routing_env)
    facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="p",
        caller_model="m",
        task_id="task",
        session_id="session",
        db_path=db_path,
    )
    assert _fetch_one(
        db_path,
        "SELECT session_id, task_id FROM routing_decisions",
    ) == ("session", "task")


def test_facade_default_flag_is_false():
    parameter = inspect.signature(facade.route_for_turn).parameters[
        "use_doctrine_reader"
    ]
    assert parameter.default is False


def test_doctrine_dump_default_version_prints_active(routing_env, capsys):
    _bootstrap(routing_env)
    assert _invoke_cli(["dump"]) == 0
    assert '"version": 1' in capsys.readouterr().out


def test_doctrine_dump_specific_version(routing_env, capsys):
    db_path = _bootstrap(routing_env)
    _install_version(db_path, [_rule(model="two")])
    assert _invoke_cli(["dump", "--version", "1"]) == 0
    output = capsys.readouterr().out
    assert '"version": 1' in output
    assert '"model"' in output


def test_doctrine_plan_validates_and_diffs(routing_env, tmp_path, capsys):
    _bootstrap(routing_env)
    plan = _plan_file(tmp_path / "plan.json", [_rule(model="new")])
    assert _invoke_cli(["plan", str(plan)]) == 0
    assert "1 added, 1 removed" in capsys.readouterr().out


def test_doctrine_apply_without_confirm_is_noop(
    routing_env,
    tmp_path,
    capsys,
):
    db_path = _bootstrap(routing_env)
    plan = _plan_file(tmp_path / "plan.json", [_rule(model="new")])
    assert _invoke_cli(["apply", str(plan)]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert _fetch_one(
        db_path,
        "SELECT max(version) FROM routing_doctrine",
    )[0] == 1


def test_doctrine_apply_with_confirm_inserts_new_inactive_version(
    routing_env,
    tmp_path,
):
    db_path = _bootstrap(routing_env)
    plan = _plan_file(tmp_path / "plan.json", [_rule(model="new")])
    assert _invoke_cli(["apply", str(plan), "--confirm"]) == 0
    assert _fetch_one(
        db_path,
        "SELECT max(version) FROM routing_doctrine",
    )[0] == 2
    assert _fetch_one(
        db_path,
        "SELECT active_version FROM routing_doctrine_meta",
    )[0] == 1


def test_doctrine_activate_with_confirm_swaps_active_and_writes_activation(
    routing_env,
    tmp_path,
):
    db_path = _bootstrap(routing_env)
    plan = _plan_file(tmp_path / "plan.json", [_rule(model="new")])
    _invoke_cli(["apply", str(plan), "--confirm"])
    assert _invoke_cli(["activate", "2", "--confirm"]) == 0
    assert _fetch_one(
        db_path,
        "SELECT active_version, previous_version FROM routing_doctrine_meta",
    ) == (2, 1)
    assert _fetch_one(
        db_path,
        "SELECT activation_type FROM routing_doctrine_activations "
        "ORDER BY id DESC LIMIT 1",
    )[0] == "activate"


def test_doctrine_deactivate_reverts_to_previous_and_writes_activation(
    routing_env,
    tmp_path,
):
    db_path = _bootstrap(routing_env)
    plan = _plan_file(tmp_path / "plan.json", [_rule(model="new")])
    _invoke_cli(["apply", str(plan), "--confirm"])
    _invoke_cli(["activate", "2", "--confirm"])
    assert _invoke_cli(["deactivate", "--confirm"]) == 0
    assert _fetch_one(
        db_path,
        "SELECT active_version, previous_version FROM routing_doctrine_meta",
    ) == (1, None)
    assert _fetch_one(
        db_path,
        "SELECT activation_type FROM routing_doctrine_activations "
        "ORDER BY id DESC LIMIT 1",
    )[0] == "deactivate"


def test_doctrine_history_lists_activations_from_activations_table(
    routing_env,
    capsys,
):
    _bootstrap(routing_env)
    assert _invoke_cli(["history"]) == 0
    assert '"activation_type": "bootstrap"' in capsys.readouterr().out


def test_doctrine_decisions_prints_recent_rows(routing_env, capsys):
    db_path = _bootstrap(routing_env)
    facade.route_for_turn(
        lane="x",
        rung="y",
        complexity="z",
        caller_provider="p",
        caller_model="m",
        task_id="task",
        db_path=db_path,
    )
    assert _invoke_cli(["decisions", "--limit", "10"]) == 0
    assert '"task_id": "task"' in capsys.readouterr().out


def test_doctrine_decisions_filter_by_profile_and_route(
    routing_env,
    capsys,
):
    db_path = _bootstrap(routing_env)
    for profile, route in (("atlas", "wanted"), ("forge", "other")):
        facade.route_for_turn(
            lane="x",
            rung="y",
            complexity="z",
            caller_provider="p",
            caller_model="m",
            profile=profile,
            route=route,
            db_path=db_path,
        )
    assert _invoke_cli(
        ["decisions", "--profile", "atlas", "--route", "wanted"]
    ) == 0
    output = capsys.readouterr().out
    assert '"route": "wanted"' in output
    assert '"route": "other"' not in output


def test_doctrine_plan_rejects_bad_schema(routing_env, tmp_path, capsys):
    _bootstrap(routing_env)
    bad = tmp_path / "bad.json"
    _write_json(bad, {"notes": "bad", "rules": [], "extra": True})
    assert _invoke_cli(["plan", str(bad)]) == 1
    assert "Invalid doctrine plan" in capsys.readouterr().out
