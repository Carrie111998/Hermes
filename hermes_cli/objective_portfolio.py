"""Governed objective decomposition and immutable portfolio lineage."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from functools import wraps
from typing import Any, Mapping, Optional

from hermes_cli import objectives_db


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objective_relationships (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    parent_objective_id TEXT NOT NULL,
    child_objective_id TEXT NOT NULL UNIQUE,
    relationship TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL DEFAULT '',
    allocated_budget_minor INTEGER NOT NULL,
    currency TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objective_relationships_parent
    ON objective_relationships(parent_objective_id,created_at);
CREATE TRIGGER IF NOT EXISTS objective_relationships_immutable_update
BEFORE UPDATE ON objective_relationships
BEGIN SELECT RAISE(ABORT, 'objective relationships are immutable'); END;
CREATE TRIGGER IF NOT EXISTS objective_relationships_immutable_delete
BEFORE DELETE ON objective_relationships
BEGIN SELECT RAISE(ABORT, 'objective relationships are immutable'); END;
"""


TERMINAL = frozenset(
    {"closed", "cancelled", "expired", "abandoned", "superseded", "verified"}
)


def _serialized_portfolio_mutation(function):
    """Serialize portfolio admission checks with SQLite's write lock.

    Budget, active-objective, and successor checks must observe one coherent
    snapshot.  A deferred transaction lets two runtimes both pass those
    checks before either inserts its child; ``BEGIN IMMEDIATE`` makes the
    admission decision and materialization one serialized operation on the
    supported single-host authority store.
    """

    @wraps(function)
    def wrapped(conn, *args, **kwargs):
        ensure_schema(conn)
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            result = function(conn, *args, **kwargs)
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
        else:
            if owns_transaction:
                conn.commit()
            return result

    return wrapped


