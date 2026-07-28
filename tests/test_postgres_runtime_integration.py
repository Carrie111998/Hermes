"""Bridge-level lifecycle integration test with Postgres authority backend.

This test exercises AuthorityBridge — the runtime-facing abstraction — in
scenarios that prove the Postgres coordination store enforces the correct
fencing, exclusivity, and idempotency invariants through the bridge API.

Limitations (to be addressed by multi-process acceptance test):
- Single process: bridge objects are instantiated directly, not via
  objective_service tick cycle or supervised worker subprocess.
- Simulated crash: lease expiry is forced via raw SQL UPDATE, not
  actual process death (SIGKILL) and natural lease timeout.
- No provider read-back: recovery checks the local effect table, not
  an external provider API.
- Default tenant: HERMES_TENANT_ID is unset, so DEFAULT_TENANT_ID
  is used — explicit multi-tenant propagation is not exercised.

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import time
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg_env(monkeypatch):
    """Set up a Postgres schema accessible via env for the bridge."""
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"runtime_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
    conn.commit()
    conn.close()

    modified_url = f"{POSTGRES_URL} options=-csearch_path={schema_name}"
    monkeypatch.setenv("AUTHORITY_POSTGRES_URL", modified_url)
    monkeypatch.delenv("HERMES_TENANT_ID", raising=False)

    yield schema_name

    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestRuntimeWithPostgresBackend:
    """Full lifecycle test via AuthorityBridge."""

    def test_worker_lifecycle_claim_permit_effect_complete(self, pg_env):
        """Simulates: worker claims task → issues permit → records effect → completes."""
        from hermes_cli.authority_bridge import AuthorityBridge

        ORG = "org-production"
        WORKER = "runtime_abc123"
        TASK = "objective-evt-001"

        bridge = AuthorityBridge(organization_id=ORG, worker_id=WORKER)
        assert bridge.active is True

        # 1. Claim
        gen = bridge.claim(
            task_id=TASK,
            claim_token=f"{WORKER}:{TASK}",
            claim_scope_url=f"urn:objective:{TASK}",
            ttl_seconds=300,
        )
        assert gen == 1, "Worker claims the objective event"

        # 2. Issue permit (policy decision)
        ACTION_PAYLOAD = {
            "action_type": "stripe.charge",
            "target_resource": "customer:cust-456",
            "amount": 2500,
            "currency": "USD",
        }
        permit_id = bridge.issue_permit(
            actor="agent:ceo",
            executor=WORKER,
            capability="payment:send",
            action_type="stripe.charge",
            target_resource="customer:cust-456",
            action_payload=ACTION_PAYLOAD,
            ttl_seconds=120,
        )
        assert permit_id is not None

        # 3. Consume permit (authorization gate before execution)
        consumed = bridge.consume_permit(
            permit_id=permit_id,
            action_payload=ACTION_PAYLOAD,
        )
        assert consumed is True

        # 4. Record effect (provider action completed)
        effect_key = f"stripe:pi_abc:{TASK}:{gen}"
        recorded = bridge.record_effect(
            effect_key=effect_key,
            effect_type="payment.sent",
            permit_id=permit_id,
            provider="stripe",
            provider_ref="pi_abc",
            idempotency_key="idem-prod-001",
            payload={"charge_id": "ch_xyz", "amount": 2500, "status": "succeeded"},
        )
        assert recorded is True

        # 5. Complete task
        completed = bridge.complete(outcome="success")
        assert completed is True
        assert bridge.has_claim is False

        bridge.close()

    def test_two_workers_race_only_one_completes(self, pg_env):
        """Two workers in the same org race for the same task."""
        from hermes_cli.authority_bridge import AuthorityBridge

        ORG = "org-race"
        TASK = "objective-race-001"

        worker1 = AuthorityBridge(organization_id=ORG, worker_id="runtime_w1")
        worker2 = AuthorityBridge(organization_id=ORG, worker_id="runtime_w2")

        gen1 = worker1.claim(task_id=TASK, claim_token="w1:tok")
        gen2 = worker2.claim(task_id=TASK, claim_token="w2:tok")

        assert gen1 == 1
        assert gen2 is None, "Second worker loses the race"

        # Winner completes
        ACTION = {"do": "work"}
        pid = worker1.issue_permit(
            actor="ceo", executor="runtime_w1",
            capability="task:execute", action_type="work",
            target_resource="resource:1",
            action_payload=ACTION,
        )
        worker1.consume_permit(permit_id=pid, action_payload=ACTION)
        worker1.record_effect(
            effect_key="race:effect:1",
            effect_type="work.done",
            payload={"result": "ok"},
        )
        assert worker1.complete(outcome="success") is True

        worker1.close()
        worker2.close()

    def test_crash_recovery_does_not_repeat_effect(self, pg_env):
        """Worker crashes after effect → recovery finds effect, does not repeat."""
        from hermes_cli.authority_bridge import AuthorityBridge
        from hermes_cli.postgres_authority import (
            connect, init_schema, reclaim_task, get_effect, DEFAULT_TENANT_ID,
        )

        ORG = "org-recovery"
        TASK = "objective-crash-001"
        EFFECT_KEY = "stripe:pi_crash:recovery:1"

        # Worker 1 claims, records effect, then "crashes"
        worker1 = AuthorityBridge(organization_id=ORG, worker_id="runtime_crash")
        gen1 = worker1.claim(task_id=TASK, claim_token="crash:tok")
        assert gen1 == 1

        ACTION = {"charge": True}
        pid = worker1.issue_permit(
            actor="ceo", executor="runtime_crash",
            capability="payment:send", action_type="stripe.charge",
            target_resource="cust:789",
            action_payload=ACTION,
        )
        worker1.consume_permit(permit_id=pid, action_payload=ACTION)
        worker1.record_effect(
            effect_key=EFFECT_KEY,
            effect_type="payment.sent",
            permit_id=pid,
            provider="stripe",
            provider_ref="pi_crash",
            payload={"charged": True},
        )
        # Worker crashes here — no complete() called
        # Simulate crash by expiring the claim
        conn = worker1._conn
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE task_claims SET expires_at = NOW() - INTERVAL '1 second' "
                "WHERE task_id = %s AND organization_id = %s",
                (TASK, ORG),
            )
        conn.commit()

        # Recovery worker reclaims
        gen2 = reclaim_task(
            conn, task_id=TASK, organization_id=ORG,
            new_claim_token="recovery:tok",
            new_worker_id="runtime_recovery",
            claim_scope_url="urn:recovery",
            expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen2 == 2

        # Recovery worker checks for existing effect (provider read-back)
        existing = get_effect(conn, effect_key=EFFECT_KEY)
        assert existing is not None, "Effect from crashed worker is visible"

        # Recovery tries to record same effect — idempotent no-op
        from hermes_cli.postgres_authority import record_effect
        duplicate = record_effect(
            conn, effect_key=EFFECT_KEY,
            task_id=TASK, organization_id=ORG,
            run_claim_token="recovery:tok", lease_generation=2,
            effect_type="payment.sent", provider="stripe",
            provider_ref="pi_crash", payload={"charged": True},
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert duplicate is False, "Effect not duplicated"

        # Count effects — must be exactly 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM execution_effects "
                "WHERE task_id = %s AND organization_id = %s",
                (TASK, ORG),
            )
            assert cur.fetchone()["cnt"] == 1

        worker1.close()
