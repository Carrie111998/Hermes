"""Fail-closed integrity and policy-drift checks for the authority store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS authority_policy_baselines (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, version INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL, policy_json TEXT NOT NULL, accepted_by TEXT NOT NULL,
    reason TEXT NOT NULL, accepted_at INTEGER NOT NULL,
    UNIQUE(organization_id, version)
);
CREATE TABLE IF NOT EXISTS authority_integrity_runs (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, checked_at INTEGER NOT NULL,
    status TEXT NOT NULL, policy_baseline_id TEXT, expected_policy_sha256 TEXT,
    observed_policy_sha256 TEXT NOT NULL, checks_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authority_integrity_latest
    ON authority_integrity_runs(organization_id, checked_at DESC, id DESC);
CREATE TRIGGER IF NOT EXISTS authority_policy_baselines_immutable_update
BEFORE UPDATE ON authority_policy_baselines
BEGIN SELECT RAISE(ABORT, 'authority policy baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS authority_policy_baselines_immutable_delete
BEFORE DELETE ON authority_policy_baselines
BEGIN SELECT RAISE(ABORT, 'authority policy baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS authority_integrity_runs_immutable_update
BEFORE UPDATE ON authority_integrity_runs
BEGIN SELECT RAISE(ABORT, 'authority integrity runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS authority_integrity_runs_immutable_delete
BEFORE DELETE ON authority_integrity_runs
BEGIN SELECT RAISE(ABORT, 'authority integrity runs are immutable'); END;
"""


