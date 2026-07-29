"""Regression: `get_auth_status` must report `logged_in=True` for a
custom_providers entry whose `api_key` field resolves to a non-empty env var.

Before the fix the dispatcher fell through to a default branch that
returned ``{"logged_in": False}`` for any provider id not in
``PROVIDER_REGISTRY`` — which silently swallows every `custom:<name>`
provider and produces confusing "logged out" output even when the
configured API key resolves to a working credential (regression seen
with `custom:Moonshot Kimi (international)` while `MOONSHOT_API_KEY`
was exported and `hermes chat -m kimi-k2.6` succeeded).
"""

from __future__ import annotations

import json

import pytest
import yaml


def _write(hermes_home, payload: dict) -> None:
    (hermes_home / "auth.json").write_text(json.dumps(payload))


def test_custom_pool_reports_logged_in_when_api_key_env_resolves(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-kimi")

    (hermes_home / "config.yaml").write_text(yaml.dump({
        "model": {},
        "custom_providers": [
            {
                "name": "Moonshot Kimi (international)",
                "base_url": "https://api.moonshot.ai/v1",
                "api_key": "${MOONSHOT_API_KEY}",
            },
        ],
    }))
    _write(hermes_home, {"version": 1, "providers": {}})

    from hermes_cli.auth import get_auth_status

    status = get_auth_status("custom:Moonshot Kimi (international)")
    assert status.get("logged_in") is True, status
    assert status.get("configured") is True, status
    # ``provider`` echoes the input the caller actually passed (raw or
    # normalized) so the CLI can render the user's exact argument back in
    # any error message. We don't pin a specific casing here because the
    # real ``hermes auth status`` CLI invokes ``_normalize_provider``
    # first, which lowercases and replaces spaces with dashes; tests that
    # exercise that path explicitly below.
    assert "moonshot" in status.get("provider", "").lower()
    # Status snapshot should advertise the resolved base_url so the CLI
    # can confirm the endpoint without a separate `hermes config get`.
    assert "moonshot.ai" in status.get("base_url", "")
    # Same lookup must also work after ``_normalize_provider`` (the path
    # the real ``hermes auth status`` CLI takes) has lowercased and
    # replaced spaces with dashes — proves the double-form lookup is
    # symmetric.
    from hermes_cli.auth_commands import _normalize_provider as _norm
    normalized = _norm("Moonshot Kimi (international)")
    status_norm = get_auth_status(normalized)
    assert status_norm.get("logged_in") is True, status_norm
    assert status_norm.get("configured") is True, status_norm


def test_custom_pool_reports_logged_out_when_api_key_env_unset(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)

    (hermes_home / "config.yaml").write_text(yaml.dump({
        "model": {},
        "custom_providers": [
            {
                "name": "Moonshot Kimi (international)",
                "base_url": "https://api.moonshot.ai/v1",
                "api_key": "${MOONSHOT_API_KEY}",
            },
        ],
    }))
    _write(hermes_home, {"version": 1, "providers": {}})

    # Both module-level caches can leak state across tests: ``_LOAD_CONFIG_CACHE``
    # holds the expanded config (so a freshly-written config.yaml may be
    # ignored), and ``_env_cache`` holds a snapshot of the .env file contents
    # keyed on (path, mtime, size) — when no .env exists, the cache_key is a
    # constant None-tuple and pytest's monkeypatch.delenv() won't trigger a
    # reload. Clear both before the assertion.
    import hermes_cli.config as _cfg
    _cfg._LOAD_CONFIG_CACHE.clear()
    _cfg.invalidate_env_cache()

    from hermes_cli.auth import get_auth_status

    status = get_auth_status("custom:Moonshot Kimi (international)")
    # No key resolved → not configured, must NOT silently report logged_in.
    assert status.get("logged_in") is False
    assert status.get("configured") is False


def test_unknown_provider_still_returns_logged_out(tmp_path, monkeypatch):
    """Belt-and-braces: providers with no config and no registry entry
    must keep returning ``logged_in: False`` so the CLI surface stays
    honest."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    (hermes_home / "config.yaml").write_text(yaml.dump({"model": {}}))
    _write(hermes_home, {"version": 1, "providers": {}})

    from hermes_cli.auth import get_auth_status

    status = get_auth_status("totally-not-a-real-provider")
    assert status.get("logged_in") is False