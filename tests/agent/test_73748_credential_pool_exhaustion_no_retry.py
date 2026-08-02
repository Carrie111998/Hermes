"""Reproduction and fix verification tests for issue #73748.

Bug 1: Gateway orchestration turn aborted by a transient 429 is not retried.
  When all credentials in the pool are exhausted by rate-limiting and no
  fallback provider exists, the conversation loop exits with an error
  message — but the message is marked as "processed" and never retried.
  FIX: ``recover_with_credential_pool`` now calls ``pool.reload_from_disk()``
  before giving up, so an out-of-process ``hermes auth reset`` is honoured.

Bug 2: ``hermes auth reset`` clears disk state but a running gateway's
  in-memory ``CredentialPool`` still holds the old exhaustion flags.
  A second turn against the same agent object sees the stale in-memory
  entries and skips them, even though the on-disk pool was reset.
  FIX: ``CredentialPool.reload_from_disk()`` re-reads status fields from
  disk and updates in-memory entries, so the gateway picks up the reset.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 1 (pre-fix): recover_with_credential_pool returns (False, True)
#    when pool is fully exhausted and disk is also exhausted ────────────

class TestBug1CredentialPoolExhaustionNoRetry:
    """When ALL pool entries are exhausted by 429 and no fallback exists,
    and the disk also shows exhausted entries, recovery still fails."""

    def test_all_credentials_exhausted_returns_no_recovery(self, tmp_path, monkeypatch):
        """When every entry in the pool is already marked exhausted,
        ``mark_exhausted_and_rotate`` returns None, and
        ``recover_with_credential_pool`` returns (False, True)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        # Write a pool with 2 credentials, both already exhausted
        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)
        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                        "last_status": STATUS_EXHAUSTED,
                        "last_status_at": time.time(),
                        "last_error_code": 429,
                    },
                    {
                        "id": "cred-b",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-key-b",
                        "last_status": STATUS_EXHAUSTED,
                        "last_status_at": time.time(),
                        "last_error_code": 429,
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        pool = load_pool("openai")

        # Both entries are exhausted — mark_exhausted_and_rotate should return None
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-key-a"
        )
        assert next_entry is None, (
            "When all entries are exhausted, mark_exhausted_and_rotate "
            "should return None (no available credential to rotate to)"
        )

        # Now test recover_with_credential_pool with a mock agent
        from agent.agent_runtime_helpers import recover_with_credential_pool

        agent = MagicMock()
        agent._credential_pool = pool
        agent.provider = "openai"
        agent.api_key = "sk-key-a"
        agent._credential_pool_entry_id = "cred-a"

        recovered, has_retried_429 = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=True,  # Already retried once
        )

        # When disk is also exhausted, recovery still fails
        assert recovered is False, (
            "When all credentials are exhausted on both disk and memory, "
            "recover_with_credential_pool cannot recover."
        )
        assert has_retried_429 is True

    def test_single_credential_pool_exhaustion_no_recovery(self, tmp_path, monkeypatch):
        """A single-credential pool that hits 429 also cannot recover
        when disk is also exhausted."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)
        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-only",
                        "label": "only-key",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-only",
                        "last_status": STATUS_EXHAUSTED,
                        "last_status_at": time.time(),
                        "last_error_code": 429,
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        pool = load_pool("openai")

        from agent.agent_runtime_helpers import recover_with_credential_pool

        agent = MagicMock()
        agent._credential_pool = pool
        agent.provider = "openai"
        agent.api_key = "sk-key-only"
        agent._credential_pool_entry_id = "cred-only"

        recovered, _ = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=True,
        )

        assert recovered is False, (
            "Single-credential case: pool cannot recover when disk is "
            "also exhausted."
        )


# ── Bug 1 (post-fix): recovery succeeds after auth reset clears disk ──

class TestBug1FixRecoveryAfterAuthReset:
    """After ``hermes auth reset`` clears exhaustion flags on disk,
    ``recover_with_credential_pool`` now reloads from disk and recovers."""

    def test_recovery_after_auth_reset_clears_disk(self, tmp_path, monkeypatch):
        """All entries exhausted in-memory, but auth reset clears disk.
        reload_from_disk + recover_with_credential_pool should now recover."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )
        from agent.agent_runtime_helpers import recover_with_credential_pool

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        # Write initial pool with 2 healthy credentials
        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                    },
                    {
                        "id": "cred-b",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-key-b",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        pool = load_pool("openai")
        entry = pool.select()
        assert entry is not None

        # Exhaust all entries in-memory (and on-disk via _persist)
        pool.mark_exhausted_and_rotate(status_code=429, api_key_hint="sk-key-a")
        pool.mark_exhausted_and_rotate(status_code=429, api_key_hint="sk-key-b")

        # Simulate auth reset: load fresh, reset, persist
        reset_pool = load_pool("openai")
        count = reset_pool.reset_statuses()
        assert count > 0, "reset_statuses should clear exhausted entries on disk"

        # Now test recover_with_credential_pool — it should reload from disk
        # and find the reset entries.
        agent = MagicMock()
        agent._credential_pool = pool
        agent.provider = "openai"
        agent.api_key = "sk-key-a"
        agent._credential_pool_entry_id = "cred-a"

        recovered, has_retried_429 = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=True,
        )

        # FIX VERIFIED: recovery succeeds after auth reset cleared disk.
        # With the reload-before-rotation fix, recover_with_credential_pool
        # reloads from disk, finds the current entry is no longer exhausted,
        # and returns True without needing to swap to a different entry.
        assert recovered is True, (
            "Fix verified: recover_with_credential_pool now reloads from "
            "disk and recovers after an out-of-process auth reset."
        )
        # The current entry's exhausted status was cleared by the reload,
        # so no _swap_credential is needed — the same credential is retried.
        assert has_retried_429 is False, (
            "After reload clears the exhausted flag, the 429 retry gate "
            "should be reset so the caller retries the same credential."
        )


