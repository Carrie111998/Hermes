"""Authority store contract tests: shared invariants across backends.

These tests verify that the Postgres authority store enforces the same
safety properties as the SQLite objectives_db. They do NOT require both
backends to have identical APIs — they verify behavioral parity:

1. Claim exclusivity: exactly one active claim per (task_id, org_id)
2. Permit once-only consumption
3. Permit payload hash binding
4. Effect idempotency via stable key
5. Fencing: stale generation rejected on all write paths
6. Permit expiry enforcement
7. Claim expiry enables reclaim

These are the authority invariants defined in the CLAUDE.md engineering
doctrine. Both the SQLite objectives_db and the Postgres authority store
must enforce them.

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import time
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

    schema_name = f"contract_{uuid.uuid4().hex[:12]}"
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


class TestClaimExclusivity:
    """Invariant: exactly one active claim per (task_id, organization_id)."""

    def test_second_claim_same_task_org_rejected(self, pg):
        from hermes_cli.postgres_authority import claim_task, DEFAULT_TENANT_ID

        gen1 = claim_task(
            pg, task_id="t1", claim_token="tok1",
            organization_id="org1", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        gen2 = claim_task(
            pg, task_id="t1", claim_token="tok2",
            organization_id="org1", worker_id="w2",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen1 == 1
        assert gen2 is None

    def test_different_org_can_claim_same_task(self, pg):
        from hermes_cli.postgres_authority import claim_task, DEFAULT_TENANT_ID

        gen1 = claim_task(
            pg, task_id="t2", claim_token="tok1",
            organization_id="org-a", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        gen2 = claim_task(
            pg, task_id="t2", claim_token="tok2",
            organization_id="org-b", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen1 == 1
        assert gen2 == 1


class TestPermitOnceOnly:
    """Invariant: a permit can be consumed at most once."""

    def test_double_consume_rejected(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, issue_permit, consume_permit, DEFAULT_TENANT_ID,
        )

        ACTION = {"do": "something"}
        claim_task(
            pg, task_id="perm-t", claim_token="perm-tok",
            organization_id="perm-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        pid = issue_permit(
            pg, task_id="perm-t", organization_id="perm-org",
            claim_token="perm-tok", lease_generation=1,
            action_payload=ACTION, ttl_seconds=300,
            tenant_id=DEFAULT_TENANT_ID,
        )
        first = consume_permit(
            pg, permit_id=pid, organization_id="perm-org",
            claim_token="perm-tok", lease_generation=1,
            action_payload=ACTION,
        )
        second = consume_permit(
            pg, permit_id=pid, organization_id="perm-org",
            claim_token="perm-tok", lease_generation=1,
            action_payload=ACTION,
        )
        assert first is True
        assert second is False


class TestPermitPayloadBinding:
    """Invariant: permit consumption verifies payload hash matches."""

    def test_wrong_payload_rejected(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, issue_permit, consume_permit, DEFAULT_TENANT_ID,
        )

        ISSUED_PAYLOAD = {"amount": 5000}
        WRONG_PAYLOAD = {"amount": 9999}

        claim_task(
            pg, task_id="hash-t", claim_token="hash-tok",
            organization_id="hash-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        pid = issue_permit(
            pg, task_id="hash-t", organization_id="hash-org",
            claim_token="hash-tok", lease_generation=1,
            action_payload=ISSUED_PAYLOAD, ttl_seconds=300,
            tenant_id=DEFAULT_TENANT_ID,
        )
        result = consume_permit(
            pg, permit_id=pid, organization_id="hash-org",
            claim_token="hash-tok", lease_generation=1,
            action_payload=WRONG_PAYLOAD,
        )
        assert result is False


class TestEffectIdempotency:
    """Invariant: same effect_key → no duplicate, returns False."""

    def test_duplicate_effect_key_is_noop(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, record_effect, DEFAULT_TENANT_ID,
        )

        claim_task(
            pg, task_id="eff-t", claim_token="eff-tok",
            organization_id="eff-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        first = record_effect(
            pg, effect_key="eff-key-1",
            task_id="eff-t", organization_id="eff-org",
            run_claim_token="eff-tok", lease_generation=1,
            effect_type="payment", payload={"x": 1},
            tenant_id=DEFAULT_TENANT_ID,
        )
        second = record_effect(
            pg, effect_key="eff-key-1",
            task_id="eff-t", organization_id="eff-org",
            run_claim_token="eff-tok", lease_generation=1,
            effect_type="payment", payload={"x": 1},
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert first is True
        assert second is False


class TestGenerationFencing:
    """Invariant: stale generation rejected on all write paths."""

    def test_complete_with_stale_gen_rejected(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, complete_task, DEFAULT_TENANT_ID,
        )

        claim_task(
            pg, task_id="gen-t", claim_token="gen-tok",
            organization_id="gen-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        result = complete_task(
            pg, task_id="gen-t", organization_id="gen-org",
            claim_token="gen-tok", lease_generation=99,
            outcome="success",
        )
        assert result is False

    def test_issue_permit_with_stale_gen_rejected(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, issue_permit, DEFAULT_TENANT_ID,
        )

        claim_task(
            pg, task_id="gen-p-t", claim_token="gen-p-tok",
            organization_id="gen-p-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        with pytest.raises(ValueError, match="No valid fenced claim"):
            issue_permit(
                pg, task_id="gen-p-t", organization_id="gen-p-org",
                claim_token="gen-p-tok", lease_generation=99,
                action_payload={"x": 1}, ttl_seconds=60,
                tenant_id=DEFAULT_TENANT_ID,
            )


class TestPermitExpiry:
    """Invariant: expired permits cannot be consumed."""

    def test_expired_permit_rejected(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, issue_permit, consume_permit, DEFAULT_TENANT_ID,
        )

        ACTION = {"expire": True}
        claim_task(
            pg, task_id="exp-t", claim_token="exp-tok",
            organization_id="exp-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        pid = issue_permit(
            pg, task_id="exp-t", organization_id="exp-org",
            claim_token="exp-tok", lease_generation=1,
            action_payload=ACTION, ttl_seconds=-1,
            tenant_id=DEFAULT_TENANT_ID,
        )
        result = consume_permit(
            pg, permit_id=pid, organization_id="exp-org",
            claim_token="exp-tok", lease_generation=1,
            action_payload=ACTION,
        )
        assert result is False


class TestClaimExpiry:
    """Invariant: expired claim can be reclaimed."""

    def test_expired_claim_allows_reclaim(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, reclaim_task, DEFAULT_TENANT_ID,
        )

        claim_task(
            pg, task_id="rcl-t", claim_token="rcl-tok",
            organization_id="rcl-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() - 1,
            tenant_id=DEFAULT_TENANT_ID,
        )
        gen = reclaim_task(
            pg, task_id="rcl-t", organization_id="rcl-org",
            new_claim_token="rcl-tok-2", new_worker_id="w2",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen == 2

    def test_active_claim_cannot_be_reclaimed(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, reclaim_task, DEFAULT_TENANT_ID,
        )

        claim_task(
            pg, task_id="act-t", claim_token="act-tok",
            organization_id="act-org", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        gen = reclaim_task(
            pg, task_id="act-t", organization_id="act-org",
            new_claim_token="act-tok-2", new_worker_id="w2",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen is None
