"""Exact, expiring, one-use human approval artifacts for governed actions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_artifacts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT NOT NULL,
    action_id TEXT NOT NULL, intervention_id TEXT NOT NULL UNIQUE,
    action_contract_sha256 TEXT NOT NULL, objective_scope_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, capability TEXT NOT NULL, target_system TEXT NOT NULL,
    target_resource TEXT, estimated_cost_minor INTEGER NOT NULL, currency TEXT,
    policy_version TEXT NOT NULL, state_evidence_sha256 TEXT,
    approved_by TEXT NOT NULL, approval_evidence_json TEXT NOT NULL,
    approval_evidence_sha256 TEXT NOT NULL, approved_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL, status TEXT NOT NULL,
    consumed_at INTEGER, consumed_by_permit_id TEXT, revoked_at INTEGER,
    revoked_by TEXT, revocation_reason TEXT,
    FOREIGN KEY(objective_id) REFERENCES objectives(id),
    FOREIGN KEY(action_id) REFERENCES candidate_actions(id),
    FOREIGN KEY(intervention_id) REFERENCES intervention_queue(id)
);
CREATE INDEX IF NOT EXISTS idx_approval_artifacts_status
    ON approval_artifacts(organization_id,status,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_artifacts_live_action
    ON approval_artifacts(action_id)
    WHERE status IN ('issued','materialized');
CREATE TRIGGER IF NOT EXISTS permits_exact_approval_guard
BEFORE INSERT ON permits
WHEN NEW.approval_artifact_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM approval_artifacts a
   WHERE a.id=NEW.approval_artifact_id
     AND a.action_id=NEW.action_id
     AND a.payload_sha256=NEW.payload_sha256
     AND a.capability=NEW.capability
     AND a.policy_version=NEW.policy_version
     AND a.status='issued'
     AND a.expires_at>NEW.issued_at
)
BEGIN SELECT RAISE(ABORT, 'permit lacks exact live approval artifact'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_contract_immutable
BEFORE UPDATE ON approval_artifacts
WHEN OLD.organization_id IS NOT NEW.organization_id
  OR OLD.objective_id IS NOT NEW.objective_id
  OR OLD.action_id IS NOT NEW.action_id
  OR OLD.intervention_id IS NOT NEW.intervention_id
  OR OLD.action_contract_sha256 IS NOT NEW.action_contract_sha256
  OR OLD.objective_scope_sha256 IS NOT NEW.objective_scope_sha256
  OR OLD.payload_sha256 IS NOT NEW.payload_sha256
  OR OLD.capability IS NOT NEW.capability
  OR OLD.target_system IS NOT NEW.target_system
  OR OLD.target_resource IS NOT NEW.target_resource
  OR OLD.estimated_cost_minor IS NOT NEW.estimated_cost_minor
  OR OLD.currency IS NOT NEW.currency
  OR OLD.policy_version IS NOT NEW.policy_version
  OR OLD.state_evidence_sha256 IS NOT NEW.state_evidence_sha256
  OR OLD.approved_by IS NOT NEW.approved_by
  OR OLD.approval_evidence_json IS NOT NEW.approval_evidence_json
  OR OLD.approval_evidence_sha256 IS NOT NEW.approval_evidence_sha256
  OR OLD.approved_at IS NOT NEW.approved_at
  OR OLD.expires_at IS NOT NEW.expires_at
BEGIN SELECT RAISE(ABORT, 'approval artifact contract is immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_status_guard
BEFORE UPDATE OF status ON approval_artifacts
WHEN NOT (
  OLD.status=NEW.status
  OR (OLD.status='issued' AND NEW.status IN ('materialized','revoked','expired'))
  OR (OLD.status='materialized' AND NEW.status IN ('consumed','revoked','expired'))
)
BEGIN SELECT RAISE(ABORT, 'invalid approval artifact transition'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_materialization_guard
BEFORE UPDATE ON approval_artifacts
WHEN NEW.status='materialized' AND (
  NEW.consumed_by_permit_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM permits p WHERE p.id=NEW.consumed_by_permit_id
      AND p.action_id=NEW.action_id AND p.approval_artifact_id=NEW.id
      AND p.consumed_at IS NULL AND p.revoked_at IS NULL
  )
)
BEGIN SELECT RAISE(ABORT, 'approval materialization lacks exact permit'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_consumption_guard
BEFORE UPDATE ON approval_artifacts
WHEN NEW.status='consumed' AND (
  NEW.consumed_at IS NULL OR NEW.consumed_by_permit_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM permits p WHERE p.id=NEW.consumed_by_permit_id
      AND p.action_id=NEW.action_id AND p.approval_artifact_id=NEW.id
      AND p.consumed_at IS NOT NULL
  )
)
BEGIN SELECT RAISE(ABORT, 'approval consumption lacks executed permit'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_revocation_guard
BEFORE UPDATE ON approval_artifacts
WHEN NEW.status='revoked' AND (
  NEW.revoked_at IS NULL OR length(trim(COALESCE(NEW.revoked_by,'')))=0
  OR length(trim(COALESCE(NEW.revocation_reason,'')))=0
)
BEGIN SELECT RAISE(ABORT, 'approval revocation lacks authority evidence'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_resolution_immutable
BEFORE UPDATE ON approval_artifacts
WHEN (OLD.consumed_at IS NOT NULL AND OLD.consumed_at IS NOT NEW.consumed_at)
 OR (OLD.consumed_by_permit_id IS NOT NULL
     AND OLD.consumed_by_permit_id IS NOT NEW.consumed_by_permit_id)
 OR (OLD.revoked_at IS NOT NULL AND OLD.revoked_at IS NOT NEW.revoked_at)
 OR (OLD.revoked_by IS NOT NULL AND OLD.revoked_by IS NOT NEW.revoked_by)
 OR (OLD.revocation_reason IS NOT NULL
     AND OLD.revocation_reason IS NOT NEW.revocation_reason)
BEGIN SELECT RAISE(ABORT, 'approval resolution evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_artifacts_immutable_delete
BEFORE DELETE ON approval_artifacts
BEGIN SELECT RAISE(ABORT, 'approval artifacts cannot be deleted'); END;
"""


