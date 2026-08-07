"""Stage 1 acceptance gate (spec: Delivery decomposition > Stage 1).

A synthetic explicit request and representative detector findings traverse
validation, target resolution, deduplication, ledger persistence, and
lifecycle telemetry WITHOUT duplicate or mutating side effects. No build,
PR, merge, deploy, worktree, or autonomy sentinel may appear.

Tests 6-8 carry coverage explicitly deferred to this file by the Task 10/11
adjudications: CLI bad-input negative paths, the explicit-adapter passthrough
(a list-typed safety_notes round-tripping intact to the envelope), and the
malformed-confidence e2e negative path.
"""
import io
import json

import pytest

from devflow_delegation import cli
from devflow_delegation.adapters import explicit, nightly_gate, security_audit, watchdog
from devflow_delegation.emitter import DelegationEmitter
from tests.devflow_delegation.conftest import make_delegate_kwargs


@pytest.fixture
def queue_all(hermes_root, allowlist_file):
    (hermes_root / "devflow" / "policy.json").write_text(json.dumps({
        "explicit": {"mode": "queue"},
        "critic": {"mode": "queue"},
        "arch-review": {"mode": "queue"},
        "watchdog": {"mode": "queue"},
        "nightly-gate": {"mode": "queue"},
        "security-audit": {"mode": "queue"},
    }), encoding="utf-8")
    return hermes_root


def test_synthetic_request_traverses_full_control_plane(queue_all):
    em = DelegationEmitter()
    r = em.delegate(**make_delegate_kwargs(
        source={"agent": "main", "kind": "explicit", "finding_id": "acc-1"}))
    assert r.status == "queued"

    # lifecycle telemetry via the CLI transition path (what a triage cron uses)
    assert cli.main(["transition", "--request-id", r.request_id, "--to", "TRIAGED",
                     "--actor", "acceptance"]) == 0
    assert cli.main(["transition", "--request-id", r.request_id, "--to", "PLANNED",
                     "--actor", "acceptance"]) == 0

    row = em.ledger.get_request(r.request_id)
    assert row["state"] == "PLANNED"
    assert [t["to_state"] for t in em.ledger.transitions_for(r.request_id)] == [
        "TRIAGED", "PLANNED"]

    types = [r_["event_type"] for r_ in em.bus._get_conn().execute(
        "SELECT event_type FROM events ORDER BY timestamp").fetchall()]
    assert "devflow.work_requested" in types
    assert "devflow.work_triaged" in types
    assert "devflow.work_planned" in types

    # envelope durable in the canonical inbox -- filename AND parsed content
    files = list(em.inbox_dir.glob("*.json"))
    assert len(files) == 1 and r.request_id in files[0].name
    env = json.loads(files[0].read_text(encoding="utf-8"))
    assert env["request_id"] == r.request_id
    assert env["target"]["repo"] == "hermes"


def test_duplicate_finding_aggregates_into_one_request(queue_all):
    em = DelegationEmitter()
    r1 = em.delegate(**make_delegate_kwargs())
    # insert_request writes NO evidence_log row; the count is exactly 0 until a
    # duplicate lands, so the post-duplicate == 1 below is a real aggregation
    # gate (it also catches a double-append regression, which >= 1 could not).
    assert em.ledger.evidence_count(r1.request_id) == 0
    r2 = em.delegate(**make_delegate_kwargs(
        source={"agent": "watchdog", "kind": "watchdog", "finding_id": "W-2"}))
    assert r1.status == "queued"
    assert r2.status == "duplicate" and r2.request_id == r1.request_id
    assert em.ledger.summary_counts()["total"] == 1
    assert em.ledger.evidence_count(r1.request_id) == 1
    assert len(list(em.inbox_dir.glob("*.json"))) == 1


def test_detector_findings_classify_cleanly_in_dry_run(queue_all, hermes_root):
    # Rebuild the stock-machine posture: no policy.json overrides at all, so
    # every source inherits the shipped default mode=dry_run.
    (hermes_root / "devflow" / "policy.json").unlink()
    em = DelegationEmitter()
    wd = watchdog.delegate_watchdog_alert(em, {
        "component": "gateway", "severity": "high", "persistent": True,
        "detail": "health loop blocked for 3 sweeps"})
    ng = nightly_gate.delegate_gate_failure(em, {
        "culprit": "_dual_path_drift", "failed_command": "python scripts/nightly_gate.py",
        "output": "drifted: x.py"})
    sec = security_audit.delegate_security_finding(em, {
        "package": "chromadb", "cve": "CVE-2026-45829", "affected_versions": "<0.5.24",
        "fixed_version": "0.5.24", "remediation": "pip install chromadb>=0.5.24"})
    hold = security_audit.delegate_security_finding(em, {
        "package": "chromadb", "cve": "CVE-2026-45829", "remediation_status": "HOLD"})
    assert (wd.status, wd.reason) == ("queued", "dry_run")
    assert (ng.status, ng.reason) == ("queued", "dry_run")
    assert (sec.status, sec.reason) == ("queued", "dry_run")
    assert hold.reason == "hold_no_fix"
    # dry-run leaves ZERO durable state
    assert em.ledger.summary_counts()["total"] == 0
    assert not list(em.inbox_dir.glob("*.json"))
    assert em.bus._get_conn().execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


