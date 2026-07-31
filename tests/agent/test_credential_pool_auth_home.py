"""Environment-credential projection under a shared HERMES_AUTH_HOME.

``env:*`` pool rows reference a variable in one session's ``.env``. When the
credential store is relocated to a residence shared by several runtime homes,
those rows are session-private: they are filtered out before pool objects are
constructed, never selected for another session, and never persisted back.
The owning session keeps its in-memory env credential, and a no-override home
keeps the historical persistence behavior.
"""

from __future__ import annotations

import json
from pathlib import Path


def _write_env(home: Path, content: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(content, encoding="utf-8")


def _activate_home(monkeypatch, home: Path) -> None:
    from hermes_cli.config import invalidate_env_cache

    monkeypatch.setenv("HERMES_HOME", str(home))
    invalidate_env_cache()


def _persisted_pool(auth_file: Path) -> dict:
    if not auth_file.exists():
        return {}
    store = json.loads(auth_file.read_text(encoding="utf-8"))
    pool = store.get("credential_pool")
    return pool if isinstance(pool, dict) else {}


def test_env_credentials_stay_session_private_in_a_shared_residence(
    monkeypatch, tmp_path
):
    from agent.credential_pool import load_pool

    residence = tmp_path / "residence"
    home_a = tmp_path / "runtime-a"
    home_b = tmp_path / "runtime-b"
    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))
    auth_file = residence / "auth.json"

    # Session A owns the key: it must see and keep its env credential…
    _write_env(home_a, "OPENROUTER_API_KEY=key-from-a\n")
    _activate_home(monkeypatch, home_a)
    pool_a = load_pool("openrouter")
    env_entries = [
        entry for entry in pool_a.entries() if entry.source == "env:OPENROUTER_API_KEY"
    ]
    assert len(env_entries) == 1
    assert env_entries[0].access_token == "key-from-a"
    # …but the shared store must never learn it.
    assert "env:" not in json.dumps(_persisted_pool(auth_file))

    # Session B has no such variable: nothing to inherit, nothing to select.
    _write_env(home_b, "")
    _activate_home(monkeypatch, home_b)
    pool_b = load_pool("openrouter")
    assert pool_b.entries() == []
    assert pool_b.current() is None


def test_foreign_persisted_env_rows_are_scrubbed_once_then_stable(
    monkeypatch, tmp_path
):
    """A legacy env row on disk is filtered before construction and removed.

    The removal happens exactly once — after the store is clean, repeated
    loads must not rewrite bytes, mtime, or ``updated_at``.
    """
    from agent.credential_pool import load_pool
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    residence = tmp_path / "residence"
    home_b = tmp_path / "runtime-b"
    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))
    _write_env(home_b, "")
    _activate_home(monkeypatch, home_b)
    auth_file = residence / "auth.json"

    with _auth_store_lock():
        store = _load_auth_store()
        store.setdefault("credential_pool", {})["openrouter"] = [
            {
                "id": "foreign-env",
                "source": "env:OPENROUTER_API_KEY",
                "auth_type": "api_key",
                "label": "OPENROUTER_API_KEY",
                "access_token": "someone-elses-secret",
                "priority": 0,
            },
            {
                "id": "manual-1",
                "source": "manual",
                "auth_type": "api_key",
                "label": "manual key",
                "access_token": "sk-or-manual",
                "priority": 1,
            },
        ]
        _save_auth_store(store)

    pool = load_pool("openrouter")
    assert [entry.id for entry in pool.entries()] == ["manual-1"]
    persisted = _persisted_pool(auth_file)["openrouter"]
    assert [entry["id"] for entry in persisted] == ["manual-1"]

    before_bytes = auth_file.read_bytes()
    before_mtime = auth_file.stat().st_mtime_ns
    reloaded = load_pool("openrouter")
    assert [entry.id for entry in reloaded.entries()] == ["manual-1"]
    assert auth_file.read_bytes() == before_bytes
    assert auth_file.stat().st_mtime_ns == before_mtime


