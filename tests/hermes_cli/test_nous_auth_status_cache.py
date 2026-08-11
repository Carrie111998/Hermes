"""Tests for the get_nous_auth_status() process-level cache.

The cache avoids re-validating Nous credentials on every menu paint —
`hermes tools` → "All Platforms" used to fire ~31 OAuth refresh POSTs
against portal.nousresearch.com during one render. The cache is keyed
on local/global auth-store identity plus strict inheritance policy so
profile or policy transitions stay isolated while auth writes invalidate
naturally; tests and other writers can also call
invalidate_nous_auth_status_cache().
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch


def _seed_auth_file(tmp_path):
    """Drop a placeholder auth.json into the test HERMES_HOME.

    The exact content doesn't matter for cache-key purposes — only that
    the file exists and we can mutate it to bump mtime.
    """
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {}}), encoding="utf-8")
    return auth


def test_get_nous_auth_status_caches_consecutive_calls(tmp_path, monkeypatch):
    """A second call within the TTL skips re-computing the snapshot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_auth_file(tmp_path)

    from hermes_cli import auth as auth_mod

    auth_mod.invalidate_nous_auth_status_cache()

    call_count = {"n": 0}

    def fake_compute():
        call_count["n"] += 1
        return {"logged_in": False, "source": "auth_store", "call": call_count["n"]}

    with patch.object(auth_mod, "_compute_nous_auth_status", side_effect=fake_compute):
        first = auth_mod.get_nous_auth_status()
        second = auth_mod.get_nous_auth_status()
        third = auth_mod.get_nous_auth_status()

    assert call_count["n"] == 1, (
        f"_compute_nous_auth_status was called {call_count['n']}× — "
        "cache is not deduplicating within TTL."
    )
    # Each call returns a copy so callers can't mutate the cached dict.
    assert first == second == third
    first["mutated"] = True
    assert "mutated" not in auth_mod.get_nous_auth_status()

    auth_mod.invalidate_nous_auth_status_cache()


def test_get_nous_auth_status_caches_failure_path(tmp_path, monkeypatch):
    """Logged-out snapshots are cached too — that's where the cost was.

    Teknium's case: ~31 cache misses per `hermes tools` "All Platforms"
    menu paint, all returning logged_in=False after a failed refresh POST.
    The whole point of the cache is to memoise that failure path too.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_auth_file(tmp_path)

    from hermes_cli import auth as auth_mod

    auth_mod.invalidate_nous_auth_status_cache()

    call_count = {"n": 0}

    def fake_compute():
        call_count["n"] += 1
        return {"logged_in": False, "source": "auth_store", "error": "refresh failed"}

    with patch.object(auth_mod, "_compute_nous_auth_status", side_effect=fake_compute):
        for _ in range(10):
            auth_mod.get_nous_auth_status()

    assert call_count["n"] == 1, (
        f"Logged-out snapshots must cache; got {call_count['n']} computes for 10 calls."
    )

    auth_mod.invalidate_nous_auth_status_cache()


def test_get_nous_auth_status_invalidates_when_global_source_changes(
    tmp_path,
    monkeypatch,
):
    """An inherited source write invalidates even if local auth is unchanged."""
    local_home = tmp_path / "profile"
    local_home.mkdir()
    global_auth = tmp_path / "global-auth.json"
    global_auth.write_text('{"version": 1}\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(local_home))

    from hermes_cli import auth as auth_mod

    auth_mod.invalidate_nous_auth_status_cache()
    monkeypatch.setattr(auth_mod, "_global_auth_file_path", lambda: global_auth)
    calls = 0

    def fake_compute():
        nonlocal calls
        calls += 1
        return {"logged_in": True, "call": calls}

    with patch.object(auth_mod, "_compute_nous_auth_status", side_effect=fake_compute):
        assert auth_mod.get_nous_auth_status()["call"] == 1
        assert auth_mod.get_nous_auth_status()["call"] == 1
        global_auth.write_text('{"version": 2}\n', encoding="utf-8")
        assert auth_mod.get_nous_auth_status()["call"] == 2

    assert calls == 2
    auth_mod.invalidate_nous_auth_status_cache()