# ── Bug 2 (pre-fix): auth reset does not affect in-memory pool ────────

class TestBug2AuthResetDoesNotRefreshInMemoryPool:
    """Without calling reload_from_disk, a separate CredentialPool
    instance's reset_statuses does not affect the gateway's in-memory copy."""

    def test_reset_statuses_does_not_affect_in_memory_copy(self, tmp_path, monkeypatch):
        """Two separate CredentialPool instances don't share state."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                    },
                    {
                        "id": "cred-b",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-key-b",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # Gateway process loads pool into memory
        gateway_pool = load_pool("openai")
        entry = gateway_pool.select()
        assert entry is not None, "Fresh pool should have available entries"

        # Gateway exhausts both entries in-memory
        gateway_pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-key-a"
        )
        gateway_pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-key-b"
        )

        # Verify: in-memory pool has no available entries
        assert gateway_pool.select() is None, (
            "After marking both entries exhausted, the in-memory pool "
            "should have no available entries."
        )

        # Verify: in-memory entries have exhausted status
        for e in gateway_pool.entries():
            assert e.last_status == STATUS_EXHAUSTED, (
                f"Entry {e.id} should be exhausted in memory"
            )

        # Simulate ``hermes auth reset`` from another process
        reset_pool = load_pool("openai")
        count = reset_pool.reset_statuses()
        assert count > 0

        # Without reload_from_disk, the gateway's in-memory pool is still exhausted
        stale_entry = gateway_pool.select()
        assert stale_entry is None, (
            "Without reload_from_disk, the gateway's in-memory pool "
            "remains stale after auth reset."
        )

    def test_reset_statuses_clears_disk_but_not_agent_memory(self, tmp_path, monkeypatch):
        """Directly call reset_statuses on a separate pool instance and
        confirm the original in-memory pool is unaffected."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        pool_data = {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-x",
                        "label": "main",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-ant-key",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # Gateway loads pool
        gateway_pool = load_pool("anthropic")

        # Gateway exhausts the entry in-memory
        gateway_pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-ant-key"
        )
        assert gateway_pool.select() is None

        # Another process (auth reset) loads a separate instance and resets
        reset_pool = load_pool("anthropic")
        count = reset_pool.reset_statuses()
        assert count > 0

        # Without reload_from_disk, gateway's in-memory pool is unaffected
        assert gateway_pool.select() is None, (
            "Without reload_from_disk, a separate CredentialPool instance's "
            "reset_statuses does not affect the gateway's in-memory copy."
        )


