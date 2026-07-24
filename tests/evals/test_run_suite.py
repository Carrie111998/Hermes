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
