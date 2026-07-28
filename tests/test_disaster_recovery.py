"""Tests for Disaster Recovery (v0.28.0).

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg():
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"dr_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    conn.commit()
    from hermes_cli.postgres_authority import init_schema
    init_schema(conn)
    yield conn
    conn.close()
    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestBackups:
    def test_create_and_list(self, pg):
        from hermes_cli.postgres_authority import (
            create_backup, list_backups, DEFAULT_TENANT_ID,
        )

        backup = create_backup(
            pg, tenant_id=DEFAULT_TENANT_ID,
            backup_type="full",
            storage_path="s3://backups/2026-07-28/full.tar.gz",
        )
        assert backup["status"] == "pending"
        assert backup["backup_type"] == "full"

        backups = list_backups(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(backups) == 1

    def test_complete_backup(self, pg):
        from hermes_cli.postgres_authority import (
            create_backup, complete_backup, list_backups, DEFAULT_TENANT_ID,
        )

        backup = create_backup(
            pg, tenant_id=DEFAULT_TENANT_ID, backup_type="snapshot",
        )
        result = complete_backup(pg, backup_id=backup["id"], size_bytes=1024000)
        assert result is True

        backups = list_backups(pg, tenant_id=DEFAULT_TENANT_ID)
        assert backups[0]["status"] == "completed"


class TestRestorePoints:
    def test_create_and_list(self, pg):
        from hermes_cli.postgres_authority import (
            create_restore_point, list_restore_points, DEFAULT_TENANT_ID,
        )

        rp = create_restore_point(
            pg, tenant_id=DEFAULT_TENANT_ID, name="pre-migration",
        )
        assert rp["name"] == "pre-migration"

        points = list_restore_points(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(points) == 1

    def test_upsert_name(self, pg):
        from hermes_cli.postgres_authority import (
            create_backup, create_restore_point, list_restore_points,
            DEFAULT_TENANT_ID,
        )

        b1 = create_backup(pg, tenant_id=DEFAULT_TENANT_ID, backup_type="full")
        b2 = create_backup(pg, tenant_id=DEFAULT_TENANT_ID, backup_type="full")

        create_restore_point(
            pg, tenant_id=DEFAULT_TENANT_ID, name="latest", backup_id=b1["id"],
        )
        create_restore_point(
            pg, tenant_id=DEFAULT_TENANT_ID, name="latest", backup_id=b2["id"],
        )

        points = list_restore_points(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(points) == 1
        assert points[0]["backup_id"] == b2["id"]


class TestFailoverDrills:
    def test_schedule_and_complete(self, pg):
        from hermes_cli.postgres_authority import (
            schedule_drill, complete_drill, list_drills, DEFAULT_TENANT_ID,
        )

        drill = schedule_drill(
            pg, tenant_id=DEFAULT_TENANT_ID, drill_type="failover-primary",
        )
        assert drill["status"] == "scheduled"

        complete_drill(
            pg, drill_id=drill["id"], status="passed",
            results={"rpo_seconds": 5, "rto_seconds": 30, "data_loss": False},
        )

        drills = list_drills(pg, tenant_id=DEFAULT_TENANT_ID)
        assert drills[0]["status"] == "passed"
        assert drills[0]["results"]["rto_seconds"] == 30

    def test_failed_drill(self, pg):
        from hermes_cli.postgres_authority import (
            schedule_drill, complete_drill, list_drills, DEFAULT_TENANT_ID,
        )

        drill = schedule_drill(
            pg, tenant_id=DEFAULT_TENANT_ID, drill_type="restore-from-backup",
        )
        complete_drill(
            pg, drill_id=drill["id"], status="failed",
            results={"error": "backup corrupted", "step": "verify_checksum"},
        )

        drills = list_drills(pg, tenant_id=DEFAULT_TENANT_ID)
        assert drills[0]["status"] == "failed"
