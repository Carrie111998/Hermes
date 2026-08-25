"""Regression tests for #94933 — Hindsight daemon fails to start under multiplexer.

Class-closure follow-up to the profile secret-scope cluster (#76462, Slack
pattern #59739, WhatsApp ``_get_wsecret``). The Hindsight memory daemon runs
on a background thread that does NOT inherit the caller's per-turn secret
scope, so under ``gateway.multiplex_profiles`` any bare ``get_secret(...)``
read inside ``initialize()`` / ``_start_daemon()`` / ``_get_client()`` /
``_build_embedded_profile_env()`` raises ``UnscopedSecretError`` and the
embedded daemon never starts. Memory silently stops working for ALL profiles.

The migrated module now carries a module-level ``_get_scoped_secret`` helper
mirroring ``gateway/platforms/whatsapp_common.py::_get_wsecret``:

- scope installed → scope is authoritative; scoped miss returns the
  default (NO borrow from ``os.environ``)
- unscoped under multiplex (the default profile's own startup loop) →
  fall back to ``os.getenv`` without raising ``UnscopedSecretError``

And the daemon-start thread wraps its whole flow in ``set_secret_scope`` so
thread-local reads of ``HINDSIGHT_API_KEY`` / ``HINDSIGHT_LLM_API_KEY``
inside ``_get_client`` and ``_build_embedded_profile_env`` honor the same
scope the caller installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent import secret_scope as ss
from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _build_embedded_profile_env,
    _get_scoped_secret,
    _load_config,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _make_provider(tmp_path, monkeypatch, *, mode: str, config_overrides=None, config_has_key: bool = True):
    """Build an initialized HindsightMemoryProvider pointed at tmp_path.

    ``config_has_key=False`` removes the config-file apiKey so the
    provider falls through to the env var / secret scope. Mirrors the
    real-world #94933 reproduction (user sets ``HINDSIGHT_LLM_API_KEY``
    in their profile ``.env`` rather than embedding it in
    ``hindsight/config.json``).
    """
    config = {
        "mode": mode,
        "api_url": "http://localhost:9999",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
        "profile": "test-profile",
    }
    if config_has_key:
        config["apiKey"] = "test-api-key"
    if config_overrides:
        config.update(config_overrides)
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(
        "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
    )
    provider = HindsightMemoryProvider()
    provider.initialize(session_id="test-session", platform="cli")
    return provider


@pytest.fixture(autouse=True)
def _reset_multiplex():
    """Multiplex mode off before and after every test."""
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """No stale env vars, isolated home — same as the broader provider tests."""
    for key in (
        "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL", "HINDSIGHT_BANK_ID",
        "HINDSIGHT_BUDGET", "HINDSIGHT_MODE", "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_IDLE_TIMEOUT", "HINDSIGHT_LLM_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    isolated_home = tmp_path / "user-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))


# ── helper existence / shape ───────────────────────────────────────────────


class TestScopedSecretHelper:
    """The module MUST expose ``_get_scoped_secret`` so plugin code never
    hits a bare ``get_secret`` that fails closed under multiplexing."""

    def test_helper_exists(self):
        assert callable(_get_scoped_secret), (
            "plugins/memory/hindsight must define the module-level "
            "_get_scoped_secret helper (WhatsApp _get_wsecret pattern)."
        )

    def test_scoped_value_wins_over_environ(self, monkeypatch):
        """Secondary profile: scope is authoritative, scope value used."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"HINDSIGHT_API_KEY": "secondary-profile-key"})
        try:
            assert _get_scoped_secret("HINDSIGHT_API_KEY") == "secondary-profile-key"
        finally:
            ss.reset_secret_scope(tok)

    def test_scoped_miss_returns_default_not_environ(self, monkeypatch):
        """Secondary profile with key absent from scope: default wins, NOT
        a borrow from ``os.environ`` (that would leak another profile's key)."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"UNRELATED": "x"})
        try:
            assert _get_scoped_secret("HINDSIGHT_API_KEY") is None
            assert _get_scoped_secret("HINDSIGHT_API_KEY", "") == ""
            assert _get_scoped_secret("HINDSIGHT_API_KEY", "fallback") == "fallback"
        finally:
            ss.reset_secret_scope(tok)

    def test_unscoped_under_multiplex_falls_back_to_environ(self, monkeypatch):
        """The DEFAULT profile constructs its adapter unscoped under multiplexing.
        A bare ``get_secret`` would raise ``UnscopedSecretError`` and crash
        startup; the helper must fall back to ``os.environ`` (that profile's
        own value) instead."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "default-profile-own-key")
        ss.set_multiplex_active(True)
        assert ss.current_secret_scope() is None
        assert _get_scoped_secret("HINDSIGHT_API_KEY") == "default-profile-own-key"

        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
        assert _get_scoped_secret("HINDSIGHT_API_KEY", "fallback") == "fallback"

    def test_single_profile_legacy_environ_read(self, monkeypatch):
        """Multiplex off, no scope: legacy ``os.environ`` read keeps working
        (no behavior change for non-multiplex deployments)."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "legacy-env-value")
        assert _get_scoped_secret("HINDSIGHT_API_KEY") == "legacy-env-value"
        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
        assert _get_scoped_secret("HINDSIGHT_API_KEY", "d") == "d"


# ── initialize() is scope-safe under multiplex ────────────────────────────


class TestInitializeScopeSafe:
    """``initialize()`` reads ``HINDSIGHT_API_KEY`` from the secret scope.
    Under multiplex it must NOT raise ``UnscopedSecretError`` (the default
    profile starts unscoped, but with multiplex active, the helper falls
    back to ``os.environ``). For a secondary profile the caller has already
    installed a scope, so the scope value must be honored."""

    def test_default_profile_unscoped_under_multiplex_uses_own_env(self, monkeypatch, tmp_path):
        """Reproduces the issue's #94933 default-profile branch:
        ``initialize()`` runs unscoped (gateway's default-profile startup
        loop), multiplex is on. Before the fix this would raise
        ``UnscopedSecretError`` and the daemon never started."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        # No apiKey in config — falls through to env/scope (matches real repro).
        provider = _make_provider(tmp_path, monkeypatch, mode="cloud", config_has_key=False)
        # initialize() must have populated the api key without raising
        assert provider._api_key == "default-profile-key"

    def test_secondary_profile_scoped_value_wins(self, monkeypatch, tmp_path):
        """Secondary profile: its scope value wins over ``os.environ`` —
        no cross-profile leak."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"HINDSIGHT_API_KEY": "secondary-profile-key"})
        try:
            provider = _make_provider(tmp_path, monkeypatch, mode="cloud", config_has_key=False)
            assert provider._api_key == "secondary-profile-key"
        finally:
            ss.reset_secret_scope(tok)


# ── daemon-start thread: scope wrapping ───────────────────────────────────


class TestDaemonStartThreadScope:
    """The daemon-start thread (``hindsight-daemon-start``) calls
    ``self._get_client()`` and ``_build_embedded_profile_env()`` —
    both read ``HINDSIGHT_LLM_API_KEY`` from the secret scope. Without
    a scope installed for the thread, the daemon fails to start under
    multiplex and memory silently stops working for ALL profiles."""

    def test_daemon_start_thread_uses_scope_not_environ(self, monkeypatch, tmp_path):
        """End-to-end repro from the issue: install a profile scope, then
        ``initialize()`` (which spawns the daemon-start thread). The thread's
        call into ``_get_client()`` and ``_build_embedded_profile_env()``
        must resolve ``HINDSIGHT_LLM_API_KEY`` from the scope built from
        the merged .env, not from ``os.environ`` of some other profile.

        Before the fix this raised ``UnscopedSecretError`` and the daemon
        never bound its port (#94933). After the fix the thread installs
        its own scope (from the merged main + profile daemon .env) so
        the helper sees a scope and resolves the LLM key to the active
        profile's value.
        """
        import plugins.memory.hindsight as hs

        ss.set_multiplex_active(True)

        # Seed the main HERMES_HOME/.env with the LLM key — this is where
        # ``_build_hindsight_scope`` reads the profile's LLM key from.
        # Note the profile daemon env at ~/.hindsight/profiles/<name>.env
        # does NOT carry the LLM key (only provider/model/idle-timeout),
        # so reading it alone would be insufficient (per the issue body).
        main_env = tmp_path / ".env"
        main_env.write_text("HINDSIGHT_LLM_API_KEY=active-profile-llm-key\n")

        # Another (default) profile's key in the process env — must NOT bleed in.
        monkeypatch.setenv("HINDSIGHT_LLM_API_KEY", "default-profile-llm-key")

        captured = {"llm_keys": [], "scopes": []}

        def fake_get_client(self):
            from agent.secret_scope import current_secret_scope
            scope = current_secret_scope()
            captured["scopes"].append(scope)
            captured["llm_keys"].append(
                _get_scoped_secret("HINDSIGHT_LLM_API_KEY", "")
            )
            client = MagicMock()
            client._ensure_started = MagicMock()
            client._manager = MagicMock()
            client._manager.is_running = MagicMock(return_value=False)
            return client

        monkeypatch.setattr(HindsightMemoryProvider, "_get_client", fake_get_client)
        monkeypatch.setattr(hs, "_check_local_runtime", lambda: (True, None))

        # Also stub the daemon's Rich-console setup and the embed-manager
        # import path so the thread body completes without actually trying
        # to launch a daemon.
        import sys as _sys
        sys_backup = {}
        for mod_name in ("hindsight_embed", "hindsight_embed.daemon_embed_manager"):
            sys_backup[mod_name] = _sys.modules.get(mod_name)
            _sys.modules[mod_name] = MagicMock()
        try:
            from rich.console import Console  # noqa: F401
        except Exception:
            _sys.modules["rich"] = MagicMock()
            _sys.modules["rich.console"] = MagicMock(Console=MagicMock())

        try:
            provider = _make_provider(tmp_path, monkeypatch, mode="local_embedded")

            # Wait for the daemon-start thread to invoke _get_client().
            import time as _time
            for _ in range(60):
                if captured["llm_keys"]:
                    break
                _time.sleep(0.05)

            assert captured["llm_keys"], (
                "daemon-start thread never invoked _get_client() — see "
                f"hindsight-embed.log tail: "
                f"{((tmp_path / 'logs' / 'hindsight-embed.log').read_text(errors='replace') or '<empty>')[-600:]}"
            )
            # Scope was installed on the thread (proves the new
            # set_secret_scope wrapping is in effect, not a fallback).
            assert captured["scopes"][0] is not None, (
                "daemon-start thread ran without a secret scope — the "
                "issue #94933 fix's set_secret_scope wrapping was not "
                "applied."
            )
            # And it resolved to the active profile's key from the .env,
            # NOT the default-profile-llm-key bleeding in from os.environ.
            assert captured["llm_keys"][0] == "active-profile-llm-key"
        finally:
            for mod_name, mod in sys_backup.items():
                if mod is None:
                    _sys.modules.pop(mod_name, None)
                else:
                    _sys.modules[mod_name] = mod


# ── _build_embedded_profile_env is scope-safe ─────────────────────────────


class TestBuildEmbeddedProfileEnvScopeSafe:
    """``_build_embedded_profile_env`` reads ``HINDSIGHT_LLM_API_KEY``.
    It must use the scoped helper so a scope miss under multiplex does
    NOT raise and a scope hit returns the profile's value."""

    def test_unscoped_under_multiplex_uses_env(self, monkeypatch, tmp_path):
        """Default profile: env value used (the helper's fallback path)."""
        monkeypatch.setenv("HINDSIGHT_LLM_API_KEY", "default-profile-llm-key")
        ss.set_multiplex_active(True)
        env = _build_embedded_profile_env(
            {"profile": "test-profile", "llm_provider": "openai", "llm_model": "x"}
        )
        assert env["HINDSIGHT_API_LLM_API_KEY"] == "default-profile-llm-key"

    def test_scoped_value_wins(self, monkeypatch, tmp_path):
        """Secondary profile: scope wins over env."""
        monkeypatch.setenv("HINDSIGHT_LLM_API_KEY", "default-profile-llm-key")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"HINDSIGHT_LLM_API_KEY": "secondary-profile-llm-key"})
        try:
            env = _build_embedded_profile_env(
                {"profile": "test-profile", "llm_provider": "openai", "llm_model": "x"}
            )
            assert env["HINDSIGHT_API_LLM_API_KEY"] == "secondary-profile-llm-key"
        finally:
            ss.reset_secret_scope(tok)

    def test_scoped_miss_under_multiplex_returns_empty_not_environ(self, monkeypatch):
        """Scope installed but key absent: returns ``""`` (the default
        passed in) — NOT the cross-profile env value."""
        monkeypatch.setenv("HINDSIGHT_LLM_API_KEY", "default-profile-llm-key")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"UNRELATED_KEY": "x"})
        try:
            env = _build_embedded_profile_env(
                {"profile": "test-profile", "llm_provider": "openai", "llm_model": "x"}
            )
            # The profile daemon env carries empty LLM key — no leak.
            assert env["HINDSIGHT_API_LLM_API_KEY"] == ""
        finally:
            ss.reset_secret_scope(tok)
