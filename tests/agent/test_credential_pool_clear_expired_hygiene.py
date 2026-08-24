"""Hygiene: clearing an exhausted entry should drop the stale extra.failure_reason.

When a credential recovers (cooldown expiry), the classifier's verdict
(``failure_reason`` persisted into ``extra``) was left behind even though every
other error field was reset. Availability gating never reads it, so this is
cosmetic — but the stale flag confuses introspection/audit tooling that reads
auth.json directly.
"""

from __future__ import annotations

import json
import time


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_clear_expired_drops_stale_failure_reason(tmp_path, monkeypatch):
    """A recovered entry must not keep a stale failure_reason in extra."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    entry = {
        "id": "cred-1",
        "label": "cred-1",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "last_status": "exhausted",
        "last_status_at": time.time() - 3600,  # well past the 401 TTL
        "last_error_code": 401,
        "extra": {"failure_reason": "auth"},
    }
    _write_auth_store(
        tmp_path,
        {"version": 1, "credential_pool": {"openrouter": [entry]}},
    )
    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    selected = pool.select()
    assert selected is not None
    assert selected.last_status == "ok"
    assert selected.extra.get("failure_reason") is None
