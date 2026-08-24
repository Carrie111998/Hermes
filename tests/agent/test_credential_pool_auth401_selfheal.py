"""Lone 401 `auth` failures must self-heal, never become permanent blocks.

Regression for issue #43747-class: providers whose router emits a *spurious*
401 (e.g. opencode.ai returning "Model is not supported" for a VALID key) were
being cached as a permanent `auth` poison. Two distinct failure shapes are
covered:

1. A fresh `_mark_exhausted` with a non-terminal `auth` 401 and no provider
   `reset_at` must always carry a bounded cooldown, so the entry re-enters
   rotation after the 401 TTL instead of being benched forever.
2. A persisted entry whose `last_status` was later cleared (by an unrelated
   write) but still carries `failure_reason == "auth"` in `extra` is a silent
   permanent block with no recovery path. Selection must heal it inline.
"""

from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _entry(*, last_status="exhausted", failure_reason="auth", extra=None, age_seconds=10):
    entry = {
        "id": "cred-1",
        "label": "cred-1",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "***",
        "base_url": "https://opencode.ai/zen/go/v1",
        "last_status": last_status,
        "last_status_at": time.time() - age_seconds,
        "last_error_code": 401,
        "failure_reason": failure_reason,
    }
    if extra is not None:
        entry["extra"] = extra
    return entry


def _load(tmp_path, monkeypatch, entries: list[dict], provider: str = "opencode-go"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_auth_store(tmp_path, {"version": 1, "credential_pool": {provider: entries}})
    from agent.credential_pool import load_pool

    return load_pool(provider)


def test_lone_401_auth_gets_bounded_cooldown(tmp_path, monkeypatch):
    """A non-terminal 401 `auth` mark must carry a finite reset_at."""
    from agent.credential_pool import (
        EXHAUSTED_TTL_401_SECONDS,
        load_pool,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {"version": 1, "credential_pool": {"opencode-go": [_entry()]}},
    )
    pool = load_pool("opencode-go")
    # Mark it exhausted the way the runtime does for a lone 401 with no reset_at.
    pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id="cred-1",
        error_context={"reason": "auth", "message": "Model is not supported"},
        failure_reason="auth",
    )
    marked = {e.id: e for e in pool.entries()}["cred-1"]
    assert marked.last_status == "exhausted"
    # The critical invariant: a bounded reset_at exists, so it is not permanent.
    assert marked.last_error_reset_at is not None
    horizon = time.time() + EXHAUSTED_TTL_401_SECONDS + 5
    assert marked.last_error_reset_at <= horizon


def test_stale_auth_poison_in_extra_self_heals_on_select(tmp_path, monkeypatch):
    """An entry with cleared last_status but lingering `failure_reason: auth`
    in extra must be selectable (healed), not silently blocked forever."""
    # The exact stuck shape seen in production: last_status=None + extra.auth.
    entry = _entry(last_status=None, extra={"failure_reason": "auth"})
    pool = _load(tmp_path, monkeypatch, [entry])
    selected = pool.select()
    assert selected is not None, "stale auth poison must not permanently block selection"
    assert selected.id == "cred-1"
    assert selected.extra.get("failure_reason") is None


def test_terminal_auth_still_goes_dead(tmp_path, monkeypatch):
    """A genuine terminal OAuth failure is unaffected: stays DEAD."""
    from agent.credential_pool import STATUS_DEAD, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {"version": 1, "credential_pool": {"openai-codex": [_entry()]}},
    )
    pool = load_pool("openai-codex")
    pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id="cred-1",
        error_context={"reason": "token_revoked", "message": "token revoked"},
        failure_reason="auth_permanent",
    )
    marked = {e.id: e for e in pool.entries()}["cred-1"]
    assert marked.last_status == STATUS_DEAD
