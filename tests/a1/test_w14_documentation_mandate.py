"""W14 Documentation Mandate completion gate fixture proof.

Fixture-only canaries: no live provider, gateway, proxy, profile/provider config,
service, secret, or protected state is touched. The harness evaluates synthetic
mutation completion packages and proves done-state is denied until the required
vault documentation, read-back, rollback, and current-session authorization
metadata are present.
"""

import json
from pathlib import Path

from hermes_cli.w14_documentation_mandate import run_w14_documentation_mandate_canaries


EXPECTED_RULES = {
    "W14-T01": "w14.documentation.build_log.required",
    "W14-T02": "w14.documentation.required_sections.missing",
    "W14-T03": "w14.documentation.readback.required",
    "W14-T04": "w14.documentation.rollback.required",
    "W14-T05": "w14.documentation.authorization.current_session_required",
    "W14-T06": "w14.documentation.complete.fixture_allowed",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_w14_t01_t06_documentation_mandate_gate_denies_until_completion_package_is_complete(tmp_path):
    result = run_w14_documentation_mandate_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        actor="pennyworth-architect-test",
        policy_manifest_hash="fixture-w14-policy-hash",
    )

    assert result.total == 6
    assert result.denied == 5
    assert result.allowed == 1
    assert result.done_state_allowed_count == 1
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False
    assert Path(result.evidence_path).exists()
    assert Path(result.summary_path).exists()

    rows = _rows(Path(result.evidence_path))
    assert [row["case_id"] for row in rows] == ["W14-T01", "W14-T02", "W14-T03", "W14-T04", "W14-T05", "W14-T06"]

    for row in rows:
        case_id = row["case_id"]
        assert row["rule_id"] == EXPECTED_RULES[case_id]
        assert row["changed_artifact_count"] == 2
        assert row["policy_manifest_hash"] == "fixture-w14-policy-hash"
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_payload_stored"] is False

    by_case = {row["case_id"]: row for row in rows}

    assert by_case["W14-T01"]["decision"] == "deny"
    assert by_case["W14-T01"]["build_log_present"] is False
    assert by_case["W14-T01"]["done_state_allowed"] is False

    assert by_case["W14-T02"]["decision"] == "deny"
    assert by_case["W14-T02"]["build_log_present"] is True
    assert by_case["W14-T02"]["missing_sections"] == ["Verification"]
    assert "Verification" not in by_case["W14-T02"]["required_sections_present"]
    assert by_case["W14-T02"]["done_state_allowed"] is False

    assert by_case["W14-T03"]["decision"] == "deny"
    assert by_case["W14-T03"]["missing_sections"] == []
    assert by_case["W14-T03"]["readback_verified"] is False
    assert by_case["W14-T03"]["done_state_allowed"] is False

    assert by_case["W14-T04"]["decision"] == "deny"
    assert by_case["W14-T04"]["rollback_present"] is False
    assert by_case["W14-T04"]["done_state_allowed"] is False

    assert by_case["W14-T05"]["decision"] == "deny"
    assert by_case["W14-T05"]["authorization_present"] is True
    assert by_case["W14-T05"]["current_session_authorization_present"] is False
    assert by_case["W14-T05"]["agent_self_approval_detected"] is True
    assert by_case["W14-T05"]["done_state_allowed"] is False

    assert by_case["W14-T06"]["decision"] == "allow"
    assert by_case["W14-T06"]["build_log_present"] is True
    assert by_case["W14-T06"]["missing_sections"] == []
    assert by_case["W14-T06"]["readback_verified"] is True
    assert by_case["W14-T06"]["rollback_present"] is True
    assert by_case["W14-T06"]["authorization_present"] is True
    assert by_case["W14-T06"]["current_session_authorization_present"] is True
    assert by_case["W14-T06"]["agent_self_approval_detected"] is False
    assert by_case["W14-T06"]["done_state_allowed"] is True
    assert by_case["W14-T06"]["scope"] == "fixture_only_no_live_mutation"

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["case_ids"] == ["W14-T01", "W14-T02", "W14-T03", "W14-T04", "W14-T05", "W14-T06"]
    assert summary["total"] == 6
    assert summary["denied"] == 5
    assert summary["allowed"] == 1
    assert summary["done_state_allowed_count"] == 1
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
