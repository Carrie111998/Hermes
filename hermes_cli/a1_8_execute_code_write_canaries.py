"""A1.8 high-fidelity synthetic execute_code nested write canaries.

This harness exercises execute_code as a nested write-capable sink without
spawning a sandbox process, dispatching providers, touching live config,
reading secrets, or writing target payloads.

Cases:
  A1.8-T01: C2 raw Python file-write intent denied before script spawn
  A1.8-T02: C2 hermes_tools.write_file nested RPC intent denied before dispatch
  A1.8-T03: missing taint denied fail-closed
  A1.8-T04: C0 non-write execute_code allowed
  A1.8-T05: C1 non-write execute_code allowed
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.a1_8_execute_code_write_guard import check_execute_code_write_permission


@dataclass(frozen=True)
class A18CanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    sandbox_spawn_count: int
    nested_tool_dispatch_count: int
    target_write_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_code_stored: bool


def _make_agent(classification: str | None = None, source: str = "hl_aos_frozen") -> Any:
    agent = SimpleNamespace()
    if classification is not None:
        agent.hl_aos_taint_classification = classification
    agent.hl_aos_classification_source = source
    return agent


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _base_row(
    *,
    case_id: str,
    decision: str,
    reason: str,
    classification: str,
    classification_source: str,
    code: str,
    intent: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "decision": decision,
        "reason": reason,
        "classification": classification,
        "classification_source": classification_source,
        "intent": intent,
        "code_digest": _digest(code),
        "sandbox_spawn_count": 0,
        "nested_tool_dispatch_count": 0,
        "target_write_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_code_stored": False,
    }


def _case_c2_raw_write_denied() -> dict[str, Any]:
    code = "from pathlib import Path\nPath('/tmp/a18-raw').write_text('classified')\n"
    agent = _make_agent("C2")
    denied = check_execute_code_write_permission(agent, code)
    if denied is None:
        return _base_row(
            case_id="A1.8-T01",
            decision="allowed",
            reason="UNEXPECTED: C2 raw Python write intent should be denied",
            classification="C2",
            classification_source="hl_aos_frozen",
            code=code,
            intent="raw_python_file_write",
        )
    return _base_row(
        case_id="A1.8-T01",
        decision="denied",
        reason=denied,
        classification="C2",
        classification_source="hl_aos_frozen",
        code=code,
        intent="raw_python_file_write",
    )


def _case_c2_nested_write_file_denied() -> dict[str, Any]:
    code = "from hermes_tools import write_file\nwrite_file('/tmp/a18-rpc', 'classified')\n"
    agent = _make_agent("C2")
    denied = check_execute_code_write_permission(agent, code)
    if denied is None:
        return _base_row(
            case_id="A1.8-T02",
            decision="allowed",
            reason="UNEXPECTED: C2 nested write_file intent should be denied",
            classification="C2",
            classification_source="hl_aos_frozen",
            code=code,
            intent="sandbox_rpc_write_file",
        )
    return _base_row(
        case_id="A1.8-T02",
        decision="denied",
        reason=denied,
        classification="C2",
        classification_source="hl_aos_frozen",
        code=code,
        intent="sandbox_rpc_write_file",
    )


def _case_missing_taint_denied() -> dict[str, Any]:
    code = "print('missing taint')\n"
    agent = _make_agent(None)
    denied = check_execute_code_write_permission(agent, code)
    if denied is None:
        return _base_row(
            case_id="A1.8-T03",
            decision="allowed",
            reason="UNEXPECTED: missing taint should be denied",
            classification="",
            classification_source="",
            code=code,
            intent="missing_taint_non_write_control",
        )
    return _base_row(
        case_id="A1.8-T03",
        decision="denied",
        reason=denied,
        classification="",
        classification_source="",
        code=code,
        intent="missing_taint_non_write_control",
    )


def _case_c0_non_write_allowed() -> dict[str, Any]:
    code = "print('public analysis')\n"
    agent = _make_agent("C0", source="")
    denied = check_execute_code_write_permission(agent, code)
    if denied is not None:
        return _base_row(
            case_id="A1.8-T04",
            decision="denied",
            reason=f"UNEXPECTED: {denied}",
            classification="C0",
            classification_source="",
            code=code,
            intent="c0_non_write_control",
        )
    return _base_row(
        case_id="A1.8-T04",
        decision="allowed",
        reason="C0 execute_code permitted",
        classification="C0",
        classification_source="",
        code=code,
        intent="c0_non_write_control",
    )


def _case_c1_non_write_allowed() -> dict[str, Any]:
    code = "print('internal analysis')\n"
    agent = _make_agent("C1")
    denied = check_execute_code_write_permission(agent, code)
    if denied is not None:
        return _base_row(
            case_id="A1.8-T05",
            decision="denied",
            reason=f"UNEXPECTED: {denied}",
            classification="C1",
            classification_source="hl_aos_frozen",
            code=code,
            intent="c1_non_write_control",
        )
    return _base_row(
        case_id="A1.8-T05",
        decision="allowed",
        reason="C1 execute_code permitted",
        classification="C1",
        classification_source="hl_aos_frozen",
        code=code,
        intent="c1_non_write_control",
    )


def run_a1_8_execute_code_write_canaries() -> A18CanaryResult:
    """Execute A1.8 execute_code nested write canaries."""
    rows = [
        _case_c2_raw_write_denied(),
        _case_c2_nested_write_file_denied(),
        _case_missing_taint_denied(),
        _case_c0_non_write_allowed(),
        _case_c1_non_write_allowed(),
    ]

    evidence_path = Path("/tmp/a1_8_execute_code_write_canary_evidence.jsonl")
    summary_path = Path("/tmp/a1_8_execute_code_write_canary_summary.json")

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "denied")
    allowed = sum(1 for row in rows if row["decision"] == "allowed")

    summary = {
        "total_cases": total,
        "denied": denied,
        "allowed": allowed,
        "sandbox_spawn_count": 0,
        "nested_tool_dispatch_count": 0,
        "target_write_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_code_stored": False,
        "evidence_path": str(evidence_path),
        "summary_path": str(summary_path),
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return A18CanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        sandbox_spawn_count=0,
        nested_tool_dispatch_count=0,
        target_write_count=0,
        provider_call_count=0,
        live_config_touched=False,
        secret_values_read=False,
        raw_code_stored=False,
    )


def main() -> None:
    result = run_a1_8_execute_code_write_canaries()
    print("Running A1.8 execute_code nested write canaries...")
    print(f"✓ Executed {result.total} canary cases")
    print(f"  - Denied: {result.denied}")
    print(f"  - Allowed: {result.allowed}")
    print(f"  Evidence: {result.evidence_path}")
    print(f"  Summary: {result.summary_path}")


if __name__ == "__main__":
    main()
