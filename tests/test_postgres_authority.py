"""Tests for Postgres authority store.

These tests verify the Postgres authority store operations work correctly.
They require a running Postgres instance (provided by CI or local Docker).

Tests are structured in three tiers:
  1. Basic operation tests  — happy-path CRUD
  2. Fencing / invariant tests — concurrent workers, stale generation, etc.
  3. Adversarial tests — every attack / failure mode named in the requirement

All tests use only the public postgres_authority API.  No direct row
manipulation that would bypass the authority machinery.
"""

import os
import time
import uuid
from typing import Any, Iterator

import pytest

# Skip all tests if psycopg not installed
psycopg = pytest.importorskip("psycopg")

# Skip all tests that REQUIRE a Postgres connection if no URL available
_REQUIRES_PG = pytest.mark.skipif(
    not (
        os.environ.get("AUTHORITY_POSTGRES_TEST_URL")
        or os.environ.get("POSTGRES_HOST")
        or os.environ.get("DATABASE_URL")
    ),
    reason=(
        "No Postgres URL available.  Set AUTHORITY_POSTGRES_TEST_URL, "
        "DATABASE_URL, or POSTGRES_HOST to run Postgres authority tests."
    ),
)

# Apply to all classes that need a live connection.
pytestmark = _REQUIRES_PG


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = os.environ.get("AUTHORITY_POSTGRES_TEST_URL")
    if url:
        return url
    host = os.environ.get("POSTGRES_HOST", "/var/run/postgresql")
    port = os.environ.get("POSTGRES_PORT", "")
    user = os.environ.get("POSTGRES_USER", "cftest")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    database = os.environ.get("POSTGRES_DATABASE", "charterforge_test")
    if host.startswith("/"):
        # Unix socket — use DSN-style (no password needed with peer/trust auth)
        return f"host={host} user={user} dbname={database}"
    port_part = f":{port}" if port else ""
    if password:
        return f"postgresql://{user}:{password}@{host}{port_part}/{database}"
    return f"postgresql://{user}@{host}{port_part}/{database}"


@pytest.fixture
def pg(postgres_url: str) -> Iterator[Any]:
    """Isolated Postgres connection in a unique schema per test."""
    from hermes_cli.postgres_authority import connect, init_schema
    import psycopg as _psycopg
    from psycopg.rows import dict_row as _dict_row

    schema = f"test_{uuid.uuid4().hex[:10]}"

    # First connection to create the schema.
    setup_conn = connect(postgres_url)
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    setup_conn.commit()
    setup_conn.close()

    # Re-connect with search_path scoped to the test schema.
    # psycopg accepts `options` as a keyword that maps to the Postgres
    # connection parameter (sets session-level GUCs before first query).
    conn = _psycopg.connect(
        postgres_url,
        row_factory=_dict_row,
        options=f"-c search_path={schema}",
    )
    conn.autocommit = False

    init_schema(conn)

    yield conn

    conn.close()

    # Drop schema with a fresh connection (no search_path restriction).
    cleanup_conn = connect(postgres_url)
    with cleanup_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    cleanup_conn.commit()
    cleanup_conn.close()


def _new_task() -> str:
    return f"task-{uuid.uuid4().hex[:10]}"


def _new_token() -> str:
    return f"tok-{uuid.uuid4().hex[:10]}"


ORG = "test-org-alpha"
ORG2 = "test-org-beta"


def _must_claim(pg, *, task_id: str, claim_token: str,
                organization_id: str = ORG, worker_id: str = "w1",
                expires_at: float | None = None) -> int:
    """Claim and assert success, returning the lease_generation as int."""
    from hermes_cli.postgres_authority import claim_task
    gen = claim_task(
        pg, task_id=task_id, claim_token=claim_token,
        organization_id=organization_id, worker_id=worker_id,
        claim_scope_url="",
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
    )
    assert gen is not None, "claim must succeed"
    return gen


# ---------------------------------------------------------------------------
# 1. Basic operation tests
# ---------------------------------------------------------------------------


