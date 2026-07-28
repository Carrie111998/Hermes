"""Authoritative KPI observations and deterministic strategy review cycles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional

from hermes_cli import objective_portfolio, objectives_db, operational_control


SCALE = 1_000_000
TERMINAL_OBJECTIVE_STATUSES = (
    "verified",
    "closed",
    "cancelled",
    "expired",
    "abandoned",
    "superseded",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS business_metrics (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    metric_key TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
    preferred_direction TEXT NOT NULL, source_system TEXT NOT NULL,
    verifier TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(organization_id,metric_key)
);
CREATE TABLE IF NOT EXISTS metric_observations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, metric_id TEXT NOT NULL,
    value_scaled INTEGER NOT NULL, observed_at INTEGER NOT NULL,
    source_reference_hash TEXT NOT NULL, verifier TEXT NOT NULL,
    evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    UNIQUE(metric_id,source_reference_hash),
    FOREIGN KEY(metric_id) REFERENCES business_metrics(id)
);
CREATE TABLE IF NOT EXISTS metric_ingestion_receipts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    observation_id TEXT NOT NULL, route_name TEXT NOT NULL,
    delivery_id_hash TEXT NOT NULL, authentication_json TEXT NOT NULL,
    authentication_sha256 TEXT NOT NULL, received_at INTEGER NOT NULL,
    UNIQUE(organization_id,route_name,delivery_id_hash),
    FOREIGN KEY(observation_id) REFERENCES metric_observations(id)
);
CREATE TABLE IF NOT EXISTS metric_targets (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT NOT NULL,
    metric_id TEXT NOT NULL, comparator TEXT NOT NULL,
    target_scaled INTEGER NOT NULL, review_interval_seconds INTEGER NOT NULL,
    max_observation_age_seconds INTEGER NOT NULL,
    first_review_at INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
    FOREIGN KEY(metric_id) REFERENCES business_metrics(id),
    FOREIGN KEY(objective_id) REFERENCES objectives(id)
);
CREATE TABLE IF NOT EXISTS metric_target_state (
    target_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    next_review_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    FOREIGN KEY(target_id) REFERENCES metric_targets(id)
);
CREATE TABLE IF NOT EXISTS metric_target_evaluations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, target_id TEXT NOT NULL,
    scheduled_for INTEGER NOT NULL, observation_id TEXT,
    verdict TEXT NOT NULL, actual_scaled INTEGER,
    target_scaled INTEGER NOT NULL, evaluated_at INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(target_id,scheduled_for)
);
CREATE TABLE IF NOT EXISTS strategy_experiments (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, objective_id TEXT NOT NULL,
    name TEXT NOT NULL, hypothesis TEXT NOT NULL, metric_id TEXT NOT NULL,
    comparator TEXT NOT NULL, success_threshold_scaled INTEGER NOT NULL,
    starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
    max_spend_minor INTEGER NOT NULL, currency TEXT,
    idempotency_key TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(metric_id) REFERENCES business_metrics(id),
    FOREIGN KEY(objective_id) REFERENCES objectives(id)
);
CREATE TABLE IF NOT EXISTS strategy_experiment_state (
    experiment_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    reason TEXT, updated_at INTEGER NOT NULL,
    FOREIGN KEY(experiment_id) REFERENCES strategy_experiments(id)
);
CREATE TABLE IF NOT EXISTS strategy_experiment_evaluations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL UNIQUE, observation_id TEXT,
    verdict TEXT NOT NULL, actual_scaled INTEGER,
    threshold_scaled INTEGER NOT NULL, evaluated_at INTEGER NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_target_reviews
    ON metric_target_state(status,next_review_at);
CREATE INDEX IF NOT EXISTS idx_strategy_experiment_reviews
    ON strategy_experiment_state(status);
CREATE TRIGGER IF NOT EXISTS business_metrics_immutable_update
BEFORE UPDATE ON business_metrics
BEGIN SELECT RAISE(ABORT, 'business metrics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS business_metrics_immutable_delete
BEFORE DELETE ON business_metrics
BEGIN SELECT RAISE(ABORT, 'business metrics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_observations_immutable_update
BEFORE UPDATE ON metric_observations
BEGIN SELECT RAISE(ABORT, 'metric observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_observations_immutable_delete
BEFORE DELETE ON metric_observations
BEGIN SELECT RAISE(ABORT, 'metric observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_ingestion_receipts_immutable_update
BEFORE UPDATE ON metric_ingestion_receipts
BEGIN SELECT RAISE(ABORT, 'metric ingestion receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_ingestion_receipts_immutable_delete
BEFORE DELETE ON metric_ingestion_receipts
BEGIN SELECT RAISE(ABORT, 'metric ingestion receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_targets_immutable_update
BEFORE UPDATE ON metric_targets
BEGIN SELECT RAISE(ABORT, 'metric targets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_targets_immutable_delete
BEFORE DELETE ON metric_targets
BEGIN SELECT RAISE(ABORT, 'metric targets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_target_evaluations_immutable_update
BEFORE UPDATE ON metric_target_evaluations
BEGIN SELECT RAISE(ABORT, 'metric target evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS metric_target_evaluations_immutable_delete
BEFORE DELETE ON metric_target_evaluations
BEGIN SELECT RAISE(ABORT, 'metric target evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiments_immutable_update
BEFORE UPDATE ON strategy_experiments
BEGIN SELECT RAISE(ABORT, 'strategy experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiments_immutable_delete
BEFORE DELETE ON strategy_experiments
BEGIN SELECT RAISE(ABORT, 'strategy experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_evaluations_immutable_update
BEFORE UPDATE ON strategy_experiment_evaluations
BEGIN SELECT RAISE(ABORT, 'strategy experiment evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_evaluations_immutable_delete
BEFORE DELETE ON strategy_experiment_evaluations
BEGIN SELECT RAISE(ABORT, 'strategy experiment evaluations are immutable'); END;
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(metric_targets)")
        }
        if "max_observation_age_seconds" in columns:
            return
    conn.executescript(SCHEMA_SQL)
    columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(metric_targets)")
    }
    if "max_observation_age_seconds" not in columns:
        conn.execute(
            """ALTER TABLE metric_targets ADD COLUMN
               max_observation_age_seconds INTEGER NOT NULL DEFAULT 86400"""
        )


def _objective_org(conn: sqlite3.Connection, objective_id: str) -> str:
    row = conn.execute(
        "SELECT organization_id FROM objectives WHERE id=?", (objective_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"objective not found: {objective_id}")
    return str(row["organization_id"])


def register_metric(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    metric_key: str,
    name: str,
    unit: str,
    preferred_direction: str,
    source_system: str,
    verifier: str,
    idempotency_key: str,
    created_by: str,
) -> tuple[str, bool]:
    ensure_schema(conn)
    if preferred_direction not in {"increase", "decrease", "maintain"}:
        raise ValueError("metric preferred_direction is invalid")
    if not all(
        str(value).strip()
        for value in (organization_id, metric_key, name, unit, source_system, verifier)
    ):
        raise ValueError("metric definition fields are required")
    if len(idempotency_key.strip()) < 16:
        raise ValueError("metric requires a high-entropy idempotency key")
    existing = conn.execute(
        """SELECT * FROM business_metrics WHERE idempotency_key=?
              OR (organization_id=? AND metric_key=?)""",
        (idempotency_key, organization_id, metric_key),
    ).fetchone()
    expected = (
        organization_id,
        metric_key,
        name,
        unit,
        preferred_direction,
        source_system,
        verifier,
    )
    if existing is not None:
        actual = tuple(
            str(existing[key])
            for key in (
                "organization_id",
                "metric_key",
                "name",
                "unit",
                "preferred_direction",
                "source_system",
                "verifier",
            )
        )
        if actual != expected:
            raise PermissionError("metric identity was reused with different parameters")
        return str(existing["id"]), False
    metric_id = f"metric_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO business_metrics (
                 id,organization_id,metric_key,name,unit,preferred_direction,
                 source_system,verifier,idempotency_key,created_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                metric_id,
                organization_id,
                metric_key,
                name,
                unit,
                preferred_direction,
                source_system,
                verifier,
                idempotency_key,
                created_by,
                int(time.time()),
            ),
        )
    return metric_id, True


def record_observation(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    metric_id: str,
    value_scaled: int,
    observed_at: int,
    source_reference: str,
    verifier: str,
    evidence: Mapping[str, Any],
) -> tuple[str, bool]:
    ensure_schema(conn)
    metric = conn.execute(
        """SELECT organization_id,verifier FROM business_metrics WHERE id=?""",
        (metric_id,),
    ).fetchone()
    if metric is None or str(metric["organization_id"]) != organization_id:
        raise PermissionError("metric does not belong to organization")
    if str(metric["verifier"]) != verifier:
        raise PermissionError("observation verifier differs from metric contract")
    if observed_at <= 0 or not source_reference or not evidence:
        raise ValueError("observation requires time, source reference, and evidence")
    source_hash = hashlib.sha256(source_reference.encode()).hexdigest()
    existing = conn.execute(
        """SELECT id,value_scaled,evidence_sha256 FROM metric_observations
            WHERE metric_id=? AND source_reference_hash=?""",
        (metric_id, source_hash),
    ).fetchone()
    evidence_json = _json(evidence)
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
    if existing is not None:
        if (
            int(existing["value_scaled"]) != int(value_scaled)
            or str(existing["evidence_sha256"]) != evidence_hash
        ):
            raise PermissionError(
                "metric source reference was reused with different evidence"
            )
        return str(existing["id"]), False
    observation_id = f"observation_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO metric_observations (
                 id,organization_id,metric_id,value_scaled,observed_at,
                 source_reference_hash,verifier,evidence_json,evidence_sha256,
                 recorded_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id,
                organization_id,
                metric_id,
                int(value_scaled),
                observed_at,
                source_hash,
                verifier,
                evidence_json,
                evidence_hash,
                int(time.time()),
            ),
        )
    return observation_id, True


def record_ingestion_receipt(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    observation_id: str,
    route_name: str,
    delivery_id: str,
    authentication_evidence: Mapping[str, Any],
    received_at: Optional[int] = None,
) -> tuple[str, bool]:
    """Persist transport authentication separately from metric value evidence."""
    ensure_schema(conn)
    if not route_name or not delivery_id or not authentication_evidence:
        raise ValueError("ingestion receipt requires route, delivery, and authentication")
    observation = conn.execute(
        """SELECT organization_id FROM metric_observations WHERE id=?""",
        (observation_id,),
    ).fetchone()
    if (
        observation is None
        or str(observation["organization_id"]) != organization_id
    ):
        raise PermissionError("ingestion receipt observation belongs elsewhere")
    delivery_hash = hashlib.sha256(delivery_id.encode()).hexdigest()
    authentication_json = _json(authentication_evidence)
    authentication_hash = hashlib.sha256(authentication_json.encode()).hexdigest()
    existing = conn.execute(
        """SELECT id,observation_id,authentication_sha256
             FROM metric_ingestion_receipts
            WHERE organization_id=? AND route_name=? AND delivery_id_hash=?""",
        (organization_id, route_name, delivery_hash),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["observation_id"]) != observation_id
            or str(existing["authentication_sha256"]) != authentication_hash
        ):
            raise PermissionError(
                "metric delivery identity was reused with different evidence"
            )
        return str(existing["id"]), False
    receipt_id = f"metricreceipt_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO metric_ingestion_receipts (
                 id,organization_id,observation_id,route_name,delivery_id_hash,
                 authentication_json,authentication_sha256,received_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                receipt_id,
                organization_id,
                observation_id,
                route_name,
                delivery_hash,
                authentication_json,
                authentication_hash,
                int(time.time()) if received_at is None else int(received_at),
            ),
        )
    return receipt_id, True


def define_target(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    metric_id: str,
    comparator: str,
    target_scaled: int,
    review_interval_seconds: int,
    max_observation_age_seconds: int,
    first_review_at: int,
    idempotency_key: str,
    created_by: str,
) -> tuple[str, bool]:
    ensure_schema(conn)
    if comparator not in {"gte", "lte"}:
        raise ValueError("target comparator must be gte or lte")
    if _objective_org(conn, objective_id) != organization_id:
        raise PermissionError("target objective belongs to another organization")
    metric = conn.execute(
        "SELECT organization_id FROM business_metrics WHERE id=?", (metric_id,)
    ).fetchone()
    if metric is None or str(metric["organization_id"]) != organization_id:
        raise PermissionError("target metric belongs to another organization")
    if (
        review_interval_seconds <= 0
        or max_observation_age_seconds <= 0
        or first_review_at <= int(time.time())
    ):
        raise ValueError(
            "target requires a future review, positive interval, and evidence age"
        )
    if len(idempotency_key.strip()) < 16:
        raise ValueError("target requires a high-entropy idempotency key")
    existing = conn.execute(
        """SELECT * FROM metric_targets WHERE idempotency_key=?""",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        expected = {
            "organization_id": organization_id,
            "objective_id": objective_id,
            "metric_id": metric_id,
            "comparator": comparator,
            "target_scaled": int(target_scaled),
            "review_interval_seconds": review_interval_seconds,
            "max_observation_age_seconds": max_observation_age_seconds,
            "first_review_at": first_review_at,
        }
        if any(existing[key] != value for key, value in expected.items()):
            raise PermissionError(
                "target idempotency key was reused with different parameters"
            )
        return str(existing["id"]), False
    target_id = f"target_{uuid.uuid4().hex}"
    now = int(time.time())
    with conn:
        conn.execute(
            """INSERT INTO metric_targets (
                 id,organization_id,objective_id,metric_id,comparator,
                 target_scaled,review_interval_seconds,max_observation_age_seconds,
                 first_review_at,
                 idempotency_key,created_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target_id,
                organization_id,
                objective_id,
                metric_id,
                comparator,
                int(target_scaled),
                review_interval_seconds,
                max_observation_age_seconds,
                first_review_at,
                idempotency_key,
                created_by,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO metric_target_state
               (target_id,status,next_review_at,updated_at)
               VALUES (?,'active',?,?)""",
            (target_id, first_review_at, now),
        )
    return target_id, True


def start_experiment(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    name: str,
    hypothesis: str,
    metric_id: str,
    comparator: str,
    success_threshold_scaled: int,
    starts_at: int,
    ends_at: int,
    max_spend_minor: int,
    currency: Optional[str],
    idempotency_key: str,
    created_by: str,
) -> tuple[str, bool]:
    ensure_schema(conn)
    if comparator not in {"gte", "lte"}:
        raise ValueError("experiment comparator must be gte or lte")
    if _objective_org(conn, objective_id) != organization_id:
        raise PermissionError("experiment objective belongs to another organization")
    metric = conn.execute(
        "SELECT organization_id FROM business_metrics WHERE id=?", (metric_id,)
    ).fetchone()
    if metric is None or str(metric["organization_id"]) != organization_id:
        raise PermissionError("experiment metric belongs to another organization")
    if not name.strip() or not hypothesis.strip():
        raise ValueError("experiment name and falsifiable hypothesis are required")
    if starts_at >= ends_at or ends_at <= int(time.time()) or max_spend_minor < 0:
        raise ValueError("experiment time box or spend ceiling is invalid")
    if len(idempotency_key.strip()) < 16:
        raise ValueError("experiment requires a high-entropy idempotency key")
    objective = objectives_db.objective_to_dict(conn, objective_id)
    if (
        objective["max_spend_minor"] is not None
        and max_spend_minor > int(objective["max_spend_minor"])
    ):
        raise PermissionError("experiment spend exceeds objective authority")
    existing = conn.execute(
        "SELECT * FROM strategy_experiments WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        expected = {
            "organization_id": organization_id,
            "objective_id": objective_id,
            "name": name,
            "hypothesis": hypothesis,
            "metric_id": metric_id,
            "comparator": comparator,
            "success_threshold_scaled": int(success_threshold_scaled),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "max_spend_minor": max_spend_minor,
            "currency": currency,
        }
        if any(existing[key] != value for key, value in expected.items()):
            raise PermissionError(
                "experiment idempotency key was reused with different parameters"
            )
        return str(existing["id"]), False
    experiment_id = f"experiment_{uuid.uuid4().hex}"
    now = int(time.time())
    with conn:
        conn.execute(
            """INSERT INTO strategy_experiments (
                 id,organization_id,objective_id,name,hypothesis,metric_id,
                 comparator,success_threshold_scaled,starts_at,ends_at,
                 max_spend_minor,currency,idempotency_key,created_by,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                experiment_id,
                organization_id,
                objective_id,
                name,
                hypothesis,
                metric_id,
                comparator,
                int(success_threshold_scaled),
                starts_at,
                ends_at,
                max_spend_minor,
                currency,
                idempotency_key,
                created_by,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO strategy_experiment_state
               (experiment_id,status,updated_at) VALUES (?,'running',?)""",
            (experiment_id, now),
        )
    return experiment_id, True


def decide_experiment(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    experiment_id: str,
    decision: str,
    reason: str,
) -> str:
    """Record the operational decision after evidence review."""
    ensure_schema(conn)
    if decision not in {"continue", "revise", "stop"}:
        raise ValueError("experiment decision must be continue, revise, or stop")
    row = conn.execute(
        """SELECT s.status FROM strategy_experiments e
             JOIN strategy_experiment_state s ON s.experiment_id=e.id
            WHERE e.id=? AND e.organization_id=?""",
        (experiment_id, organization_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"experiment not found: {experiment_id}")
    if str(row["status"]) not in {"running", "awaiting_decision"}:
        return str(row["status"])
    next_status = {
        "continue": "completed",
        "revise": "revised",
        "stop": "stopped",
    }[decision]
    with conn:
        conn.execute(
            """UPDATE strategy_experiment_state
                  SET status=?,reason=?,updated_at=? WHERE experiment_id=?""",
            (next_status, reason, int(time.time()), experiment_id),
        )
    return next_status


def _passes(comparator: str, actual: int, expected: int) -> bool:
    return actual >= expected if comparator == "gte" else actual <= expected


def _active_root(conn: sqlite3.Connection, organization_id: str) -> Optional[str]:
    objective_portfolio.ensure_schema(conn)
    row = conn.execute(
        """SELECT o.id FROM objectives o
            WHERE o.organization_id=?
              AND o.status NOT IN (?,?,?,?,?,?,?)
              AND NOT EXISTS (
                    SELECT 1 FROM objective_relationships r
                     WHERE r.child_objective_id=o.id
                       AND r.relationship='decomposes_to'
                  )
            ORDER BY o.created_at,o.id LIMIT 1""",
        (organization_id, "proposed", *TERMINAL_OBJECTIVE_STATUSES),
    ).fetchone()
    return str(row["id"]) if row else None


def dispatch_reviews(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Evaluate due targets and experiments, then wake governed CEO work."""
    ensure_schema(conn)
    operational_control.ensure_schema(conn)
    current = int(time.time()) if now is None else int(now)
    root_id = _active_root(conn, organization_id)
    evaluations = 0
    events = 0
    interventions = 0

    target_rows = conn.execute(
        """SELECT t.*,s.next_review_at
             FROM metric_targets t JOIN metric_target_state s ON s.target_id=t.id
            WHERE t.organization_id=? AND s.status='active'
              AND s.next_review_at<=?
            ORDER BY s.next_review_at,t.id""",
        (organization_id, current),
    ).fetchall()
    for target in target_rows:
        scheduled_for = int(target["next_review_at"])
        observation = conn.execute(
            """SELECT id,value_scaled,observed_at,evidence_sha256
                 FROM metric_observations
                WHERE metric_id=? AND observed_at BETWEEN ? AND ?
                ORDER BY observed_at DESC,recorded_at DESC,id DESC LIMIT 1""",
            (
                target["metric_id"],
                current - int(target["max_observation_age_seconds"]),
                current,
            ),
        ).fetchone()
        actual = int(observation["value_scaled"]) if observation else None
        verdict = (
            "no_evidence"
            if actual is None
            else "on_track"
            if _passes(str(target["comparator"]), actual, int(target["target_scaled"]))
            else "off_track"
        )
        evaluation_id = f"targeteval_{uuid.uuid4().hex}"
        evidence = {
            "method": "deterministic_metric_comparison",
            "observation_evidence_sha256": (
                str(observation["evidence_sha256"]) if observation else None
            ),
        }
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO metric_target_evaluations (
                     id,organization_id,target_id,scheduled_for,observation_id,
                     verdict,actual_scaled,target_scaled,evaluated_at,evidence_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id,
                    organization_id,
                    target["id"],
                    scheduled_for,
                    observation["id"] if observation else None,
                    verdict,
                    actual,
                    int(target["target_scaled"]),
                    current,
                    _json(evidence),
                ),
            )
            interval = int(target["review_interval_seconds"])
            missed = ((current - scheduled_for) // interval) + 1
            advanced = conn.execute(
                """UPDATE metric_target_state SET next_review_at=?,updated_at=?
                    WHERE target_id=? AND next_review_at=?""",
                (
                    scheduled_for + missed * interval,
                    current,
                    target["id"],
                    scheduled_for,
                ),
            )
        if advanced.rowcount != 1:
            continue
        evaluation_id = str(
            conn.execute(
                """SELECT id FROM metric_target_evaluations
                    WHERE target_id=? AND scheduled_for=?""",
                (target["id"], scheduled_for),
            ).fetchone()["id"]
        )
        evaluations += 1
        event_payload = {
            "kind": "metric_target_review",
            "target_id": str(target["id"]),
            "metric_id": str(target["metric_id"]),
            "evaluation_id": evaluation_id,
            "scheduled_for": scheduled_for,
            "verdict": verdict,
            "actual_scaled": actual,
            "target_scaled": int(target["target_scaled"]),
            "scale": SCALE,
            "missed_intervals": missed - 1,
        }
        routed = _route_review(
            conn,
            organization_id=organization_id,
            root_id=root_id,
            event_type="strategy.metric_target.reviewed",
            payload=event_payload,
            dedupe_key=f"metric-target-review:{target['id']}:{scheduled_for}",
        )
        events += routed == "event"
        interventions += routed == "intervention"

    experiment_rows = conn.execute(
        """SELECT e.* FROM strategy_experiments e
             JOIN strategy_experiment_state s ON s.experiment_id=e.id
            WHERE e.organization_id=? AND s.status='running' AND e.ends_at<=?
            ORDER BY e.ends_at,e.id""",
        (organization_id, current),
    ).fetchall()
    for experiment in experiment_rows:
        observation = conn.execute(
            """SELECT id,value_scaled,observed_at,evidence_sha256
                 FROM metric_observations
                WHERE metric_id=? AND observed_at BETWEEN ? AND ?
                ORDER BY observed_at DESC,recorded_at DESC,id DESC LIMIT 1""",
            (
                experiment["metric_id"],
                experiment["starts_at"],
                experiment["ends_at"],
            ),
        ).fetchone()
        actual = int(observation["value_scaled"]) if observation else None
        verdict = (
            "no_evidence"
            if actual is None
            else "supported"
            if _passes(
                str(experiment["comparator"]),
                actual,
                int(experiment["success_threshold_scaled"]),
            )
            else "not_supported"
        )
        evaluation_id = f"experimenteval_{uuid.uuid4().hex}"
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO strategy_experiment_evaluations (
                     id,organization_id,experiment_id,observation_id,verdict,
                     actual_scaled,threshold_scaled,evaluated_at,evidence_json
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id,
                    organization_id,
                    experiment["id"],
                    observation["id"] if observation else None,
                    verdict,
                    actual,
                    int(experiment["success_threshold_scaled"]),
                    current,
                    _json(
                        {
                            "method": "deterministic_experiment_threshold",
                            "observation_evidence_sha256": (
                                str(observation["evidence_sha256"])
                                if observation
                                else None
                            ),
                        }
                    ),
                ),
            )
            advanced = conn.execute(
                """UPDATE strategy_experiment_state
                      SET status='awaiting_decision',reason=?,updated_at=?
                    WHERE experiment_id=? AND status='running'""",
                (verdict, current, experiment["id"]),
            )
        if advanced.rowcount != 1:
            continue
        evaluation_id = str(
            conn.execute(
                """SELECT id FROM strategy_experiment_evaluations
                    WHERE experiment_id=?""",
                (experiment["id"],),
            ).fetchone()["id"]
        )
        evaluations += 1
        routed = _route_review(
            conn,
            organization_id=organization_id,
            root_id=root_id,
            event_type="strategy.experiment.reviewed",
            payload={
                "kind": "strategy_experiment",
                "experiment_id": str(experiment["id"]),
                "evaluation_id": evaluation_id,
                "verdict": verdict,
                "actual_scaled": actual,
                "threshold_scaled": int(experiment["success_threshold_scaled"]),
                "scale": SCALE,
                "decision_required": ["continue", "revise", "stop"],
            },
            dedupe_key=f"strategy-experiment-review:{experiment['id']}",
        )
        events += routed == "event"
        interventions += routed == "intervention"
    return {
        "evaluations_recorded": evaluations,
        "events_enqueued": events,
        "interventions_raised": interventions,
    }


def _route_review(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    root_id: Optional[str],
    event_type: str,
    payload: Mapping[str, Any],
    dedupe_key: str,
) -> str:
    if root_id is not None:
        before = conn.execute(
            "SELECT id FROM objective_inbox WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        objectives_db.enqueue_objective_event(
            conn,
            objective_id=root_id,
            event_type=event_type,
            payload=dict(payload),
            dedupe_key=dedupe_key,
        )
        return "existing" if before else "event"
    intervention_key = f"unowned:{dedupe_key}"
    before = conn.execute(
        """SELECT id FROM intervention_queue
            WHERE dedupe_key=? AND status='open'""",
        (intervention_key,),
    ).fetchone()
    operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        category="strategy_review_unowned",
        summary=f"{event_type} has no active root objective",
        context=dict(payload),
        options=[
            {"id": "create_objective", "label": "Create governed objective"},
            {"id": "review", "label": "Review evidence"},
            {"id": "stop", "label": "Stop the strategy"},
        ],
        dedupe_key=intervention_key,
    )
    return "existing" if before else "intervention"