# ── Bug 2 (post-fix): reload_from_disk refreshes in-memory pool ──────

class TestBug2FixReloadFromDisk:
    """``CredentialPool.reload_from_disk()`` refreshes in-memory status
    fields from the on-disk store, fixing the stale-state problem."""

    def test_reload_from_disk_picks_up_auth_reset(self, tmp_path, monkeypatch):
        """After auth reset clears disk state, reload_from_disk on the
        gateway's in-memory pool should refresh the entries."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                    },
                    {
                        "id": "cred-b",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-key-b",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # Gateway loads pool
        gateway_pool = load_pool("openai")
        gateway_pool.select()
        gateway_pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-key-a"
        )
        gateway_pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-key-b"
        )
        assert gateway_pool.select() is None, "All entries should be exhausted"

        # Auth reset clears disk
        reset_pool = load_pool("openai")
        reset_pool.reset_statuses()

        # FIX: reload_from_disk refreshes the gateway's in-memory pool
        refreshed = gateway_pool.reload_from_disk()
        assert refreshed > 0, "reload_from_disk should report refreshed entries"

        # After reload, the gateway's pool should have available entries
        recovered_entry = gateway_pool.select()
        assert recovered_entry is not None, (
            "Fix verified: after reload_from_disk, the gateway's in-memory "
            "pool picks up the auth reset and has available entries."
        )
        assert recovered_entry.last_status is None, (
            "Reloaded entry should have no exhausted status"
        )

    def test_reload_from_disk_adds_new_entry(self, tmp_path, monkeypatch):
        """If a new credential was added to disk, reload_from_disk
        should append it to the in-memory entries."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import load_pool

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        # Start with 1 credential
        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        gateway_pool = load_pool("openai")
        assert len(gateway_pool.entries()) == 1

        # Another process adds a new credential to disk
        pool_data["credential_pool"]["openai"].append({
            "id": "cred-b",
            "label": "secondary",
            "auth_type": "api_key",
            "priority": 1,
            "source": "manual",
            "access_token": "sk-key-b",
        })
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # FIX: reload_from_disk picks up the new entry
        refreshed = gateway_pool.reload_from_disk()
        assert refreshed > 0, "reload_from_disk should report the new entry"
        assert len(gateway_pool.entries()) == 2, (
            "Fix verified: reload_from_disk adds new entries from disk."
        )

    def test_reload_from_disk_noop_when_disk_unchanged(self, tmp_path, monkeypatch):
        """When disk state matches memory, reload_from_disk returns 0."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import load_pool

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        pool = load_pool("openai")
        refreshed = pool.reload_from_disk()
        assert refreshed == 0, (
            "When disk matches memory, reload_from_disk should return 0."
        )

    def test_reload_from_disk_single_credential_after_reset(self, tmp_path, monkeypatch):
        """Single-credential pool exhausted, auth reset, reload recovers."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)

        pool_data = {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-x",
                        "label": "main",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-ant-key",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        pool = load_pool("anthropic")
        pool.select()
        pool.mark_exhausted_and_rotate(
            status_code=429, api_key_hint="sk-ant-key"
        )
        assert pool.select() is None

        # Auth reset
        reset_pool = load_pool("anthropic")
        reset_pool.reset_statuses()

        # Reload
        refreshed = pool.reload_from_disk()
        assert refreshed > 0

        recovered = pool.select()
        assert recovered is not None, (
            "Fix verified: single-credential pool recovers after "
            "reload_from_disk following auth reset."
        )


# ── Concurrent 429 interleaving: reset_statuses must be atomic ────────

class TestResetStatusesAtomicity:
    """Verify that ``reset_statuses`` performs an atomic read-clear-write
    so a concurrent 429 written by a gateway between ``load_pool`` and
    ``reset_statuses`` is correctly cleared (not silently dropped nor
    resurrected by a stale merge)."""

    def test_reset_uses_latest_disk_state_not_stale_snapshot(self, tmp_path, monkeypatch):
        """If a gateway writes a new 429 to disk AFTER ``load_pool`` but
        BEFORE ``reset_statuses``, the reset must clear that 429 (since it
        reads the latest disk state atomically) — it must NOT resurrect a
        stale pre-reset 429 via the merge path."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )
        from hermes_cli.auth import write_credential_pool

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)
        ts_1 = time.time() - 10  # old timestamp

        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                        "last_status": STATUS_EXHAUSTED,
                        "last_status_at": ts_1,
                        "last_error_code": 429,
                        "last_error_reason": "rate_limit",
                        "last_error_message": "Too many requests",
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # Step 1: load_pool reads disk state (entry has old 429)
        pool = load_pool("openai")

        # Step 2: a concurrent gateway writes a NEW 429 to disk
        ts_2 = time.time()  # newer timestamp
        write_credential_pool(
            "openai",
            [
                {
                    "id": "cred-a",
                    "label": "primary",
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": "manual",
                    "access_token": "sk-key-a",
                    "last_status": STATUS_EXHAUSTED,
                    "last_status_at": ts_2,
                    "last_error_code": 429,
                    "last_error_reason": "rate_limit",
                    "last_error_message": "New concurrent 429",
                },
            ],
        )

        # Step 3: reset_statuses — must use latest disk state (ts_2), not
        # the stale snapshot (ts_1), and clear ALL status fields atomically.
        count = pool.reset_statuses()
        assert count >= 1

        # Verify disk: all status fields must be cleared
        import json as _json
        disk = _json.loads((auth_store / "auth.json").read_text())
        disk_entry = disk["credential_pool"]["openai"][0]
        assert disk_entry.get("last_status") is None, (
            "reset_statuses must clear the LATEST disk state, not the stale snapshot"
        )
        assert disk_entry.get("last_status_at") is None
        assert disk_entry.get("last_error_code") is None

        # Verify in-memory: pool also has cleared state
        entry = pool._entries[0]
        assert entry.last_status is None
        assert entry.last_status_at is None

    def test_reset_does_not_drop_entries_added_concurrently(self, tmp_path, monkeypatch):
        """If a new credential is added to disk between load_pool and
        reset_statuses, the reset must not drop that new entry."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

        from agent.credential_pool import (
            STATUS_EXHAUSTED,
            load_pool,
        )
        from hermes_cli.auth import write_credential_pool

        auth_store = tmp_path / "hermes"
        auth_store.mkdir(parents=True, exist_ok=True)
        ts = time.time() - 10

        pool_data = {
            "version": 1,
            "credential_pool": {
                "openai": [
                    {
                        "id": "cred-a",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-key-a",
                        "last_status": STATUS_EXHAUSTED,
                        "last_status_at": ts,
                        "last_error_code": 429,
                    },
                ]
            },
        }
        (auth_store / "auth.json").write_text(json.dumps(pool_data, indent=2))

        # Step 1: load_pool reads disk state (1 entry)
        pool = load_pool("openai")

        # Step 2: a concurrent process adds a NEW credential to disk
        write_credential_pool(
            "openai",
            [
                {
                    "id": "cred-a",
                    "label": "primary",
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": "manual",
                    "access_token": "sk-key-a",
                    "last_status": STATUS_EXHAUSTED,
                    "last_status_at": ts,
                    "last_error_code": 429,
                },
                {
                    "id": "cred-b",
                    "label": "secondary",
                    "auth_type": "api_key",
                    "priority": 1,
                    "source": "manual",
                    "access_token": "sk-key-b",
                },
            ],
        )

        # Step 3: reset_statuses — must not drop cred-b
        count = pool.reset_statuses()
        assert count >= 1

        # Verify disk: both entries must exist, both cleared
        import json as _json
        disk = _json.loads((auth_store / "auth.json").read_text())
        disk_entries = disk["credential_pool"]["openai"]
        disk_ids = {e["id"] for e in disk_entries}
        assert "cred-a" in disk_ids, "Original entry must not be dropped"
        assert "cred-b" in disk_ids, "Concurrently added entry must not be dropped"
        for e in disk_entries:
            assert e.get("last_status") is None
            assert e.get("last_error_code") is None
