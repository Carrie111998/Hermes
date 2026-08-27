"""Authoritative execution state for one completed agent turn."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def build_execution_receipt(
    turn_evidence: Mapping[str, Any] | None,
    process_sessions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a receipt from evidence recorded while the turn was executing."""
    evidence = turn_evidence or {}
    tool_calls = len(evidence.get("tool_calls") or [])
    active_processes: list[str] = []
    exited_processes: list[dict[str, Any]] = []
    for session in process_sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        if session.get("status") == "running":
            active_processes.append(session_id)
        elif session.get("status") == "exited":
            exited_processes.append(
                {"session_id": session_id, "exit_code": session.get("exit_code")}
            )

    if active_processes:
        status = "active"
    elif exited_processes:
        status = "exited"
    elif tool_calls:
        status = "completed"
    else:
        status = "not_started"
    return {
        "status": status,
        "tool_calls": tool_calls,
        "active_processes": active_processes,
        "exited_processes": exited_processes,
    }


def render_execution_status(receipt: Mapping[str, Any]) -> dict[str, str]:
    """Render the structured receipt into deterministic delivery text."""
    status = str(receipt.get("status") or "not_started")
    tool_calls = int(receipt.get("tool_calls") or 0)
    active = list(receipt.get("active_processes") or [])
    exited = list(receipt.get("exited_processes") or [])
    if status == "active":
        detail = f"active; managed process(es) still running: {', '.join(active)}."
    elif status == "exited":
        rendered = ", ".join(
            f"{item['session_id']} (exit {item.get('exit_code')})" for item in exited
        )
        detail = f"exited; managed process(es) started this turn finished: {rendered}."
    elif status == "completed":
        detail = f"completed; {tool_calls} tool call(s) ran and no managed process remains active."
    else:
        detail = "not started; no tools ran and no managed process was started this turn."
    return {"status": status, "text": f"Execution status: {detail}"}


def finalize_execution_result(
    result: dict[str, Any],
    *,
    turn_evidence: Mapping[str, Any] | None,
    process_sessions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach structured and rendered authoritative status to a turn result."""
    receipt = build_execution_receipt(turn_evidence, process_sessions)
    execution_status = render_execution_status(receipt)
    result["execution_receipt"] = receipt
    result["execution_status"] = execution_status
    return result


def attach_execution_status_for_delivery(result: dict[str, Any]) -> dict[str, Any]:
    """Render an attached status into final text at a delivery boundary."""
    response = result.get("final_response")
    status = result.get("execution_status") or {}
    status_text = status.get("text") if isinstance(status, Mapping) else None
    if (
        isinstance(response, str)
        and response
        and isinstance(status_text, str)
        and status_text
        and not result.get("interrupted")
        and not response.rstrip().endswith(status_text)
    ):
        result["final_response"] = response.rstrip() + "\n\n" + status_text
    return result


__all__ = [
    "attach_execution_status_for_delivery",
    "build_execution_receipt",
    "finalize_execution_result",
    "render_execution_status",
]
