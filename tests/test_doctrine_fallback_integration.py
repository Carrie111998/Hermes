from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import conversation_loop
from hermes_cli.routing import (
    bootstrap,
    drift,
    drift_schema,
    facade,
    route_context,
    schema,
)
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.sqlite_util import retrying_write_txn


PLUGIN = (
    Path.home()
    / ".hermes"
    / "profiles"
    / "atlas"
    / "plugins"
    / "task-model-router"
    / "__init__.py"
)
LAUNCHER = Path("/Users/genesis/AgentOS/scripts/hermes-private-query.py")


def _load(path: Path, prefix: str):
    name = f"{prefix}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(
    decision_row_id: int = 1,
    *,
    chain: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "decision_row_id": decision_row_id,
        "task_id": "atlas-t-cascade",
        "session_id": "atlas-s-cascade",
        "matched_rule_id": 2,
        "primary_provider": "openai-codex",
        "primary_model": "gpt-5-6-sol",
        "fallback_chain": chain
        if chain is not None
        else [{"provider": "openai", "model": "fallback-model"}],
        "nonce": str(uuid.uuid4()),
    }


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    route_context._reset_for_tests()
    facade._READERS.clear()
    monkeypatch.delenv("HERMES_ROUTE_CONTEXT_JSON", raising=False)
    monkeypatch.setattr(
        facade.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    yield
    route_context._reset_for_tests()
    facade._READERS.clear()


@pytest.fixture
def doctrine_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    doctrine_path = tmp_path / "doctrine_v1.json"
    doctrine_path.write_text(
        json.dumps(
            {
                "notes": "CS-10b tests",
                "created_by": "tests",
                "rules": [
                    {
                        "lane": "default",
                        "rung": "default",
                        "complexity": "default",
                        "primary_provider": "openai-codex",
                        "primary_model": "gpt-5-6-sol",
                        "fallback_chain": [
                            {
                                "provider": "openai",
                                "model": "fallback-model",
                            }
                        ],
                        "forbid_paths": [],
                        "priority": 0,
                        "notes": "test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_DOCTRINE_V1_PATH", str(doctrine_path))
    bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    drift_schema.migrate(db_path)
    side_effects_schema.migrate(db_path)
    return db_path, tmp_path


def _decision(db_path) -> int:
    result = facade.route_for_turn(
        lane="default",
        rung="default",
        complexity="default",
        caller_provider="openai-codex",
        caller_model="gpt-5-6-sol",
        task_id="atlas-t-cascade",
        session_id="atlas-s-cascade",
        use_doctrine_reader=False,
        db_path=db_path,
    )
    return int(result["decision_row_id"])


def _install_context(monkeypatch, payload: dict) -> None:
    monkeypatch.setenv(
        "HERMES_ROUTE_CONTEXT_JSON",
        json.dumps(payload),
    )
    assert route_context.get_route_context() is not None


def _row(db_path, query: str, values=()):
    conn = schema.connect(db_path)
    try:
        return conn.execute(query, values).fetchone()
    finally:
        conn.close()


def _mock_client():
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.api_key = "test-key"
    return client


def _make_agent(monkeypatch, *, chain=None, generic=None):
    route_context._reset_for_tests()
    if chain is not None:
        monkeypatch.setenv(
            "HERMES_ROUTE_CONTEXT_JSON",
            json.dumps(_context(chain=chain)),
        )
    else:
        monkeypatch.delenv("HERMES_ROUTE_CONTEXT_JSON", raising=False)
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="primary-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=generic,
        )
    agent.client = MagicMock()
    return agent


def _switch(agent, *, failure_class="timeout", latency_ms=1234, error=None):
    agent._doctrine_fallback_failure = {
        "failure_class": failure_class,
        "latency_ms": latency_ms,
        "error_repr": repr(error or TimeoutError("mock")),
        "transition_reason": "provider_switch",
    }
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(_mock_client(), "fallback-model"),
    ):
        assert agent._try_activate_fallback() is True


def _flush_fixture(db_path, monkeypatch):
    decision_id = _decision(db_path)
    _install_context(monkeypatch, _context(decision_id))
    route_context.append_failure(
        provider="openai-codex",
        model="gpt-5-6-sol",
        failure_class="timeout",
        latency_ms=123,
        error_repr="TimeoutError: mock",
        transition_reason="provider_switch",
    )
    return decision_id


def test_plugin_route_for_turn_returns_decision_row_id(doctrine_env):
    db_path, _ = doctrine_env
    result = facade.route_for_turn(
        lane="default",
        rung="default",
        complexity="default",
        use_doctrine_reader=True,
        db_path=db_path,
    )
    assert isinstance(result["decision_row_id"], int)
    assert result["fallbacks"] == [
        {"provider": "openai", "model": "fallback-model"}
    ]


def test_plugin_writes_route_context_file_mode_0600(
    doctrine_env,
    monkeypatch,
):
    _db_path, tmp_path = doctrine_env
    module = _load(PLUGIN, "router")
    module.AUDIT_DIR = tmp_path / "audit"
    module.SLOT_DIR = tmp_path / "slots"
    module.PRIVATE_QUERY_LAUNCHER = tmp_path / "launcher"
    module.PRIVATE_QUERY_LAUNCHER.write_text("#!/bin/sh\n", encoding="utf-8")
    module.PRIVATE_QUERY_LAUNCHER.chmod(0o700)
    workdir = tmp_path / "work"
    workdir.mkdir()
    observed = {}

    def fake_run(command, **_kwargs):
        path = Path(command[command.index("--route-context") + 1])
        observed["mode"] = stat.S_IMODE(path.stat().st_mode)
        observed["data"] = json.loads(path.read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "_session_usage",
        lambda *_a: {
            "id": "s1",
            "matching_session_count": 1,
            "api_call_count": 0,
            "models": [],
        },
    )
    monkeypatch.setattr(module, "_final_assistant_message", lambda *_a: "ok")
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "work",
            "use_doctrine_reader": True,
            "workdir": str(workdir),
        }
    )
    assert observed["mode"] == 0o600
    assert isinstance(observed["data"]["decision_row_id"], int)


def test_plugin_passes_route_context_arg_to_launcher(
    doctrine_env,
    monkeypatch,
):
    _db_path, tmp_path = doctrine_env
    module = _load(PLUGIN, "router")
    module.AUDIT_DIR = tmp_path / "audit"
    module.SLOT_DIR = tmp_path / "slots"
    launcher = tmp_path / "launcher"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    module.PRIVATE_QUERY_LAUNCHER = launcher
    workdir = tmp_path / "work"
    workdir.mkdir()
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "work",
            "use_doctrine_reader": True,
            "workdir": str(workdir),
        }
    )
    assert "--route-context" in captured["command"]
    assert captured["command"].index("--route-context") < (
        captured["command"].index("--")
    )