def planning_snapshot(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    """Return bounded, credential-free KPI and experiment evidence."""
    ensure_schema(conn)
    metrics = []
    for row in conn.execute(
        """SELECT * FROM business_metrics WHERE organization_id=?
            ORDER BY created_at,id LIMIT 50""",
        (organization_id,),
    ).fetchall():
        observation = conn.execute(
            """SELECT id,value_scaled,observed_at,evidence_sha256
                FROM metric_observations WHERE metric_id=?
                ORDER BY observed_at DESC,recorded_at DESC,id DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        metrics.append(
            {
                "id": str(row["id"]),
                "metric_key": str(row["metric_key"]),
                "name": str(row["name"]),
                "unit": str(row["unit"]),
                "preferred_direction": str(row["preferred_direction"]),
                "source_system": str(row["source_system"]),
                "latest_observation": (
                    {
                        "id": str(observation["id"]),
                        "value_scaled": int(observation["value_scaled"]),
                        "scale": SCALE,
                        "observed_at": int(observation["observed_at"]),
                        "evidence_sha256": str(observation["evidence_sha256"]),
                    }
                    if observation
                    else None
                ),
            }
        )
    targets = [
        {
            "id": str(row["id"]),
            "objective_id": str(row["objective_id"]),
            "metric_id": str(row["metric_id"]),
            "comparator": str(row["comparator"]),
            "target_scaled": int(row["target_scaled"]),
            "scale": SCALE,
            "status": str(row["status"]),
            "next_review_at": int(row["next_review_at"]),
            "max_observation_age_seconds": int(
                row["max_observation_age_seconds"]
            ),
        }
        for row in conn.execute(
            """SELECT t.*,s.status,s.next_review_at FROM metric_targets t
                JOIN metric_target_state s ON s.target_id=t.id
               WHERE t.organization_id=? ORDER BY t.created_at,t.id LIMIT 50""",
            (organization_id,),
        ).fetchall()
    ]
    experiments = [
        {
            "id": str(row["id"]),
            "objective_id": str(row["objective_id"]),
            "name": str(row["name"]),
            "hypothesis": str(row["hypothesis"]),
            "metric_id": str(row["metric_id"]),
            "status": str(row["status"]),
            "ends_at": int(row["ends_at"]),
            "max_spend_minor": int(row["max_spend_minor"]),
            "currency": row["currency"],
        }
        for row in conn.execute(
            """SELECT e.*,s.status FROM strategy_experiments e
                JOIN strategy_experiment_state s ON s.experiment_id=e.id
               WHERE e.organization_id=? ORDER BY e.created_at,e.id LIMIT 50""",
            (organization_id,),
        ).fetchall()
    ]
    return {
        "metrics": metrics,
        "targets": targets,
        "experiments": experiments,
        "limits": {"metrics": 50, "targets": 50, "experiments": 50},
        "sensitive_data_included": False,
    }


def sync_financial_observations(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Record daily deterministic financial KPIs from authoritative ledgers."""
    from hermes_cli import accounting_db, finance_db

    current = int(time.time()) if now is None else int(now)
    accounting_db.ensure_schema(conn)
    finance_db.ensure_schema(conn)
    organization = conn.execute(
        "SELECT base_currency FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if organization is None:
        raise KeyError(f"organization not found: {organization_id}")
    currency = str(organization["base_currency"])
    account_id = finance_db.operating_account_for_organization(
        conn, organization_id, currency
    )
    if account_id is None:
        return {"metrics_ensured": 0, "observations_recorded": 0}
    statements = accounting_db.financial_statements(conn, organization_id)
    values = {
        "accounting.revenue_minor": (
            "Cumulative revenue",
            f"minor_{currency}",
            "increase",
            int(statements["profit_and_loss"]["revenue_minor"]),
        ),
        "accounting.expenses_minor": (
            "Cumulative expenses",
            f"minor_{currency}",
            "decrease",
            int(statements["profit_and_loss"]["expenses_minor"]),
        ),
        "finance.cash_available_minor": (
            "Available operating cash",
            f"minor_{currency}",
            "increase",
            int(finance_db.available_balance(conn, account_id)),
        ),
        "accounting.tax_liability_minor": (
            "Recorded tax liability",
            f"minor_{currency}",
            "decrease",
            int(statements["tax_liability_minor"]),
        ),
    }
    recorded = 0
    snapshot_hash = hashlib.sha256(
        _json(
            {
                "values": {key: value[3] for key, value in values.items()},
                "currency": currency,
            }
        ).encode()
    ).hexdigest()
    day_bucket = current // 86_400
    for metric_key, (name, unit, direction, value) in values.items():
        metric_id, _ = register_metric(
            conn,
            organization_id=organization_id,
            metric_key=metric_key,
            name=name,
            unit=unit,
            preferred_direction=direction,
            source_system="authoritative_accounting",
            verifier="deterministic:accounting-readback",
            idempotency_key=f"system-financial-metric:{organization_id}:{metric_key}",
            created_by="control:financial-measurement",
        )
        _, created = record_observation(
            conn,
            organization_id=organization_id,
            metric_id=metric_id,
            value_scaled=value * SCALE,
            observed_at=current,
            source_reference=(
                f"financial-snapshot:{organization_id}:{day_bucket}:"
                f"{snapshot_hash}:{metric_key}"
            ),
            verifier="deterministic:accounting-readback",
            evidence={
                "method": "authoritative_ledger_and_treasury_readback",
                "snapshot_sha256": snapshot_hash,
                "day_bucket": day_bucket,
            },
        )
        recorded += int(created)
    return {"metrics_ensured": len(values), "observations_recorded": recorded}
