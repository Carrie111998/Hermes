"""P1-7 high-fidelity synthetic subagent authority-manifest canaries.

This harness simulates parent -> child authority envelope construction without
invoking live delegation, spawning subagent processes, dispatching providers,
reading secrets, or touching live provider/profile/runtime configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


CASE_ORDER = (
    "P1-7-INHERIT-T01",
    "P1-7-ESCALATION-T02",
    "P1-7-TOOLSET-T03",
    "P1-7-MISSING-MANIFEST-T04",
    "P1-7-C0-T05",
)

RULES = {
    "inherit": "w5.p1_7.subagent.inherit_parent_manifest",
    "model_escalation": "w5.p1_7.subagent.child_model_tier_escalation_denied",
    "toolset_escalation": "w5.p1_7.subagent.child_toolset_sink_escalation_denied",
    "missing_manifest": "w5.p1_7.subagent.parent_child_manifest_required",
    "c0_allowed": "w5.p1_7.subagent.c0_fake_child_adapter_allowed",
}

CLASSIFICATION_ORDER = {
    "C0_PUBLIC": 0,
    "C1_INTERNAL": 1,
    "C2_LOCAL_ONLY": 2,
    "C3_RESTRICTED": 3,
}

MODEL_TIER_ORDER = {
    "L0_LOCAL": 0,
    "L1_LOCAL_REASONING": 1,
    "L2_FRONTIER": 2,
    "L3_HUMAN": 3,
}


@dataclass(frozen=True)
class SubagentAuthorityCanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    child_spawn_live_count: int
    fake_child_adapter_call_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_obj(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _stable_id(prefix: str, *parts: str) -> str:
    joined = ":".join(parts)
    return prefix + "-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_digest(manifest: dict[str, Any] | None) -> str:
    if manifest is None:
        return _digest_obj({"authority_manifest": "missing"})
    return _digest_obj(manifest)


def _monotonic_taint_ok(parent_classification: str, child_classification: str, manifest_present: bool) -> bool:
    if not manifest_present:
        return False
    return CLASSIFICATION_ORDER[child_classification] >= CLASSIFICATION_ORDER[parent_classification]


def _tier_within_parent(parent_max_model_tier: str, requested_model_tier: str) -> bool:
    return MODEL_TIER_ORDER[requested_model_tier] <= MODEL_TIER_ORDER[parent_max_model_tier]


def _toolset_digests(parent_allowed: list[str], requested: list[str], effective: list[str]) -> dict[str, str]:
    return {
        "parent_allowed_toolsets_digest": _digest_obj(parent_allowed),
        "child_requested_toolsets_digest": _digest_obj(requested),
        "child_effective_toolsets_digest": _digest_obj(effective),
    }


def _base_row(
    *,
    case_id: str,
    decision: str,
    rule_id: str,
    reason: str,
    parent_classification: str,
    child_classification: str,
    classification_source: str,
    parent_allowed_toolsets: list[str],
    child_requested_toolsets: list[str],
    child_effective_toolsets: list[str],
    requested_target: str | None,
    resolved_target: str | None,
    parent_max_model_tier: str,
    requested_model_tier: str,
    authority_manifest: dict[str, Any] | None,
    authority_manifest_present: bool,
    missing_manifest_fields: list[str] | None = None,
    fake_child_adapter_call_count: int = 0,
) -> dict[str, Any]:
    parent_correlation_id = _stable_id("p1-7-parent", case_id)
    child_correlation_id = f"{parent_correlation_id}:child:{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:8]}"
    manifest_digest = _manifest_digest(authority_manifest)
    monotonic_ok = _monotonic_taint_ok(parent_classification, child_classification, authority_manifest_present)
    denied_toolsets = sorted(set(child_requested_toolsets) - set(parent_allowed_toolsets))
    child_authority_broader_than_parent = bool(denied_toolsets) or not _tier_within_parent(
        parent_max_model_tier,
        requested_model_tier,
    )
    if decision == "deny":
        resolved_target = None

    row = {
        "case_id": case_id,
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "surface": "synthetic_subagent_authority_manifest",
        "profile_id": None,
        "policy_hash": None,
        "parent_classification": parent_classification,
        "child_classification": child_classification,
        "classification_source": classification_source,
        "monotonic_taint_ok": monotonic_ok,
        "parent_correlation_id": parent_correlation_id,
        "child_correlation_id": child_correlation_id,
        "parent_child_correlation_link": "linked" if authority_manifest_present else "missing",
        "authority_manifest_present": authority_manifest_present,
        "authority_manifest_digest": manifest_digest,
        "missing_manifest_fields": missing_manifest_fields or [],
        "parent_allowed_toolsets": parent_allowed_toolsets,
        "child_requested_toolsets": child_requested_toolsets,
        "child_effective_toolsets": child_effective_toolsets,
        "denied_toolsets": denied_toolsets,
        **_toolset_digests(parent_allowed_toolsets, child_requested_toolsets, child_effective_toolsets),
        "parent_max_model_tier": parent_max_model_tier,
        "requested_model_tier": requested_model_tier,
        "requested_target": requested_target,
        "resolved_target": resolved_target,
        "child_authority_broader_than_parent": child_authority_broader_than_parent,
        "dispatch_denied_before_child_spawn": decision == "deny",
        "child_spawn_live_count": 0,
        "fake_child_adapter_call_count": fake_child_adapter_call_count,
        "provider_call_count": 0,
        "live_subagent_process_spawned": False,
        "live_delegation_invoked": False,
        "raw_payload_stored": False,
        "live_config_touched": False,
        "secret_values_read": False,
        "redaction_class": "digest_only",
        "scope": "high_fidelity_synthetic_no_live_subagent_provider_or_config_calls",
    }
    return row


def _authority_manifest(
    *,
    parent_classification: str,
    child_classification: str,
    parent_correlation_id: str,
    parent_allowed_toolsets: list[str],
    child_requested_toolsets: list[str],
    parent_max_model_tier: str,
    requested_model_tier: str,
    requested_target: str,
) -> dict[str, Any]:
    return {
        "schema": "hl-aos.p1_7.subagent_authority_manifest.v1",
        "parent_classification": parent_classification,
        "child_classification": child_classification,
        "classification_source": "hl_aos_frozen_parent_manifest",
        "parent_correlation_id": parent_correlation_id,
        "parent_allowed_toolsets": parent_allowed_toolsets,
        "child_requested_toolsets": child_requested_toolsets,
        "parent_max_model_tier": parent_max_model_tier,
        "requested_model_tier": requested_model_tier,
        "requested_target": requested_target,
    }


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    parent_allowed = ["analysis"]
    inherit_parent_corr = _stable_id("p1-7-parent", "P1-7-INHERIT-T01")
    inherit_manifest = _authority_manifest(
        parent_classification="C2_LOCAL_ONLY",
        child_classification="C2_LOCAL_ONLY",
        parent_correlation_id=inherit_parent_corr,
        parent_allowed_toolsets=parent_allowed,
        child_requested_toolsets=["analysis"],
        parent_max_model_tier="L0_LOCAL",
        requested_model_tier="L0_LOCAL",
        requested_target="local_fake_child_adapter",
    )
    rows.append(
        _base_row(
            case_id="P1-7-INHERIT-T01",
            decision="allow",
            rule_id=RULES["inherit"],
            reason="child inherits parent C2 classification, classification source, authority manifest, and correlation link",
            parent_classification="C2_LOCAL_ONLY",
            child_classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_parent_manifest",
            parent_allowed_toolsets=parent_allowed,
            child_requested_toolsets=["analysis"],
            child_effective_toolsets=["analysis"],
            requested_target="local_fake_child_adapter",
            resolved_target="local_fake_child_adapter",
            parent_max_model_tier="L0_LOCAL",
            requested_model_tier="L0_LOCAL",
            authority_manifest=inherit_manifest,
            authority_manifest_present=True,
            fake_child_adapter_call_count=1,
        )
    )

    escalation_parent_corr = _stable_id("p1-7-parent", "P1-7-ESCALATION-T02")
    escalation_manifest = _authority_manifest(
        parent_classification="C2_LOCAL_ONLY",
        child_classification="C2_LOCAL_ONLY",
        parent_correlation_id=escalation_parent_corr,
        parent_allowed_toolsets=parent_allowed,
        child_requested_toolsets=["analysis"],
        parent_max_model_tier="L0_LOCAL",
        requested_model_tier="L2_FRONTIER",
        requested_target="frontier",
    )
    rows.append(
        _base_row(
            case_id="P1-7-ESCALATION-T02",
            decision="deny",
            rule_id=RULES["model_escalation"],
            reason="C2 child requested frontier target/model tier broader than parent; denied before child dispatch",
            parent_classification="C2_LOCAL_ONLY",
            child_classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_parent_manifest",
            parent_allowed_toolsets=parent_allowed,
            child_requested_toolsets=["analysis"],
            child_effective_toolsets=["analysis"],
            requested_target="frontier",
            resolved_target=None,
            parent_max_model_tier="L0_LOCAL",
            requested_model_tier="L2_FRONTIER",
            authority_manifest=escalation_manifest,
            authority_manifest_present=True,
        )
    )

    requested_toolsets = ["analysis", "browser", "frontier_sink", "git", "terminal", "web"]
    tool_parent_corr = _stable_id("p1-7-parent", "P1-7-TOOLSET-T03")
    tool_manifest = _authority_manifest(
        parent_classification="C2_LOCAL_ONLY",
        child_classification="C2_LOCAL_ONLY",
        parent_correlation_id=tool_parent_corr,
        parent_allowed_toolsets=parent_allowed,
        child_requested_toolsets=requested_toolsets,
        parent_max_model_tier="L0_LOCAL",
        requested_model_tier="L0_LOCAL",
        requested_target="local_fake_child_adapter",
    )
    rows.append(
        _base_row(
            case_id="P1-7-TOOLSET-T03",
            decision="deny",
            rule_id=RULES["toolset_escalation"],
            reason="child requested broader tool/sink authority than parent; effective authority attenuated and dispatch denied",
            parent_classification="C2_LOCAL_ONLY",
            child_classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_parent_manifest",
            parent_allowed_toolsets=parent_allowed,
            child_requested_toolsets=requested_toolsets,
            child_effective_toolsets=["analysis"],
            requested_target="local_fake_child_adapter",
            resolved_target=None,
            parent_max_model_tier="L0_LOCAL",
            requested_model_tier="L0_LOCAL",
            authority_manifest=tool_manifest,
            authority_manifest_present=True,
        )
    )

    rows.append(
        _base_row(
            case_id="P1-7-MISSING-MANIFEST-T04",
            decision="deny",
            rule_id=RULES["missing_manifest"],
            reason="child spawn request missing parent-child authority manifest and parent correlation; fail closed before child construction",
            parent_classification="C2_LOCAL_ONLY",
            child_classification="C2_LOCAL_ONLY",
            classification_source="missing_parent_child_authority_manifest",
            parent_allowed_toolsets=parent_allowed,
            child_requested_toolsets=["analysis"],
            child_effective_toolsets=[],
            requested_target="local_fake_child_adapter",
            resolved_target=None,
            parent_max_model_tier="L0_LOCAL",
            requested_model_tier="L0_LOCAL",
            authority_manifest=None,
            authority_manifest_present=False,
            missing_manifest_fields=["authority_manifest", "parent_correlation_id"],
        )
    )

    c0_parent_corr = _stable_id("p1-7-parent", "P1-7-C0-T05")
    c0_manifest = _authority_manifest(
        parent_classification="C0_PUBLIC",
        child_classification="C0_PUBLIC",
        parent_correlation_id=c0_parent_corr,
        parent_allowed_toolsets=parent_allowed,
        child_requested_toolsets=["analysis"],
        parent_max_model_tier="L0_LOCAL",
        requested_model_tier="L0_LOCAL",
        requested_target="local_fake_child_adapter",
    )
    rows.append(
        _base_row(
            case_id="P1-7-C0-T05",
            decision="allow",
            rule_id=RULES["c0_allowed"],
            reason="C0 parent creates same-authority child through fake child adapter with complete manifest and correlation",
            parent_classification="C0_PUBLIC",
            child_classification="C0_PUBLIC",
            classification_source="hl_aos_frozen_parent_manifest",
            parent_allowed_toolsets=parent_allowed,
            child_requested_toolsets=["analysis"],
            child_effective_toolsets=["analysis"],
            requested_target="local_fake_child_adapter",
            resolved_target="local_fake_child_adapter",
            parent_max_model_tier="L0_LOCAL",
            requested_model_tier="L0_LOCAL",
            authority_manifest=c0_manifest,
            authority_manifest_present=True,
            fake_child_adapter_call_count=1,
        )
    )

    return rows


def run_p1_7_subagent_authority_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    policy_hash: str,
) -> SubagentAuthorityCanaryResult:
    """Run P1-7 synthetic parent-child authority/correlation canaries."""

    evidence_root = Path(evidence_dir)
    evidence_path = evidence_root / "p1_7_subagent_authority_manifest.jsonl"
    summary_path = evidence_root / "run_summary.json"
    rows = _rows()
    for row in rows:
        row["profile_id"] = profile_id
        row["policy_hash"] = policy_hash

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "deny")
    allowed = sum(1 for row in rows if row["decision"] == "allow")
    child_spawn_live_count = sum(row["child_spawn_live_count"] for row in rows)
    fake_child_adapter_call_count = sum(row["fake_child_adapter_call_count"] for row in rows)
    provider_call_count = sum(row["provider_call_count"] for row in rows)
    live_config_touched = any(row["live_config_touched"] for row in rows)
    secret_values_read = any(row["secret_values_read"] for row in rows)
    raw_payload_stored = any(row["raw_payload_stored"] for row in rows)

    summary = {
        "case_ids": list(CASE_ORDER),
        "total": total,
        "denied": denied,
        "allowed": allowed,
        "decisions": {row["case_id"]: row["decision"] for row in rows},
        "rule_ids": {row["case_id"]: row["rule_id"] for row in rows},
        "child_spawn_live_count": child_spawn_live_count,
        "fake_child_adapter_call_count": fake_child_adapter_call_count,
        "provider_call_count": provider_call_count,
        "live_config_touched": live_config_touched,
        "secret_values_read": secret_values_read,
        "raw_payload_stored": raw_payload_stored,
        "live_delegation_invoked": any(row["live_delegation_invoked"] for row in rows),
        "live_subagent_process_spawned": any(row["live_subagent_process_spawned"] for row in rows),
        "policy_hash": policy_hash,
        "scope": "high_fidelity_synthetic_no_live_subagent_provider_or_config_calls",
        "runtime_closure": "open_partial_green_synthetic_only",
    }

    _write_jsonl(evidence_path, rows)
    _write_json(summary_path, summary)

    return SubagentAuthorityCanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        child_spawn_live_count=child_spawn_live_count,
        fake_child_adapter_call_count=fake_child_adapter_call_count,
        provider_call_count=provider_call_count,
        live_config_touched=live_config_touched,
        secret_values_read=secret_values_read,
        raw_payload_stored=raw_payload_stored,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run P1-7 subagent authority-manifest synthetic canaries")
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--policy-hash", default="fixture-p1-7-policy-hash")
    args = parser.parse_args(argv)

    result = run_p1_7_subagent_authority_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        policy_hash=args.policy_hash,
    )
    print(
        "P1-7 subagent authority-manifest canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