def test_plugin_skips_route_context_when_force_legacy_routing(
    doctrine_env,
    monkeypatch,
):
    _db_path, tmp_path = doctrine_env
    module = _load(PLUGIN, "router")
    module.AUDIT_DIR = tmp_path / "audit"
    module.SLOT_DIR = tmp_path / "slots"
    launcher = tmp_path / "launcher"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    module.PRIVATE_QUERY_LAUNCHER = launcher
    workdir = tmp_path / "work"
    workdir.mkdir()
    captured = {}
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: (
            captured.setdefault("command", command)
            or SimpleNamespace(returncode=1, stdout="", stderr="")
        ),
    )
    module._handle_task_model_route(
        {
            "route": "single",
            "provider": "p",
            "model": "m",
            "prompt": "work",
            "force_legacy_routing": True,
            "workdir": str(workdir),
        }
    )
    assert "--route-context" not in captured["command"]


def test_launcher_reads_and_unlinks_route_context_file(tmp_path):
    launcher = _load(LAUNCHER, "private_launcher")
    path = tmp_path / "route.json"
    payload = _context()
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert launcher._load_route_context(str(path)) == payload
    assert not path.exists()


def test_launcher_exports_HERMES_ROUTE_CONTEXT_JSON(monkeypatch):
    launcher = _load(LAUNCHER, "private_launcher")
    payload = _context()
    launcher._install_route_context(payload)
    assert route_context.get_route_context() == payload
    assert "HERMES_ROUTE_CONTEXT_JSON" not in os.environ


def test_launcher_does_not_crash_on_invalid_route_context(
    tmp_path,
    monkeypatch,
):
    launcher = _load(LAUNCHER, "private_launcher")
    context_path = tmp_path / "invalid.json"
    context_path.write_text("{}", encoding="utf-8")
    prompt_path = tmp_path / "prompt"
    prompt_path.write_text("hello", encoding="utf-8")
    prompt_path.chmod(0o600)
    import hermes_cli.main

    monkeypatch.setattr(hermes_cli.main, "main", lambda: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launcher",
            "--profile",
            "atlas",
            "--prompt-file",
            str(prompt_path),
            "--route-context",
            str(context_path),
            "--",
            "--provider",
            "test",
        ],
    )
    assert launcher.main() == 0


