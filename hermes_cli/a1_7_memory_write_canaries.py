"""A1.7 high-fidelity synthetic memory write sink canaries.

This harness exercises the cross-session persistence boundary enforcement
without invoking live memory writes, disk I/O, provider dispatch, secrets,
or live profile/runtime configuration.

Cases:
  A1.7-T01: C2 agent without allowed_paths denied memory write
  A1.7-T02: C2 agent with memory path in allowed_paths allowed
  A1.7-T03: C0 agent allowed without restrictions
  A1.7-T04: Agent without taint classification denied (fail-closed)
  A1.7-T05: C2 agent denied memory write when only other file in allowed_paths
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.a1_7_memory_write_guard import check_memory_write_permission


@dataclass(frozen=True)
class A17CanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    live_memory_write_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool
    raw_memory_content_stored: bool


def _memory_dir() -> str:
    """Resolve memory directory using canonical get_memory_dir()."""
    from tools.memory_tool import get_memory_dir
    return str(get_memory_dir())


def _make_agent(
    classification: str | None = None,
    allowed_paths: list[str] | None = None,
    source: str = "hl_aos_frozen",
) -> Any:
    """Build a synthetic agent with taint attributes."""
    agent = SimpleNamespace()
    if classification is not None:
        agent.hl_aos_taint_classification = classification
    if allowed_paths is not None:
        agent.hl_aos_allowed_paths = allowed_paths
    agent.hl_aos_classification_source = source
    return agent


def _base_row(
    *,
    case_id: str,
    decision: str,
    reason: str,
    classification: str,
    classification_source: str,
    target: str,
    requested_content: str,
    allowed_paths: list[str],
) -> dict[str, Any]:
    """Base evidence row template for A1.7 canaries."""
    return {
        "case_id": case_id,
        "decision": decision,
        "reason": reason,
        "classification": classification,
        "classification_source": classification_source,
        "target": target,
        "requested_content_digest": f"sha256:{hashlib.sha256(requested_content.encode('utf-8')).hexdigest()}" if requested_content else None,
        "allowed_paths": allowed_paths,
        "live_memory_write_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "raw_memory_content_stored": False,
    }


def _case_c2_denied_without_allowed_paths() -> dict[str, Any]:
    """C2 agent without allowed_paths is denied memory write."""
    agent = _make_agent(classification="C2")
    content = "synthetic C2 memory write payload"

    result = check_memory_write_permission(agent, "memory", content)

    if result is None:
        return _base_row(
            case_id="A1.7-T01",
            decision="allowed",
            reason="UNEXPECTED: C2 should be denied without allowed_paths",
            classification="C2",
            classification_source="hl_aos_frozen",
            target="memory",
            requested_content=content,
            allowed_paths=[],
        )

    return _base_row(
        case_id="A1.7-T01",
        decision="denied",
        reason=result,
        classification="C2",
        classification_source="hl_aos_frozen",
        target="memory",
        requested_content=content,
        allowed_paths=[],
    )


def _case_c2_allowed_with_memory_path() -> dict[str, Any]:
    """C2 agent with memory dir in allowed_paths can write."""
    memdir = _memory_dir()
    agent = _make_agent(classification="C2", allowed_paths=[memdir])
    content = "synthetic C2 memory write payload"

    result = check_memory_write_permission(agent, "memory", content)

    if result is not None:
        return _base_row(
            case_id="A1.7-T02",
            decision="denied",
            reason=f"UNEXPECTED: {result}",
            classification="C2",
            classification_source="hl_aos_frozen",
            target="memory",
            requested_content=content,
            allowed_paths=[memdir],
        )

    return _base_row(
        case_id="A1.7-T02",
        decision="allowed",
        reason="C2 permitted with memory dir in allowed_paths",
        classification="C2",
        classification_source="hl_aos_frozen",
        target="memory",
        requested_content=content,
        allowed_paths=[memdir],
    )


def _case_c0_allowed() -> dict[str, Any]:
    """C0 agent allowed without restrictions."""
    agent = _make_agent(classification="C0")
    content = "synthetic C0 memory write payload"

    result = check_memory_write_permission(agent, "memory", content)

    if result is not None:
        return _base_row(
            case_id="A1.7-T03",
            decision="denied",
            reason=f"UNEXPECTED: {result}",
            classification="C0",
            classification_source="",
            target="memory",
            requested_content=content,
            allowed_paths=[],
        )

    return _base_row(
        case_id="A1.7-T03",
        decision="allowed",
        reason="C0 permitted without restrictions",
        classification="C0",
        classification_source="",
        target="memory",
        requested_content=content,
        allowed_paths=[],
    )


def _case_fail_closed_no_taint() -> dict[str, Any]:
    """Agent without taint is denied (fail-closed)."""
    agent = SimpleNamespace()  # no taint attribute
    content = "synthetic unclassified memory write payload"

    result = check_memory_write_permission(agent, "memory", content)

    if result is None:
        return _base_row(
            case_id="A1.7-T04",
            decision="allowed",
            reason="UNEXPECTED: missing taint should be denied",
            classification="",
            classification_source="",
            target="memory",
            requested_content=content,
            allowed_paths=[],
        )

    return _base_row(
        case_id="A1.7-T04",
        decision="denied",
        reason=result,
        classification="",
        classification_source="",
        target="memory",
        requested_content=content,
        allowed_paths=[],
    )


def _case_c2_user_denied_memory_only() -> dict[str, Any]:
    """C2 agent denied USER.md write when only MEMORY.md in allowed_paths."""
    memdir = _memory_dir()
    agent = _make_agent(classification="C2", allowed_paths=[f"{memdir}/MEMORY.md"])
    content = "synthetic C2 user write payload"

    result = check_memory_write_permission(agent, "user", content)

    if result is None:
        return _base_row(
            case_id="A1.7-T05",
            decision="allowed",
            reason="UNEXPECTED: USER.md should be denied when only MEMORY.md allowed",
            classification="C2",
            classification_source="hl_aos_frozen",
            target="user",
            requested_content=content,
            allowed_paths=[f"{memdir}/MEMORY.md"],
        )

    return _base_row(
        case_id="A1.7-T05",
        decision="denied",
        reason=result,
        classification="C2",
        classification_source="hl_aos_frozen",
        target="user",
        requested_content=content,
        allowed_paths=[f"{memdir}/MEMORY.md"],
    )


def run_a1_7_memory_write_canaries() -> A17CanaryResult:
    """Execute A1.7 memory write sink canaries."""
    rows: list[dict[str, Any]] = [
        _case_c2_denied_without_allowed_paths(),
        _case_c2_allowed_with_memory_path(),
        _case_c0_allowed(),
        _case_fail_closed_no_taint(),
        _case_c2_user_denied_memory_only(),
    ]

    evidence_path = Path("/tmp/a1_7_memory_write_canary_evidence.jsonl")
    summary_path = Path("/tmp/a1_7_memory_write_canary_summary.json")

    # Write evidence
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    # Write summary
    total = len(rows)
    denied = sum(1 for r in rows if r["decision"] == "denied")
    allowed = sum(1 for r in rows if r["decision"] == "allowed")

    summary = {
        "total_cases": total,
        "denied": denied,
        "allowed": allowed,
        "live_memory_write_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "raw_memory_content_stored": False,
        "evidence_path": str(evidence_path),
        "summary_path": str(summary_path),
    }

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return A17CanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        live_memory_write_count=0,
        provider_call_count=0,
        live_config_touched=False,
        secret_values_read=False,
        raw_payload_stored=False,
        raw_memory_content_stored=False,
    )


def main() -> None:
    """Entry point for CLI invocation."""
    print("Running A1.7 memory write sink canaries...")
    result = run_a1_7_memory_write_canaries()
    print(f"✓ Executed {result.total} canary cases")
    print(f"  - Denied: {result.denied}")
    print(f"  - Allowed: {result.allowed}")
    print(f"  Evidence: {result.evidence_path}")
    print(f"  Summary: {result.summary_path}")


if __name__ == "__main__":
    main()
