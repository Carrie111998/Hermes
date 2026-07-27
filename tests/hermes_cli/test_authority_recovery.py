from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import (
    authority_integrity,
    authority_recovery,
    operational_control,
    organization_db,
)
from hermes_cli import objectives_db as db


def _ready_company(tmp_path):
    path = tmp_path / "authority.db"
    conn = db.connect(path)
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Recoverable Company",
        purpose="Recover without replaying external effects",
        profile_name="default",
        charter={},
    )
    policy = {"enabled": True, "policy_version": "recovery-test"}
    authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy=policy,
        actor="human:setup",
        reason="test charter",
    )
    posture = authority_integrity.enforce_preflight(
        conn, organization_id=organization_id, policy=policy
    )
    assert posture.ready
    return conn, path, organization_id


def test_known_good_snapshot_is_consistent_and_tenant_bound(tmp_path):
    conn, _, organization_id = _ready_company(tmp_path)
    created = authority_recovery.create_known_good_snapshot(
        conn,
        organization_id=organization_id,
        root=tmp_path / "recovery",
    )

    assert created["valid"] is True
    manifest = created["manifest"]
    assert manifest["format"] == "charterforge-authority-snapshot-v1"
    assert manifest["compatibility_format"] == "hermes-authority-snapshot-v1"
    assert manifest["organization_id"] == organization_id
    assert manifest["integrity_run_id"]
    assert manifest["database_sha256"]
    assert (
        authority_recovery.verify_snapshot(
            Path(created["manifest_path"]),
            expected_organization_id="different-org",
        )["failures"]
        == ["organization_id"]
    )
    conn.close()


def test_snapshot_byte_tampering_is_detected(tmp_path):
    conn, _, organization_id = _ready_company(tmp_path)
    created = authority_recovery.create_known_good_snapshot(
        conn, organization_id=organization_id, root=tmp_path / "recovery"
    )
    conn.close()
    database = Path(created["database_path"])
    with database.open("ab") as stream:
        stream.write(b"tamper")

    verified = authority_recovery.verify_snapshot(Path(created["manifest_path"]))

    assert verified["valid"] is False
    assert "database_hash" in verified["failures"]


def test_retention_prunes_database_and_manifest_together(tmp_path, monkeypatch):
    conn, _, organization_id = _ready_company(tmp_path)
    clock = {"value": 100}

    def tick():
        clock["value"] += 1
        return clock["value"]

    monkeypatch.setattr(authority_recovery.time, "time", tick)
    for _ in range(3):
        authority_recovery.create_known_good_snapshot(
            conn,
            organization_id=organization_id,
            root=tmp_path / "recovery",
            retention_count=2,
        )

    snapshots = authority_recovery.list_snapshots(
        organization_id, root=tmp_path / "recovery"
    )
    assert len(snapshots) == 2
    assert len(list((tmp_path / "recovery" / organization_id).glob("*.db"))) == 2
    conn.close()


def test_restore_requires_paused_authority_and_returns_reconciliation_state(tmp_path):
    conn, path, organization_id = _ready_company(tmp_path)
    created = authority_recovery.create_known_good_snapshot(
        conn, organization_id=organization_id, root=tmp_path / "recovery"
    )
    conn.close()

    with pytest.raises(RuntimeError, match="pause autonomy"):
        authority_recovery.restore_snapshot(
            target_db=path,
            manifest_path=Path(created["manifest_path"]),
            actor="human:advisor",
            reason="test restore",
            evidence={"ticket": "REC-1"},
        )

    current = db.connect(path)
    operational_control.set_autonomy_mode(
        current,
        mode="paused",
        actor="human:advisor",
        reason="prepare offline restore",
    )
    objective = db.create_objective(
        current,
        organization_id=organization_id,
        desired_outcome="This state is newer than the snapshot",
        originator="test",
    )
    current.close()

    result = authority_recovery.restore_snapshot(
        target_db=path,
        manifest_path=Path(created["manifest_path"]),
        actor="human:advisor",
        reason="recover known-good authority",
        evidence={"ticket": "REC-1", "reviewed": True},
    )

    assert result["autonomy"] == "paused"
    assert result["reconciliation_required"] is True
    assert result["quarantine_path"]
    restored = db.connect(path)
    assert operational_control.autonomy_state(restored)["mode"] == "paused"
    with pytest.raises(KeyError):
        db.get_objective(restored, objective.id)
    event = restored.execute(
        "SELECT * FROM authority_recovery_events"
    ).fetchone()
    assert event["snapshot_id"] == created["manifest"]["snapshot_id"]
    assert json.loads(event["evidence_json"])["ticket"] == "REC-1"
    intervention = operational_control.list_interventions(
        restored, organization_id=organization_id
    )
    assert any(
        row["category"] == "post_restore_reconciliation"
        for row in intervention
    )
    with pytest.raises(
        operational_control.AutonomyRevokedError,
        match="recovery control is unresolved",
    ):
        operational_control.set_autonomy_mode(
            restored,
            mode="autonomous",
            actor="human:advisor",
            reason="premature resume",
        )
    restored.close()


def test_restore_rejects_non_human_or_evidence_free_requests(tmp_path):
    with pytest.raises(ValueError, match="human actor"):
        authority_recovery.restore_snapshot(
            target_db=tmp_path / "authority.db",
            manifest_path=tmp_path / "missing.json",
            actor="employee:ceo",
            reason="self-authorized",
            evidence={},
        )


def test_unreadable_source_requires_explicit_worker_stop_evidence(tmp_path):
    conn, path, organization_id = _ready_company(tmp_path)
    created = authority_recovery.create_known_good_snapshot(
        conn, organization_id=organization_id, root=tmp_path / "recovery"
    )
    operational_control.set_autonomy_mode(
        conn,
        mode="paused",
        actor="human:advisor",
        reason="prepare corruption test",
    )
    conn.close()
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(RuntimeError, match="workers are stopped"):
        authority_recovery.restore_snapshot(
            target_db=path,
            manifest_path=Path(created["manifest_path"]),
            actor="human:advisor",
            reason="recover corrupt authority",
            evidence={"source_integrity_failed": True},
        )

    result = authority_recovery.restore_snapshot(
        target_db=path,
        manifest_path=Path(created["manifest_path"]),
        actor="human:advisor",
        reason="recover corrupt authority",
        evidence={
            "source_integrity_failed": True,
            "workers_stopped": True,
            "ticket": "REC-2",
        },
    )
    assert result["source_was_unreadable"] is True
    restored = db.connect(path)
    assert operational_control.autonomy_state(restored)["mode"] == "paused"
    restored.close()
