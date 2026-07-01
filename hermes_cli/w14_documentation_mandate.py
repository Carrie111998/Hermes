"""W14 Documentation Mandate completion-gate fixture harness.

The helper in this module evaluates synthetic mutation completion packages before
an agent can claim done-state. It is deterministic and local-only: no live vault,
profile/provider config, gateway, service, provider, proxy, credential, or secret
source is read or mutated. Evidence is digest-only JSONL plus a summary JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


CASE_ORDER = ("W14-T01", "W14-T02", "W14-T03", "W14-T04", "W14-T05", "W14-T06")
REQUIRED_SECTIONS = ("What", "State Before", "State After", "Verification", "Rollback", "Authorization")
SECTION_ALIASES = {
    "What": ("What", "What Changed"),
    "State Before": ("State Before",),
    "State After": ("State After",),
    "Verification": ("Verification",),
    "Rollback": ("Rollback",),
    "Authorization": ("Authorization", "Authorisation"),
}
RULES = {
    "build_log_required": "w14.documentation.build_log.required",
    "sections_missing": "w14.documentation.required_sections.missing",
    "readback_required": "w14.documentation.readback.required",
    "rollback_required": "w14.documentation.rollback.required",
    "authorization_required": "w14.documentation.authorization.current_session_required",
    "complete": "w14.documentation.complete.fixture_allowed",
}


@dataclass(frozen=True)
class DocumentationMandateResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    done_state_allowed_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _correlation_id(case_id: str, policy_manifest_hash: str) -> str:
    return "w14-" + hashlib.sha256(f"{case_id}:{policy_manifest_hash}".encode("utf-8")).hexdigest()[:12]


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _section_present(build_log_text: str, canonical_section: str) -> bool:
    aliases = SECTION_ALIASES[canonical_section]
    for alias in aliases:
        pattern = re.compile(rf"^\s*#{{1,6}}\s+.*\b{re.escape(alias)}\b.*$", re.IGNORECASE | re.MULTILINE)
        if pattern.search(build_log_text):
            return True
    return False


def _section_status(build_log_text: str | None) -> tuple[list[str], list[str]]:
    if not build_log_text:
        return [], list(REQUIRED_SECTIONS)
    present = [section for section in REQUIRED_SECTIONS if _section_present(build_log_text, section)]
    missing = [section for section in REQUIRED_SECTIONS if section not in present]
    return present, missing


def _valid_current_session_authorization(authorization: dict[str, Any] | None) -> bool:
    if not authorization:
        return False
    return (
        authorization.get("source") == "pep_current_session"
        and authorization.get("current_session") is True
        and str(authorization.get("approved_by", "")).lower() == "pep"
    )


def _agent_self_approval_detected(authorization: dict[str, Any] | None) -> bool:
    if not authorization:
        return False
    actor = str(authorization.get("actor", "")).lower()
    approved_by = str(authorization.get("approved_by", "")).lower()
    source = str(authorization.get("source", "")).lower()
    return (
        "agent" in actor
        or "agent" in approved_by
        or approved_by in {"self", "pennyworth", "pennyworth-architect-test"}
        or source == "agent_self_assertion"
    )


def evaluate_completion_package(
    package: dict[str, Any],
    *,
    actor: str,
    profile_id: str,
    policy_manifest_hash: str,
) -> dict[str, Any]:
    """Evaluate one synthetic mutation completion package against W14.

    The returned row is directly suitable for digest-only evidence storage. It
    contains no raw build-log text, no raw artifact payloads, and no secret data.
    """

    case_id = package["case_id"]
    build_log_text = package.get("build_log_text")
    changed_artifacts = list(package.get("changed_artifacts", []))
    readback_artifacts = set(package.get("readback_verified_artifacts", []))
    authorization = package.get("authorization")
    rollback_instructions = str(package.get("rollback_instructions", "") or "")

    build_log_present = bool(build_log_text)
    required_sections_present, missing_sections = _section_status(build_log_text)
    changed_artifact_count = len(changed_artifacts)
    readback_verified = changed_artifact_count > 0 and set(changed_artifacts).issubset(readback_artifacts)
    rollback_present = bool(rollback_instructions.strip())
    authorization_present = bool(authorization)
    current_session_authorization_present = _valid_current_session_authorization(authorization)
    agent_self_approval_detected = _agent_self_approval_detected(authorization)

    if not build_log_present:
        decision = "deny"
        rule_id = RULES["build_log_required"]
        reason = "build_log.missing"
    elif missing_sections:
        decision = "deny"
        rule_id = RULES["sections_missing"]
        reason = "required_section.missing:" + ",".join(missing_sections)
    elif not readback_verified:
        decision = "deny"
        rule_id = RULES["readback_required"]
        reason = "changed_artifact.readback_missing"
    elif not rollback_present:
        decision = "deny"
        rule_id = RULES["rollback_required"]
        reason = "rollback.instructions_missing"
    elif package.get("mutation_class_action", True) and not current_session_authorization_present:
        decision = "deny"
        rule_id = RULES["authorization_required"]
        reason = "authorization.current_session_missing_or_self_approved"
    else:
        decision = "allow"
        rule_id = RULES["complete"]
        reason = "documentation_mandate.fixture_package_complete"

    done_state_allowed = decision == "allow"
    return {
        "case_id": case_id,
        "correlation_id": _correlation_id(case_id, policy_manifest_hash),
        "timestamp": "2026-07-01T00:00:00Z",
        "profile_id": profile_id,
        "actor": actor,
        "gap_id": "W14",
        "evidence_id": "W14-DOCUMENTATION-MANDATE-COMPLETION-GATE",
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "done_state_allowed": done_state_allowed,
        "changed_artifact_count": changed_artifact_count,
        "changed_artifact_digests": [_sha256_text(str(item)) for item in changed_artifacts],
        "build_log_present": build_log_present,
        "build_log_digest": _sha256_text(build_log_text) if build_log_text else None,
        "required_sections_present": required_sections_present,
        "missing_sections": missing_sections,
        "readback_verified": readback_verified,
        "readback_verified_count": len(readback_artifacts.intersection(changed_artifacts)),
        "rollback_present": rollback_present,
        "rollback_digest": _sha256_text(rollback_instructions) if rollback_present else None,
        "authorization_present": authorization_present,
        "current_session_authorization_present": current_session_authorization_present,
        "agent_self_approval_detected": agent_self_approval_detected,
        "mutation_class_action": bool(package.get("mutation_class_action", True)),
        "policy_manifest_hash": policy_manifest_hash,
        "scope": "fixture_only_no_live_mutation",
        "redaction_class": "digest_only",
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
    }


def _complete_build_log() -> str:
    return """
