"""Workflow-level trust-boundary and exact-selection tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "label-rerun.yml"
EXPECTED_WORKFLOW = "ci.yaml"
EXPECTED_RUN = """set -euo pipefail
python3 scripts/ci/pipeline_state.py \\
  --repo \"$REPO\" \\
  --pr \"$PR_NUMBER\" \\
  --workflow \"$WORKFLOW_FILE\" \\
  --required-context \"$REQUIRED_CONTEXT\" \\
  --event-head-sha \"$EVENT_HEAD_SHA\" \\
  --json"""


def _workflow():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    # PyYAML 1.1 parses an unquoted GitHub `on` key as True.
    return data, data.get("on", data.get(True))


def _job(workflow):
    return workflow["jobs"]["rerun-review-labels"]


def _run_step(workflow):
    return next(step for step in _job(workflow)["steps"] if "run" in step)


def _assert_trusted_control_plane(workflow):
    """Strict oracle: only the known base-owned control steps are permitted."""
    _, trigger = _workflow_from(workflow)
    assert set(trigger) == {"pull_request_target"}
    job = _job(workflow)
    assert set(job) == {"name", "if", "runs-on", "timeout-minutes", "steps"}
    assert len(job["steps"]) == 2
    checkout, run = job["steps"]
    assert set(checkout) == {"name", "uses", "with"}
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}",
        "persist-credentials": False,
    }
    assert set(run) == {"name", "env", "run"}
    assert run["name"] == "Read exact provider state and rerun when eligible"
    assert run["run"].strip() == EXPECTED_RUN
    assert set(run["env"]) == {
        "GH_TOKEN", "REPO", "PR_NUMBER", "EVENT_HEAD_SHA", "BASE_SHA", "WORKFLOW_FILE", "REQUIRED_CONTEXT"
    }
    assert "head.sha" not in str(checkout)
    assert "git fetch" not in run["run"]
    assert "git checkout" not in run["run"]


def _workflow_from(workflow):
    yaml = pytest.importorskip("yaml")
    return workflow, workflow.get("on", workflow.get(True))


def test_workflow_is_base_owned_and_uses_the_real_ci_yaml():
    workflow, trigger = _workflow()
    assert "pull_request_target" in trigger
    assert (ROOT / ".github" / "workflows" / EXPECTED_WORKFLOW).is_file()
    assert not (ROOT / ".github" / "workflows" / "ci.yml").exists()
    job = _job(workflow)
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert "head.sha" not in str(checkout)
    env = _run_step(workflow)["env"]
    assert env["WORKFLOW_FILE"] == EXPECTED_WORKFLOW
    assert env["REQUIRED_CONTEXT"] == "All required checks pass"


def test_workflow_grants_minimum_explicit_checks_read_permission():
    workflow, _ = _workflow()
    assert workflow["permissions"] == {
        "contents": "read",
        "checks": "read",
        "actions": "write",
        "pull-requests": "read",
    }


def test_workflow_command_body_is_exact_and_no_pr_head_execution_surface_exists():
    workflow, _ = _workflow()
    _assert_trusted_control_plane(workflow)
    run = _run_step(workflow)
    assert run["env"]["EVENT_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert "github.event.pull_request.head.sha" not in str(workflow["jobs"]["rerun-review-labels"]["steps"][0])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w["jobs"]["rerun-review-labels"]["steps"].append({"run": "git fetch origin \"$EVENT_HEAD_SHA\""}),
        lambda w: w["jobs"]["rerun-review-labels"]["steps"][1].update({"run": EXPECTED_RUN + "\ngit fetch origin \\\"$EVENT_HEAD_SHA\\\""}),
    ],
)
def test_mutation_oracle_rejects_appended_pr_head_paths(mutate):
    workflow, _ = _workflow()
    mutated = deepcopy(workflow)
    if mutate.__name__ == "<lambda>":
        # The second mutation is applied explicitly below; this branch keeps
        # the parametrized cases readable without source-text assertions.
        pass
    mutate(mutated)
    with pytest.raises(AssertionError):
        _assert_trusted_control_plane(mutated)


def test_appended_checkout_and_execution_commands_are_rejected():
    workflow, _ = _workflow()
    for payload in (
        {"run": "git checkout \"$EVENT_HEAD_SHA\""},
        {"run": "python3 /tmp/pr-head.py"},
        {"run": "git fetch origin \"$EVENT_HEAD_SHA\""},
    ):
        mutated = deepcopy(workflow)
        mutated["jobs"]["rerun-review-labels"]["steps"].append(payload)
        with pytest.raises(AssertionError):
            _assert_trusted_control_plane(mutated)


def test_job_level_pr_head_execution_surfaces_are_rejected():
    workflow, _ = _workflow()
    for key, value in (
        ("container", {"image": "${{ github.event.pull_request.head.label }}"}),
        ("services", {"hostile": {"image": "${{ github.event.pull_request.head.label }}"}}),
    ):
        mutated = deepcopy(workflow)
        mutated["jobs"]["rerun-review-labels"][key] = value
        with pytest.raises(AssertionError):
            _assert_trusted_control_plane(mutated)


def test_step_uses_and_head_checkout_mutants_are_rejected():
    workflow, _ = _workflow()
    uses_mutant = deepcopy(workflow)
    uses_mutant["jobs"]["rerun-review-labels"]["steps"].append({"uses": "actions/checkout@deadbeef"})
    with pytest.raises(AssertionError):
        _assert_trusted_control_plane(uses_mutant)

    head_checkout = deepcopy(workflow)
    head_checkout["jobs"]["rerun-review-labels"]["steps"][0]["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"
    with pytest.raises(AssertionError):
        _assert_trusted_control_plane(head_checkout)


def test_no_error_swallowing_or_done_status_in_workflow_body():
    workflow, _ = _workflow()
    text = str(workflow)
    assert "|| true" not in text
    assert "set -euo pipefail" in text
    assert "Done" not in text
    assert "RERUN_REQUESTED" not in text
