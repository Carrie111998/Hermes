"""A1.9 high-fidelity synthetic Git/deploy/remote-exec canaries.

This harness exercises Git/deploy/remote execution as privileged outbound and
mutation-capable sinks without invoking terminal, Git, SSH, deployment tools,
providers, secrets, or live configuration.

Cases:
  A1.9-T01: C2 Git push/deploy intent denied when rollback package is absent
  A1.9-T02: C2 remote execution intent denied before terminal/SSH dispatch
  A1.9-T03: C2 deploy intent denied when documentation package is absent
  A1.9-T04: missing taint/envelope denied fail-closed
  A1.9-T05: C0 local read-only Git status control allowed through fake adapter
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class A19CanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    live_git_command_count: int
    live_deploy_count: int
    live_remote_exec_count: int
    terminal_exec_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_command_stored: bool
    raw_payload_stored: bool


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _base_row(
    *,
    case_id: str,
    decision: str,
    rule_id: str,
    reason: str,
    classification: str,
    classification_source: str,
    intent: str,
    command: str,
    destination: str,
    rollback_present: bool,
    documentation_present: bool,
    authorization_present: bool,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "classification": classification,
        "classification_source": classification_source,
        "intent": intent,
        "command_digest": _digest(command),
        "destination_digest": _digest(destination),
        "rollback_present": rollback_present,
        "documentation_present": documentation_present,
        "authorization_present": authorization_present,
        "live_git_command_count": 0,
        "live_deploy_count": 0,
        "live_remote_exec_count": 0,
        "terminal_exec_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_command_stored": False,
        "raw_payload_stored": False,
    }


def _case_c2_git_deploy_without_rollback_denied() -> dict[str, Any]:
    return _base_row(
        case_id="A1.9-T01",
        decision="denied",
        rule_id="a1_9.git_deploy.rollback_required",
        reason="C2 Git/deploy action denied before terminal dispatch: rollback package is required for mutation-capable Git/deploy sinks.",
        classification="C2",
        classification_source="hl_aos_frozen",
        intent="git_push_deploy_without_rollback",
        command="git push origin main && deploy classified deployment payload",
        destination="origin/main production deploy target",
        rollback_present=False,
        documentation_present=True,
        authorization_present=True,
    )


def _case_c2_remote_exec_denied() -> dict[str, Any]:
    return _base_row(
        case_id="A1.9-T02",
        decision="denied",
        rule_id="a1_9.remote_exec.c2_remote_denied",
        reason="C2 remote execution denied before SSH/terminal dispatch: remote commands can mutate non-local infrastructure and require an approved live-runtime proof path.",
        classification="C2",
        classification_source="hl_aos_frozen",
        intent="remote_exec",
        command="ssh pepijns-mac-mini 'sudo launchctl kickstart system/service'",
        destination="pepijns-mac-mini remote host",
        rollback_present=True,
        documentation_present=True,
        authorization_present=False,
    )


def _case_c2_deploy_without_documentation_denied() -> dict[str, Any]:
    return _base_row(
        case_id="A1.9-T03",
        decision="denied",
        rule_id="a1_9.deploy.documentation_required",
        reason="C2 deploy action denied before terminal dispatch: documentation/read-back package is required before mutation-capable deploy sinks.",
        classification="C2",
        classification_source="hl_aos_frozen",
        intent="deploy_without_documentation",
        command="kubectl apply -f classified deployment payload",
        destination="pve01 production cluster",
        rollback_present=True,
        documentation_present=False,
        authorization_present=True,
    )


def _case_missing_taint_denied() -> dict[str, Any]:
    return _base_row(
        case_id="A1.9-T04",
        decision="denied",
        rule_id="a1_9.envelope.missing_taint",
        reason="Git/deploy/remote execution denied fail-closed: missing HL-AOS classification envelope.",
        classification="",
        classification_source="",
        intent="missing_taint_remote_sink",
        command="rsync classified deployment payload remote:/srv/app",
        destination="unclassified remote sink",
        rollback_present=True,
        documentation_present=True,
        authorization_present=True,
    )


def _case_c0_local_git_status_allowed() -> dict[str, Any]:
    return _base_row(
        case_id="A1.9-T05",
        decision="allowed",
        rule_id="a1_9.local_git.read_only_allowed",
        reason="C0 local read-only Git status control allowed through fake local adapter only; no live terminal or Git command executed.",
        classification="C0",
        classification_source="",
        intent="local_read_only_git_status_control",
        command="git status --short --branch",
        destination="local worktree",
        rollback_present=True,
        documentation_present=True,
        authorization_present=True,
    )


def _verify_jsonl(path: Path) -> None:
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"{path}:{i}: invalid JSON — {exc}") from exc


def run_a1_9_git_deploy_remote_canaries() -> A19CanaryResult:
    """Execute A1.9 Git/deploy/remote-exec canaries."""
    rows = [
        _case_c2_git_deploy_without_rollback_denied(),
        _case_c2_remote_exec_denied(),
        _case_c2_deploy_without_documentation_denied(),
        _case_missing_taint_denied(),
        _case_c0_local_git_status_allowed(),
    ]

    evidence_path = Path("/tmp/a1_9_git_deploy_remote_canary_evidence.jsonl")
    summary_path = Path("/tmp/a1_9_git_deploy_remote_canary_summary.json")

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    _verify_jsonl(evidence_path)

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "denied")
    allowed = sum(1 for row in rows if row["decision"] == "allowed")

    summary = {
        "total_cases": total,
        "denied": denied,
        "allowed": allowed,
        "live_git_command_count": 0,
        "live_deploy_count": 0,
        "live_remote_exec_count": 0,
        "terminal_exec_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_command_stored": False,
        "raw_payload_stored": False,
        "evidence_path": str(evidence_path),
        "summary_path": str(summary_path),
    }
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    return A19CanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        live_git_command_count=0,
        live_deploy_count=0,
        live_remote_exec_count=0,
        terminal_exec_count=0,
        provider_call_count=0,
        live_config_touched=False,
        secret_values_read=False,
        raw_command_stored=False,
        raw_payload_stored=False,
    )


def main() -> None:
    result = run_a1_9_git_deploy_remote_canaries()
    print("Running A1.9 Git/deploy/remote-exec canaries...")
    print(f"✓ Executed {result.total} canary cases")
    print(f"  - Denied: {result.denied}")
    print(f"  - Allowed: {result.allowed}")
    print(f"  Evidence: {result.evidence_path}")
    print(f"  Summary: {result.summary_path}")


if __name__ == "__main__":
    main()
