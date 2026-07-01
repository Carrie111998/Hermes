"""W5 Config Guard fixture-only dry-run helpers.

This module is deliberately non-mutating: it observes a protected target, builds a
mutation plan, resolves the missing-approval branch, writes digest/read-back
evidence into a caller-supplied tempdir, and confirms the protected target stayed
unchanged. It is not wired to live Hermes profile/provider configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


CASE_ID = "W5-P0-2-PROTECTED-MUTATION-DRY-RUN-001"
DENY_RULE_ID = "w5.config_guard.protected_mutation.approval_required"
REQUIRED_APPROVAL = "pep_current_session"


@dataclass(frozen=True)
class ConfigGuardDryRunResult:
    case_id: str
    correlation_id: str
    decision: str
    rule_id: str
    reason: str
    required_approval: str
    target_path: str
    before_hash: str
    after_hash: str
    before_after_hash_equal: bool
    target_write_count: int
    rollback_artifact: str
    evidence_path: str
    live_config_touched: bool
    secret_values_read: bool


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _unified_diff(before: str, after: str, target_path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"before/{target_path.name}",
            tofile=f"after/{target_path.name}",
        )
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def run_protected_mutation_dry_run(
    *,
    target_path: str | Path,
    proposed_content: str,
    surface_id: str,
    mutation_class: str,
    actor: str,
    profile_id: str,
    evidence_dir: str | Path,
    approval_ref: str | None = None,
) -> ConfigGuardDryRunResult:
    """Run a fixture-only Config Guard protected-mutation dry run.

    The target is never written by this function. Missing approval returns a
    fail-closed denial after emitting both a rollback artifact and a JSONL
    evidence row. Evidence writes are scoped to ``evidence_dir`` supplied by the
    caller, normally under pytest ``tmp_path``.
    """

    target = Path(target_path)
    evidence_root = Path(evidence_dir)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"protected target does not exist: {target}")

    before_content = target.read_text(encoding="utf-8")
    before_hash = _sha256_file(target)
    planned_diff = _unified_diff(before_content, proposed_content, target)
    correlation_id = f"w5-config-guard-{uuid4().hex[:12]}"
    timestamp = _utc_now()

    rollback_path = evidence_root / "rollback" / f"{CASE_ID}.rollback.json"
    rollback_payload = {
        "case_id": CASE_ID,
        "correlation_id": correlation_id,
        "created_at": timestamp,
        "target_path": str(target),
        "restore_sha256": before_hash,
        "restore_content": before_content,
        "rollback_type": "restore_exact_fixture_content",
    }
    _write_json(rollback_path, rollback_payload)

    if approval_ref:
        decision = "allow"
        rule_id = "w5.config_guard.protected_mutation.approved_dry_run"
        reason = "approved dry-run only; target mutation still not executed by fixture helper"
        required_approval = "satisfied"
    else:
        decision = "deny"
        rule_id = DENY_RULE_ID
        reason = "protected route-affecting mutation requires current-session Pep approval"
        required_approval = REQUIRED_APPROVAL

    # Deliberately do not write target. Re-read after resolution to prove no-op.
    after_hash = _sha256_file(target)
    before_after_hash_equal = before_hash == after_hash

    evidence_path = evidence_root / "w5_config_guard_dry_run.jsonl"
    row = {
        "case_id": CASE_ID,
        "correlation_id": correlation_id,
        "timestamp": timestamp,
        "surface": surface_id,
        "gap_id": "W5-G02",
        "evidence_id": "W5-E02/W5-E05/W5-E10",
        "actor": actor,
        "profile_id": profile_id,
        "mutation_class": mutation_class,
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "required_approval": required_approval,
        "approval_ref_present": bool(approval_ref),
        "source_config_hash_before": before_hash,
        "source_config_hash_after": after_hash,
        "before_after_hash_equal": before_after_hash_equal,
        "target_write_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "rollback_artifact": str(rollback_path),
        "mutation_plan_capture": {
            "target_path": str(target),
            "surface_id": surface_id,
            "mutation_class": mutation_class,
            "planned_diff": planned_diff,
            "before_hash": before_hash,
            "proposed_hash": _sha256_bytes(proposed_content.encode("utf-8")),
            "approval_required": REQUIRED_APPROVAL,
        },
    }
    _append_jsonl(evidence_path, row)

    return ConfigGuardDryRunResult(
        case_id=CASE_ID,
        correlation_id=correlation_id,
        decision=decision,
        rule_id=rule_id,
        reason=reason,
        required_approval=required_approval,
        target_path=str(target),
        before_hash=before_hash,
        after_hash=after_hash,
        before_after_hash_equal=before_after_hash_equal,
        target_write_count=0,
        rollback_artifact=str(rollback_path),
        evidence_path=str(evidence_path),
        live_config_touched=False,
        secret_values_read=False,
    )
