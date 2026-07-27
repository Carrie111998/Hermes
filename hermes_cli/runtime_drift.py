"""Durable runtime-baseline and state-drift detection for unattended operation.

The authority policy baseline protects decision rights.  This module protects
the execution substrate those rights assume: the non-secret agentic charter
and the SQLite schema.  A mismatch is never repaired by the model; autonomy
is paused until a human explicitly accepts a new baseline.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli.audit_redaction import sanitize


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_drift_baselines (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,
    accepted_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_drift_baselines_org
    ON runtime_drift_baselines(organization_id, accepted_at DESC, id DESC);
CREATE TRIGGER IF NOT EXISTS runtime_drift_baselines_immutable_update
BEFORE UPDATE ON runtime_drift_baselines
BEGIN SELECT RAISE(ABORT, 'runtime drift baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS runtime_drift_baselines_immutable_delete
BEFORE DELETE ON runtime_drift_baselines
BEGIN SELECT RAISE(ABORT, 'runtime drift baselines are immutable'); END;
"""


class RuntimeDriftError(RuntimeError):
    """Raised when a runtime baseline cannot be accepted or verified."""


@dataclass(frozen=True)
class DriftPosture:
    status: str
    baseline_id: Optional[str]
    expected_sha256: Optional[str]
    observed_sha256: str
    differences: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "untracked"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema_fingerprint(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """SELECT type, name, COALESCE(tbl_name, ''), COALESCE(sql, '')
             FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name"""
    ).fetchall()
    return [
        {"type": str(row[0]), "name": str(row[1]), "table": str(row[2]), "sql": str(row[3])}
        for row in rows
    ]


def _deployment_fingerprint() -> dict[str, Any]:
    """Capture package identity and lock manifests without reading secrets."""
    root = Path(__file__).resolve().parents[1]
    manifests: dict[str, str] = {}
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        path = root / name
        if path.is_file():
            manifests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        version = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        version = "source-checkout"
    return {"package": "hermes-agent", "version": version, "manifests": manifests}


def fingerprint(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    charter: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic, credential-free fingerprint of runtime inputs."""
    safe_charter = sanitize(dict(charter))
    # Secret values are not expected in the charter, but omit the redacted
    # marker itself from the fingerprint so rotating a prohibited secret does
    # not create evidence containing or depending on that secret.
    safe_charter = {
        key: value for key, value in safe_charter.items() if value != "[REDACTED]"
    }
    value = {
        "format": "charterforge-runtime-drift-v1",
        "organization_id": organization_id,
        "charter": safe_charter,
        "schema": _schema_fingerprint(conn),
        "python": platform.python_version(),
        "deployment": _deployment_fingerprint(),
    }
    value["sha256"] = _sha256(_canonical(value))
    return value


def accept_baseline(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    charter: Mapping[str, Any],
    actor: str,
    reason: str,
) -> str:
    """Append a new runtime baseline; only an explicitly human actor may do so."""
    if not actor.startswith("human:"):
        raise RuntimeDriftError("runtime rebaseline requires a human actor")
    if not reason.strip():
        raise RuntimeDriftError("runtime rebaseline requires a reason")
    ensure_schema(conn)
    current = fingerprint(conn, organization_id=organization_id, charter=charter)
    latest = conn.execute(
        """SELECT id, fingerprint_sha256 FROM runtime_drift_baselines
            WHERE organization_id=? ORDER BY accepted_at DESC, id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if latest is not None and str(latest["fingerprint_sha256"]) == current["sha256"]:
        return str(latest["id"])
    baseline_id = f"runtime_baseline_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO runtime_drift_baselines
               (id, organization_id, fingerprint_sha256, fingerprint_json,
                accepted_by, reason, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                baseline_id,
                organization_id,
                current["sha256"],
                _canonical(current),
                actor,
                reason.strip(),
                int(time.time()),
            ),
        )
    return baseline_id


def check(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    charter: Mapping[str, Any],
    require_baseline: bool,
) -> DriftPosture:
    ensure_schema(conn)
    current = fingerprint(conn, organization_id=organization_id, charter=charter)
    baseline = conn.execute(
        """SELECT id, fingerprint_sha256, fingerprint_json
             FROM runtime_drift_baselines
            WHERE organization_id=? ORDER BY accepted_at DESC, id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if baseline is None:
        return DriftPosture(
            "missing" if require_baseline else "untracked",
            None,
            None,
            current["sha256"],
            ("baseline_missing",) if require_baseline else (),
        )
    expected = json.loads(str(baseline["fingerprint_json"]))
    expected_unsigned = dict(expected)
    expected_unsigned.pop("sha256", None)
    differences = tuple(
        key for key in (
            "charter", "schema", "python", "deployment", "organization_id"
        )
        if expected.get(key) != current.get(key)
    )
    if expected.get("sha256") != _sha256(_canonical(expected_unsigned)):
        differences += ("baseline_hash",)
    if str(baseline["fingerprint_sha256"]) != str(expected.get("sha256")):
        differences += ("baseline_digest",)
    status = "ready" if not differences else "drifted"
    return DriftPosture(
        status,
        str(baseline["id"]),
        str(baseline["fingerprint_sha256"]),
        current["sha256"],
        differences,
    )


def enforce(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    charter: Mapping[str, Any],
) -> DriftPosture:
    """Pause autonomy and raise one intervention when required drift is found."""
    security = charter.get("security") or {}
    posture = check(
        conn,
        organization_id=organization_id,
        charter=charter,
        require_baseline=bool(security.get("require_runtime_baseline", False)),
    )
    if posture.ready:
        return posture
    from hermes_cli import operational_control

    reason = "runtime baseline " + posture.status
    state = operational_control.autonomy_state(conn)
    if state["mode"] == "autonomous":
        operational_control.set_autonomy_mode(
            conn, mode="paused", actor="control:runtime-drift", reason=reason
        )
    operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        category="runtime_drift_detected",
        summary="Runtime baseline no longer matches the accepted operating substrate",
        context={
            "status": posture.status,
            "baseline_id": posture.baseline_id,
            "expected_sha256": posture.expected_sha256,
            "observed_sha256": posture.observed_sha256,
            "differences": list(posture.differences),
        },
        options=[
            {"id": "inspect", "label": "Inspect runtime differences"},
            {"id": "rebaseline", "label": "Accept a new human-reviewed baseline"},
            {"id": "manual", "label": "Remain in manual operation"},
        ],
        dedupe_key=f"runtime-drift:{organization_id}",
    )
    return posture
