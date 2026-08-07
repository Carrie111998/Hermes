from devflow_delegation.adapters import (
    critic, curator, explicit, matcher_shadow, nightly_gate, roadmap,
    security_audit, tracker, watchdog,
)
from tests.devflow_delegation.conftest import make_delegate_kwargs


def test_explicit_passthrough_queues_in_queue_mode(emitter):
    r = explicit.delegate_explicit(emitter, make_delegate_kwargs(mode=None))
    # default policy is dry_run -> classification only
    assert r.status == "queued" and r.reason == "dry_run"


def test_critic_actionable_vs_commentary(emitter):
    finding = {
        "kind": "proposal", "proposal_id": "P-9",
        "title": "Raise orchestrator success weight", "problem_statement": "Underrated skill.",
        "evidence": [{"kind": "critic_evidence", "ref": "audit", "summary": "3 hits"}],
        "acceptance_criteria": ["metadata updated"], "target": {"repo": "hermes", "subsystem": "skills"},
        "severity": "medium", "priority": "P2", "confidence": 0.8,
        "reversal_path": "profiles/critic/workspace/reversals/x.json",
    }
    r = critic.delegate_critic_finding(emitter, finding)
    assert r.status == "queued" and r.reason == "dry_run"
    assert critic.delegate_critic_finding(emitter, {**finding, "kind": "commentary"}).reason == "non_actionable"
    assert critic.delegate_critic_finding(emitter, {**finding, "kind": "rejected_evidence"}).reason == "non_actionable"
    assert critic.delegate_critic_finding(emitter, {**finding, "proposal_id": None}).reason == "missing_finding_id"


def test_roadmap_row_preserves_sr_idempotency(emitter):
    row = {"line": 132, "id": "SR-470", "status": "OPEN",
           "item": "Collapse the canary's dual probe paths into one helper.",
           "priority": "high", "reversibility": "Revert the helper split.",
           "traces_to": "backend_conformance_canary", "schema": "B"}
    r = roadmap.delegate_roadmap_row(emitter, row, "architecture-blueprint/roadmap/simplification-roadmap.md")
    assert r.status == "queued" and r.reason == "dry_run"
    # idempotency must match the legacy producer's key exactly
    from devflow_delegation import contract
    kw = roadmap._roadmap_kwargs(row, "architecture-blueprint/roadmap/simplification-roadmap.md")
    assert kw["idempotency_key"] == "roadmap:sr-470:v1"


def test_watchdog_requires_persistent_high_severity(emitter):
    base = {"component": "gateway", "severity": "high", "detail": "health loop blocked",
            "alert_id": "W-1", "target": {"repo": "hermes", "subsystem": "gateway-health"}}
    assert watchdog.delegate_watchdog_alert(emitter, {**base, "persistent": True}).status == "queued"
    assert watchdog.delegate_watchdog_alert(emitter, {**base, "state_transition": True}).status == "queued"
    assert watchdog.delegate_watchdog_alert(emitter, base).reason == "transient"
    assert watchdog.delegate_watchdog_alert(emitter, {**base, "persistent": True, "severity": "info"}).reason == "below_severity"


def test_nightly_gate_rejects_generic_red(emitter):
    good = {"culprit": "_dual_path_drift", "failed_command": "python scripts/nightly_gate.py",
            "output": "drifted: foo.py", "subsystem": "nightly-gate"}
    assert nightly_gate.delegate_gate_failure(emitter, good).status == "queued"
    assert nightly_gate.delegate_gate_failure(emitter, {"output": "RED"}).reason == "generic_red_headline"
    assert nightly_gate.delegate_gate_failure(emitter, {**good, "culprit": "  "}).reason == "generic_red_headline"


def test_nightly_gate_bounds_output(emitter):
    huge = {"culprit": "suite", "failed_command": "pytest", "output": "x" * 10000}
    r = nightly_gate.delegate_gate_failure(emitter, huge)
    assert r.status == "queued"  # bounded internally; no exception


def test_security_audit_hold_and_verification(emitter):
    finding = {"package": "chromadb", "cve": "CVE-2026-45829", "affected_versions": "<0.5.24",
               "fixed_version": "0.5.24", "remediation": "pip install chromadb>=0.5.24",
               "remediation_status": "VERIFIED"}
    assert security_audit.delegate_security_finding(emitter, finding).status == "queued"
    assert security_audit.delegate_security_finding(emitter, {**finding, "remediation_status": "HOLD"}).reason == "hold_no_fix"
    assert security_audit.delegate_security_finding(emitter, {**finding, "hold": True}).reason == "hold_no_fix"
    assert security_audit.delegate_security_finding(emitter, {**finding, "fixed_version": None}).reason == "unverified_finding"


def test_tracker_requires_repro(emitter):
    finding = {"title": "Stage move drops partial", "problem_statement": "...",
               "repro": "run matcher twice", "evidence": [{"kind": "repro", "ref": "log", "summary": "s"}],
               "acceptance_criteria": ["partial persists"], "target": {"repo": "hermes", "subsystem": "tracker"}}
    assert tracker.delegate_tracker_defect(emitter, finding).status == "queued"
    assert tracker.delegate_tracker_defect(emitter, {**finding, "repro": ""}).reason == "not_reproducible"


def test_curator_and_matcher_shadow_threshold(emitter):
    finding = {"title": "Prune stale embeddings", "problem_statement": "...",
               "meets_evidence_threshold": True,
               "evidence": [{"kind": "curator_evidence", "ref": "log", "summary": "s"}],
               "acceptance_criteria": ["pruned"], "target": {"repo": "hermes", "subsystem": "memory"}}
    assert curator.delegate_curator_change(emitter, finding).status == "queued"
    assert curator.delegate_curator_change(emitter, {**finding, "meets_evidence_threshold": False}).reason == "below_evidence_threshold"
    assert matcher_shadow.delegate_matcher_shadow_change(emitter, finding).status == "queued"
    assert matcher_shadow.delegate_matcher_shadow_change(emitter, {**finding, "meets_evidence_threshold": 0}).reason == "below_evidence_threshold"
