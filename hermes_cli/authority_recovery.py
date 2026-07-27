"""Known-good snapshots and advisor-governed authority-store restoration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS authority_recovery_events (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, event_type TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL, source_quarantine_path TEXT,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS authority_recovery_events_immutable_update
BEFORE UPDATE ON authority_recovery_events
BEGIN SELECT RAISE(ABORT, 'authority recovery events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS authority_recovery_events_immutable_delete
BEFORE DELETE ON authority_recovery_events
BEGIN SELECT RAISE(ABORT, 'authority recovery events are immutable'); END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def recovery_root(organization_id: str, root: Optional[Path] = None) -> Path:
    base = root or (get_hermes_home().resolve() / "business-recovery")
    return Path(base).expanduser().resolve() / organization_id


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_json(unsigned).encode("utf-8")).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("recovery manifest must be an object")
    return value


def verify_snapshot(
    manifest_path: Path, *, expected_organization_id: Optional[str] = None
) -> dict[str, Any]:
    """Verify manifest, database bytes, SQLite structure, and tenant identity."""
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _read_manifest(manifest_path)
    failures: list[str] = []
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        failures.append("manifest_hash")
    if expected_organization_id is not None and (
        manifest.get("organization_id") != expected_organization_id
    ):
        failures.append("organization_id")
    database = manifest_path.parent / str(manifest.get("database_file") or "")
    if not database.is_file():
        failures.append("database_missing")
    elif _sha256(database) != manifest.get("database_sha256"):
        failures.append("database_hash")
    if not failures:
        probe = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        probe.row_factory = sqlite3.Row
        try:
            if [row[0] for row in probe.execute("PRAGMA quick_check")] != ["ok"]:
                failures.append("database_integrity")
            if list(probe.execute("PRAGMA foreign_key_check")):
                failures.append("foreign_keys")
            org = probe.execute(
                "SELECT 1 FROM organizations WHERE id=?",
                (manifest["organization_id"],),
            ).fetchone()
            if org is None:
                failures.append("organization_missing")
            from hermes_cli import business_audit

            if not business_audit.verify_chain(probe, manifest["organization_id"]):
                failures.append("audit_chain")
        finally:
            probe.close()
    return {
        "valid": not failures,
        "failures": failures,
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "database_path": str(database),
    }


def create_known_good_snapshot(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    root: Optional[Path] = None,
    retention_count: int = 7,
) -> dict[str, Any]:
    """Create a consistent snapshot only after a successful integrity run."""
    if retention_count < 1:
        raise ValueError("snapshot retention count must be positive")
    ensure_schema(conn)
    from hermes_cli import operational_control

    operational_control.ensure_schema(conn)
    integrity = conn.execute(
        """SELECT * FROM authority_integrity_runs WHERE organization_id=?
           ORDER BY checked_at DESC,id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if integrity is None or integrity["status"] != "ready":
        raise RuntimeError("known-good snapshot requires a ready integrity run")
    directory = recovery_root(organization_id, root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    created_at = int(time.time())
    snapshot_id = f"snapshot_{created_at}_{uuid.uuid4().hex}"
    database = directory / f"{snapshot_id}.db"
    temporary = directory / f".{snapshot_id}.tmp"
    destination = sqlite3.connect(temporary)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, database)
    head = conn.execute(
        """SELECT sequence,event_hash FROM business_audit_events
           WHERE organization_id=? ORDER BY sequence DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    manifest: dict[str, Any] = {
        "format": "hermes-authority-snapshot-v1",
        "snapshot_id": snapshot_id,
        "organization_id": organization_id,
        "created_at": created_at,
        "database_file": database.name,
        "database_size": database.stat().st_size,
        "database_sha256": _sha256(database),
        "integrity_run_id": str(integrity["id"]),
        "integrity_evidence_sha256": str(integrity["evidence_sha256"]),
        "audit_sequence": int(head["sequence"]) if head else 0,
        "audit_head_sha256": str(head["event_hash"]) if head else None,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    manifest_path = directory / f"{snapshot_id}.json"
    manifest_path.write_text(_json(manifest) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    verified = verify_snapshot(
        manifest_path, expected_organization_id=organization_id
    )
    if not verified["valid"]:
        database.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise RuntimeError(
            "created authority snapshot failed verification: "
            + ", ".join(verified["failures"])
        )
    manifests = sorted(
        directory.glob("snapshot_*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for expired in manifests[retention_count:]:
        old = _read_manifest(expired)
        (directory / str(old.get("database_file") or "")).unlink(missing_ok=True)
        expired.unlink(missing_ok=True)
    return verified


def maybe_snapshot(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    interval_seconds: int = 86400,
    retention_count: int = 7,
    root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    directory = recovery_root(organization_id, root)
    manifests = list(directory.glob("snapshot_*.json")) if directory.exists() else []
    now = int(time.time())
    if manifests:
        newest = max(manifests, key=lambda path: path.stat().st_mtime_ns)
        if now - int(_read_manifest(newest).get("created_at", 0)) < interval_seconds:
            return None
    return create_known_good_snapshot(
        conn,
        organization_id=organization_id,
        root=root,
        retention_count=retention_count,
    )


def list_snapshots(
    organization_id: str, *, root: Optional[Path] = None
) -> list[dict[str, Any]]:
    directory = recovery_root(organization_id, root)
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("snapshot_*.json"), reverse=True):
        verification = verify_snapshot(
            path, expected_organization_id=organization_id
        )
        result.append(
            {
                **verification["manifest"],
                "valid": verification["valid"],
                "failures": verification["failures"],
                "manifest_path": str(path),
            }
        )
    return result


def restore_snapshot(
    *,
    target_db: Path,
    manifest_path: Path,
    actor: str,
    reason: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore offline authority state; always return it paused for reconciliation."""
    if not actor.startswith("human:") or not reason.strip() or not evidence:
        raise ValueError("restore requires human actor, reason, and evidence")
    verification = verify_snapshot(manifest_path)
    if not verification["valid"]:
        raise RuntimeError(
            "authority snapshot verification failed: "
            + ", ".join(verification["failures"])
        )
    target_db = target_db.expanduser().resolve()
    manifest_organization_id = str(
        verification["manifest"]["organization_id"]
    )
    current_unreadable = False
    if target_db.exists():
        try:
            current = sqlite3.connect(
                f"file:{target_db}?mode=ro", uri=True
            )
            current.row_factory = sqlite3.Row
            try:
                tables = {
                    str(row[0])
                    for row in current.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "organizations" in tables:
                    organizations = {
                        str(row[0])
                        for row in current.execute("SELECT id FROM organizations")
                    }
                    if (
                        organizations
                        and manifest_organization_id not in organizations
                    ):
                        raise RuntimeError(
                            "snapshot organization does not match current authority store"
                        )
                if "autonomy_control" not in tables:
                    raise RuntimeError(
                        "pause autonomy before restoring authority state"
                    )
                mode = current.execute(
                    "SELECT mode FROM autonomy_control WHERE singleton=1"
                ).fetchone()
                if mode is None or mode["mode"] == "autonomous":
                    raise RuntimeError(
                        "pause autonomy before restoring authority state"
                    )
                if "objective_workers" in tables:
                    workers = current.execute(
                        """SELECT COUNT(*) FROM objective_workers
                           WHERE status='running' AND heartbeat_at>?""",
                        (int(time.time()) - 60,),
                    ).fetchone()
                    if workers is not None and int(workers[0]) > 0:
                        raise RuntimeError(
                            "stop active objective workers before restore"
                        )
            finally:
                current.close()
        except sqlite3.DatabaseError:
            current_unreadable = True
    if current_unreadable and not (
        evidence.get("source_integrity_failed") is True
        and evidence.get("workers_stopped") is True
    ):
        raise RuntimeError(
            "unreadable authority store recovery requires evidence that "
            "source integrity failed and objective workers are stopped"
        )
    target_db.parent.mkdir(parents=True, exist_ok=True)
    quarantine = target_db.with_name(
        f"{target_db.name}.pre-restore-{int(time.time())}-{uuid.uuid4().hex}.bak"
    )
    if target_db.exists():
        shutil.copy2(target_db, quarantine)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{target_db}{suffix}")
            if sidecar.exists():
                shutil.move(sidecar, Path(f"{quarantine}{suffix}"))
    source = Path(verification["database_path"])
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target_db.name}.restore-", dir=target_db.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target_db)
    finally:
        temporary.unlink(missing_ok=True)
    restored = sqlite3.connect(target_db)
    restored.row_factory = sqlite3.Row
    restored.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_schema(restored)
        from hermes_cli import operational_control

        operational_control.set_autonomy_mode(
            restored,
            mode="paused",
            actor=actor,
            reason="authority snapshot restored; reconciliation required",
        )
        event_id = f"recovery_{uuid.uuid4().hex}"
        manifest = verification["manifest"]
        with restored:
            restored.execute(
                """INSERT INTO authority_recovery_events
                   (id,organization_id,event_type,snapshot_id,actor,reason,
                    evidence_json,source_quarantine_path,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    manifest["organization_id"],
                    "snapshot_restored",
                    manifest["snapshot_id"],
                    actor,
                    reason.strip(),
                    _json(dict(evidence)),
                    str(quarantine) if quarantine.exists() else None,
                    int(time.time()),
                ),
            )
        operational_control.raise_intervention(
            restored,
            organization_id=str(manifest["organization_id"]),
            category="post_restore_reconciliation",
            summary="Authority snapshot restored; reconcile external systems before resume",
            context={
                "snapshot_id": manifest["snapshot_id"],
                "audit_sequence": manifest["audit_sequence"],
                "recovery_event_id": event_id,
            },
            options=[
                {"id": "reconcile", "label": "Reconcile external systems"},
                {"id": "manual", "label": "Remain in manual operation"},
            ],
            dedupe_key=f"post-restore:{manifest['organization_id']}:{manifest['snapshot_id']}",
        )
        uncertain = operational_control.recover_incomplete_executions(restored)
    finally:
        restored.close()
    return {
        "restored": True,
        "snapshot_id": verification["manifest"]["snapshot_id"],
        "target_db": str(target_db),
        "quarantine_path": str(quarantine) if quarantine.exists() else None,
        "autonomy": "paused",
        "reconciliation_required": True,
        "uncertain_execution_count": len(uncertain),
        "source_was_unreadable": current_unreadable,
    }


def recovery_posture(
    conn: sqlite3.Connection, organization_id: str
) -> dict[str, Any]:
    database_path = Path(
        conn.execute("PRAGMA database_list").fetchone()["file"]
    ).resolve()
    snapshots = list_snapshots(
        organization_id, root=database_path.parent / "business-recovery"
    )
    latest_event = None
    ensure_schema(conn)
    row = conn.execute(
        """SELECT * FROM authority_recovery_events WHERE organization_id=?
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (organization_id,),
    ).fetchone()
    if row is not None:
        latest_event = dict(row)
        latest_event["evidence"] = json.loads(latest_event.pop("evidence_json"))
    return {
        "snapshot_count": len(snapshots),
        "latest_snapshot": snapshots[0] if snapshots else None,
        "all_snapshots_valid": all(item["valid"] for item in snapshots),
        "latest_recovery_event": latest_event,
    }
