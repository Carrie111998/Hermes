from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli.lanes import approvals, schema
from hermes_cli.lanes.contracts import LaneDraft, LaneTask
from hermes_cli.lanes.errors import (
    ApprovalExpired,
    ApprovalNotGranted,
    PublishDisabled,
)
from hermes_cli.lanes.harness import LaneHarness


def _manifest(tmp_path: Path, *, publish_enabled: bool) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "lanes": [
                    {
                        "lane_id": "dayroute",
                        "enabled": False,
                        "module": "missing.dayroute",
                        "approval_channel": "dashboard",
                        "approval_timeout_hours": 24,
                        "per_lane_daily_cost_cap_aud": 3.0,
                        "per_lane_daily_task_cap": 50,
                        "per_lane_hourly_ingest_cap": 20,
                        "publish_enabled": publish_enabled,
                        "description": "test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _task(db: Path, external_id: str = "external-1") -> LaneTask:
    schema.ensure_migrated(db)
    conn = schema.connect(db)
    cursor = conn.execute(
        """INSERT INTO lane_task(
             lane_id,external_id,task_id,ingested_at,status,payload_json)
           VALUES('dayroute',?,'task-1','2026-01-01T00:00:00Z',
                  'drafted','{}')""",
        (external_id,),
    )
    conn.commit()
    task_id = int(cursor.lastrowid)
    conn.close()
    return LaneTask(
        lane_id="dayroute",
        external_id=external_id,
        task_id="task-1",
        payload={},
        id=task_id,
        status="drafted",
    )


def _queued(db: Path, task: LaneTask, *, now=None):
    return approvals.enqueue(
        task=task,
        draft=LaneDraft("draft"),
        channel="dashboard",
        timeout_hours=24,
        db_path=db,
        now=now,
    )


def _publish_harness(
    tmp_path: Path,
    db: Path,
    *,
    enabled: bool = True,
    publisher=None,
) -> LaneHarness:
    return LaneHarness(
        lane_id="dayroute",
        db_path=db,
        manifest_path=_manifest(tmp_path, publish_enabled=enabled),
        publisher=publisher or (lambda payload: "external-ref"),
    )


def test_generate_token_is_12_chars_alphanumeric():
    token = approvals.generate_token()
    assert len(token) == 12
    assert token.isalnum()


def test_generate_token_is_unique_across_10000_calls():
    tokens = {approvals.generate_token() for _ in range(10_000)}
    assert len(tokens) == 10_000


def test_generate_token_cryptographically_secure(monkeypatch):
    calls = []
    original = approvals.secrets.choice

    def spy(alphabet):
        calls.append(alphabet)
        return original(alphabet)

    monkeypatch.setattr(approvals.secrets, "choice", spy)
    approvals.generate_token()
    assert len(calls) == 12
    assert all(len(alphabet) == 62 for alphabet in calls)


def test_enqueue_approval_writes_row_with_expires_at(tmp_path):
    db = tmp_path / "kanban.db"
    task = _task(db)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    request = _queued(db, task, now=now)
    conn = schema.connect(db)
    row = conn.execute(
        "SELECT * FROM lane_approval_queue WHERE approval_token=?",
        (request.token,),
    ).fetchone()
    conn.close()
    assert row["status"] == "pending"
    assert row["expires_at"] == "2026-01-02T00:00:00Z"


def test_check_approval_returns_pending_when_new(tmp_path):
    db = tmp_path / "kanban.db"
    request = _queued(db, _task(db))
    assert approvals.check(request.token, db_path=db).status == "pending"


def test_grant_approval_updates_status_and_grant_ts(tmp_path):
    db = tmp_path / "kanban.db"
    request = _queued(db, _task(db))
    result = approvals.grant(request.token, note="looks good", db_path=db)
    conn = schema.connect(db)
    row = conn.execute(
        "SELECT status,grant_ts,grant_note FROM lane_approval_queue"
    ).fetchone()
    conn.close()
    assert result.status == "granted"
    assert row["grant_ts"]
    assert row["grant_note"] == "looks good"


def test_reject_approval_stores_reason(tmp_path):
    db = tmp_path / "kanban.db"
    request = _queued(db, _task(db))
    result = approvals.reject(
        request.token,
        reason="needs revision",
        db_path=db,
    )
    assert result.status == "rejected"
    assert result.reject_reason == "needs revision"


def test_expire_sweep_flips_pending_past_expiry_to_expired(tmp_path):
    db = tmp_path / "kanban.db"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    request = _queued(db, _task(db), now=now)
    changed = approvals.expire_sweep(
        db_path=db,
        now=now + timedelta(hours=25),
    )
    assert changed == 1
    assert (
        approvals.check(
            request.token,
            db_path=db,
            now=now + timedelta(hours=25),
        ).status
        == "expired"
    )


def test_publish_with_ledger_refuses_when_publish_enabled_false(tmp_path):
    db = tmp_path / "kanban.db"
    task = _task(db)
    request = _queued(db, task)
    approvals.grant(request.token, db_path=db)
    harness = _publish_harness(tmp_path, db, enabled=False)
    with pytest.raises(PublishDisabled):
        harness.publish_with_ledger(
            task=task,
            external_target="test:target",
            payload={"draft": "x"},
        )


def test_publish_with_ledger_refuses_when_approval_not_granted(tmp_path):
    db = tmp_path / "kanban.db"
    task = _task(db)
    _queued(db, task)
    harness = _publish_harness(tmp_path, db)
    with pytest.raises(ApprovalNotGranted):
        harness.publish_with_ledger(
            task=task,
            external_target="test:target",
            payload={"draft": "x"},
        )


def test_publish_with_ledger_refuses_when_approval_expired(tmp_path):
    db = tmp_path / "kanban.db"
    task = _task(db)
    request = _queued(db, task)
    conn = schema.connect(db)
    conn.execute(
        """UPDATE lane_approval_queue SET status='granted',
           expires_at='2000-01-01T00:00:00Z'
           WHERE approval_token=?""",
        (request.token,),
    )
    conn.commit()
    conn.close()
    harness = _publish_harness(tmp_path, db)
    with pytest.raises(ApprovalExpired):
        harness.publish_with_ledger(
            task=task,
            external_target="test:target",
            payload={"draft": "x"},
        )


def test_publish_with_ledger_writes_lane_publish_log_and_side_effects_row(
    tmp_path,
):
    db = tmp_path / "kanban.db"
    task = _task(db)
    request = _queued(db, task)
    approvals.grant(request.token, db_path=db)
    result = _publish_harness(tmp_path, db).publish_with_ledger(
        task=task,
        external_target="test:target",
        payload={"draft": "x"},
    )
    conn = schema.connect(db)
    assert result.outcome == "success"
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_publish_log"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM side_effects WHERE status='done'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM lane_task WHERE id=?", (task.id,)
    ).fetchone()[0] == "published"
    conn.close()


def test_publish_with_ledger_returns_skipped_duplicate_when_side_effect_key_exists(
    tmp_path,
):
    db = tmp_path / "kanban.db"
    task = _task(db)
    request = _queued(db, task)
    approvals.grant(request.token, db_path=db)
    harness = _publish_harness(tmp_path, db)
    first = harness.publish_with_ledger(
        task=task,
        external_target="test:target",
        payload={"draft": "x"},
        side_effect_key="stable-key",
    )
    second = harness.publish_with_ledger(
        task=task,
        external_target="test:target",
        payload={"draft": "x"},
        side_effect_key="stable-key",
    )
    assert first.outcome == "success"
    assert second.outcome == "skipped_duplicate"
    assert second.log_id == first.log_id


def test_publish_with_ledger_atomic_all_or_nothing(tmp_path):
    db = tmp_path / "kanban.db"
    task = _task(db)
    request = _queued(db, task)
    approvals.grant(request.token, db_path=db)

    def fail(_payload):
        raise RuntimeError("synthetic publish failure")

    harness = _publish_harness(tmp_path, db, publisher=fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        harness.publish_with_ledger(
            task=task,
            external_target="test:target",
            payload={"draft": "x"},
        )
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_publish_log"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM side_effects"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM lane_task WHERE id=?", (task.id,)
    ).fetchone()[0] == "awaiting_approval"
    conn.close()
