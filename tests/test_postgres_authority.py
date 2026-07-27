"""Tests for Postgres authority store.

These tests verify the Postgres authority store operations work correctly.
They require a running Postgres instance (provided by CI or local Docker).
"""

import os
import time
import uuid
from typing import Any, Iterator

import pytest

# Skip all tests if psycopg not installed
psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Get Postgres URL for testing.
    
    Uses AUTHORITY_POSTGRES_TEST_URL if set, otherwise constructs
    from standard Postgres environment variables.
    """
    url = os.environ.get("AUTHORITY_POSTGRES_TEST_URL")
    if url:
        return url
    
    # Fallback to standard Postgres env vars
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "test")
    password = os.environ.get("POSTGRES_PASSWORD", "test")
    database = os.environ.get("POSTGRES_DATABASE", "charterforge_test")
    
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture
def postgres_conn(postgres_url: str) -> Iterator[Any]:
    """Create a Postgres connection for testing.
    
    Creates a unique schema for each test to isolate them.
    """
    from hermes_cli.postgres_authority import connect, init_schema
    import psycopg
    
    # Use a unique schema per test
    test_schema = f"test_{uuid.uuid4().hex[:8]}"
    url_with_schema = f"{postgres_url}?options=-c%20search_path%3D{test_schema}"
    
    conn = connect(url_with_schema)
    
    # Create schema
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
    
    # Initialize tables
    init_schema(conn)
    
    yield conn
    
    # Cleanup: drop schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
    conn.commit()
    conn.close()


class TestPostgresClaimOperations:
    """Test claim operations."""
    
    def test_claim_task_succeeds(self, postgres_conn: Any):
        """Claiming a task should succeed."""
        from hermes_cli.postgres_authority import claim_task
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        result = claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        assert result is True
    
    def test_claim_already_claimed_fails(self, postgres_conn: Any):
        """Claiming an already-claimed task should fail."""
        from hermes_cli.postgres_authority import claim_task
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock1 = f"claim-{uuid.uuid4().hex[:8]}"
        claim_lock2 = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        # First claim succeeds
        result1 = claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock1,
            organization_id="test-org",
            worker_id="test-worker-1",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        assert result1 is True
        
        # Second claim fails (task_id already claimed via unique constraint)
        result2 = claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock2,
            organization_id="test-org",
            worker_id="test-worker-2",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        assert result2 is False
    
    def test_get_claim_returns_claim(self, postgres_conn: Any):
        """get_claim should return active claim."""
        from hermes_cli.postgres_authority import claim_task, get_claim
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        claim = get_claim(
            postgres_conn,
            task_id=task_id,
            organization_id="test-org",
        )
        
        assert claim is not None
        assert claim["task_id"] == task_id
        assert claim["claim_lock"] == claim_lock
    
    def test_release_claim_removes_claim(self, postgres_conn: Any):
        """release_claim should remove the claim."""
        from hermes_cli.postgres_authority import claim_task, release_claim, get_claim
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        result = release_claim(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
        )
        
        assert result is True
        
        # Verify claim is gone
        claim = get_claim(
            postgres_conn,
            task_id=task_id,
            organization_id="test-org",
        )
        assert claim is None


class TestPostgresCompletion:
    """Test task completion."""
    
    def test_complete_task_succeeds(self, postgres_conn: Any):
        """complete_task should succeed for valid claim."""
        from hermes_cli.postgres_authority import claim_task, complete_task, get_claim
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        result = complete_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            outcome="success",
        )
        
        assert result is True
        
        # Claim should be released
        claim = get_claim(
            postgres_conn,
            task_id=task_id,
            organization_id="test-org",
        )
        assert claim is None
    
    def test_complete_task_wrong_claim_fails(self, postgres_conn: Any):
        """complete_task should fail for wrong claim lock."""
        from hermes_cli.postgres_authority import claim_task, complete_task
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        wrong_claim = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        # Wrong claim lock should fail
        result = complete_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=wrong_claim,
            organization_id="test-org",
            outcome="success",
        )
        
        assert result is False
    
    def test_complete_with_effects(self, postgres_conn: Any):
        """complete_task should record effects."""
        from hermes_cli.postgres_authority import claim_task, complete_task
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        effects = [
            {"type": "payment", "amount": 1000},
            {"type": "notification", "message": "done"},
        ]
        
        result = complete_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            outcome="success",
            effects=effects,
        )
        
        assert result is True
        
        # Verify effects were recorded
        with postgres_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as count FROM execution_effects WHERE task_id = %s",
                (task_id,)
            )
            row = cur.fetchone()
            assert row["count"] == 2


class TestPostgresPermitFlow:
    """Test execution permit flow."""
    
    def test_issue_permit_succeeds(self, postgres_conn: Any):
        """issue_permit should return a permit ID."""
        from hermes_cli.postgres_authority import claim_task, issue_permit
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        permit_id = issue_permit(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            action_payload={"action": "test"},
        )
        
        assert permit_id is not None
        assert len(permit_id) == 36  # UUID format
    
    def test_consume_permit_succeeds(self, postgres_conn: Any):
        """consume_permit should succeed for valid permit."""
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        action_payload = {"action": "test", "nonce": uuid.uuid4().hex}
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        permit_id = issue_permit(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            action_payload=action_payload,
        )
        
        result = consume_permit(
            postgres_conn,
            permit_id=permit_id,
            claim_lock=claim_lock,
            action_payload=action_payload,
        )
        
        assert result is True
    
    def test_consume_permit_twice_fails(self, postgres_conn: Any):
        """consume_permit should fail for already-consumed permit."""
        from hermes_cli.postgres_authority import claim_task, issue_permit, consume_permit
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() + 3600
        action_payload = {"action": "test", "nonce": uuid.uuid4().hex}
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        permit_id = issue_permit(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            action_payload=action_payload,
        )
        
        # First consume succeeds
        consume_permit(
            postgres_conn,
            permit_id=permit_id,
            claim_lock=claim_lock,
            action_payload=action_payload,
        )
        
        # Second consume fails
        result = consume_permit(
            postgres_conn,
            permit_id=permit_id,
            claim_lock=claim_lock,
            action_payload=action_payload,
        )
        
        assert result is False


class TestPostgresCleanup:
    """Test cleanup operations."""
    
    def test_cleanup_expired_claims(self, postgres_conn: Any):
        """cleanup_expired_claims should remove old claims."""
        from hermes_cli.postgres_authority import claim_task, cleanup_expired_claims, get_claim
        
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        claim_lock = f"claim-{uuid.uuid4().hex[:8]}"
        expires_at = time.time() - 1  # Already expired
        
        claim_task(
            postgres_conn,
            task_id=task_id,
            claim_lock=claim_lock,
            organization_id="test-org",
            worker_id="test-worker",
            claim_scope_url="https://example.com/scope",
            expires_at=expires_at,
        )
        
        count = cleanup_expired_claims(postgres_conn)
        
        assert count >= 1
        
        # Claim should be gone
        claim = get_claim(
            postgres_conn,
            task_id=task_id,
            organization_id="test-org",
        )
        assert claim is None


class TestAuthorityBackendDetection:
    """Test authority backend detection."""
    
    def test_sqlite_backend_by_default(self, monkeypatch):
        """Should default to sqlite if no Postgres env vars."""
        monkeypatch.delenv("AUTHORITY_POSTGRES_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        
        from hermes_cli.postgres_authority import get_authority_backend
        
        backend = get_authority_backend()
        assert backend == "sqlite"
    
    def test_postgres_backend_with_database_url(self, monkeypatch):
        """Should use postgres if DATABASE_URL set."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test@test/test")
        monkeypatch.delenv("AUTHORITY_POSTGRES_URL", raising=False)
        
        from hermes_cli.postgres_authority import get_authority_backend
        
        backend = get_authority_backend()
        assert backend == "postgres"
    
    def test_postgres_backend_with_authority_url(self, monkeypatch):
        """Should use postgres if AUTHORITY_POSTGRES_URL set."""
        monkeypatch.setenv("AUTHORITY_POSTGRES_URL", "postgresql://auth@test/test")
        
        from hermes_cli.postgres_authority import get_authority_backend
        
        backend = get_authority_backend()
        assert backend == "postgres"