def ensure_schema(conn: sqlite3.Connection) -> None:
    if not (
        conn.in_transaction
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='objective_relationships'"
        ).fetchone()
    ):
        conn.executescript(SCHEMA_SQL)
    columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(objective_relationships)"
        )
    }
    if "request_sha256" not in columns:
        conn.execute(
            "ALTER TABLE objective_relationships ADD COLUMN "
            "request_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _request_sha256(payload: Mapping[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


@_serialized_portfolio_mutation
def create_child_objective(
    conn: sqlite3.Connection,
    *,
    parent_objective_id: str,
    desired_outcome: str,
    success_criteria: list[Mapping[str, Any]],
    termination_conditions: list[Any],
    permitted_systems: list[str],
    prohibited_actions: list[str],
    constraints: Any,
    allocated_budget_minor: int,
    currency: Optional[str],
    expires_at: int,
    idempotency_key: str,
    created_by: str,
    max_active_objectives: int,
) -> tuple[str, bool]:
    """Create and wake one accepted child without expanding parent authority."""
    if len(idempotency_key.strip()) < 16:
        raise ValueError("child objective requires a high-entropy idempotency key")
    if max_active_objectives <= 0:
        raise ValueError("max_active_objectives must be positive")
    request_sha256 = _request_sha256(
        {
            "desired_outcome": desired_outcome,
            "success_criteria": success_criteria,
            "termination_conditions": termination_conditions,
            "permitted_systems": permitted_systems,
            "prohibited_actions": prohibited_actions,
            "constraints": constraints,
            "allocated_budget_minor": allocated_budget_minor,
            "currency": currency,
            "expires_at": expires_at,
            "created_by": created_by,
        }
    )
    existing = conn.execute(
        """SELECT parent_objective_id,child_objective_id,relationship,
                      request_sha256
             FROM objective_relationships WHERE idempotency_key=?""",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["parent_objective_id"]) != parent_objective_id
            or str(existing["relationship"]) != "decomposes_to"
            or not str(existing["request_sha256"])
            or str(existing["request_sha256"]) != request_sha256
        ):
            raise PermissionError(
                "objective idempotency key belongs to a different operation"
            )
        return str(existing["child_objective_id"]), False
    parent = objectives_db.objective_to_dict(conn, parent_objective_id)
    if parent["status"] in TERMINAL or parent["status"] == "proposed":
        raise ValueError("parent objective is not active")
    now = int(time.time())
    if expires_at <= now:
        raise ValueError("child objective expiry must be in the future")
    if parent["expires_at"] is not None and expires_at > int(parent["expires_at"]):
        raise ValueError("child objective cannot outlive its parent")
    if allocated_budget_minor < 0:
        raise ValueError("child objective budget must be non-negative")
    parent_budget = parent["max_spend_minor"]
    if parent_budget is None and allocated_budget_minor:
        raise PermissionError("unbudgeted parent cannot allocate child spend")
    allocated = int(
        conn.execute(
            """SELECT COALESCE(SUM(r.allocated_budget_minor),0) AS allocated
               FROM objective_relationships r
               JOIN objectives child ON child.id=r.child_objective_id
              WHERE r.parent_objective_id=?
                AND child.status NOT IN (
                  'closed','cancelled','expired','abandoned','superseded','verified'
                )""",
            (parent_objective_id,),
        ).fetchone()["allocated"]
    )
    if parent_budget is not None and (
        allocated + allocated_budget_minor > int(parent_budget)
    ):
        raise PermissionError("child objectives exceed the parent budget allocation")
    parent_systems = set(parent["permitted_systems"])
    if not set(permitted_systems).issubset(parent_systems):
        raise PermissionError("child objective expands permitted systems")
    parent_prohibited = set(parent["prohibited_actions"])
    if not parent_prohibited.issubset(set(prohibited_actions)):
        raise PermissionError("child objective removes a parent prohibition")
    if not success_criteria or any(
        not isinstance(item, Mapping)
        or not str(item.get("verifier") or "")
        or not isinstance(item.get("params", {}), Mapping)
        for item in success_criteria
    ):
        raise ValueError("child objective requires structured success verifiers")
    active_count = int(
        conn.execute(
            """SELECT COUNT(*) AS n FROM objectives
               WHERE organization_id=? AND status NOT IN (
                 'closed','cancelled','expired','abandoned','superseded','verified'
               )""",
            (parent["organization_id"],),
        ).fetchone()["n"]
    )
    if active_count >= max_active_objectives:
        raise PermissionError("organization active-objective ceiling reached")
    resolved_currency = currency or parent["currency"]
    if parent["currency"] and resolved_currency != parent["currency"]:
        raise PermissionError("child objective currency differs from parent")
    child = objectives_db.create_objective(
        conn,
        organization_id=parent["organization_id"],
        desired_outcome=desired_outcome,
        originator=created_by,
        owner=parent["owner"],
        constraints=constraints,
        authority_scope=parent["authority_scope"],
        success_criteria=success_criteria,
        termination_conditions=termination_conditions,
        permitted_systems=permitted_systems,
        prohibited_actions=prohibited_actions,
        max_spend_minor=allocated_budget_minor,
        currency=resolved_currency,
        expires_at=expires_at,
    )
    objectives_db.transition_objective(
        conn, child.id, "accepted", actor=created_by,
        reason=f"decomposed from {parent_objective_id}",
    )
    relationship_id = f"relationship_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO objective_relationships (
                 id,organization_id,parent_objective_id,child_objective_id,
                 relationship,idempotency_key,request_sha256,
                 allocated_budget_minor,currency,created_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                relationship_id,
                parent["organization_id"],
                parent_objective_id,
                child.id,
                "decomposes_to",
                idempotency_key,
                request_sha256,
                allocated_budget_minor,
                resolved_currency,
                created_by,
                now,
            ),
        )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=child.id,
        event_type="objective.accepted",
        payload={
            "parent_objective_id": parent_objective_id,
            "relationship_id": relationship_id,
        },
        dedupe_key=f"child-objective-accepted:{relationship_id}",
    )
    return child.id, True