class ApprovalArtifactError(PermissionError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='approval_artifacts'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _audit(
    conn: sqlite3.Connection,
    artifact_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    permit_id: Optional[str] = None,
) -> None:
    from hermes_cli import business_audit

    row = conn.execute(
        """SELECT organization_id,objective_id,action_id
             FROM approval_artifacts WHERE id=?""",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ApprovalArtifactError("approval artifact disappeared before audit")
    business_audit.append(
        conn,
        organization_id=str(row["organization_id"]),
        event_type=event_type,
        objective_id=str(row["objective_id"]),
        action_id=str(row["action_id"]),
        permit_id=permit_id,
        payload={"approval_artifact_id": artifact_id, **dict(payload)},
    )


def _action_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "objective_id": str(row["objective_id"]),
        "plan_id": str(row["plan_id"]),
        "action_type": str(row["action_type"]),
        "payload_sha256": str(row["payload_sha256"]),
        "expected_outcome": str(row["expected_outcome"]),
        "required_capability": str(row["required_capability"]),
        "verification_method": str(row["verification_method"]),
        "risk_class": str(row["risk_class"]),
        "reversible": int(row["reversible"]),
        "compensation_action_type": row["compensation_action_type"],
        "compensation_payload_json": row["compensation_payload_json"],
        "compensation_capability": row["compensation_capability"],
        "compensation_verification_method": row[
            "compensation_verification_method"
        ],
        "estimated_cost_minor": int(row["estimated_cost_minor"] or 0),
    }


def _objective_scope(conn: sqlite3.Connection, objective_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM objectives WHERE id=?", (objective_id,)).fetchone()
    if row is None:
        raise ApprovalArtifactError("approval objective no longer exists")
    return {
        "organization_id": str(row["organization_id"]),
        "desired_outcome": str(row["desired_outcome"]),
        "constraints_json": str(row["constraints_json"]),
        "authority_scope_json": str(row["authority_scope_json"]),
        "success_criteria_json": str(row["success_criteria_json"]),
        "termination_json": str(row["termination_json"]),
        "permitted_systems_json": str(row["permitted_systems_json"]),
        "prohibited_actions_json": str(row["prohibited_actions_json"]),
        "max_spend_minor": row["max_spend_minor"],
        "currency": row["currency"],
        "expires_at": row["expires_at"],
    }


def issue_for_intervention(
    conn: sqlite3.Connection,
    *,
    intervention_id: str,
    organization_id: str,
    actor: str,
    evidence: Mapping[str, Any],
    now: Optional[int] = None,
    default_ttl_seconds: int = 900,
    maximum_ttl_seconds: int = 3600,
) -> str:
    """Issue exact authority only for an eligible recorded escalation."""
    ensure_schema(conn)
    if not actor.startswith("human:") or not evidence:
        raise ApprovalArtifactError(
            "exact action approval requires human identity and evidence"
        )
    intervention = conn.execute(
        """SELECT * FROM intervention_queue
            WHERE id=? AND organization_id=? AND status='open'""",
        (intervention_id, organization_id),
    ).fetchone()
    if (
        intervention is None
        or intervention["category"] != "authority_insufficient"
        or intervention["action_id"] is None
    ):
        raise ApprovalArtifactError("intervention is not an open action approval")
    context = json.loads(intervention["context_json"])
    if context.get("approval_eligible") is not True:
        raise ApprovalArtifactError(
            "this authority gap requires policy change, not one-time approval"
        )
    action = conn.execute(
        "SELECT * FROM candidate_actions WHERE id=?",
        (intervention["action_id"],),
    ).fetchone()
    if action is None or action["status"] != "proposed":
        raise ApprovalArtifactError("approved action is no longer proposed")
    objective_scope = _objective_scope(conn, str(action["objective_id"]))
    if objective_scope["organization_id"] != intervention["organization_id"]:
        raise ApprovalArtifactError("approval organization binding is inconsistent")
    payload = json.loads(action["payload_json"])
    ts = int(time.time()) if now is None else int(now)
    requested_expiry = evidence.get("expires_at")
    expires_at = (
        int(requested_expiry)
        if isinstance(requested_expiry, int)
        and not isinstance(requested_expiry, bool)
        else ts + default_ttl_seconds
    )
    if expires_at <= ts or expires_at > ts + maximum_ttl_seconds:
        raise ApprovalArtifactError("approval expiry exceeds the bounded TTL")
    observed_at = payload.get("observed_state_at")
    max_age = payload.get("max_state_age_seconds")
    if isinstance(observed_at, int) and isinstance(max_age, int) and max_age > 0:
        expires_at = min(expires_at, observed_at + max_age)
        if expires_at <= ts:
            raise ApprovalArtifactError(
                "action state evidence expired before approval"
            )
    state_evidence = payload.get("state_evidence")
    evidence_json = _json(dict(evidence))
    artifact_id = f"approval_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO approval_artifacts
               (id,organization_id,objective_id,action_id,intervention_id,
                action_contract_sha256,objective_scope_sha256,payload_sha256,
                capability,target_system,target_resource,estimated_cost_minor,
                currency,policy_version,state_evidence_sha256,approved_by,
                approval_evidence_json,approval_evidence_sha256,approved_at,
                expires_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'issued')""",
            (
                artifact_id,
                objective_scope["organization_id"],
                action["objective_id"],
                action["id"],
                intervention_id,
                _hash(_action_material(action)),
                _hash(objective_scope),
                action["payload_sha256"],
                action["required_capability"],
                str(payload.get("system") or ""),
                str(payload.get("target_resource") or "") or None,
                int(action["estimated_cost_minor"] or 0),
                objective_scope["currency"],
                str(context["policy_version"]),
                _hash(state_evidence) if isinstance(state_evidence, Mapping) else None,
                actor,
                evidence_json,
                hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
                ts,
                expires_at,
            ),
        )
    _audit(
        conn,
        artifact_id,
        "approval.issued",
        {
            "approved_by": actor,
            "approval_evidence_sha256": hashlib.sha256(
                evidence_json.encode("utf-8")
            ).hexdigest(),
            "expires_at": expires_at,
            "policy_version": str(context["policy_version"]),
        },
    )
    return artifact_id


def validate_for_action(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    action_id: str,
    policy_version: str,
    now: Optional[int] = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if row is None or row["status"] != "issued":
        raise ApprovalArtifactError("approval artifact is unavailable or already used")
    ts = int(time.time()) if now is None else int(now)
    if int(row["expires_at"]) <= ts:
        with conn:
            conn.execute(
                """UPDATE approval_artifacts SET status='expired'
                   WHERE id=? AND status='issued'""",
                (artifact_id,),
            )
        raise ApprovalArtifactError("approval artifact expired")
    if row["action_id"] != action_id or row["policy_version"] != policy_version:
        raise ApprovalArtifactError("approval action or policy version does not match")
    action = conn.execute(
        "SELECT * FROM candidate_actions WHERE id=?", (action_id,)
    ).fetchone()
    if action is None or action["status"] != "proposed":
        raise ApprovalArtifactError("approved action is no longer proposed")
    if _hash(_action_material(action)) != row["action_contract_sha256"]:
        raise ApprovalArtifactError("approved action contract changed")
    if _hash(_objective_scope(conn, str(action["objective_id"]))) != row[
        "objective_scope_sha256"
    ]:
        raise ApprovalArtifactError("approved objective authority scope changed")
    payload = json.loads(action["payload_json"])
    if (
        str(action["payload_sha256"]) != row["payload_sha256"]
        or str(action["required_capability"]) != row["capability"]
        or str(payload.get("system") or "") != row["target_system"]
        or (str(payload.get("target_resource") or "") or None)
        != row["target_resource"]
        or int(action["estimated_cost_minor"] or 0)
        != int(row["estimated_cost_minor"])
    ):
        raise ApprovalArtifactError("approved action parameters changed")
    return dict(row)


def bind_permit(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    permit_id: str,
) -> None:
    with conn:
        updated = conn.execute(
            """UPDATE approval_artifacts SET status='materialized',
               consumed_by_permit_id=? WHERE id=? AND status='issued'""",
            (permit_id, artifact_id),
        )
        if updated.rowcount != 1:
            raise ApprovalArtifactError("approval artifact was materialized concurrently")
    _audit(
        conn,
        artifact_id,
        "approval.materialized",
        {"permit_id": permit_id},
        permit_id=permit_id,
    )


def mark_consumed(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    permit_id: str,
    now: Optional[int] = None,
) -> None:
    ts = int(time.time()) if now is None else int(now)
    with conn:
        updated = conn.execute(
            """UPDATE approval_artifacts SET status='consumed',consumed_at=?
               WHERE id=? AND status='materialized'
                 AND consumed_by_permit_id=?""",
            (ts, artifact_id, permit_id),
        )
        if updated.rowcount != 1:
            raise ApprovalArtifactError(
                "approval artifact is not materialized to this permit"
            )
    _audit(
        conn,
        artifact_id,
        "approval.consumed",
        {"permit_id": permit_id, "consumed_at": ts},
        permit_id=permit_id,
    )


def revoke(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    actor: str,
    reason: str,
) -> None:
    if not actor.startswith("human:") or not reason.strip():
        raise ApprovalArtifactError("revocation requires human actor and reason")
    with conn:
        row = conn.execute(
            """SELECT status,consumed_by_permit_id FROM approval_artifacts
               WHERE id=?""",
            (artifact_id,),
        ).fetchone()
        if row is None or row["status"] not in {"issued", "materialized"}:
            raise ApprovalArtifactError(
                "only unexecuted approval authority can be revoked"
            )
        updated = conn.execute(
            """UPDATE approval_artifacts SET status='revoked',revoked_at=?,
               revoked_by=?,revocation_reason=?
               WHERE id=? AND status IN ('issued','materialized')""",
            (int(time.time()), actor, reason.strip(), artifact_id),
        )
        if updated.rowcount != 1:
            raise ApprovalArtifactError("approval revocation raced with execution")
        if row["consumed_by_permit_id"]:
            conn.execute(
                """UPDATE permits SET revoked_at=?
                   WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL""",
                (int(time.time()), row["consumed_by_permit_id"]),
            )
            conn.execute(
                """UPDATE candidate_actions SET status='expired',updated_at=?
                   WHERE id=(SELECT action_id FROM permits WHERE id=?)
                     AND status='permitted'""",
                (int(time.time()), row["consumed_by_permit_id"]),
            )
    _audit(
        conn,
        artifact_id,
        "approval.revoked",
        {"revoked_by": actor, "reason": reason.strip()},
        permit_id=(
            str(row["consumed_by_permit_id"])
            if row["consumed_by_permit_id"]
            else None
        ),
    )


def revoke_all_unexecuted(
    conn: sqlite3.Connection,
    *,
    actor: str,
    reason: str,
) -> list[str]:
    """Revoke every unexecuted approval when the master authority generation changes."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT id,consumed_by_permit_id FROM approval_artifacts
           WHERE status IN ('issued','materialized') ORDER BY approved_at,id"""
    ).fetchall()
    revoked: list[str] = []
    ts = int(time.time())
    for row in rows:
        with conn:
            conn.execute(
                """UPDATE approval_artifacts SET status='revoked',revoked_at=?,
                   revoked_by=?,revocation_reason=?
                   WHERE id=? AND status IN ('issued','materialized')""",
                (ts, actor, reason.strip(), row["id"]),
            )
            if row["consumed_by_permit_id"]:
                conn.execute(
                    """UPDATE permits SET revoked_at=COALESCE(revoked_at,?)
                       WHERE id=? AND consumed_at IS NULL""",
                    (ts, row["consumed_by_permit_id"]),
                )
            conn.execute(
                """UPDATE candidate_actions SET status='expired',updated_at=?
                   WHERE id=(SELECT action_id FROM approval_artifacts WHERE id=?)
                     AND status IN ('proposed','permitted')""",
                (ts, row["id"]),
            )
        _audit(
            conn,
            str(row["id"]),
            "approval.revoked",
            {"revoked_by": actor, "reason": reason.strip()},
            permit_id=(
                str(row["consumed_by_permit_id"])
                if row["consumed_by_permit_id"]
                else None
            ),
        )
        revoked.append(str(row["id"]))
    return revoked


def expire_due(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[str]:
    """Expire unused authority and wake replanning without executing the action."""
    ensure_schema(conn)
    from hermes_cli import objectives_db

    ts = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        """SELECT id,objective_id,action_id,consumed_by_permit_id
             FROM approval_artifacts
            WHERE status IN ('issued','materialized') AND expires_at<=?
            ORDER BY expires_at,id""",
        (ts,),
    ).fetchall()
    expired = []
    for row in rows:
        with conn:
            conn.execute(
                """UPDATE approval_artifacts SET status='expired'
                   WHERE id=? AND status IN ('issued','materialized')""",
                (row["id"],),
            )
            if row["consumed_by_permit_id"]:
                conn.execute(
                    """UPDATE permits SET revoked_at=?
                       WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL""",
                    (ts, row["consumed_by_permit_id"]),
                )
            conn.execute(
                """UPDATE candidate_actions SET status='expired',updated_at=?
                   WHERE id=? AND status IN ('proposed','permitted')""",
                (ts, row["action_id"]),
            )
            conn.execute(
                """UPDATE objective_inbox SET status='failed',last_error=?,
                   claimed_by=NULL,claim_expires=NULL,updated_at=?
                   WHERE dedupe_key=? AND status IN ('pending','processing')""",
                (
                    "exact approval expired before execution",
                    ts,
                    f"approval-granted:{row['id']}",
                ),
            )
        objectives_db.enqueue_objective_event(
            conn,
            objective_id=str(row["objective_id"]),
            event_type="approval.expired",
            payload={
                "approval_artifact_id": str(row["id"]),
                "action_id": str(row["action_id"]),
                "expired_at": ts,
            },
            dedupe_key=f"approval-expired:{row['id']}",
        )
        _audit(
            conn,
            str(row["id"]),
            "approval.expired",
            {"expired_at": ts},
            permit_id=(
                str(row["consumed_by_permit_id"])
                if row["consumed_by_permit_id"]
                else None
            ),
        )
        expired.append(str(row["id"]))
    return expired


def approval_posture(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    ensure_schema(conn)
    now = int(time.time())
    rows = conn.execute(
        """SELECT id,objective_id,action_id,capability,target_system,
                  target_resource,estimated_cost_minor,currency,policy_version,
                  approved_by,approved_at,expires_at,status,consumed_at,
                  consumed_by_permit_id
             FROM approval_artifacts WHERE organization_id=?
            ORDER BY approved_at DESC,id DESC LIMIT 50""",
        (organization_id,),
    ).fetchall()
    return {
        "pending_count": sum(
            1
            for row in rows
            if row["status"] in {"issued", "materialized"}
            and int(row["expires_at"]) > now
        ),
        "artifacts": [dict(row) for row in rows],
        "evidence_included": False,
        "limit": 50,
    }


def verify_artifacts(conn: sqlite3.Connection, organization_id: str) -> bool:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM approval_artifacts WHERE organization_id=?",
        (organization_id,),
    ).fetchall()
    for row in rows:
        action = conn.execute(
            "SELECT * FROM candidate_actions WHERE id=?", (row["action_id"],)
        ).fetchone()
        if action is None:
            return False
        if (
            _hash(_action_material(action)) != row["action_contract_sha256"]
            or _hash(_objective_scope(conn, str(row["objective_id"])))
            != row["objective_scope_sha256"]
            or hashlib.sha256(
                str(row["approval_evidence_json"]).encode("utf-8")
            ).hexdigest()
            != row["approval_evidence_sha256"]
        ):
            return False
        if row["status"] in {"materialized", "consumed"}:
            permit = conn.execute(
                """SELECT action_id,approval_artifact_id,consumed_at
                     FROM permits WHERE id=?""",
                (row["consumed_by_permit_id"],),
            ).fetchone()
            if (
                permit is None
                or permit["action_id"] != row["action_id"]
                or permit["approval_artifact_id"] != row["id"]
            ):
                return False
            if row["status"] == "consumed" and (
                row["consumed_at"] is None or permit["consumed_at"] is None
            ):
                return False
    return True