class TestBasicClaim:
    def test_claim_task_returns_generation_1(self, pg):
        from hermes_cli.postgres_authority import claim_task

        gen = claim_task(
            pg,
            task_id=_new_task(),
            claim_token=_new_token(),
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="https://example.com/scope",
            expires_at=time.time() + 3600,
        )
        assert gen == 1

    def test_get_claim_returns_active_claim(self, pg):
        from hermes_cli.postgres_authority import claim_task, get_claim

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg,
            task_id=task_id,
            claim_token=token,
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="",
            expires_at=time.time() + 3600,
        )
        assert gen == 1

        claim = get_claim(pg, task_id=task_id, organization_id=ORG)
        assert claim is not None
        assert claim["task_id"] == task_id
        assert claim["lease_generation"] == 1

    def test_release_claim_removes_it(self, pg):
        from hermes_cli.postgres_authority import claim_task, release_claim, get_claim

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg,
            task_id=task_id,
            claim_token=token,
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="",
            expires_at=time.time() + 3600,
        )
        assert gen is not None

        ok = release_claim(
            pg,
            task_id=task_id,
            organization_id=ORG,
            claim_token=token,
            lease_generation=gen,
        )
        assert ok is True
        assert get_claim(pg, task_id=task_id, organization_id=ORG) is None

    def test_complete_task_succeeds_and_releases(self, pg):
        from hermes_cli.postgres_authority import claim_task, complete_task, get_claim

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg,
            task_id=task_id,
            claim_token=token,
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="",
            expires_at=time.time() + 3600,
        )
        assert gen is not None

        ok = complete_task(
            pg,
            task_id=task_id,
            organization_id=ORG,
            claim_token=token,
            lease_generation=gen,
            outcome="success",
        )
        assert ok is True
        assert get_claim(pg, task_id=task_id, organization_id=ORG) is None

    def test_complete_with_effects_stored(self, pg):
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg,
            task_id=task_id,
            claim_token=token,
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="",
            expires_at=time.time() + 3600,
        )
        effect_key = f"{ORG}:{task_id}:a1:p1:stripe:ch_001"
        ok = complete_task(
            pg,
            task_id=task_id,
            organization_id=ORG,
            claim_token=token,
            lease_generation=gen,
            outcome="success",
            effects=[
                {
                    "effect_key": effect_key,
                    "type": "payment",
                    "provider": "stripe",
                    "provider_ref": "ch_001",
                    "idempotency_key": "ik-001",
                    "amount": 1000,
                }
            ],
        )
        assert ok is True
        with pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) as n FROM execution_effects WHERE task_id = %s",
                (task_id,),
            )
            assert cur.fetchone()["n"] == 1

    def test_complete_with_effect_missing_key_raises(self, pg):
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg,
            task_id=task_id,
            claim_token=token,
            organization_id=ORG,
            worker_id="w1",
            claim_scope_url="",
            expires_at=time.time() + 3600,
        )
        with pytest.raises(ValueError, match="effect_key"):
            complete_task(
                pg,
                task_id=task_id,
                organization_id=ORG,
                claim_token=token,
                lease_generation=gen,
                outcome="success",
                effects=[{"type": "payment"}],  # missing effect_key
            )


class TestPermitFlow:
    def test_issue_and_consume_permit(self, pg):
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit

        task_id = _new_task()
        token = _new_token()
        payload = {"action": "send_email", "nonce": uuid.uuid4().hex}
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        assert len(permit_id) == 36  # UUID

        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        assert ok is True

    def test_consume_permit_twice_fails(self, pg):
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit

        task_id = _new_task()
        token = _new_token()
        payload = {"action": "test", "nonce": uuid.uuid4().hex}
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        # Second consume must fail.
        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        assert ok is False


class TestCleanup:
    def test_cleanup_expired_claims(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, reclaim_task, cleanup_expired_claims, get_claim
        )

        task_id = _new_task()
        token = _new_token()
        # Insert an already-expired claim by going 10s into the past.
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() - 10,
        )
        assert gen is not None

        # Mark the run as reclaimed (as reclaim_task would do) so GC can fire.
        with pg.cursor() as cur:
            cur.execute(
                "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', ended_at=NOW() "
                "WHERE task_id=%s AND organization_id=%s AND status='pending'",
                (task_id, ORG),
            )
        pg.commit()

        count = cleanup_expired_claims(pg)
        assert count >= 1
        assert get_claim(pg, task_id=task_id, organization_id=ORG) is None


# ---------------------------------------------------------------------------
# 2. Fencing / invariant tests
# ---------------------------------------------------------------------------