def test_cooldown_persistence_never_republishes_env_rows(monkeypatch, tmp_path):
    """Real pool mutations keep env rows out of the shared store.

    Exhaustion marking goes through ``_persist`` — not through a direct
    ``write_credential_pool`` call — so this proves the disk boundary holds
    on the live rotation path. The owning session keeps the cooldown on its
    in-memory env entry.
    """
    from agent.credential_pool import STATUS_EXHAUSTED, load_pool
    from hermes_cli.auth import write_credential_pool

    residence = tmp_path / "residence"
    home = tmp_path / "runtime-a"
    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))
    _write_env(home, "OPENROUTER_API_KEY=key-from-a\n")
    _activate_home(monkeypatch, home)
    auth_file = residence / "auth.json"

    write_credential_pool(
        "openrouter",
        [
            {
                "id": "manual-1",
                "source": "manual",
                "auth_type": "api_key",
                "label": "manual key",
                "access_token": "sk-or-manual",
                "priority": 0,
            }
        ],
    )

    pool = load_pool("openrouter")
    assert {entry.source for entry in pool.entries()} == {
        "manual",
        "env:OPENROUTER_API_KEY",
    }

    # A dirty persist: the manual entry's cooldown must land on disk while
    # the env row stays memory-only.
    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"reason": "rate_limit_exceeded"},
        api_key_hint="sk-or-manual",
    )
    persisted = _persisted_pool(auth_file)["openrouter"]
    assert [entry["id"] for entry in persisted] == ["manual-1"]
    assert persisted[0]["last_status"] == STATUS_EXHAUSTED
    assert "env:" not in json.dumps(persisted)

    # Exhausting the env entry itself cools it down in memory only.
    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"reason": "rate_limit_exceeded"},
        api_key_hint="key-from-a",
    )
    env_entry = next(
        entry for entry in pool.entries()
        if entry.source == "env:OPENROUTER_API_KEY"
    )
    assert env_entry.last_status == STATUS_EXHAUSTED
    persisted = _persisted_pool(auth_file)["openrouter"]
    assert "env:" not in json.dumps(persisted)


def test_write_credential_pool_filters_env_rows_at_the_disk_boundary(
    monkeypatch, tmp_path
):
    """Both caller entries and disk-merge entries are filtered."""
    from hermes_cli.auth import (
        _auth_store_lock,
        _load_auth_store,
        _save_auth_store,
        write_credential_pool,
    )

    residence = tmp_path / "residence"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))
    auth_file = residence / "auth.json"

    with _auth_store_lock():
        store = _load_auth_store()
        store.setdefault("credential_pool", {})["openrouter"] = [
            {
                "id": "disk-env",
                "source": "env:OPENROUTER_API_KEY",
                "auth_type": "api_key",
                "label": "OPENROUTER_API_KEY",
                "access_token": "disk-secret",
                "priority": 0,
            },
        ]
        _save_auth_store(store)

    entries = [
        {
            "id": "manual-1",
            "source": "manual",
            "auth_type": "api_key",
            "label": "manual key",
            "access_token": "sk-or-manual",
            "priority": 0,
        },
        {
            "id": "session-env",
            "source": "env:OPENROUTER_API_KEY",
            "auth_type": "api_key",
            "label": "OPENROUTER_API_KEY",
            "access_token": "session-secret",
            "priority": 1,
        },
    ]
    path = write_credential_pool("openrouter", [dict(e) for e in entries])
    assert path == residence.resolve() / "auth.json"
    persisted = _persisted_pool(auth_file)["openrouter"]
    assert [entry["id"] for entry in persisted] == ["manual-1"]
    assert "env:" not in json.dumps(persisted)

    # An unchanged persistable projection is a byte/mtime/updated_at no-op.
    before_bytes = auth_file.read_bytes()
    before_mtime = auth_file.stat().st_mtime_ns
    before_updated_at = json.loads(before_bytes)["updated_at"]
    write_credential_pool("openrouter", [dict(e) for e in entries])
    assert auth_file.read_bytes() == before_bytes
    assert auth_file.stat().st_mtime_ns == before_mtime
    assert json.loads(auth_file.read_bytes())["updated_at"] == before_updated_at


def test_no_override_home_keeps_persisting_env_rows(monkeypatch, tmp_path):
    """Without a distinct residence the store and .env share one session."""
    from agent.credential_pool import load_pool

    home = tmp_path / "runtime"
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)
    _write_env(home, "OPENROUTER_API_KEY=key-local\n")
    _activate_home(monkeypatch, home)

    pool = load_pool("openrouter")
    sources = [entry.source for entry in pool.entries()]
    assert "env:OPENROUTER_API_KEY" in sources
    persisted = _persisted_pool(home / "auth.json")["openrouter"]
    assert any(
        entry.get("source") == "env:OPENROUTER_API_KEY" for entry in persisted
    )


def test_path_equal_override_keeps_persisting_env_rows(monkeypatch, tmp_path):
    """A path-equal override is a total no-op, not a session split."""
    from agent.credential_pool import load_pool

    home = tmp_path / "runtime"
    _write_env(home, "OPENROUTER_API_KEY=key-local\n")
    _activate_home(monkeypatch, home)
    monkeypatch.setenv("HERMES_AUTH_HOME", str(home))

    pool = load_pool("openrouter")
    assert any(
        entry.source == "env:OPENROUTER_API_KEY" for entry in pool.entries()
    )
    persisted = _persisted_pool(home / "auth.json")["openrouter"]
    assert any(
        entry.get("source") == "env:OPENROUTER_API_KEY" for entry in persisted
    )
