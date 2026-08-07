"""Offline contract tests for the tool-search live harness."""

from __future__ import annotations

import importlib.util
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
