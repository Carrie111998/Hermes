import json

from agent.orchestration.agent_runner import OrchestrationLog, run_development_workflow
from agent.orchestration.task_context import TaskContext


class Parent:
    session_id = "parent-test"


def _delegate_from(summaries):
    calls = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        summary = summaries.pop(0)
        return json.dumps(
            {
                "results": [
                    {
                        "task_index": 0,
                        "status": "completed",
                        "summary": summary,
                        "api_calls": 1,
                        "duration_seconds": 0.01,
                    }
                ],
                "total_duration_seconds": 0.01,
            }
        )

    fake_delegate.calls = calls
    return fake_delegate


def test_development_workflow_runs_developer_tester_reviewer(tmp_path):
    delegate = _delegate_from(
        [
            "DEVELOPER RESULT\nStatus: DONE\nGeänderte Dateien: app.py",
            "TEST PASSED\nIssue: #123\nTests: pytest",
            "REVIEW RESULT\nStatus: APPROVED\nFindings: keine",
        ]
    )
    ctx = TaskContext.from_mapping(
        {
            "issue_id": "#123",
            "issue_title": "Fix bug",
            "repository": "/repo",
            "branch": "feature",
            "acceptance_criteria": ["works"],
        }
    )

    result = run_development_workflow(
        task_context=ctx,
        delegate_fn=delegate,
        parent_agent=Parent(),
        objective="Bearbeite #123",
        log=OrchestrationLog(root=tmp_path),
    )

    assert result["status"] == "APPROVED"
    assert [call["role"] for call in delegate.calls] == ["leaf", "leaf", "leaf"]
    assert "developer" in result["latest"]
    assert result["log_path"].startswith(str(tmp_path))
    assert len(tmp_path.joinpath(result["run_id"] + ".jsonl").read_text().splitlines()) == 5


def test_development_workflow_retries_after_test_failure(tmp_path):
    delegate = _delegate_from(
        [
            "DEVELOPER RESULT\nStatus: DONE\nÄnderungen: erster Fix",
            "TEST FAILED\nIssue: #123\nExpected: ok\nActual: kaputt",
            "DEBUG RESULT\nHIGH CONFIDENCE: erster Fix unvollständig",
            "DEVELOPER RESULT\nStatus: DONE\nÄnderungen: zweiter Fix",
            "TEST PASSED\nIssue: #123\nTests: pytest",
            "REVIEW RESULT\nStatus: APPROVED",
        ]
    )
    ctx = TaskContext.from_mapping({"issue_id": "#123", "issue_title": "Fix bug"})

    result = run_development_workflow(
        task_context=ctx,
        delegate_fn=delegate,
        parent_agent=Parent(),
        objective="Bearbeite #123",
        max_correction_loops=2,
        log=OrchestrationLog(root=tmp_path),
    )

    assert result["status"] == "APPROVED"
    assert result["loop_guard"]["attempts"] == 2
    assert len(delegate.calls) == 6
    assert delegate.calls[2]["goal"].startswith("Debug-Agent")


def test_development_workflow_stops_when_developer_contract_is_blocked(tmp_path):
    delegate = _delegate_from(
        [
            "DEVELOPER RESULT\nStatus: BLOCKED\nProblem: fehlende Rechte",
            "TEST PASSED\nIssue: #123",
            "REVIEW RESULT\nStatus: APPROVED",
        ]
    )
    ctx = TaskContext.from_mapping({"issue_id": "#123", "issue_title": "Fix bug"})

    result = run_development_workflow(
        task_context=ctx,
        delegate_fn=delegate,
        parent_agent=Parent(),
        objective="Bearbeite #123",
        max_correction_loops=3,
        log=OrchestrationLog(root=tmp_path),
    )

    assert result["status"] == "BLOCKED"
    assert result["latest"]["developer"].startswith("DEVELOPER RESULT")
    assert len(delegate.calls) == 1


def test_development_workflow_stops_at_loop_limit(tmp_path):
    delegate = _delegate_from(
        [
            "DEVELOPER RESULT\nStatus: DONE",
            "TEST PASSED\nIssue: #123",
            "REVIEW RESULT\nStatus: CHANGES REQUESTED\nFindings: missing edge case",
            "DEBUG RESULT\nHIGH CONFIDENCE: Review-Finding reproduziert",
        ]
    )
    ctx = TaskContext.from_mapping({"issue_id": "#123", "issue_title": "Fix bug"})

    result = run_development_workflow(
        task_context=ctx,
        delegate_fn=delegate,
        parent_agent=Parent(),
        objective="Bearbeite #123",
        max_correction_loops=1,
        log=OrchestrationLog(root=tmp_path),
    )

    assert result["status"] == "LOOP_LIMIT_REACHED"
    assert result["loop_guard"]["attempts"] == 1