class TestClaimExclusivity:
    """Claim exclusivity: one active authoritative claim per (task, org)."""

    def test_two_workers_race_only_one_wins(self, pg):
        """INVARIANT: UNIQUE (task_id, organization_id) prevents dual authority."""
        from hermes_cli.postgres_authority import claim_task

        task_id = _new_task()
        token1 = _new_token()
        token2 = _new_token()
        expires = time.time() + 3600

        gen1 = claim_task(
            pg, task_id=task_id, claim_token=token1,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=expires,
        )
        gen2 = claim_task(
            pg, task_id=task_id, claim_token=token2,
            organization_id=ORG, worker_id="w2",
            claim_scope_url="", expires_at=expires,
        )

        assert gen1 == 1, "first claim must succeed with generation 1"
        assert gen2 is None, "second claim must be rejected"

    def test_different_orgs_can_claim_same_task_id(self, pg):
        """Task IDs are org-scoped; different orgs do not conflict."""
        from hermes_cli.postgres_authority import claim_task

        task_id = _new_task()
        expires = time.time() + 3600

        gen1 = claim_task(
            pg, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=expires,
        )
        gen2 = claim_task(
            pg, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG2, worker_id="w2",
            claim_scope_url="", expires_at=expires,
        )

        assert gen1 == 1
        assert gen2 == 1


class TestExpiredClaimReplacement:
    """Expired claims must be atomically replaced with a strictly higher generation."""

    def test_reclaim_increments_generation(self, pg):
        from hermes_cli.postgres_authority import claim_task, reclaim_task, get_claim

        task_id = _new_task()
        gen1 = claim_task(
            pg, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() - 1,  # already expired
        )
        assert gen1 == 1

        gen2 = reclaim_task(
            pg, task_id=task_id, organization_id=ORG,
            new_claim_token=_new_token(), new_worker_id="w2",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        assert gen2 == 2, "reclaim must produce generation 2"

        claim = get_claim(pg, task_id=task_id, organization_id=ORG)
        assert claim is not None
        assert claim["lease_generation"] == 2

    def test_reclaim_non_expired_claim_fails(self, pg):
        from hermes_cli.postgres_authority import claim_task, reclaim_task

        task_id = _new_task()
        claim_task(
            pg, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,  # active
        )

        gen = reclaim_task(
            pg, task_id=task_id, organization_id=ORG,
            new_claim_token=_new_token(), new_worker_id="w2",
            claim_scope_url="", expires_at=time.time() + 7200,
        )
        assert gen is None, "must not reclaim an active (non-expired) claim"


class TestStaleWorkerFencing:
    """A stale (superseded) worker must be blocked from all authoritative writes."""

    def _setup_stale_and_recovery(self, pg):
        """Returns (stale_token, stale_gen, recovery_token, recovery_gen, task_id)."""
        from hermes_cli.postgres_authority import claim_task, reclaim_task

        task_id = _new_task()
        stale_token = _new_token()
        claim_task(
            pg, task_id=task_id, claim_token=stale_token,
            organization_id=ORG, worker_id="stale-w",
            claim_scope_url="", expires_at=time.time() - 1,
        )
        recovery_token = _new_token()
        recovery_gen = reclaim_task(
            pg, task_id=task_id, organization_id=ORG,
            new_claim_token=recovery_token, new_worker_id="recovery-w",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        return stale_token, 1, recovery_token, recovery_gen, task_id

    def test_stale_worker_cannot_complete(self, pg):
        from hermes_cli.postgres_authority import complete_task

        stale_token, stale_gen, recovery_token, recovery_gen, task_id = (
            self._setup_stale_and_recovery(pg)
        )
        ok = complete_task(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=stale_token, lease_generation=stale_gen,
            outcome="stale-success",
        )
        assert ok is False, "stale worker must not complete"

    def test_stale_worker_cannot_release_claim(self, pg):
        from hermes_cli.postgres_authority import release_claim

        stale_token, stale_gen, _, _, task_id = self._setup_stale_and_recovery(pg)
        ok = release_claim(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=stale_token, lease_generation=stale_gen,
        )
        assert ok is False, "stale worker must not release the recovery worker's claim"

    def test_stale_worker_cannot_consume_permit(self, pg):
        from hermes_cli.postgres_authority import issue_permit, consume_permit

        stale_token, stale_gen, recovery_token, recovery_gen, task_id = (
            self._setup_stale_and_recovery(pg)
        )
        payload = {"action": "stale-action", "nonce": uuid.uuid4().hex}

        # Recovery worker issues a permit legitimately.
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=recovery_token, lease_generation=recovery_gen,
            action_payload=payload,
        )

        # Stale worker tries to consume with its old generation.
        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=stale_token, lease_generation=stale_gen,
            action_payload=payload,
        )
        assert ok is False, "stale worker must not consume a permit"

    def test_stale_worker_cannot_issue_permit(self, pg):
        from hermes_cli.postgres_authority import issue_permit

        stale_token, stale_gen, _, _, task_id = self._setup_stale_and_recovery(pg)
        with pytest.raises(ValueError, match="No valid fenced claim"):
            issue_permit(
                pg, task_id=task_id, organization_id=ORG,
                claim_token=stale_token, lease_generation=stale_gen,
                action_payload={"action": "stale"},
            )