@_serialized_portfolio_mutation
def create_successor_objective(
    conn: sqlite3.Connection,
    *,
    predecessor_objective_id: str,
    desired_outcome: str,
    success_criteria: list[Mapping[str, Any]],
    termination_conditions: list[Any],
    permitted_systems: list[str],
    prohibited_actions: list[str],
    constraints: Any,
    allocated_budget_minor: int,
    currency: Optional[str],
    expires_at: int,
    idempotency_key: str,
    created_by: str,
    max_active_objectives: int,
) -> tuple[str, bool]:
    """Create one peer root that continues the organization's objective chain."""
    if len(idempotency_key.strip()) < 16:
        raise ValueError("successor objective requires a high-entropy idempotency key")
    request_sha256 = _request_sha256(
        {
            "desired_outcome": desired_outcome,
            "success_criteria": success_criteria,
            "termination_conditions": termination_conditions,
            "permitted_systems": permitted_systems,
            "prohibited_actions": prohibited_actions,
            "constraints": constraints,
            "allocated_budget_minor": allocated_budget_minor,
            "currency": currency,
            "expires_at": expires_at,
            "created_by": created_by,
        }
    )
    existing = conn.execute(
        """SELECT parent_objective_id,child_objective_id,relationship,
                      request_sha256
             FROM objective_relationships
            WHERE idempotency_key=?""",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["parent_objective_id"]) != predecessor_objective_id
            or str(existing["relationship"]) != "succeeds"
            or not str(existing["request_sha256"])
            or str(existing["request_sha256"]) != request_sha256
        ):
            raise PermissionError(
                "objective idempotency key belongs to a different operation"
            )
        return str(existing["child_objective_id"]), False
    predecessor = objectives_db.objective_to_dict(conn, predecessor_objective_id)
    if predecessor["organization_id"] == "__unscoped__":
        raise ValueError("unscoped objectives cannot establish business succession")
    if predecessor["status"] in TERMINAL or predecessor["status"] == "proposed":
        raise ValueError("predecessor objective is not active")
    if conn.execute(
        """SELECT 1 FROM objective_relationships
            WHERE child_objective_id=? AND relationship='decomposes_to'""",
        (predecessor_objective_id,),
    ).fetchone():
        raise ValueError("only a top-level objective can create a successor")
    active_successor = conn.execute(
        """SELECT r.child_objective_id
             FROM objective_relationships r
             JOIN objectives o ON o.id=r.child_objective_id
            WHERE r.parent_objective_id=? AND r.relationship='succeeds'
              AND o.status NOT IN (
                'closed','cancelled','expired','abandoned','superseded','verified'
              )
            LIMIT 1""",
        (predecessor_objective_id,),
    ).fetchone()
    if active_successor is not None:
        raise PermissionError("predecessor already has an active successor")
    if max_active_objectives <= 0:
        raise ValueError("max_active_objectives must be positive")
    now = int(time.time())
    if expires_at <= now:
        raise ValueError("successor objective expiry must be in the future")
    if allocated_budget_minor < 0:
        raise ValueError("successor objective budget must be non-negative")
    predecessor_budget = predecessor["max_spend_minor"]
    if predecessor_budget is None and allocated_budget_minor:
        raise PermissionError("unbudgeted predecessor cannot establish successor spend")
    if (
        predecessor_budget is not None
        and allocated_budget_minor > int(predecessor_budget)
    ):
        raise PermissionError("successor budget exceeds predecessor authority")
    if not set(permitted_systems).issubset(set(predecessor["permitted_systems"])):
        raise PermissionError("successor objective expands permitted systems")
    predecessor_prohibited = set(predecessor["prohibited_actions"])
    if not predecessor_prohibited.issubset(set(prohibited_actions)):
        raise PermissionError("successor objective removes a predecessor prohibition")
    if not success_criteria or any(
        not isinstance(item, Mapping)
        or not str(item.get("verifier") or "")
        or not isinstance(item.get("params", {}), Mapping)
        for item in success_criteria
    ):
        raise ValueError("successor objective requires structured success verifiers")
    active_count = int(
        conn.execute(
            """SELECT COUNT(*) AS n FROM objectives
                WHERE organization_id=? AND status NOT IN (
                  'closed','cancelled','expired','abandoned','superseded','verified'
                )""",
            (predecessor["organization_id"],),
        ).fetchone()["n"]
    )
    if active_count >= max_active_objectives:
        raise PermissionError("organization active-objective ceiling reached")
    resolved_currency = currency or predecessor["currency"]
    if predecessor["currency"] and resolved_currency != predecessor["currency"]:
        raise PermissionError("successor objective currency differs from predecessor")
    successor = objectives_db.create_objective(
        conn,
        organization_id=predecessor["organization_id"],
        desired_outcome=desired_outcome,
        originator=created_by,
        owner=predecessor["owner"],
        constraints=constraints,
        authority_scope=predecessor["authority_scope"],
        success_criteria=success_criteria,
        termination_conditions=termination_conditions,
        permitted_systems=permitted_systems,
        prohibited_actions=prohibited_actions,
        max_spend_minor=allocated_budget_minor,
        currency=resolved_currency,
        expires_at=expires_at,
    )
    objectives_db.transition_objective(
        conn,
        successor.id,
        "accepted",
        actor=created_by,
        reason=f"succeeds {predecessor_objective_id}",
    )
    relationship_id = f"relationship_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO objective_relationships (
                 id,organization_id,parent_objective_id,child_objective_id,
                 relationship,idempotency_key,request_sha256,
                 allocated_budget_minor,currency,created_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                relationship_id,
                predecessor["organization_id"],
                predecessor_objective_id,
                successor.id,
                "succeeds",
                idempotency_key,
                request_sha256,
                allocated_budget_minor,
                resolved_currency,
                created_by,
                now,
            ),
        )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=successor.id,
        event_type="objective.accepted",
        payload={
            "predecessor_objective_id": predecessor_objective_id,
            "relationship_id": relationship_id,
            "relationship": "succeeds",
        },
        dedupe_key=f"successor-objective-accepted:{relationship_id}",
    )
    _transfer_operating_cadence(
        conn,
        predecessor_objective_id=predecessor_objective_id,
        successor_objective_id=successor.id,
        organization_id=str(predecessor["organization_id"]),
    )
    return successor.id, True


