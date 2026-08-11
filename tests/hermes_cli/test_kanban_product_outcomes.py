"""Canonical product-workflow outcome fixtures and validation regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "kanban" / "product_outcomes"


def _production_envelope(run_id: int) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / f"run_{run_id}.json").read_text(encoding="utf-8"))


def test_production_run_407_has_marker_without_canonical_outcome():
    row = _production_envelope(407)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_304_is_an_independent_missing_canonical_occurrence():
    row = _production_envelope(304)
    assert row["task_id"] != _production_envelope(407)["task_id"]
    assert row["epic_id"] != _production_envelope(407)["epic_id"]
    assert row["outcome"] == "advanced"
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


@pytest.mark.parametrize("run_id", [354, 369])
def test_production_preflight_repairs_are_non_verdict_terminal_runs(run_id):
    row = _production_envelope(run_id)
    assert row["step_key"] == "test"
    assert row["outcome"] == "preflight_repaired"
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_410_has_marker_and_canonical_outcome():
    row = _production_envelope(410)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert row["metadata"]["workflow_outcome"] == {"verdict": "approved"}
