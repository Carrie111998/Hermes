"""Tests für den read-only Upcaster der Async-Delegation."""

from tools.delegation_upcaster import (
    upcast_async_task,
    upcast_async_result,
)


def test_upcasts_single_async_task_to_lane_task():
    task = upcast_async_task(
        {
            "delegation_id": "deleg_001",
            "goal": "Prüfe Contracts",
            "context": "Nur lesend",
            "role": "leaf",
            "model": "MiniMax-M3",
            "is_batch": False,
        }
    )

    assert task.task_id == "deleg_001"
    assert task.goal == "Prüfe Contracts"
    assert task.context == "Nur lesend"
    assert task.role == "leaf"


def test_upcasts_batch_async_task_with_index():
    task = upcast_async_task(
        {
            "delegation_id": "deleg_002",
            "goals": ["Erstes Ziel", "Zweites Ziel"],
            "role": "leaf",
            "is_batch": True,
        },
        task_index=1,
    )

    assert task.task_id == "deleg_002:task-1"
    assert task.goal == "Zweites Ziel"


def test_upcasts_completed_result_and_preserves_summary():
    result = upcast_async_result(
        {
            "status": "completed",
            "summary": "Alles geprüft",
            "artifacts": ["/tmp/report.md"],
            "verification": ["6 Tests bestanden"],
        },
        task_id="deleg_003",
    )

    assert result.task_id == "deleg_003"
    assert result.status == "completed"
    assert result.summary == "Alles geprüft"
    assert result.artifacts == ["/tmp/report.md"]


def test_upcasts_native_batch_result_shape():
    result = upcast_async_result(
        {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "Erstes Ziel erledigt"},
                {"task_index": 1, "status": "completed", "summary": "Zweites Ziel erledigt"},
            ]
        },
        task_id="deleg_005",
        task_index=1,
    )

    assert result.status == "completed"
    assert result.summary == "Zweites Ziel erledigt"


def test_upcasts_unknown_or_missing_summary_to_blocked_with_error():
    result = upcast_async_result(
        {"status": "unknown", "summary": None, "error": "Worker beendet"},
        task_id="deleg_004",
    )

    assert result.status == "blocked"
    assert result.summary == "Delegationsergebnis konnte nicht verifiziert werden"
    assert "Worker beendet" in result.error


def test_upcaster_returns_diagnostics_for_malformed_payload():
    task = upcast_async_task(
        {"delegation_id": "deleg_006", "goal": "", "role": "unexpected"}
    )

    assert task is None
