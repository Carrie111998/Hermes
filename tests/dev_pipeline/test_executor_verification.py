"""Verification classification tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import dev_executor as ex


def _result(cmd: str, code: int) -> ex.CommandResult:
    return ex.CommandResult(command=cmd, exit_code=code, output_path=Path("/tmp/x.log"))


def test_candidate_pass():
    assert ex.classify_verification([_result("true", 0)]) == "pass"


def test_candidate_fail_base_fail_baseline():
    cand = [_result("pytest", 1)]
    base = [_result("pytest", 1)]
    assert ex.classify_verification(cand, base) == "baseline_failure"


def test_candidate_fail_base_pass_regression():
    cand = [_result("pytest", 1)]
    base = [_result("pytest", 0)]
    assert ex.classify_verification(cand, base) == "regression"


def test_repair_prompt_contains_failure_evidence():
    results = [
        ex.CommandResult(
            command="pytest tests/foo.py",
            exit_code=1,
            output_path=Path("/tmp/log"),
            output_preview="AssertionError: boom",
        )
    ]
    prompt = ex.build_repair_prompt("fix foo", {"task_summary": "x"}, results, "diff here")
    assert "pytest tests/foo.py" in prompt
    assert "AssertionError: boom" in prompt
    assert "diff here" in prompt
