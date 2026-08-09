"""Regression guards for CI workflows that inspect historical Git objects."""

from pathlib import Path

import yaml


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "tests.yml"
)


def test_python_test_job_checkout_preserves_historical_git_objects():
    """The Python test job must support tests that read pinned Git history."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    checkout_steps = [
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]

    assert len(checkout_steps) == 1
    assert checkout_steps[0].get("with", {}).get("fetch-depth") == 0