def _transfer_operating_cadence(
    conn: sqlite3.Connection,
    *,
    predecessor_objective_id: str,
    successor_objective_id: str,
    organization_id: str,
) -> None:
    """Move the built-in CEO cadence without cloning arbitrary schedules."""
    from hermes_cli import objective_triggers

    objective_triggers.ensure_schema(conn)
    row = conn.execute(
        """SELECT * FROM objective_schedules
            WHERE objective_id=? AND event_type='ceo.operating_review'
              AND status='active'
            ORDER BY created_at,id LIMIT 1""",
        (predecessor_objective_id,),
    ).fetchone()
    if row is None:
        return
    objective_triggers.create_schedule(
        conn,
        organization_id=organization_id,
        objective_id=successor_objective_id,
        event_type="ceo.operating_review",
        interval_seconds=int(row["interval_seconds"]),
        next_fire_at=int(row["next_fire_at"]),
        payload=json.loads(row["payload_json"]),
        idempotency_key=(
            f"ceo-operating-cadence:{organization_id}:{successor_objective_id}"
        ),
    )
    objective_triggers.set_trigger_status(
        conn,
        organization_id=organization_id,
        trigger_id=str(row["id"]),
        status="disabled",
    )


def final_root_requires_successor(
    conn: sqlite3.Connection, objective_id: str
) -> bool:
    """Return true only when closing this objective would leave no active root."""
    ensure_schema(conn)
    objective = objectives_db.objective_to_dict(conn, objective_id)
    organization_id = str(objective["organization_id"])
    if organization_id == "__unscoped__":
        return False
    if conn.execute(
        """SELECT 1 FROM objective_relationships
            WHERE child_objective_id=? AND relationship='decomposes_to'""",
        (objective_id,),
    ).fetchone():
        return False
    row = conn.execute(
        """SELECT 1
             FROM objectives other
            WHERE other.organization_id=? AND other.id<>?
              AND other.status NOT IN (
                'closed','cancelled','expired','abandoned','superseded','verified'
              )
              AND NOT EXISTS (
                    SELECT 1 FROM objective_relationships r
                     WHERE r.child_objective_id=other.id
                       AND r.relationship='decomposes_to'
                  )
            LIMIT 1""",
        (organization_id, objective_id),
    ).fetchone()
    return row is None


def cancel_child_by_creation_key(
    conn: sqlite3.Connection,
    *,
    parent_objective_id: str,
    idempotency_key: str,
    actor: str,
) -> str:
    """Cancel the exact child identified before its creation action executed."""
    ensure_schema(conn)
    row = conn.execute(
        """SELECT r.child_objective_id,o.status
           FROM objective_relationships r
           JOIN objectives o ON o.id=r.child_objective_id
          WHERE r.parent_objective_id=? AND r.idempotency_key=?""",
        (parent_objective_id, idempotency_key),
    ).fetchone()
    if row is None:
        raise ValueError("child objective relationship not found")
    child_id = str(row["child_objective_id"])
    if row["status"] == "cancelled":
        return child_id
    if row["status"] in TERMINAL:
        raise ValueError(f"child objective is already terminal: {row['status']}")
    objectives_db.transition_objective(
        conn,
        child_id,
        "cancelled",
        actor=actor,
        reason=f"compensation for parent {parent_objective_id}",
    )
    return child_id
