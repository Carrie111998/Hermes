"""Tests for Security Hardening (v0.29.0).

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

    schema_name = f"sec_{uuid.uuid4().hex[:12]}"
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


class TestAuditLog:
    def test_record_and_query(self, pg):
        from hermes_cli.postgres_authority import (
            record_audit_event, query_audit_log, DEFAULT_TENANT_ID,
        )

        record_audit_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            actor_type="user", actor_id="admin@acme.com",
            action="claim_task", resource_type="task",
            resource_id="task-001", outcome="success",
            details={"generation": 1},
            ip_address="192.168.1.1",
        )

        entries = query_audit_log(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(entries) == 1
        assert entries[0]["action"] == "claim_task"
        assert entries[0]["outcome"] == "success"

    def test_filter_by_actor(self, pg):
        from hermes_cli.postgres_authority import (
            record_audit_event, query_audit_log, DEFAULT_TENANT_ID,
        )

        record_audit_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            actor_type="user", actor_id="alice@acme.com",
            action="login", outcome="success",
        )
        record_audit_event(
            pg, tenant_id=DEFAULT_TENANT_ID,
            actor_type="user", actor_id="bob@acme.com",
            action="login", outcome="denied",
        )

        alice_entries = query_audit_log(
            pg, tenant_id=DEFAULT_TENANT_ID, actor_id="alice@acme.com",
        )
        assert len(alice_entries) == 1
        assert alice_entries[0]["outcome"] == "success"

    def test_tenant_isolation(self, pg):
        from hermes_cli.postgres_authority import (
            record_audit_event, query_audit_log,
        )

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        record_audit_event(
            pg, tenant_id=tenant_a,
            actor_type="worker", actor_id="w1",
            action="claim_task", outcome="success",
        )

        entries_b = query_audit_log(pg, tenant_id=tenant_b)
        assert len(entries_b) == 0

    def test_immutable_append(self, pg):
        from hermes_cli.postgres_authority import (
            record_audit_event, query_audit_log, DEFAULT_TENANT_ID,
        )

        for i in range(5):
            record_audit_event(
                pg, tenant_id=DEFAULT_TENANT_ID,
                actor_type="system", actor_id="scheduler",
                action=f"action_{i}", outcome="success",
            )

        entries = query_audit_log(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(entries) == 5


class TestSecrets:
    def test_store_and_get(self, pg):
        from hermes_cli.postgres_authority import (
            store_secret, get_secret, DEFAULT_TENANT_ID,
        )

        secret = store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="stripe-api-key",
            encrypted_value="enc:sk_live_abc123",
            created_by="admin@acme.com",
        )
        assert secret["secret_name"] == "stripe-api-key"
        assert secret["version"] == 1

        fetched = get_secret(
            pg, tenant_id=DEFAULT_TENANT_ID, secret_name="stripe-api-key",
        )
        assert fetched["encrypted_value"] == "enc:sk_live_abc123"

    def test_rotation_increments_version(self, pg):
        from hermes_cli.postgres_authority import (
            store_secret, get_secret, DEFAULT_TENANT_ID,
        )

        store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="db-password",
            encrypted_value="enc:old_password",
        )
        store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="db-password",
            encrypted_value="enc:new_password",
        )

        secret = get_secret(
            pg, tenant_id=DEFAULT_TENANT_ID, secret_name="db-password",
        )
        assert secret["version"] == 2
        assert secret["encrypted_value"] == "enc:new_password"
        assert secret["rotated_at"] is not None

    def test_delete_secret(self, pg):
        from hermes_cli.postgres_authority import (
            store_secret, delete_secret, get_secret, DEFAULT_TENANT_ID,
        )

        store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="temp-key", encrypted_value="enc:temp",
        )
        result = delete_secret(
            pg, tenant_id=DEFAULT_TENANT_ID, secret_name="temp-key",
        )
        assert result is True
        assert get_secret(
            pg, tenant_id=DEFAULT_TENANT_ID, secret_name="temp-key",
        ) is None

    def test_list_excludes_values(self, pg):
        from hermes_cli.postgres_authority import (
            store_secret, list_secrets, DEFAULT_TENANT_ID,
        )

        store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="api-key-1", encrypted_value="enc:secret1",
        )
        store_secret(
            pg, tenant_id=DEFAULT_TENANT_ID,
            secret_name="api-key-2", encrypted_value="enc:secret2",
        )

        secrets = list_secrets(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(secrets) == 2
        for s in secrets:
            assert "encrypted_value" not in s

    def test_tenant_isolation(self, pg):
        from hermes_cli.postgres_authority import store_secret, get_secret

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        store_secret(
            pg, tenant_id=tenant_a,
            secret_name="private-key", encrypted_value="enc:tenant_a_only",
        )
        assert get_secret(
            pg, tenant_id=tenant_b, secret_name="private-key",
        ) is None
