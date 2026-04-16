"""Tests for Phase 0 load_pool TTL caching.

The uncached implementation re-reads auth.json and re-imports Codex CLI
tokens on every call_llm, which produces the 1000+/session hot loop that
this cache fixes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch) -> Path:
    """Point all 'hermes home' resolvers at a fresh tmp dir.

    Matches what hermes_cli.config.get_hermes_home() returns when
    HERMES_HOME is set. We seed an empty auth.json so read_credential_pool
    has something to parse.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    auth_json = home / "auth.json"
    auth_json.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {"openai-codex": []},
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_load_pool_returns_same_object_within_ttl(hermes_home, monkeypatch):
    """Back-to-back load_pool calls within TTL must only hit disk once."""
    # Reload the credential_pool module so our cache starts empty
    import importlib
    from agent import credential_pool as cp_mod
    importlib.reload(cp_mod)

    call_count = {"n": 0}
    real_uncached = cp_mod._load_pool_uncached

    def counting(provider: str) -> cp_mod.CredentialPool:
        call_count["n"] += 1
        return real_uncached(provider)

    monkeypatch.setattr(cp_mod, "_load_pool_uncached", counting)

    cp_mod.load_pool("openai-codex")
    cp_mod.load_pool("openai-codex")
    cp_mod.load_pool("openai-codex")

    assert call_count["n"] == 1, (
        f"expected 1 underlying read, got {call_count['n']} — "
        "cache is not short-circuiting."
    )


def test_load_pool_re_reads_after_invalidate(hermes_home, monkeypatch):
    """Explicit invalidate_pool_cache() forces the next call to re-read."""
    import importlib
    from agent import credential_pool as cp_mod
    importlib.reload(cp_mod)

    call_count = {"n": 0}
    real_uncached = cp_mod._load_pool_uncached
    monkeypatch.setattr(
        cp_mod,
        "_load_pool_uncached",
        lambda provider: (call_count.__setitem__("n", call_count["n"] + 1) or real_uncached(provider)),
    )

    cp_mod.load_pool("openai-codex")
    cp_mod.load_pool("openai-codex")
    assert call_count["n"] == 1

    cp_mod.invalidate_pool_cache("openai-codex")
    cp_mod.load_pool("openai-codex")
    assert call_count["n"] == 2


def test_load_pool_re_reads_after_auth_json_mtime_change(hermes_home, monkeypatch):
    """An external write to auth.json invalidates the cache on next load_pool."""
    import importlib
    from agent import credential_pool as cp_mod
    importlib.reload(cp_mod)

    call_count = {"n": 0}
    real_uncached = cp_mod._load_pool_uncached
    monkeypatch.setattr(
        cp_mod,
        "_load_pool_uncached",
        lambda provider: (call_count.__setitem__("n", call_count["n"] + 1) or real_uncached(provider)),
    )

    cp_mod.load_pool("openai-codex")
    assert call_count["n"] == 1

    # Simulate another process rewriting auth.json (e.g. `hermes auth login`)
    auth_json = hermes_home / "auth.json"
    # Ensure mtime actually advances on Windows by sleeping past FS granularity
    time.sleep(0.05)
    auth_json.write_text(auth_json.read_text() + " ")

    cp_mod.load_pool("openai-codex")
    assert call_count["n"] == 2, (
        "cache did not notice auth.json was rewritten — mtime invalidation broken"
    )