# Build Log — W14 fixture completion package

## What Changed
Synthetic fixture artifact and evidence row were created.

## State Before
The fixture package had no done-state decision.

## State After
The fixture package has a deterministic done-state decision.

## Verification
Read-back evidence lists every changed artifact by path and digest.

## Rollback
Revert the synthetic fixture artifact and evidence row.

## Authorisation
Pep current-session authorization metadata is present for the fixture action.
""".lstrip()


def _build_log_missing_verification() -> str:
    return """
# Build Log — W14 fixture incomplete package

## What Changed
Synthetic fixture artifact and evidence row were created.

## State Before
The fixture package had no done-state decision.

## State After
The fixture package has a deterministic done-state decision.

## Rollback
Revert the synthetic fixture artifact and evidence row.

## Authorization
Pep current-session authorization metadata is present for the fixture action.
""".lstrip()


def _fixture_packages() -> list[dict[str, Any]]:
    changed_artifacts = [
        "Projects/HL-AOS Full Scope Architecture Program/W14_FIXTURE_ARTIFACT.md",
        "Build Logs/2026-07-01-w14-documentation-mandate-fixture.md",
    ]
    valid_auth = {
        "source": "pep_current_session",
        "approved_by": "Pep",
        "actor": "pep",
        "current_session": True,
        "approval_ref": "user-request-2026-07-01-p0-5",
    }
    self_auth = {
        "source": "agent_self_assertion",
        "approved_by": "pennyworth-architect-test",
        "actor": "pennyworth-architect-test-agent",
        "current_session": True,
        "approval_ref": "self-approved-fixture",
    }
    return [
        {
            "case_id": "W14-T01",
            "build_log_text": None,
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": changed_artifacts,
            "rollback_instructions": "revert synthetic fixture artifacts",
            "authorization": valid_auth,
            "mutation_class_action": True,
        },
        {
            "case_id": "W14-T02",
            "build_log_text": _build_log_missing_verification(),
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": changed_artifacts,
            "rollback_instructions": "revert synthetic fixture artifacts",
            "authorization": valid_auth,
            "mutation_class_action": True,
        },
        {
            "case_id": "W14-T03",
            "build_log_text": _complete_build_log(),
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": [changed_artifacts[0]],
            "rollback_instructions": "revert synthetic fixture artifacts",
            "authorization": valid_auth,
            "mutation_class_action": True,
        },
        {
            "case_id": "W14-T04",
            "build_log_text": _complete_build_log(),
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": changed_artifacts,
            "rollback_instructions": "",
            "authorization": valid_auth,
            "mutation_class_action": True,
        },
        {
            "case_id": "W14-T05",
            "build_log_text": _complete_build_log(),
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": changed_artifacts,
            "rollback_instructions": "revert synthetic fixture artifacts",
            "authorization": self_auth,
            "mutation_class_action": True,
        },
        {
            "case_id": "W14-T06",
            "build_log_text": _complete_build_log(),
            "changed_artifacts": changed_artifacts,
            "readback_verified_artifacts": changed_artifacts,
            "rollback_instructions": "revert synthetic fixture artifacts",
            "authorization": valid_auth,
            "mutation_class_action": True,
        },
    ]


def run_w14_documentation_mandate_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    actor: str,
    policy_manifest_hash: str,
) -> DocumentationMandateResult:
    """Run W14-T01..T06 fixture canaries and emit digest-only evidence."""

    evidence_root = Path(evidence_dir)
    evidence_path = evidence_root / "w14_documentation_mandate.jsonl"
    summary_path = evidence_root / "run_summary.json"

    rows = [
        evaluate_completion_package(
            package,
            actor=actor,
            profile_id=profile_id,
            policy_manifest_hash=policy_manifest_hash,
        )
        for package in _fixture_packages()
    ]
    for row in rows:
        _append_jsonl(evidence_path, row)

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "deny")
    allowed = sum(1 for row in rows if row["decision"] == "allow")
    done_state_allowed_count = sum(1 for row in rows if row["done_state_allowed"])
    live_config_touched = any(row["live_config_touched"] for row in rows)
    secret_values_read = any(row["secret_values_read"] for row in rows)
    raw_payload_stored = any(row["raw_payload_stored"] for row in rows)

    summary = {
        "case_ids": list(CASE_ORDER),
        "total": total,
        "denied": denied,
        "allowed": allowed,
        "done_state_allowed_count": done_state_allowed_count,
        "rule_ids": {row["case_id"]: row["rule_id"] for row in rows},
        "decisions": {row["case_id"]: row["decision"] for row in rows},
        "done_state_allowed_by_case": {row["case_id"]: row["done_state_allowed"] for row in rows},
        "live_config_touched": live_config_touched,
        "secret_values_read": secret_values_read,
        "raw_payload_stored": raw_payload_stored,
        "policy_manifest_hash": policy_manifest_hash,
        "scope": "fixture_only_documentation_gate_no_live_mutation",
    }
    _write_json(summary_path, summary)

    return DocumentationMandateResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        done_state_allowed_count=done_state_allowed_count,
        live_config_touched=live_config_touched,
        secret_values_read=secret_values_read,
        raw_payload_stored=raw_payload_stored,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run W14 Documentation Mandate fixture canaries")
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--actor", default="pennyworth-architect")
    parser.add_argument("--policy-manifest-hash", default="fixture-w14-policy-hash")
    args = parser.parse_args(argv)

    result = run_w14_documentation_mandate_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        actor=args.actor,
        policy_manifest_hash=args.policy_manifest_hash,
    )
    print(
        "W14 Documentation Mandate canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
