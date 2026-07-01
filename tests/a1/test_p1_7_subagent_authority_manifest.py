"""P1-7 subagent authority-manifest and correlation canary proof.

High-fidelity synthetic only: no live delegation, subagent process spawning,
provider dispatch, profile/provider config mutation, or secret inspection.
"""

import json
from pathlib import Path

from hermes_cli.p1_7_subagent_authority_manifest import run_p1_7_subagent_authority_canaries


EXPECTED_RULES = {
    "P1-7-INHERIT-T01": "w5.p1_7.subagent.inherit_parent_manifest",
    "P1-7-ESCALATION-T02": "w5.p1_7.subagent.child_model_tier_escalation_denied",
    "P1-7-TOOLSET-T03": "w5.p1_7.subagent.child_toolset_sink_escalation_denied",
    "P1-7-MISSING-MANIFEST-T04": "w5.p1_7.subagent.parent_child_manifest_required",
    "P1-7-C0-T05": "w5.p1_7.subagent.c0_fake_child_adapter_allowed",
}

REQUIRED_FIELDS = {
    "case_id",
    "decision",
    "rule_id",
    "parent_classification",
    "child_classification",
    "classification_source",
    "monotonic_taint_ok",
    "parent_correlation_id",
    "child_correlation_id",
    "parent_child_correlation_link",
    "authority_manifest_digest",
    "parent_allowed_toolsets_digest",
    "parent_allowed_toolsets",
    "child_requested_toolsets_digest",
    "child_requested_toolsets",
    "child_effective_toolsets_digest",
    "child_effective_toolsets",
    "requested_target",
    "resolved_target",
    "child_spawn_live_count",
    "fake_child_adapter_call_count",
    "provider_call_count",
    "raw_payload_stored",
    "live_config_touched",
    "secret_values_read",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p1_7_subagent_authority_manifest_attentuates_and_correlates_child_envelopes(tmp_path):
    result = run_p1_7_subagent_authority_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        policy_hash="fixture-p1-7-policy-hash",
    )

    assert result.total == 5
    assert result.denied == 3
    assert result.allowed == 2
    assert result.child_spawn_live_count == 0
    assert result.fake_child_adapter_call_count == 2
    assert result.provider_call_count == 0
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False

    evidence_path = Path(result.evidence_path)
    summary_path = Path(result.summary_path)
    assert evidence_path.exists()
    assert summary_path.exists()

    rows = _rows(evidence_path)
    assert [row["case_id"] for row in rows] == [
        "P1-7-INHERIT-T01",
        "P1-7-ESCALATION-T02",
        "P1-7-TOOLSET-T03",
        "P1-7-MISSING-MANIFEST-T04",
        "P1-7-C0-T05",
    ]

    for row in rows:
        assert REQUIRED_FIELDS.issubset(row.keys())
        assert row["rule_id"] == EXPECTED_RULES[row["case_id"]]
        assert row["profile_id"] == "architect-fixture"
        assert row["policy_hash"] == "fixture-p1-7-policy-hash"
        assert row["authority_manifest_digest"].startswith("sha256:")
        assert row["parent_allowed_toolsets_digest"].startswith("sha256:")
        assert row["child_requested_toolsets_digest"].startswith("sha256:")
        assert row["child_effective_toolsets_digest"].startswith("sha256:")
        assert row["parent_correlation_id"].startswith("p1-7-parent-")
        assert row["child_correlation_id"].startswith(row["parent_correlation_id"] + ":child:")
        assert row["child_spawn_live_count"] == 0
        assert row["provider_call_count"] == 0
        assert row["live_subagent_process_spawned"] is False
        assert row["live_delegation_invoked"] is False
        assert row["raw_payload_stored"] is False
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False

    by_case = {row["case_id"]: row for row in rows}

    inherit = by_case["P1-7-INHERIT-T01"]
    assert inherit["decision"] == "allow"
    assert inherit["parent_classification"] == "C2_LOCAL_ONLY"
    assert inherit["child_classification"] == "C2_LOCAL_ONLY"
    assert inherit["classification_source"] == "hl_aos_frozen_parent_manifest"
    assert inherit["monotonic_taint_ok"] is True
    assert inherit["parent_child_correlation_link"] == "linked"
    assert inherit["requested_target"] == "local_fake_child_adapter"
    assert inherit["resolved_target"] == "local_fake_child_adapter"
    assert inherit["child_effective_toolsets"] == ["analysis"]
    assert inherit["fake_child_adapter_call_count"] == 1

    escalation = by_case["P1-7-ESCALATION-T02"]
    assert escalation["decision"] == "deny"
    assert escalation["parent_classification"] == "C2_LOCAL_ONLY"
    assert escalation["child_classification"] == "C2_LOCAL_ONLY"
    assert escalation["requested_target"] == "frontier"
    assert escalation["resolved_target"] is None
    assert escalation["requested_model_tier"] == "L2_FRONTIER"
    assert escalation["parent_max_model_tier"] == "L0_LOCAL"
    assert escalation["dispatch_denied_before_child_spawn"] is True
    assert escalation["fake_child_adapter_call_count"] == 0

    toolset = by_case["P1-7-TOOLSET-T03"]
    assert toolset["decision"] == "deny"
    assert toolset["requested_target"] == "local_fake_child_adapter"
    assert toolset["resolved_target"] is None
    assert toolset["parent_allowed_toolsets"] == ["analysis"]
    assert toolset["child_requested_toolsets"] == ["analysis", "browser", "frontier_sink", "git", "terminal", "web"]
    assert toolset["child_effective_toolsets"] == ["analysis"]
    assert toolset["denied_toolsets"] == ["browser", "frontier_sink", "git", "terminal", "web"]
    assert toolset["dispatch_denied_before_child_spawn"] is True

    missing = by_case["P1-7-MISSING-MANIFEST-T04"]
    assert missing["decision"] == "deny"
    assert missing["authority_manifest_present"] is False
    assert missing["missing_manifest_fields"] == ["authority_manifest", "parent_correlation_id"]
    assert missing["parent_child_correlation_link"] == "missing"
    assert missing["monotonic_taint_ok"] is False
    assert missing["dispatch_denied_before_child_spawn"] is True
    assert missing["fake_child_adapter_call_count"] == 0

    positive = by_case["P1-7-C0-T05"]
    assert positive["decision"] == "allow"
    assert positive["parent_classification"] == "C0_PUBLIC"
    assert positive["child_classification"] == "C0_PUBLIC"
    assert positive["monotonic_taint_ok"] is True
    assert positive["requested_target"] == "local_fake_child_adapter"
    assert positive["resolved_target"] == "local_fake_child_adapter"
    assert positive["child_requested_toolsets"] == ["analysis"]
    assert positive["child_effective_toolsets"] == ["analysis"]
    assert positive["fake_child_adapter_call_count"] == 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_ids"] == [
        "P1-7-INHERIT-T01",
        "P1-7-ESCALATION-T02",
        "P1-7-TOOLSET-T03",
        "P1-7-MISSING-MANIFEST-T04",
        "P1-7-C0-T05",
    ]
    assert summary["total"] == 5
    assert summary["denied"] == 3
    assert summary["allowed"] == 2
    assert summary["child_spawn_live_count"] == 0
    assert summary["fake_child_adapter_call_count"] == 2
    assert summary["provider_call_count"] == 0
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
    assert summary["runtime_closure"] == "open_partial_green_synthetic_only"

    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "confidential child raw prompt" not in evidence_text
    assert "SECRET" not in evidence_text
