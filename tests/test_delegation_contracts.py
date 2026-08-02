"""Vertragstests für den isolierten Delegation-Contracts-Piloten."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tools.delegation_contracts import (
    LaneResult,
    LaneTask,
    ReviewDecision,
    validate_contract,
)


SCHEMA_DIR = Path(__file__).parents[1] / "schemas" / "delegation"


def test_lane_task_accepts_minimal_strict_payload():
    task = LaneTask.model_validate({
        "task_id": "task-001",
        "goal": "Prüfe den Delegationsvertrag",
        "role": "leaf",
    })

    assert task.task_id == "task-001"
    assert task.role == "leaf"


def test_lane_task_rejects_unknown_fields_and_blank_goal():
    with pytest.raises(ValidationError):
        LaneTask.model_validate({
            "task_id": "task-001",
            "goal": " ",
            "role": "leaf",
            "unexpected": True,
        })


def test_lane_result_requires_explicit_status_and_summary():
    result = LaneResult.model_validate({
        "task_id": "task-001",
        "status": "completed",
        "summary": "Schema-Prüfung erfolgreich",
    })

    assert result.status == "completed"

    with pytest.raises(ValidationError):
        LaneResult.model_validate({
            "task_id": "task-001",
            "status": "completed",
        })


def test_review_decision_rejects_unknown_decision():
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({
            "task_id": "task-001",
            "decision": "maybe",
            "rationale": "Unklar",
        })


def test_validate_contract_reports_contract_type_errors():
    valid, errors = validate_contract(
        "LaneTask",
        {"task_id": "task-001", "goal": "Prüfen", "role": "leaf"},
    )
    assert valid is True
    assert errors == []

    valid, errors = validate_contract(
        "LaneTask",
        {"task_id": "task-001", "goal": "", "role": "leaf"},
    )
    assert valid is False
    assert errors


def test_schema_artifacts_exist_and_are_valid_json_schema():
    for name in ("LaneTask", "LaneResult", "ReviewDecision"):
        path = SCHEMA_DIR / f"{name}.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"].startswith("https://json-schema.org/")
        assert schema["title"] == name
        assert schema["additionalProperties"] is False
        assert schema["required"]
