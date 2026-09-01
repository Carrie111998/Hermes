from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from hermes_constants import get_hermes_home

from .loop_guard import LoopGuard
from .task_context import TaskContext, sanitize_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrchestrationLog:
    """Append-only JSONL audit log for one orchestration run."""

    def __init__(self, run_id: str | None = None, root: Path | None = None):
        self.run_id = run_id or f"orch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        self.root = root or (Path(get_hermes_home()) / "orchestration" / "runs")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{self.run_id}.jsonl"

    def event(self, *, agent: str, task: str, issue: str, status: str, input_summary: str = "", output_summary: str = "", errors: str = "", usage: dict[str, Any] | None = None) -> None:
        row = {
            "Agent": sanitize_text(agent, max_chars=200),
            "Task": sanitize_text(task, max_chars=1000),
            "Issue": sanitize_text(issue, max_chars=200),
            "Started": utc_now(),
            "Finished": utc_now(),
            "Status": sanitize_text(status, max_chars=200),
            "Input Summary": sanitize_text(input_summary, max_chars=3000),
            "Output Summary": sanitize_text(output_summary, max_chars=6000),
            "Errors": sanitize_text(errors, max_chars=3000),
            "Token/API Usage": usage or {},
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_delegate_result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        return {"error": f"delegate_task returned non-JSON: {exc}", "raw": sanitize_text(raw)}
    return parsed if isinstance(parsed, dict) else {"error": "delegate_task returned non-object JSON", "raw": parsed}


def _first_result(raw: str) -> dict[str, Any]:
    parsed = _parse_delegate_result(raw)
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if isinstance(results, list) and results:
        return results[0]
    return {"status": "error", "summary": json.dumps(parsed, ensure_ascii=False)}


def _summary(result: dict[str, Any]) -> str:
    return sanitize_text(result.get("summary") or result.get("error") or json.dumps(result, ensure_ascii=False), max_chars=12000)


def _contract_status(summary: str, contract: str) -> str | None:
    """Extract the first explicit status line from an agent contract summary."""
    upper = summary.upper()
    lines = [line.strip().upper() for line in summary.splitlines() if line.strip()]
    if contract == "developer":
        for line in lines:
            if line.startswith("STATUS:"):
                value = line.split(":", 1)[1].strip()
                if value in {"DONE", "BLOCKED"}:
                    return value.lower()
        if "DEVELOPER RESULT" in upper and "STATUS: DONE" in upper:
            return "done"
        if "DEVELOPER RESULT" in upper and "STATUS: BLOCKED" in upper:
            return "blocked"
    if contract == "tester":
        if any(line == "TEST FAILED" for line in lines):
            return "failed"
        if any(line == "TEST PASSED" for line in lines):
            return "passed"
    if contract == "reviewer":
        for idx, line in enumerate(lines):
            if line.startswith("STATUS:"):
                value = line.split(":", 1)[1].strip()
                if value in {"APPROVED", "CHANGES REQUESTED"}:
                    return value.lower().replace(" ", "_")
        if "REVIEW RESULT" in upper and "CHANGES REQUESTED" in upper:
            return "changes_requested"
        if "REVIEW RESULT" in upper and "APPROVED" in upper:
            return "approved"
    return None


def _contract_failed(summary: str, contract: str) -> bool:
    status = _contract_status(summary, contract)
    if status is None:
        return True
    return status in {"failed", "changes_requested"}


def _handoff_contract(role: str) -> str:
    if role == "developer":
        return """Ergebnisformat an den Orchestrator:
DEVELOPER RESULT
Status: DONE oder BLOCKED
Root Cause:
Geänderte Dateien:
Änderungen:
Build:
Risiken:
Notwendige Tests:
Offene Fragen:
Commit-Vorschlag:
"""
    if role == "tester":
        return """Wenn ein Fehler gefunden wird, nutze exakt dieses Format:
TEST FAILED
Issue: [Issue-ID]
Test: [Beschreibung]
Expected: [Erwartetes Verhalten]
Actual: [Tatsächliches Verhalten]
Evidence: [Screenshot / Log / Fehler]
Possible Cause: [Vermutung]

Wenn alles passt:
TEST PASSED
Issue: [Issue-ID]
Tests:
Evidence:
Risks:
"""
    if role == "reviewer":
        return """Nutze exakt dieses Format:
REVIEW RESULT
Status: APPROVED oder CHANGES REQUESTED
Findings:
Risks:
Recommended Changes:
"""
    return """Liefere Hypothesen mit Priorität:
HIGH CONFIDENCE:
MEDIUM CONFIDENCE:
LOW CONFIDENCE:
Evidence:
"""


def _role_context(ctx: TaskContext, *, role: str, previous_result: str = "") -> str:
    return f"""Du bist der spezialisierte {role.upper()} Agent in einem Hermes-Multi-Agent-Workflow.

Regeln:
- Arbeite nur innerhalb des beschriebenen TaskContext.
- Keine Secrets, .env-Inhalte, Tokens oder Passwörter ausgeben.
- Bestehende Daten, Credentials, Issues, Projekte und Konfigurationen nicht löschen oder überschreiben.
- Repository-Status vor Änderungen prüfen.
- Wenn du schreibst: keine fremden Änderungen überschreiben.
- Wenn Deutsch im TaskContext vorkommt, antworte auf Deutsch.

TaskContext:
{ctx.render_markdown()}

Vorheriges Agent-Ergebnis:
{sanitize_text(previous_result, max_chars=12000) or '–'}

Handoff Contract:
{_handoff_contract(role)}
"""


def run_development_workflow(
    *,
    task_context: TaskContext,
    delegate_fn,
    parent_agent,
    objective: str = "",
    max_correction_loops: int = 3,
    run_reviewer: bool = True,
    developer_toolsets: list[str] | None = None,
    tester_toolsets: list[str] | None = None,
    reviewer_toolsets: list[str] | None = None,
    debugger_toolsets: list[str] | None = None,
    log: OrchestrationLog | None = None,
) -> dict[str, Any]:
    """Run Developer→Tester→Reviewer using real delegate_task child agents."""
    guard = LoopGuard(max_correction_loops=max(1, int(max_correction_loops or 3)))
    log = log or OrchestrationLog()
    issue = task_context.issue_id or task_context.issue_title or "unspecified"
    objective = sanitize_text(objective or task_context.issue_title or "Bearbeite die Aufgabe", max_chars=2000)
    developer_toolsets = developer_toolsets or ["terminal", "file", "web"]
    tester_toolsets = tester_toolsets or ["terminal", "file", "browser"]
    reviewer_toolsets = reviewer_toolsets or ["terminal", "file"]
    debugger_toolsets = debugger_toolsets or ["terminal", "file", "browser"]

    log.event(agent="orchestrator", task=objective, issue=issue, status="START", input_summary=task_context.render_markdown())

    last_developer = ""
    last_tester = ""
    last_reviewer = ""
    final_status = "IN_PROGRESS"

    while guard.can_retry():
        attempt = guard.next_attempt()
        dev_goal = f"Developer-Agent Versuch {attempt}/{guard.max_correction_loops}: {objective}"
        raw_dev = delegate_fn(
            goal=dev_goal,
            context=_role_context(task_context, role="developer", previous_result="\n".join([last_tester, last_reviewer])),
            toolsets=developer_toolsets,
            role="leaf",
            parent_agent=parent_agent,
        )
        dev_result = _first_result(raw_dev)
        last_developer = _summary(dev_result)
        task_context.append_handoff("developer", last_developer)
        developer_contract_status = _contract_status(last_developer, "developer")
        log.event(
            agent="developer",
            task=dev_goal,
            issue=issue,
            status=str(dev_result.get("status", "unknown")),
            output_summary=last_developer,
            usage={
                "api_calls": dev_result.get("api_calls"),
                "duration_seconds": dev_result.get("duration_seconds"),
                "contract_status": developer_contract_status,
            },
        )
        if str(dev_result.get("status")) not in {"completed", "success", "done"}:
            final_status = "BLOCKED"
            task_context.open_problems.append(last_developer)
            break
        if developer_contract_status != "done":
            final_status = "BLOCKED"
            task_context.open_problems.append(last_developer or "Developer agent did not return Status: DONE")
            break

        test_goal = f"Test/QA-Agent Versuch {attempt}/{guard.max_correction_loops}: verifiziere Umsetzung für {objective}"
        raw_test = delegate_fn(
            goal=test_goal,
            context=_role_context(task_context, role="tester", previous_result=last_developer),
            toolsets=tester_toolsets,
            role="leaf",
            parent_agent=parent_agent,
        )
        test_result = _first_result(raw_test)
        last_tester = _summary(test_result)
        task_context.append_handoff("tester", last_tester)
        log.event(agent="tester", task=test_goal, issue=issue, status=str(test_result.get("status", "unknown")), output_summary=last_tester, usage={"api_calls": test_result.get("api_calls"), "duration_seconds": test_result.get("duration_seconds")})
        if str(test_result.get("status")) not in {"completed", "success", "done"} or _contract_failed(last_tester, "tester"):
            final_status = "TEST_FAILED"
            task_context.open_problems.append(last_tester)
            debug_goal = f"Debug-Agent nach Testfehler Versuch {attempt}/{guard.max_correction_loops}: isoliere Ursache für {objective}"
            raw_debug = delegate_fn(
                goal=debug_goal,
                context=_role_context(task_context, role="debugger", previous_result="\n".join([last_developer, last_tester])),
                toolsets=debugger_toolsets,
                role="leaf",
                parent_agent=parent_agent,
            )
            debug_result = _first_result(raw_debug)
            debug_summary = _summary(debug_result)
            task_context.append_handoff("debugger", debug_summary)
            log.event(agent="debugger", task=debug_goal, issue=issue, status=str(debug_result.get("status", "unknown")), output_summary=debug_summary, usage={"api_calls": debug_result.get("api_calls"), "duration_seconds": debug_result.get("duration_seconds")})
            continue

        if not run_reviewer:
            final_status = "TEST_PASSED"
            break

        review_goal = f"Review-Agent Versuch {attempt}/{guard.max_correction_loops}: prüfe Diff/Qualität für {objective}"
        raw_review = delegate_fn(
            goal=review_goal,
            context=_role_context(task_context, role="reviewer", previous_result="\n".join([last_developer, last_tester])),
            toolsets=reviewer_toolsets,
            role="leaf",
            parent_agent=parent_agent,
        )
        review_result = _first_result(raw_review)
        last_reviewer = _summary(review_result)
        task_context.append_handoff("reviewer", last_reviewer)
        log.event(agent="reviewer", task=review_goal, issue=issue, status=str(review_result.get("status", "unknown")), output_summary=last_reviewer, usage={"api_calls": review_result.get("api_calls"), "duration_seconds": review_result.get("duration_seconds")})
        if str(review_result.get("status")) not in {"completed", "success", "done"} or _contract_failed(last_reviewer, "reviewer"):
            final_status = "CHANGES_REQUESTED"
            task_context.open_problems.append(last_reviewer)
            debug_goal = f"Debug-Agent nach Review-Finding Versuch {attempt}/{guard.max_correction_loops}: isoliere Review-Risiken für {objective}"
            raw_debug = delegate_fn(
                goal=debug_goal,
                context=_role_context(task_context, role="debugger", previous_result="\n".join([last_developer, last_tester, last_reviewer])),
                toolsets=debugger_toolsets,
                role="leaf",
                parent_agent=parent_agent,
            )
            debug_result = _first_result(raw_debug)
            debug_summary = _summary(debug_result)
            task_context.append_handoff("debugger", debug_summary)
            log.event(agent="debugger", task=debug_goal, issue=issue, status=str(debug_result.get("status", "unknown")), output_summary=debug_summary, usage={"api_calls": debug_result.get("api_calls"), "duration_seconds": debug_result.get("duration_seconds")})
            continue

        final_status = "APPROVED"
        break

    if final_status in {"TEST_FAILED", "CHANGES_REQUESTED"} and not guard.can_retry():
        final_status = "LOOP_LIMIT_REACHED"
        log.event(agent="orchestrator", task=objective, issue=issue, status="LOOP_LIMIT_REACHED", output_summary="Maximale Developer→Tester/Reviewer-Korrekturschleifen erreicht.")

    result = {
        "run_id": log.run_id,
        "log_path": str(log.path),
        "status": final_status,
        "loop_guard": guard.status(),
        "task_context": task_context.to_dict(),
        "latest": {
            "developer": last_developer,
            "tester": last_tester,
            "reviewer": last_reviewer,
        },
        "next_step": "Commit/Push/PR nur nach Orchestrator-Prüfung und bestehenden Sicherheitsregeln ausführen." if final_status == "APPROVED" else "Orchestrator muss Problem zusammenfassen und Nutzerentscheidung einholen oder Debug-Agent starten.",
    }
    log.event(agent="orchestrator", task=objective, issue=issue, status=final_status, output_summary=json.dumps({"status": final_status, "log_path": str(log.path)}, ensure_ascii=False))
    return result