def test_launcher_does_not_log_route_context_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    launcher = _load(LAUNCHER, "private_launcher")
    context_path = tmp_path / "secret-route-context.json"
    context_path.write_text("{}", encoding="utf-8")
    prompt_path = tmp_path / "prompt"
    prompt_path.write_text("hello", encoding="utf-8")
    prompt_path.chmod(0o600)
    import hermes_cli.main

    monkeypatch.setattr(hermes_cli.main, "main", lambda: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launcher",
            "--profile",
            "atlas",
            "--prompt-file",
            str(prompt_path),
            "--route-context",
            str(context_path),
            "--",
            "--provider",
            "test",
        ],
    )
    launcher.main()
    captured = capsys.readouterr()
    assert str(context_path) not in captured.err


def test_agent_init_uses_doctrine_chain_when_route_context_present(
    monkeypatch,
):
    doctrine_chain = [{"provider": "openai", "model": "fallback-model"}]
    agent = _make_agent(
        monkeypatch,
        chain=doctrine_chain,
        generic=[{"provider": "zai", "model": "generic-model"}],
    )
    assert agent._fallback_chain == doctrine_chain
    assert agent._fallback_source == "doctrine"


def test_agent_init_uses_generic_chain_when_route_context_absent(
    monkeypatch,
):
    generic = [{"provider": "zai", "model": "generic-model"}]
    agent = _make_agent(monkeypatch, generic=generic)
    assert agent._fallback_chain == generic
    assert agent._fallback_source == "profile"


def test_agent_init_does_not_mutate_profile_config(monkeypatch):
    generic = [{"provider": "zai", "model": "generic-model"}]
    original = json.loads(json.dumps(generic))
    _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
        generic=generic,
    )
    assert generic == original


