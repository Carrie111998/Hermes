import pytest

from devflow_delegation.contract import (
    ContractError,
    MSG_TYPE,
    SCHEMA_VERSION,
    compute_fingerprint,
    parse_request,
    parse_v2_fix_request,
    validate_payload,
)


def valid_payload(**over):
    p = {
        "schema_version": SCHEMA_VERSION,
        "type": MSG_TYPE,
        "idempotency_key": "critic:gateway-health-timeout:2026-08-06:v1",
        "source": {"agent": "critic", "kind": "critic", "finding_id": "F-1"},
        "kind": "bug",
        "title": "Restore bounded gateway health query",
        "problem_statement": "The health query scans all sessions without a LIMIT.",
        "evidence": [
            {"kind": "test_failure", "ref": "tests/test_health.py", "summary": "timeout at 30s"}
        ],
        "target": {"repo": "hermes", "subsystem": "gateway-health"},
        "severity": "high",
        "priority": "P1",
        "confidence": 0.94,
        "proposed_approach": "Add a bounded read with a 3s budget.",
        "acceptance_criteria": ["Health query returns within 3s on a 10k-row state.db"],
        "safety_notes": ["Do not restart the live gateway"],
    }
    p.update(over)
    return p


def test_valid_payload_passes_validation():
    assert validate_payload(valid_payload()) == []


def test_parse_request_builds_work_request_with_stable_fingerprint():
    req = parse_request(valid_payload())
    assert req.request_id.startswith("dwr_")
    assert req.dedup_fingerprint.startswith("sha256:")
    env = req.to_envelope()
    assert env["schema_version"] == "3.0"
    assert env["type"] == MSG_TYPE
    assert env["source"]["agent"] == "critic"
    assert env["target"]["repo"] == "hermes"


@pytest.mark.parametrize("field", ["title", "problem_statement"])
def test_missing_required_text_fields_rejected(field):
    errs = validate_payload(valid_payload(**{field: "   "}))
    assert any(field in e for e in errs)


def test_missing_or_empty_evidence_rejected():
    assert any("evidence" in e for e in validate_payload(valid_payload(evidence=[])))
    assert any("evidence" in e for e in validate_payload(valid_payload(evidence=[{"kind": "x", "summary": ""}])))


def test_missing_acceptance_criteria_rejected():
    errs = validate_payload(valid_payload(acceptance_criteria=[]))
    assert any("acceptance" in e for e in errs)


def test_unsupported_schema_and_type_rejected():
    assert any("schema" in e for e in validate_payload(valid_payload(schema_version="2.0")))
    assert any("type" in e for e in validate_payload(valid_payload(type="SOMETHING_ELSE")))


@pytest.mark.parametrize("bad", [1.5, -0.1, "high", None])
def test_invalid_confidence_rejected(bad):
    assert any("confidence" in e for e in validate_payload(valid_payload(confidence=bad)))


@pytest.mark.parametrize("sev,pri", [("urgent", "P1"), ("high", "P9")])
def test_invalid_severity_or_priority_rejected(sev, pri):
    errs = validate_payload(valid_payload(severity=sev, priority=pri))
    assert errs


def test_fingerprint_stable_across_source_metadata_and_timestamps():
    base = dict(target_repo="hermes", target_subsystem="gateway-health",
                kind="bug", title="Restore bounded gateway health query",
                problem_statement="The health query scans all sessions without a LIMIT.")
    fp1 = compute_fingerprint(**base)
    fp2 = compute_fingerprint(**base)  # same identity, new observation
    assert fp1 == fp2
    assert fp1.startswith("sha256:")


def test_fingerprint_discriminates_meaningful_differences():
    base = dict(target_repo="hermes", target_subsystem="gateway-health",
                kind="bug", title="Restore bounded gateway health query",
                problem_statement="The health query scans all sessions without a LIMIT.")
    fp = compute_fingerprint(**base)
    assert fp != compute_fingerprint(**{**base, "target_subsystem": "cron"})
    assert fp != compute_fingerprint(**{**base, "kind": "improvement"})
    assert fp != compute_fingerprint(**{**base, "title": "A different problem"})
    # case/whitespace normalization must NOT change identity
    assert fp == compute_fingerprint(**{**base, "title": "  restore BOUNDED gateway health QUERY "})


def test_v2_roadmap_envelope_converts_to_v3_payload():
    # Shape pinned by profiles/main/scripts/roadmap_devflow_intake.py::build_envelope
    v2 = {
        "message_id": "m-1",
        "idempotency_key": "roadmap:sr-470:v1",
        "protocol_version": "2.0",
        "type": "DEVFLOW_FIX_REQUEST",
        "from": "roadmap-intake",
        "to": "devflow",
        "payload": {
            "issue": "SR-470",
            "priority": "high",
            "evidence": {"traces_to": "backend_conformance_canary", "source_row": 132,
                         "schema": "B", "roadmap": "architecture-blueprint/roadmap/simplification-roadmap.md"},
            "task": "Collapse the canary's dual probe paths into one helper.",
            "recovery": "Revert the helper split; probes are independent.",
            "safety": "Roadmap-sourced; read-only against roadmap; no autonomous merge/deploy.",
        },
    }
    v3 = parse_v2_fix_request(v2)
    assert v3["idempotency_key"] == "roadmap:sr-470:v1"  # SR idempotency preserved
    assert v3["source"]["agent"] == "roadmap-intake"
    assert v3["source"]["finding_id"] == "SR-470"
    assert v3["kind"] == "task"
    assert v3["priority"] == "P1"  # high -> P1
    assert v3["severity"] == "medium"
    assert v3["target"] == {"repo": "hermes", "subsystem": "roadmap"}
    assert v3["evidence"][0]["ref"].endswith(":L132")
    assert v3["acceptance_criteria"], "compat conversion must enrich acceptance criteria"
    assert validate_payload(v3) == [], "converted v2 payload must be v3-valid"


def test_v2_parser_rejects_non_v2_envelopes():
    with pytest.raises(ContractError):
        parse_v2_fix_request({"type": MSG_TYPE})


def test_parse_request_raises_with_reasons_on_invalid_payload():
    with pytest.raises(ContractError) as ei:
        parse_request(valid_payload(title="", evidence=[]))
    assert len(ei.value.reasons) >= 2