def test_invalid_off_target_and_flooded_findings_never_reach_a_queue(queue_all):
    em = DelegationEmitter()
    # evidence-less -- assert BOTH the outcome and the classifying reason
    ev = em.delegate(**make_delegate_kwargs(evidence=[]))
    assert ev.status == "declined" and ev.reason == "invalid:missing_evidence"
    # off-allowlist target -- same, both fields
    off = em.delegate(**make_delegate_kwargs(target={"repo": "rogue", "subsystem": "x"}))
    assert off.status == "declined" and off.reason == "target_unresolved"
    # flooded source (policy override to 1/window for explicit)
    (em.ledger.db_path.parent / "policy.json").write_text(
        json.dumps({"explicit": {"mode": "queue", "max_per_window": 1}}), encoding="utf-8")
    em2 = DelegationEmitter()
    a = em2.delegate(**make_delegate_kwargs(
        source={"agent": "main", "kind": "explicit"}, title="Flood A"))
    b = em2.delegate(**make_delegate_kwargs(
        source={"agent": "main", "kind": "explicit"}, title="Flood B"))
    assert a.status == "queued" and b.status == "suppressed" and b.reason == "rate_limit_source"


def test_no_stage2_or_stage3_side_effects_or_authority(queue_all, hermes_root):
    em = DelegationEmitter()
    em.delegate(**make_delegate_kwargs())
    # no autonomy sentinel ever created
    from events.paths import autonomy_sentinel_path

    assert not autonomy_sentinel_path().exists()
    # no worktree base created
    assert not (hermes_root / "devflow" / "worktrees").exists()
    # nothing advanced past PLANNED anywhere
    for row in em.ledger.list_requests():
        assert row["state"] in {"REQUESTED", "TRIAGED", "PLANNED", "DECLINED", "SUPPRESSED"}


def test_explicit_adapter_passthrough_and_safety_notes_list(queue_all):
    """Deferred from Task 11: the explicit adapter's passthrough, including a
    LIST-typed safety_notes, exercised end-to-end through the same ledger the
    direct emitter surface uses."""
    em = DelegationEmitter()
    req = make_delegate_kwargs(
        idempotency_key="explicit:acc-passthrough:v1",
        safety_notes=["Do not restart the live gateway", "No schema migrations"])
    r1 = explicit.delegate_explicit(em, req)
    assert r1.status == "queued"

    env = json.loads(next(iter(em.inbox_dir.glob("*.json"))).read_text(encoding="utf-8"))
    assert env["safety_notes"] == ["Do not restart the live gateway", "No schema migrations"]
    assert env["idempotency_key"] == "explicit:acc-passthrough:v1"

    # the adapter went through the SAME ledger: the identical idempotency key
    # via the direct surface dedups instead of double-writing
    r2 = em.delegate(**req)
    assert r2.status == "duplicate" and r2.request_id == r1.request_id
    assert len(list(em.inbox_dir.glob("*.json"))) == 1


def test_malformed_confidence_never_reaches_queue(queue_all):
    """Deferred from Task 11: a non-numeric confidence must DECLINE through the
    contract gate on BOTH surfaces (direct and explicit adapter), never raise,
    and leave zero durable state."""
    em = DelegationEmitter()
    r1 = em.delegate(**make_delegate_kwargs(confidence="high"))
    assert r1.status == "declined" and r1.reason == "invalid:invalid_confidence"
    r2 = explicit.delegate_explicit(em, make_delegate_kwargs(confidence="high"))
    assert r2.status == "declined" and r2.reason == "invalid:invalid_confidence"
    assert em.ledger.summary_counts()["total"] == 0
    assert not list(em.inbox_dir.glob("*.json"))


def test_cli_bad_input_negative_paths(queue_all, monkeypatch):
    """Deferred from Task 10: bad input exits 2 with no side effects. argparse
    SystemExit(2) for an unknown subcommand is intentionally NOT caught by
    main()'s try (parse_args sits outside it)."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["delegate"]) == 2

    with pytest.raises(SystemExit) as ei:
        cli.main(["no-such-subcommand"])
    assert ei.value.code == 2

    assert cli.main(["transition", "--request-id", "no-such-id", "--to", "TRIAGED",
                     "--actor", "acceptance"]) == 2

    assert DelegationEmitter().ledger.summary_counts()["total"] == 0