def test_provider_switch_appends_failure_entry(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    _switch(agent)
    assert len(route_context._failure_history) == 1


def test_same_provider_retry_does_not_append_failure_entry(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    conversation_loop._arm_doctrine_fallback_failure(
        agent,
        failure_class="timeout",
        latency_ms=5,
        error=TimeoutError("retry"),
    )
    assert route_context._failure_history == []


def test_empty_response_failover_appends_failure_entry(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    _switch(agent, failure_class="empty_response")
    assert route_context._failure_history[0]["failure_class"] == (
        "empty_response"
    )


def test_failure_class_from_existing_classifier_recorded(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    _switch(agent, failure_class="rate_limit")
    assert route_context._failure_history[0]["failure_class"] == "rate_limit"


def test_latency_ms_recorded(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    _switch(agent, latency_ms=4321)
    assert route_context._failure_history[0]["latency_ms"] == 4321


def test_error_repr_truncated_to_500_chars(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    agent._doctrine_fallback_failure = {
        "failure_class": "timeout",
        "latency_ms": 1,
        "error_repr": "x" * 900,
        "transition_reason": "provider_switch",
    }
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(_mock_client(), "fallback-model"),
    ):
        assert agent._try_activate_fallback()
    assert len(route_context._failure_history[0]["error_repr"]) == 500


def test_transition_reason_recorded(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        chain=[{"provider": "openai", "model": "fallback-model"}],
    )
    _switch(agent)
    assert route_context._failure_history[0]["transition_reason"] == (
        "provider_switch"
    )


def test_flush_on_successful_turn_updates_chosen_when_switched(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _flush_fixture(db_path, monkeypatch)
    route_context.flush_to_db(
        chosen_provider="openai",
        chosen_model="fallback-model",
        outcome="success",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT chosen_provider, chosen_model FROM routing_decisions "
        "ORDER BY id DESC LIMIT 1",
    )
    assert tuple(row) == ("openai", "fallback-model")


def test_flush_on_terminal_failure_writes_sentinel_provider_model(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _flush_fixture(db_path, monkeypatch)
    route_context.mark_cascade_exhausted("timeout")
    route_context.flush_to_db(
        chosen_provider="openai",
        chosen_model="fallback-model",
        outcome="failure",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT chosen_provider, chosen_model FROM routing_decisions "
        "ORDER BY id DESC LIMIT 1",
    )
    assert tuple(row) == ("__all_failed__", "__none__")


def test_flush_records_full_failure_history_json(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _flush_fixture(db_path, monkeypatch)
    route_context.flush_to_db(
        chosen_provider="openai",
        chosen_model="fallback-model",
        outcome="success",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT failure_history_json FROM routing_decisions "
        "ORDER BY id DESC LIMIT 1",
    )
    history = json.loads(row[0])
    assert history[0]["error_repr"] == "TimeoutError: mock"


def test_flush_leaf_verdict_uses_outcome_failure_class_infra(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _flush_fixture(db_path, monkeypatch)
    route_context.mark_cascade_exhausted("timeout")
    route_context.flush_to_db(
        chosen_provider="ignored",
        chosen_model="ignored",
        outcome="failure",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT outcome, failure_class FROM leaf_verdicts "
        "ORDER BY id DESC LIMIT 1",
    )
    assert tuple(row) == ("failure", "infra")


def test_flush_leaf_verdict_metadata_contains_cascade_exhausted_flag(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _flush_fixture(db_path, monkeypatch)
    route_context.mark_cascade_exhausted("timeout")
    route_context.flush_to_db(
        chosen_provider="ignored",
        chosen_model="ignored",
        outcome="failure",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT raw_meta FROM leaf_verdicts ORDER BY id DESC LIMIT 1",
    )
    assert json.loads(row[0])["cascade_exhausted"] is True


def test_max_cascade_switches_stops_engine_at_3(monkeypatch):
    chain = [
        {"provider": f"provider-{index}", "model": f"model-{index}"}
        for index in range(4)
    ]
    agent = _make_agent(monkeypatch, chain=chain)
    for index in range(3):
        route_context.append_failure(
            provider=f"p{index}",
            model=f"m{index}",
            failure_class="timeout",
            latency_ms=1,
            error_repr="mock",
            transition_reason="provider_switch",
        )
    assert agent._try_activate_fallback() is False
    assert agent._fallback_index == len(chain)
    assert route_context.is_cascade_exhausted()


def test_cascade_cap_configurable_via_fallback_config(monkeypatch):
    from hermes_cli import fallback_config

    monkeypatch.setattr(fallback_config, "max_cascade_switches", 1)
    agent = _make_agent(
        monkeypatch,
        chain=[
            {"provider": "p1", "model": "m1"},
            {"provider": "p2", "model": "m2"},
        ],
    )
    route_context.append_failure(
        provider="p0",
        model="m0",
        failure_class="timeout",
        latency_ms=1,
        error_repr="mock",
        transition_reason="provider_switch",
    )
    assert agent._try_activate_fallback() is False


def _seed_drift(db_path):
    chosen_at = drift._utc_now()
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            for index in range(20):
                all_failed = index < 2
                history = (
                    [
                        {
                            "provider": "p",
                            "model": "m",
                            "failure_class": (
                                "timeout" if index == 0 else "overloaded"
                            ),
                        }
                    ]
                    if all_failed
                    else []
                )
                conn.execute(
                    """
                    INSERT INTO routing_decisions (
                        lane, rung, complexity, chosen_provider,
                        chosen_model, used_doctrine_reader,
                        overridden_by_caller, failure_history_json,
                        chosen_at, forced_legacy
                    ) VALUES (
                        'default', 'default', 'default', ?, ?, 1, 0, ?, ?, 0
                    )
                    """,
                    (
                        "__all_failed__" if all_failed else "p",
                        "__none__" if all_failed else "m",
                        json.dumps(history),
                        chosen_at,
                    ),
                )
            drift.refresh_bucket(conn, drift._hour_bucket(chosen_at))
    finally:
        conn.close()


def test_all_failed_count_and_pct_in_rollup(doctrine_env):
    db_path, _ = doctrine_env
    _seed_drift(db_path)
    result = drift.compute_drift_window(hours=6, db_path=db_path)
    assert result["all_failed_count"] == 2
    assert result["all_failed_pct"] == 10.0


def test_cascade_failing_high_alert_fires_above_5pct_and_20_decisions(
    doctrine_env,
    monkeypatch,
):
    db_path, _ = doctrine_env
    _seed_drift(db_path)
    sent = []
    monkeypatch.setattr(
        drift.telegram_alert,
        "send_bridge_alert",
        sent.append,
    )
    conn = drift_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            label = drift.maybe_alert(conn)
    finally:
        conn.close()
    assert label == "cascade_failing_high"
    assert sent


def test_top_cascade_failure_classes_ranked_from_failure_history_json(
    doctrine_env,
):
    db_path, _ = doctrine_env
    _seed_drift(db_path)
    result = drift.compute_drift_window(hours=6, db_path=db_path)
    assert result["top_cascade_failure_classes"] == [
        ("overloaded", 1),
        ("timeout", 1),
    ]
