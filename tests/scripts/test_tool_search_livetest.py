"""Offline contract tests for the tool-search live harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

import yaml


def _load_harness():
    script = Path(__file__).resolve().parents[2] / "scripts" / "tool_search_livetest.py"
    spec = importlib.util.spec_from_file_location("tool_search_livetest_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builtin_scenarios_cover_required_disclosure_flows():
    harness = _load_harness()

    scenarios = {scenario["id"]: scenario for scenario in harness.BUILTIN_SCENARIOS}

    assert scenarios["builtin_tool_free"]["expected_underlying_tools"] == []
    assert scenarios["builtin_browser"]["builtin_defer_groups"] == ["browser"]
    assert scenarios["builtin_file_then_browser"]["expected_eager_tools"] == ["read_file"]
    assert {
        "session_search",
        "delegation",
        "code_execution",
        "todo",
        "vision",
    } <= {
        group
        for scenario in harness.BUILTIN_SCENARIOS
        for group in scenario["builtin_defer_groups"]
    }


def test_build_run_matrix_selects_only_requested_builtin_case():
    harness = _load_harness()

    cases = harness.build_run_matrix(
        suite="builtins",
        scenario_ids=["builtin_browser"],
        modes=["deferred", "direct"],
    )

    assert [(case["scenario"]["id"], case["mode"]) for case in cases] == [
        ("builtin_browser", "deferred"),
        ("builtin_browser", "direct"),
    ]
    assert all(case["suite"] == "builtins" for case in cases)


def test_isolated_home_encodes_selected_builtin_policy(monkeypatch, tmp_path):
    harness = _load_harness()
    monkeypatch.setattr(harness, "ORIGINAL_AUTH", tmp_path / "missing-auth.json")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    hermes_home = harness.setup_isolated_home(
        enabled=True,
        builtins_enabled=True,
        builtin_defer_groups=["browser"],
        builtin_min_schema_tokens=0,
    )
    try:
        config = yaml.safe_load((hermes_home / "config.yaml").read_text())
        builtins = config["tools"]["tool_search"]["builtins"]
        assert builtins == {
            "enabled": True,
            "defer": ["browser"],
            "min_schema_tokens": 0,
        }
    finally:
        shutil.rmtree(hermes_home.parent, ignore_errors=True)


def test_dry_run_never_executes_a_live_scenario(monkeypatch, capsys):
    harness = _load_harness()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run attempted a live model call")

    monkeypatch.setattr(harness, "run_one_scenario", fail_if_called)

    assert harness.main([
        "--suite", "builtins",
        "--scenario", "builtin_browser",
        "--mode", "deferred",
        "--dry-run",
    ]) == 0
    output = capsys.readouterr().out
    assert '"scenario_id": "builtin_browser"' in output
    assert '"mode": "deferred"' in output


def test_effective_tool_observer_captures_inline_calls_and_preserves_hooks(
    monkeypatch,
):
    harness = _load_harness()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live-secret-value")

    original_events = []

    def original_hook(**kwargs):
        original_events.append(kwargs["tool_name"])

    class PluginManager:
        _hooks = {"post_tool_call": [original_hook]}

    manager = PluginManager()
    observed = []
    detach = harness._install_effective_tool_observer(manager, observed)
    try:
        for callback in list(manager._hooks["post_tool_call"]):
            callback(
                tool_name="tool_search",
                args={"query": "todo"},
                result="{}",
                status="ok",
            )
        for tool_name in ("todo", "session_search", "delegate_task"):
            for callback in list(manager._hooks["post_tool_call"]):
                callback(
                    tool_name=tool_name,
                    args={
                        "content": "use sk-live-secret-value",
                        "api_key": "sk-another-secret-value",
                    },
                    result="{}",
                    status="ok",
                )
    finally:
        detach()

    assert original_events == [
        "tool_search",
        "todo",
        "session_search",
        "delegate_task",
    ]
    assert manager._hooks["post_tool_call"] == [original_hook]
    assert [call["name"] for call in observed] == [
        "todo",
        "session_search",
        "delegate_task",
    ]
    assert all(call["args"] == {
        "content": "use [REDACTED]",
        "api_key": "[REDACTED]",
    } for call in observed)


def test_case_evaluation_checks_effective_and_bridge_paths():
    harness = _load_harness()
    scenario = {
        "expected_underlying_tools": ["read_file", "browser_navigate"],
        "expected_eager_tools": ["read_file"],
    }
    record = {
        "error": None,
        "underlying_tool_calls": [
            {"name": "read_file"},
            {"name": "browser_navigate"},
        ],
        "bridge_calls": [{
            "name": "tool_call",
            "args": {"name": "browser_navigate", "arguments": {}},
        }],
    }

    assert harness._evaluate_case(scenario, "deferred", record) == (True, [])

    for mode in ("direct", "off"):
        direct_passed, direct_reasons = harness._evaluate_case(
            scenario,
            mode,
            record,
        )
        assert direct_passed is False
        assert f"{mode} mode used bridge tool_call" in direct_reasons


def test_case_evaluation_rejects_errors_missing_tools_and_tool_free_calls():
    harness = _load_harness()

    passed, reasons = harness._evaluate_case(
        {
            "expected_underlying_tools": ["todo"],
            "expected_eager_tools": ["read_file"],
        },
        "deferred",
        {
            "error": "provider failed",
            "underlying_tool_calls": [],
            "bridge_calls": [],
        },
    )
    assert passed is False
    assert "scenario error: provider failed" in reasons
    assert "missing expected underlying tool: todo" in reasons
    assert "missing expected eager tool: read_file" in reasons
    assert "expected deferred tool did not use tool_call: todo" in reasons

    passed, reasons = harness._evaluate_case(
        {"expected_underlying_tools": ["todo"]},
        "deferred",
        {
            "error": None,
            "underlying_tool_calls": [{
                "name": "todo",
                "status": "error",
                "error_message": "write failed",
            }],
            "bridge_calls": [{
                "name": "tool_call",
                "args": {"name": "todo", "arguments": {}},
            }],
        },
    )
    assert passed is False
    assert "underlying tool failed: todo: write failed" in reasons

    passed, reasons = harness._evaluate_case(
        {"expected_underlying_tools": [], "expected_eager_tools": []},
        "deferred",
        {
            "error": None,
            "underlying_tool_calls": [{"name": "todo"}],
            "bridge_calls": [],
        },
    )
    assert passed is False
    assert "tool-free scenario invoked underlying tool: todo" in reasons


def test_main_returns_nonzero_for_failed_live_case(monkeypatch, tmp_path):
    harness = _load_harness()

    def fake_run(*_args, **_kwargs):
        return {
            "tool_search_enabled": True,
            "bridge_calls": [],
            "underlying_tool_calls": [],
            "elapsed_seconds": 0.01,
            "error": None,
        }

    monkeypatch.setattr(harness, "run_one_scenario", fake_run)

    exit_code = harness.main([
        "--suite", "builtins",
        "--scenario", "builtin_browser",
        "--mode", "deferred",
        "--out-dir", str(tmp_path),
    ])

    summary = json.loads((tmp_path / "_summary.json").read_text())
    assert exit_code == 1
    assert summary[0]["passed"] is False
    assert "missing expected underlying tool: browser_navigate" in summary[0][
        "failure_reasons"
    ]