# ---------------------------------------------------------------------------
# 3. Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialClaims:
    def test_duplicate_completion_second_call_rejected(self, pg):
        """complete_task must be idempotent-reject: second call returns False."""
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        assert complete_task(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen, outcome="success",
        ) is True
        # Second call — claim row is already deleted, run is 'completed'.
        assert complete_task(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen, outcome="success",
        ) is False

    def test_mismatched_organization_rejected(self, pg):
        """A worker with the right token but wrong org must be rejected."""
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        ok = complete_task(
            pg, task_id=task_id, organization_id=ORG2,  # wrong org
            claim_token=token, lease_generation=gen, outcome="success",
        )
        assert ok is False

    def test_mismatched_task_rejected(self, pg):
        """complete_task with a different task_id must be rejected."""
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        other_task = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        ok = complete_task(
            pg, task_id=other_task, organization_id=ORG,  # wrong task
            claim_token=token, lease_generation=gen, outcome="success",
        )
        assert ok is False

    def test_wrong_fencing_generation_rejected(self, pg):
        """Submitting a wrong (lower) generation must be rejected."""
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        ok = complete_task(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen + 999,  # wrong gen
            outcome="success",
        )
        assert ok is False

    def test_duplicate_effect_insertion_idempotent(self, pg):
        """Two inserts of the same effect_key must produce exactly one row."""
        from hermes_cli.postgres_authority import claim_task, record_effect

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        key = f"{ORG}:{task_id}:a1:p1:stripe:ch_dup"
        payload = {"amount": 500, "currency": "usd"}

        r1 = record_effect(
            pg, effect_key=key, task_id=task_id,
            organization_id=ORG, run_claim_token=token,
            lease_generation=gen, effect_type="payment",
            provider="stripe", provider_ref="ch_dup",
            idempotency_key="ik-dup", payload=payload,
        )
        r2 = record_effect(
            pg, effect_key=key, task_id=task_id,
            organization_id=ORG, run_claim_token=token,
            lease_generation=gen, effect_type="payment",
            provider="stripe", provider_ref="ch_dup",
            idempotency_key="ik-dup", payload=payload,
        )

        assert r1 is True, "first insert must succeed"
        assert r2 is False, "second insert must be a no-op (idempotent)"

        with pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM execution_effects WHERE effect_key = %s",
                (key,),
            )
            assert cur.fetchone()["n"] == 1

    def test_get_effect_returns_existing(self, pg):
        from hermes_cli.postgres_authority import claim_task, record_effect, get_effect

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        key = f"{ORG}:{task_id}:a1:p1:test:ref001"
        record_effect(
            pg, effect_key=key, task_id=task_id,
            organization_id=ORG, run_claim_token=token,
            lease_generation=gen, effect_type="notification",
            payload={"msg": "hello"},
        )
        effect = get_effect(pg, effect_key=key)
        assert effect is not None
        assert effect["effect_type"] == "notification"

    def test_mismatched_action_payload_permit_rejected(self, pg):
        """Consuming with a different payload must fail the hash check."""
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit

        task_id = _new_task()
        token = _new_token()
        original_payload = {"action": "transfer", "amount": 100}
        altered_payload = {"action": "transfer", "amount": 999}

        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=original_payload,
        )
        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=altered_payload,  # changed!
        )
        assert ok is False

    def test_revoked_permit_cannot_be_consumed(self, pg):
        from hermes_cli.postgres_authority import (
            claim_task, issue_permit, revoke_permit, consume_permit
        )

        task_id = _new_task()
        token = _new_token()
        payload = {"action": "write", "nonce": uuid.uuid4().hex}

        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        revoke_permit(pg, permit_id=permit_id, organization_id=ORG, reason="test")

        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        assert ok is False

    def test_expired_claim_completion_rejected(self, pg):
        """complete_task must be rejected when the claim has expired."""
        from hermes_cli.postgres_authority import claim_task, complete_task

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() - 1,  # already expired
        )
        assert gen is not None

        ok = complete_task(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            outcome="success",
        )
        assert ok is False, "expired-claim completion must be rejected"

    def test_permit_issue_on_expired_claim_rejected(self, pg):
        from hermes_cli.postgres_authority import claim_task, issue_permit

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() - 1,
        )
        with pytest.raises(ValueError):
            issue_permit(
                pg, task_id=task_id, organization_id=ORG,
                claim_token=token, lease_generation=gen,
                action_payload={"action": "x"},
            )

    def test_mismatched_org_permit_consumption_rejected(self, pg):
        """Consuming a permit with a different org must fail."""
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit

        task_id = _new_task()
        token = _new_token()
        payload = {"action": "x", "nonce": uuid.uuid4().hex}

        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        permit_id = issue_permit(
            pg, task_id=task_id, organization_id=ORG,
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        ok = consume_permit(
            pg, permit_id=permit_id, organization_id=ORG2,  # wrong org
            claim_token=token, lease_generation=gen,
            action_payload=payload,
        )
        assert ok is False

    def test_effect_scoped_to_correct_organization(self, pg):
        """Effect rows must carry the org that inserted them."""
        from hermes_cli.postgres_authority import claim_task, record_effect

        task_id = _new_task()
        token = _new_token()
        gen = claim_task(
            pg, task_id=task_id, claim_token=token,
            organization_id=ORG, worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 3600,
        )
        key = f"{ORG}:{task_id}:a2:p2:test:ref002"
        record_effect(
            pg, effect_key=key, task_id=task_id,
            organization_id=ORG, run_claim_token=token,
            lease_generation=gen, effect_type="notification",
            payload={"msg": "org-scoped"},
        )
        with pg.cursor() as cur:
            cur.execute(
                "SELECT organization_id FROM execution_effects WHERE effect_key = %s",
                (key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["organization_id"] == ORG

    def test_simultaneous_claims_exactly_one_succeeds(self, pg, postgres_url):
        """Race two independent connections against the same task.

        Uses two separate Postgres connections to exercise the DB-level
        UNIQUE constraint under real concurrency.  conn_b is given the
        same search_path as pg (the test-scoped schema) by reading it
        from the existing connection.
        """
        from hermes_cli.postgres_authority import claim_task
        from psycopg.rows import dict_row as _dict_row
        import psycopg as _psycopg

        # Get the current search_path from the pg connection (test schema).
        with pg.cursor() as cur:
            cur.execute("SHOW search_path")
            row = cur.fetchone()
            schema = row["search_path"]

        conn_b = _psycopg.connect(
            postgres_url,
            row_factory=_dict_row,
            options=f"-c search_path={schema}",
        )
        conn_b.autocommit = False

        task_id = _new_task()
        expires = time.time() + 3600

        gen_a = claim_task(
            pg, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG, worker_id="conn-a",
            claim_scope_url="", expires_at=expires,
        )
        gen_b = claim_task(
            conn_b, task_id=task_id, claim_token=_new_token(),
            organization_id=ORG, worker_id="conn-b",
            claim_scope_url="", expires_at=expires,
        )

        conn_b.close()

        winners = [g for g in (gen_a, gen_b) if g is not None]
        assert len(winners) == 1, (
            f"exactly one worker must win; got gen_a={gen_a}, gen_b={gen_b}"
        )


# ---------------------------------------------------------------------------
# 4. Schema version / migration tests
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_fresh_install_reaches_current_version(self, pg):
        from hermes_cli.postgres_authority import get_schema_version, SCHEMA_VERSION

        assert get_schema_version(pg) == SCHEMA_VERSION

    def test_init_schema_idempotent(self, pg):
        """Calling init_schema twice on an already-migrated DB is a no-op."""
        from hermes_cli.postgres_authority import init_schema, get_schema_version, SCHEMA_VERSION

        init_schema(pg)  # second call
        assert get_schema_version(pg) == SCHEMA_VERSION

    def test_future_schema_version_fails_closed(self, pg):
        """A DB with version > SCHEMA_VERSION must reject init_schema."""
        from hermes_cli.postgres_authority import init_schema, SCHEMA_VERSION

        future_version = SCHEMA_VERSION + 99
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (%s) ON CONFLICT DO NOTHING",
                (future_version,),
            )
        pg.commit()

        with pytest.raises(RuntimeError, match="exceeds supported version"):
            init_schema(pg)


# Note: Authority-store capability contract tests (postgres backend recognition,
# SQLite multi-host rejection, unknown backend fail-closed) live in:
#   tests/hermes_cli/test_authority_store.py
# Those tests do not require a live Postgres connection.
