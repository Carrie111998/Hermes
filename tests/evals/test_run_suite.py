"""Behavioral contracts for the standalone eval-suite runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from evals.runners import run_suite as runner


def _write_suite(tmp_path: Path, name: str, scenarios: list[dict]) -> Path:
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir(exist_ok=True)
    path = suites_dir / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump({"name": name, "scenarios": scenarios}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["run_suite.py", *argv])
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    return int(exc_info.value.code)


def test_live_constructor_failure_returns_error_and_restores_config(monkeypatch):
    import run_agent
    from cli import CLI_CONFIG

    original = dict(CLI_CONFIG.get("delegation") or {})

    class FailingAgent:
        def __init__(self, **_kwargs):
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(run_agent, "AIAgent", FailingAgent)

    result = runner.run_scenario_live(
        {
            "user_message": "hello",
            "config_overrides": {"delegation.max_concurrent_children": 99},
        },
        "provider",
        "model",
    )

    assert "constructor failed" in result["error"]
    assert result["messages"] == []
    assert CLI_CONFIG.get("delegation") == original


def test_live_result_preserves_agent_error_and_api_count(monkeypatch):
    import run_agent

    class ErrorAgent:
        iteration_budget = None

        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            return {
                "final_response": "partial",
                "messages": [{"role": "assistant", "content": "partial"}],
                "api_calls": 3,
                "error": "provider failed",
            }

    monkeypatch.setattr(run_agent, "AIAgent", ErrorAgent)

    result = runner.run_scenario_live(
        {"user_message": "hello", "config_overrides": "invalid"},
        "provider",
        "model",
    )

    assert result["error"] == "provider failed"
    assert result["api_calls"] == 3
    assert result["messages"][0]["role"] == "assistant"


def test_quiet_suppresses_per_scenario_progress(tmp_path, capsys):
    suite = _write_suite(
        tmp_path,
        "quiet_contract",
        [
            {
                "id": "Q1",
                "description": "deterministic quiet scenario",
                "user_message": "hello",
                "pass_conditions": [{"type": "response_contains", "value": "done"}],
                "_mock_final_response": "done",
                "_mock_messages": [{"role": "assistant", "content": "done"}],
            }
        ],
    )

    report = runner.run_suite(suite, deterministic_only=True, quiet=True)

    captured = capsys.readouterr()
    assert report["passed"] == 1
    assert "[1/1]" not in captured.err


def test_empty_suite_exits_cleanly_without_traceback(tmp_path, monkeypatch, capsys):
    suite = _write_suite(tmp_path, "empty", [])
    output = tmp_path / "empty-report.json"

    code = _run_main(
        monkeypatch,
        [
            "--suite",
            suite.stem,
            "--suites-dir",
            str(suite.parent),
            "--output",
            str(output),
            "--deterministic-only",
        ],
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err
    assert "Total:   0" in captured.out
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["error"] == "no scenarios"
    assert report["total"] == 0
    assert report["errored"] == 1


@pytest.mark.parametrize(
    ("yaml_value", "expected_error"),
    [
        ([{"name": "wrong-root"}], "suite root must be a mapping"),
        ({"name": 17, "scenarios": []}, "suite name must be a non-empty string"),
        ({"name": "wrong-scenarios", "scenarios": {}}, "scenarios must be a list"),
        ({"name": "wrong-scenario", "scenarios": ["oops"]}, "scenario 0 must be a mapping"),
        (
            {
                "name": "bad-metadata",
                "scenarios": [
                    {
                        "id": "S1",
                        "description": 17,
                        "user_message": "hello",
                        "pass_conditions": [],
                    }
                ],
            },
            "scenario 0 description must be a string",
        ),
        (
            {
                "name": "duplicate-id",
                "scenarios": [
                    {"id": "S1", "user_message": "a", "pass_conditions": []},
                    {"id": "S1", "user_message": "b", "pass_conditions": []},
                ],
            },
            "duplicate scenario id: S1",
        ),
    ],
)
def test_valid_yaml_with_wrong_shapes_fails_closed(tmp_path, yaml_value, expected_error):
    suite_path = tmp_path / "wrong-shape.yaml"
    suite_path.write_text(yaml.safe_dump(yaml_value), encoding="utf-8")

    report = runner.run_suite(suite_path, deterministic_only=True, quiet=True)

    assert report["errored"] == 1
    assert report["passed"] == 0
    assert expected_error in report["error"]


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("_mock_messages", "not-a-list", "_mock_messages must be a list"),
        ("_mock_messages", {"role": "assistant"}, "_mock_messages must be a list"),
        ("_mock_api_calls", True, "_mock_api_calls must be a non-negative integer"),
        ("_mock_api_calls", -1, "_mock_api_calls must be a non-negative integer"),
        ("_mock_api_calls", "one", "_mock_api_calls must be a non-negative integer"),
    ],
)
def test_invalid_deterministic_fixture_fields_fail_closed(
    tmp_path, field, value, expected_error
):
    scenario = {
        "id": "BAD_FIXTURE",
        "user_message": "hello",
        "pass_conditions": [{"type": "response_contains", "value": "done"}],
        "_mock_final_response": "done",
        field: value,
    }
    suite = _write_suite(tmp_path, "bad_fixture", [scenario])

    report = runner.run_suite(suite, deterministic_only=True, quiet=True)

    assert report["errored"] == 1
    assert report["passed"] == 0
    assert expected_error in report["scenarios"][0]["details"]["error"]


@pytest.mark.parametrize("raw_grade", [None, "pass", {}, {"pass": True}, {"pass": 1, "score": 1.0}])
def test_malformed_rubric_results_fail_closed(raw_grade):
    class BadRubric:
        @staticmethod
        def grade(_scenario, _result):
            return raw_grade

    grade = runner.grade_scenario(
        {"pass_conditions": [{"type": "response_contains", "value": "done"}]},
        {"messages": [], "final_response": "done"},
        rubric_module=BadRubric,
    )

    assert grade["pass"] is False
    assert grade["score"] == 0.0
    assert "rubric_error" in grade["details"]


def test_scalar_fallback_condition_fails_closed_without_crashing():
    grade = runner.grade_scenario(
        {"pass_conditions": ["response_contains"]},
        {"messages": [], "final_response": "done"},
        rubric_module=None,
    )

    assert grade["pass"] is False
    assert grade["score"] == 0.0
    assert grade["details"]["unsupported_conditions"] == ["<invalid>"]


def test_unknown_condition_fails_closed():
    grade = runner.grade_scenario(
        {"pass_conditions": [{"type": "made_up_condition"}]},
        {"messages": [], "final_response": ""},
        rubric_module=None,
    )

    assert grade["pass"] is False
    assert grade["score"] == 0.0
    assert grade["details"]["unsupported_conditions"] == ["made_up_condition"]


def test_missing_conditions_fail_closed():
    grade = runner.grade_scenario(
        {"pass_conditions": []},
        {"messages": [], "final_response": ""},
        rubric_module=None,
    )

    assert grade["pass"] is False
    assert grade["score"] == 0.0
    assert grade["details"]["error"] == "no pass conditions and no rubric"


def test_delegate_call_count_honors_maximum():
    result = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "delegate_task"}},
                    {"function": {"name": "delegate_task"}},
                ],
            }
        ]
    }
    grade = runner.grade_scenario(
        {"pass_conditions": [{"type": "delegate_call_count", "max": 1}]},
        result,
        rubric_module=None,
    )

    assert grade["pass"] is False
    assert grade["details"]["delegate_calls"] == 2


def test_deterministic_mode_errors_without_fixture_or_explicit_skip(tmp_path):
    suite = _write_suite(
        tmp_path,
        "missing_fixture",
        [
            {
                "id": "M1",
                "description": "forgot deterministic fixture",
                "user_message": "live-only task",
                "pass_conditions": [{"type": "response_contains", "value": "done"}],
            }
        ],
    )

    report = runner.run_suite(suite, deterministic_only=True, quiet=True)

    assert report["passed"] == 0
    assert report["errored"] == 1
    assert report["scenarios"][0]["pass"] is False
    assert "deterministic fixture" in report["scenarios"][0]["details"]["error"]


def test_all_explicitly_skipped_deterministic_scenarios_exit_zero(
    tmp_path, monkeypatch, capsys
):
    suite = _write_suite(
        tmp_path,
        "live_only",
        [
            {
                "id": "L1",
                "description": "requires a live provider",
                "user_message": "search the web",
                "deterministic_skip": True,
                "deterministic_skip_reason": "Tier 2 live provider required",
                "pass_conditions": [{"type": "response_contains", "value": "http"}],
            }
        ],
    )
    output = tmp_path / "live-only-report.json"

    code = _run_main(
        monkeypatch,
        [
            "--suite",
            suite.stem,
            "--suites-dir",
            str(suite.parent),
            "--output",
            str(output),
            "--deterministic-only",
            "--quiet",
        ],
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "[1/1]" not in captured.err
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] == 0
    assert report["errored"] == 0
    assert report["skipped"] == 1


def test_any_failed_scenario_makes_cli_exit_nonzero(
    tmp_path, monkeypatch, capsys
):
    suite = _write_suite(
        tmp_path,
        "partial_failure",
        [
            {
                "id": "P1",
                "description": "passes",
                "user_message": "hello",
                "pass_conditions": [{"type": "response_contains", "value": "done"}],
                "_mock_final_response": "done",
            },
            {
                "id": "P2",
                "description": "fails",
                "user_message": "hello",
                "pass_conditions": [{"type": "response_contains", "value": "expected"}],
                "_mock_final_response": "different",
            },
        ],
    )

    code = _run_main(
        monkeypatch,
        [
            "--suite",
            suite.stem,
            "--suites-dir",
            str(suite.parent),
            "--output",
            str(tmp_path / "partial-report.json"),
            "--deterministic-only",
            "--quiet",
        ],
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "Failed:  1" in captured.out
    assert "Traceback" not in captured.err


def test_missing_baseline_is_reported_without_keyerror(tmp_path, monkeypatch, capsys):
    suite = _write_suite(
        tmp_path,
        "baseline_contract",
        [
            {
                "id": "B1",
                "description": "known deterministic result",
                "user_message": "hello",
                "pass_conditions": [{"type": "response_contains", "value": "done"}],
                "_mock_final_response": "done",
                "_mock_messages": [{"role": "assistant", "content": "done"}],
            }
        ],
    )

    code = _run_main(
        monkeypatch,
        [
            "--suite",
            suite.stem,
            "--suites-dir",
            str(suite.parent),
            "--output",
            str(tmp_path / "report.json"),
            "--baseline",
            str(tmp_path / "does-not-exist.json"),
            "--deterministic-only",
            "--quiet",
        ],
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "no_baseline" in captured.out
    assert "Traceback" not in captured.err


def test_malformed_suite_exits_cleanly(tmp_path, monkeypatch, capsys):
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir()
    (suites_dir / "malformed.yaml").write_text("name: [broken\n", encoding="utf-8")

    code = _run_main(
        monkeypatch,
        [
            "--suite",
            "malformed",
            "--suites-dir",
            str(suites_dir),
            "--output",
            str(tmp_path / "malformed-report.json"),
            "--deterministic-only",
        ],
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err
    assert "ERROR loading" in captured.err


def test_live_tier_suites_explicitly_skip_deterministic_mode():
    for suite_name in ("code_task", "research_citation"):
        suite = yaml.safe_load(
            (runner._EVALS_DIR / "suites" / f"{suite_name}.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert suite["scenarios"]
        assert all(s.get("deterministic_skip") is True for s in suite["scenarios"])
        assert all(s.get("deterministic_skip_reason") for s in suite["scenarios"])


def _suite_scenario(suite_name: str, scenario_id: str) -> dict:
    suite = yaml.safe_load(
        (runner._EVALS_DIR / "suites" / f"{suite_name}.yaml").read_text(
            encoding="utf-8"
        )
    )
    return next(s for s in suite["scenarios"] if s["id"] == scenario_id)


def test_orchestration_rubric_rejects_empty_or_errored_result():
    from evals.rubrics import orchestration

    scenario = _suite_scenario("orchestration", "O3_no_spawn_trivial")
    empty = orchestration.grade(
        scenario,
        {"final_response": "", "messages": [], "error": None},
    )
    errored = orchestration.grade(
        scenario,
        {"final_response": "4", "messages": [], "error": "provider failed"},
    )

    assert empty["pass"] is False
    assert "evidence" in empty["details"]["error"]
    assert errored["pass"] is False
    assert errored["details"]["error"] == "provider failed"


def test_subagent_verify_requires_real_delegation_and_verification():
    from evals.rubrics import subagent_verify

    scenario = _suite_scenario("subagent_verify", "S4_verification_cheap")
    grade = subagent_verify.grade(
        scenario,
        {"final_response": "looks good", "messages": [], "error": None},
    )

    assert grade["pass"] is False
    assert grade["details"]["delegate_calls"] == 0
    assert grade["details"]["verified_delegates"] == 0


def test_cost_cache_requires_multi_turn_snapshot_evidence():
    from evals.rubrics import cost_cache

    scenario = _suite_scenario("cost_cache", "E1_cache_stable")
    empty = cost_cache.grade(
        scenario,
        {"final_response": "fact", "messages": [], "error": None},
    )
    one_snapshot = cost_cache.grade(
        scenario,
        {
            "final_response": "fact",
            "messages": [{"role": "system", "content": "stable"}],
            "api_call_snapshots": [
                {"messages": [{"role": "system", "content": "stable"}], "tools": []}
            ],
            "error": None,
        },
    )

    assert empty["pass"] is False
    assert "snapshot" in empty["details"]["error"]
    assert one_snapshot["pass"] is False
    assert "at least 2" in one_snapshot["details"]["error"]


@pytest.mark.parametrize(
    "scenario_id",
    ["W1_encoding", "W2_longpath", "W3_home_spaces", "W4_unicode_arg"],
)
def test_windows_rubric_rejects_empty_evidence(scenario_id):
    from evals.rubrics import windows_reliability

    scenario = _suite_scenario("windows_reliability", scenario_id)
    grade = windows_reliability.grade(
        scenario,
        {"final_response": "", "messages": [], "error": None},
    )

    assert grade["pass"] is False
    assert "evidence" in grade["details"]["error"]


@pytest.mark.parametrize(
    ("suite_name", "scenario_id"),
    [
        ("memory_recall", "M1_cross_session"),
        ("code_task", "C2_feature_tdd"),
        ("research_citation", "R1_single_source"),
    ],
)
def test_suite_rubrics_fail_closed_on_unknown_conditions(suite_name, scenario_id):
    module = __import__(f"evals.rubrics.{suite_name}", fromlist=["grade"])
    scenario = dict(_suite_scenario(suite_name, scenario_id))
    scenario["pass_conditions"] = [{"type": "made_up_condition"}]
    result = {
        "final_response": "pass https://python.org",
        "messages": [
            {
                "role": "tool",
                "name": "terminal",
                "content": "1 passed in 0.01s",
            }
        ],
        "error": None,
    }

    grade = module.grade(scenario, result)

    assert grade["pass"] is False
    assert "unsupported_conditions" in grade["details"]


def test_required_runtime_probe_failure_fails_suite_closed(tmp_path):
    suite = _write_suite(
        tmp_path,
        "runtime_probe_contract",
        [
            {
                "id": "RP1",
                "description": "rubric fixture passes but runtime probe must gate it",
                "user_message": "hello",
                "pass_conditions": [{"type": "response_contains", "value": "done"}],
                "_mock_final_response": "done",
            }
        ],
    )
    data = yaml.safe_load(suite.read_text(encoding="utf-8"))
    data["runtime_probe"] = "does_not_exist"
    suite.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    report = runner.run_suite(suite, deterministic_only=True, quiet=True)

    assert report["passed"] == 1  # fixture grading remains visible
    assert report["runtime_probe"]["pass"] is False
    assert report["errored"] == 1


def test_shipped_tier1_suites_require_real_runtime_probes():
    tier1 = {
        "orchestration",
        "cost_cache",
        "subagent_verify",
        "memory_recall",
        "windows_reliability",
    }
    for suite_name in tier1:
        suite = yaml.safe_load(
            (runner._EVALS_DIR / "suites" / f"{suite_name}.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert suite.get("runtime_probe") == suite_name


@pytest.mark.parametrize(
    "probe_name",
    [
        "orchestration",
        "cost_cache",
        "subagent_verify",
        "memory_recall",
        "windows_reliability",
    ],
)
def test_runtime_probes_exercise_production_paths_without_api_calls(probe_name):
    from evals.runtime_probes import run_runtime_probe

    result = run_runtime_probe(probe_name)

    assert result["pass"] is True, result
    assert result["api_calls"] == 0
    assert result["production_modules"]
    assert all(
        name.startswith(("agent.", "tools.", "hermes_cli.", "model_tools"))
        for name in result["production_modules"]
    )


def test_unknown_runtime_probe_fails_closed_without_raising():
    from evals.runtime_probes import run_runtime_probe

    result = run_runtime_probe("does_not_exist")

    assert result["pass"] is False
    assert result["api_calls"] == 0
    assert "unknown runtime probe" in result["details"]["error"]


def test_runtime_probes_restore_preexisting_global_state(monkeypatch):
    """A probe must restore caller state, not merely clear it."""
    import model_tools
    from evals.runtime_probes import run_runtime_probe
    from hermes_constants import get_hermes_home
    from hermes_cli import config as config_module
    from tools import registry as registry_module

    home_before = get_hermes_home()
    sentinel_key = ("preexisting",)
    sentinel_value = [{"type": "function", "function": {"name": "sentinel"}}]
    sentinel_check = lambda: True
    monkeypatch.setattr(
        model_tools,
        "_tool_defs_cache",
        {sentinel_key: sentinel_value},
    )
    monkeypatch.setattr(model_tools, "_last_resolved_tool_names", ["sentinel"])
    monkeypatch.setattr(
        registry_module,
        "_check_fn_cache",
        {sentinel_check: (1.0, True)},
    )
    monkeypatch.setattr(
        registry_module,
        "_check_fn_last_good",
        {sentinel_check: 1.0},
    )
    monkeypatch.setattr(
        config_module,
        "_LAST_EXPANDED_CONFIG_BY_PATH",
        {"sentinel": {"value": 1}},
    )
    monkeypatch.setattr(config_module, "_LOAD_CONFIG_CACHE", {})
    monkeypatch.setattr(config_module, "_RAW_CONFIG_CACHE", {})
    monkeypatch.setattr(
        config_module,
        "_env_cache",
        (("sentinel", None, None), {"KEY": "value"}),
    )

    result = run_runtime_probe("cost_cache")

    assert result["pass"] is True, result
    assert get_hermes_home() == home_before
    assert model_tools._tool_defs_cache == {sentinel_key: sentinel_value}
    assert model_tools._last_resolved_tool_names == ["sentinel"]
    with registry_module._check_fn_cache_lock:
        assert registry_module._check_fn_cache == {sentinel_check: (1.0, True)}
        assert registry_module._check_fn_last_good == {sentinel_check: 1.0}
    with config_module._CONFIG_LOCK:
        assert config_module._LAST_EXPANDED_CONFIG_BY_PATH == {
            "sentinel": {"value": 1}
        }
        assert config_module._LOAD_CONFIG_CACHE == {}
        assert config_module._RAW_CONFIG_CACHE == {}
        assert config_module._env_cache == (
            ("sentinel", None, None),
            {"KEY": "value"},
        )


def test_file_probes_never_inherit_external_terminal_backend(monkeypatch):
    """Tier-1 file probes stay local even when the caller normally uses Docker."""
    import copy

    from evals.runtime_probes import run_runtime_probe
    from tools import file_tools, terminal_tool
    from tools.file_state import get_registry

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    active_before = dict(terminal_tool._active_environments)
    activity_before = dict(terminal_tool._last_activity)
    creation_locks_before = dict(terminal_tool._creation_locks)
    overrides_before = copy.deepcopy(terminal_tool._task_env_overrides)
    cwd_before = dict(terminal_tool._session_cwd)
    file_ops_before = dict(file_tools._file_ops_cache)
    read_tracker_before = copy.deepcopy(file_tools._read_tracker)
    patch_tracker_before = copy.deepcopy(file_tools._patch_failure_tracker)
    max_read_before = file_tools._max_read_chars_cached
    config_resolved_before = file_tools._hermes_config_resolved
    config_loaded_before = file_tools._hermes_config_resolved_loaded
    registry = get_registry()
    with registry._state_lock:
        registry_reads_before = copy.deepcopy(registry._reads)
        registry_writers_before = dict(registry._last_writer)
    with registry._meta_lock:
        registry_locks_before = dict(registry._path_locks)
    file_tools._max_read_chars_cached = None
    monkeypatch.setattr(
        terminal_tool,
        "_create_environment",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("external backend factory must not run")
        ),
    )

    try:
        result = run_runtime_probe("windows_reliability")

        assert result["pass"] is True, result
        assert terminal_tool._active_environments == active_before
        assert terminal_tool._last_activity == activity_before
        assert terminal_tool._creation_locks == creation_locks_before
        assert terminal_tool._task_env_overrides == overrides_before
        assert terminal_tool._session_cwd == cwd_before
        assert file_tools._file_ops_cache == file_ops_before
        assert file_tools._read_tracker == read_tracker_before
        assert file_tools._patch_failure_tracker == patch_tracker_before
        assert file_tools._max_read_chars_cached is None
        assert file_tools._hermes_config_resolved == config_resolved_before
        assert file_tools._hermes_config_resolved_loaded == config_loaded_before
        with registry._state_lock:
            assert registry._reads == registry_reads_before
            assert registry._last_writer == registry_writers_before
        with registry._meta_lock:
            assert registry._path_locks == registry_locks_before
    finally:
        file_tools._max_read_chars_cached = max_read_before


def test_file_probe_constructor_failure_restores_registered_state(monkeypatch):
    """Partial setup must roll back overrides even if LocalEnvironment init fails."""
    from evals.runtime_probes import run_runtime_probe
    from tools import terminal_tool
    from tools.environments import local as local_module

    active_before = dict(terminal_tool._active_environments)
    activity_before = dict(terminal_tool._last_activity)
    creation_locks_before = dict(terminal_tool._creation_locks)
    overrides_before = dict(terminal_tool._task_env_overrides)
    cwd_before = dict(terminal_tool._session_cwd)
    monkeypatch.setattr(
        local_module,
        "LocalEnvironment",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )

    result = run_runtime_probe("windows_reliability")

    assert result["pass"] is False
    assert "constructor failed" in result["details"]["error"]
    assert terminal_tool._active_environments == active_before
    assert terminal_tool._last_activity == activity_before
    assert terminal_tool._creation_locks == creation_locks_before
    assert terminal_tool._task_env_overrides == overrides_before
    assert terminal_tool._session_cwd == cwd_before


@pytest.mark.parametrize("invalid_name", ["", None, 17, [], {}])
def test_declared_runtime_probe_name_must_be_nonempty_string(tmp_path, invalid_name):
    suite = _write_suite(
        tmp_path,
        "invalid_probe_name",
        [{
            "id": "RP1",
            "user_message": "hello",
            "pass_conditions": [{"type": "response_contains", "value": "done"}],
            "_mock_final_response": "done",
        }],
    )
    data = yaml.safe_load(suite.read_text(encoding="utf-8"))
    data["runtime_probe"] = invalid_name
    suite.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    report = runner.run_suite(suite, deterministic_only=True, quiet=True)

    assert report["errored"] == 1
    assert report["runtime_probe"]["pass"] is False
    assert "non-empty string" in report["runtime_probe"]["details"]["error"]


def test_runtime_probe_runs_only_in_deterministic_mode(tmp_path, monkeypatch):
    suite = _write_suite(
        tmp_path,
        "live_probe_isolation",
        [{
            "id": "L1",
            "user_message": "hello",
            "pass_conditions": [{"type": "response_contains", "value": "done"}],
        }],
    )
    data = yaml.safe_load(suite.read_text(encoding="utf-8"))
    data["runtime_probe"] = "does_not_exist"
    suite.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "run_scenario_live",
        lambda *_args, **_kwargs: {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
            "error": None,
        },
    )

    report = runner.run_suite(suite, deterministic_only=False, quiet=True)

    assert report["errored"] == 0
    assert report["passed"] == 1
    assert "runtime_probe" not in report


@pytest.mark.parametrize(
    ("raw_result", "expected_error"),
    [
        (None, "must return a dict"),
        ({"pass": "yes", "api_calls": 0, "production_modules": ["tools.x"], "details": {}}, "boolean"),
        ({"pass": True, "api_calls": 1, "production_modules": ["tools.x"], "details": {}}, "zero API calls"),
        ({"pass": True, "api_calls": 0, "production_modules": [], "details": {}}, "production_modules"),
        ({"pass": True, "api_calls": 0, "production_modules": ["tools.x"], "details": "ok"}, "details"),
    ],
)
def test_runtime_probe_results_are_validated_and_fail_closed(monkeypatch, raw_result, expected_error):
    from evals import runtime_probes

    monkeypatch.setitem(runtime_probes._PROBES, "malformed", lambda: raw_result)

    result = runtime_probes.run_runtime_probe("malformed")

    assert result["pass"] is False
    assert result["api_calls"] == 0
    assert expected_error in result["details"]["error"]
