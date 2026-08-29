"""Regression tests for the CI platform and selector contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
_CLASSIFIER_PATH = REPO_ROOT / "scripts" / "ci" / "classify_changes.py"
_CLASSIFIER_SPEC = importlib.util.spec_from_file_location(
    "classify_changes", _CLASSIFIER_PATH
)
if _CLASSIFIER_SPEC is None or _CLASSIFIER_SPEC.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_CLASSIFIER = importlib.util.module_from_spec(_CLASSIFIER_SPEC)
_CLASSIFIER_SPEC.loader.exec_module(_CLASSIFIER)
classify = _CLASSIFIER.classify


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_script(workflow: dict, step_name: str) -> str:
    for step in workflow["jobs"]["os-tests"]["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(f"missing step {step_name!r}")


def test_workflow_change_activates_python_and_ci_review_lanes():
    """The changed lint workflow must reach its Python and review lanes."""
    lanes = classify([".github/workflows/lint.yml"])

    assert lanes["python"] is True
    assert lanes["ci_review"] is True


def test_windows_footgun_guard_uses_native_windows_and_keeps_check_name():
    """The Windows-focused blocking guard must not be hosted on Linux."""
    workflow = _load_workflow("lint.yml")
    job = workflow["jobs"]["windows-footguns"]

    assert job["name"] == "Windows footguns (blocking)"
    assert job["runs-on"] == "windows-latest"

    checker_steps = [
        step for step in job["steps"] if step.get("name") == "Run footgun checker"
    ]
    assert len(checker_steps) == 1
    assert checker_steps[0]["name"] == "Run footgun checker"
    assert checker_steps[0]["run"] == (
        "python scripts/check-windows-footguns.py --all"
    )


def test_os_matrix_binds_windows_marker_to_native_runner():
    """The Windows-only pytest marker has a real Windows execution lane."""
    workflow = _load_workflow("tests-os.yml")
    matrix = workflow["jobs"]["os-tests"]["strategy"]["matrix"]["include"]

    by_marker = {entry["marker"]: entry for entry in matrix}
    assert by_marker["windows_only"]["runner"] == "windows-latest"
    assert by_marker["windows_only"]["name"] == "Windows-only tests"
    assert by_marker["macos_only"]["runner"] == "macos-latest"


def test_os_selector_fails_closed_for_empty_selection():
    """A renamed marker or empty pytest selection must fail the job."""
    workflow = _load_workflow("tests-os.yml")
    run = _run_script(workflow, "Run ${{ matrix.marker }} tests")

    assert "scripts/ci/list_os_marked_tests.py" in run
    assert 'if [ ! -s "$LIST" ]' in run
    assert 'if [ "$status" -eq 5 ]' in run
    assert '-m "${{ matrix.marker }} and not integration"' in run
