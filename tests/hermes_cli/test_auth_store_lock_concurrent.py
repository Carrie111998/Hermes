"""Concurrency regression tests for the shared auth-store lock."""

from __future__ import annotations

import threading

from agent.credential_pool import AUTH_TYPE_OAUTH, PooledCredential
from hermes_cli.auth import _auth_store_lock, read_credential_pool, write_credential_pool


def test_concurrent_windows_lock_initialization_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    concurrency = 40
    barrier = threading.Barrier(concurrency)
    errors: list[BaseException] = []
    entered = 0
    entered_guard = threading.Lock()

    def acquire() -> None:
        nonlocal entered
        try:
            barrier.wait(timeout=10)
            with _auth_store_lock(timeout_seconds=10):
                with entered_guard:
                    entered += 1
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert entered == concurrency


def test_stale_pool_writer_cannot_restore_consumed_pkce_generation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def entry(access: str, refresh: str, *, status=None):
        return PooledCredential(
            provider="anthropic",
            id="canonical",
            label="Hermes PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source="hermes_pkce",
            access_token=access,
            refresh_token=refresh,
            expires_at_ms=2_000,
            last_status=status,
            last_status_at=50.0 if status else None,
        ).to_dict()

    write_credential_pool("anthropic", [entry("winner-access", "winner-refresh")])
    write_credential_pool(
        "anthropic", [entry("consumed-access", "consumed-refresh", status="exhausted")]
    )

    persisted = read_credential_pool("anthropic")[0]
    assert persisted["access_token"] == "winner-access"
    assert persisted["refresh_token"] == "winner-refresh"
    assert persisted.get("last_status") is None

    write_credential_pool(
        "anthropic",
        [entry("next-access", "next-refresh")],
        authoritative_ids=["canonical"],
    )
    persisted = read_credential_pool("anthropic")[0]
    assert persisted["refresh_token"] == "next-refresh"