@dataclass(frozen=True)
class IntegrityPosture:
    run_id: str
    status: str
    checks: dict[str, Any]
    policy_baseline_id: Optional[str]
    expected_policy_sha256: Optional[str]
    observed_policy_sha256: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def _canonical(policy: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(policy), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def policy_sha256(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(policy).encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def accept_policy_baseline(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    policy: Mapping[str, Any],
    actor: str,
    reason: str,
) -> str:
    """Append an explicitly accepted policy version; never mutate prior policy."""
    ensure_schema(conn)
    canonical = _canonical(policy)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    latest = conn.execute(
        """SELECT id,version,policy_sha256 FROM authority_policy_baselines
           WHERE organization_id=? ORDER BY version DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if latest is not None and str(latest["policy_sha256"]) == digest:
        return str(latest["id"])
    version = int(latest["version"]) + 1 if latest is not None else 1
    baseline_id = f"policy_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO authority_policy_baselines
               (id,organization_id,version,policy_sha256,policy_json,accepted_by,
                reason,accepted_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                baseline_id,
                organization_id,
                version,
                digest,
                canonical,
                actor,
                reason,
                int(time.time()),
            ),
        )
    if latest is not None:
        from hermes_cli import (
            approval_artifacts,
            objectives_db,
            workforce_delegation,
        )

        reason_text = "standing operating charter was revised"
        approval_artifacts.revoke_all_unexecuted(
            conn, actor=actor, reason=reason_text
        )
        objectives_db.revoke_unconsumed_permits(
            conn,
            organization_id=organization_id,
            actor=actor,
            reason=reason_text,
        )
        workforce_delegation.revoke_active_grants(
            conn,
            organization_id=organization_id,
            actor=actor,
            reason=reason_text,
        )
    return baseline_id


def _count(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> int:
    return int(conn.execute(query, params).fetchone()[0])


def run_preflight(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    policy: Mapping[str, Any],
    allow_legacy_baseline: bool = True,
    record_interval_seconds: int = 300,
) -> IntegrityPosture:
    """Verify storage, lineage, policy, and cross-record authority invariants."""
    ensure_schema(conn)
    observed = policy_sha256(policy)
    baseline = conn.execute(
        """SELECT * FROM authority_policy_baselines WHERE organization_id=?
           ORDER BY version DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if baseline is None and allow_legacy_baseline:
        baseline_id = accept_policy_baseline(
            conn,
            organization_id=organization_id,
            policy=policy,
            actor="system:legacy-migration",
            reason="initial integrity baseline for an existing authority store",
        )
        baseline = conn.execute(
            "SELECT * FROM authority_policy_baselines WHERE id=?", (baseline_id,)
        ).fetchone()

    quick_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    foreign_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    from hermes_cli import (
        approval_artifacts,
        business_audit,
        business_commitments,
        organization_db,
        outcome_attribution,
        workforce_delegation,
    )

    checks: dict[str, Any] = {
        "database_quick_check": quick_rows == ["ok"],
        "foreign_key_check": not foreign_rows,
        "audit_chain": business_audit.verify_chain(conn, organization_id),
        "approval_artifacts": approval_artifacts.verify_artifacts(
            conn, organization_id
        ),
        "business_commitment_contracts": business_commitments.verify_contracts(
            conn, organization_id
        ),
        "outcome_attributions": outcome_attribution.verify_attributions(
            conn, organization_id
        ),
        "employee_task_grants": workforce_delegation.verify_grants(
            conn, organization_id
        ),
        "employee_mandate_chains": organization_db.verify_mandate_chains(
            conn, organization_id
        ),
        "policy_baseline_present": baseline is not None,
        "policy_matches_baseline": (
            baseline is not None and str(baseline["policy_sha256"]) == observed
        ),
        "exactly_one_active_ceo": _count(
            conn,
            """SELECT COUNT(*) FROM employees
               WHERE organization_id=? AND level='ceo' AND status='active'""",
            (organization_id,),
        )
        == 1,
        "active_employees_have_mandates": _count(
            conn,
            """SELECT COUNT(*) FROM employees e
               WHERE e.organization_id=? AND e.status='active'
                 AND NOT EXISTS (
                   SELECT 1 FROM employee_mandates m WHERE m.employee_id=e.id
                 )""",
            (organization_id,),
        )
        == 0,
        "action_plan_objective_consistency": _count(
            conn,
            """SELECT COUNT(*) FROM candidate_actions a
               JOIN plans p ON p.id=a.plan_id
               JOIN objectives o ON o.id=a.objective_id
               WHERE o.organization_id=? AND p.objective_id<>a.objective_id""",
            (organization_id,),
        )
        == 0,
        "verification_lineage_consistency": _count(
            conn,
            """SELECT COUNT(*) FROM verification_records v
               JOIN execution_results r ON r.id=v.execution_result_id
               JOIN candidate_actions a ON a.id=r.action_id
               JOIN objectives o ON o.id=v.objective_id
               WHERE o.organization_id=?
                 AND (v.action_id<>r.action_id OR a.objective_id<>v.objective_id)""",
            (organization_id,),
        )
        == 0,
    }
    failures = [name for name, passed in checks.items() if not passed]
    if foreign_rows:
        checks["foreign_key_violation_count"] = len(foreign_rows)
    checks["failed_checks"] = failures
    status = "ready" if not failures else "failed"
    expected = str(baseline["policy_sha256"]) if baseline is not None else None
    baseline_id = str(baseline["id"]) if baseline is not None else None
    evidence = {
        "checks": checks,
        "expected_policy_sha256": expected,
        "observed_policy_sha256": observed,
        "organization_id": organization_id,
        "policy_baseline_id": baseline_id,
        "status": status,
    }
    evidence_json = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
    evidence_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    now = int(time.time())
    latest = conn.execute(
        """SELECT id,checked_at,evidence_sha256 FROM authority_integrity_runs
           WHERE organization_id=? ORDER BY checked_at DESC,id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if (
        latest is not None
        and str(latest["evidence_sha256"]) == evidence_sha256
        and now - int(latest["checked_at"]) < max(1, record_interval_seconds)
    ):
        return IntegrityPosture(
            run_id=str(latest["id"]),
            status=status,
            checks=checks,
            policy_baseline_id=baseline_id,
            expected_policy_sha256=expected,
            observed_policy_sha256=observed,
        )
    run_id = f"integrity_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO authority_integrity_runs
               (id,organization_id,checked_at,status,policy_baseline_id,
                expected_policy_sha256,observed_policy_sha256,checks_json,
                evidence_sha256) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                organization_id,
                now,
                status,
                baseline_id,
                expected,
                observed,
                json.dumps(checks, separators=(",", ":"), sort_keys=True),
                evidence_sha256,
            ),
        )
    return IntegrityPosture(
        run_id=run_id,
        status=status,
        checks=checks,
        policy_baseline_id=baseline_id,
        expected_policy_sha256=expected,
        observed_policy_sha256=observed,
    )


def latest_posture(
    conn: sqlite3.Connection, organization_id: str
) -> Optional[dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT * FROM authority_integrity_runs WHERE organization_id=?
           ORDER BY checked_at DESC,id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["checks"] = json.loads(result.pop("checks_json"))
    return result


def enforce_preflight(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    policy: Mapping[str, Any],
) -> IntegrityPosture:
    """Pause all authority and raise one advisor intervention on failure."""
    posture = run_preflight(
        conn, organization_id=organization_id, policy=policy
    )
    if posture.ready:
        return posture
    from hermes_cli import operational_control

    reason = "authority integrity preflight failed: " + ", ".join(
        posture.checks["failed_checks"]
    )
    state = operational_control.autonomy_state(conn)
    if state["mode"] == "autonomous":
        operational_control.set_autonomy_mode(
            conn, mode="paused", actor="control:integrity", reason=reason
        )
    operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        category="authority_integrity_failure",
        summary="Authority integrity verification failed; autonomy was paused",
        context={
            "integrity_run_id": posture.run_id,
            "failed_checks": posture.checks["failed_checks"],
            "expected_policy_sha256": posture.expected_policy_sha256,
            "observed_policy_sha256": posture.observed_policy_sha256,
        },
        options=[
            {"id": "inspect", "label": "Inspect immutable evidence"},
            {"id": "restore", "label": "Restore the accepted policy or data"},
            {"id": "review_rebaseline", "label": "Review and explicitly accept policy"},
        ],
        dedupe_key=f"authority-integrity:{organization_id}",
    )
    return posture
