"""Integration tests for the ACP runtime provider seam.

Tests the full flow:
  1. Plugin register → runtime registry
  2. acp_runtime_switch apply → config mutation
  3. resolve_runtime_provider → descriptor with acp_client
  4. agent init metadata/transport detection
  5. acp_runtime_switch off → full restore

Uses temp HERMES_HOME + real imports (no mocks for the seam itself).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_hermes_home(tmp_path, monkeypatch):
    """Create a temporary HERMES_HOME with a minimal config.yaml."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    config_dir = home / ".hermes"
    config_dir.mkdir(exist_ok=True)

    # Minimal config.yaml
    config_yaml = """
model:
  provider: auto
  default: gpt-4o
"""
    (config_dir / "config.yaml").write_text(config_yaml)
    monkeypatch.setenv("HERMES_HOME", str(config_dir))
    monkeypatch.setenv("HOME", str(home))
    return config_dir


@pytest.fixture(autouse=True)
def _clean_registries():
    """Clear the runtime registry before and after each test."""
    from hermes_cli.acp_runtime_provider_registry import _clear_for_testing as _clear_rt
    _clear_rt()
    yield
    _clear_rt()


# ---------------------------------------------------------------------------
# Fake plugin resolver
# ---------------------------------------------------------------------------

_FAKE_DESCRIPTOR = {
    "provider": "acp_client",
    "api_mode": "acp_client",
    "display_provider": "fake-acp-agent",
    "model": "opus[1m]",
    "command": "fake-agent-binary",
    "args": ["--flag", "value"],
    "base_url": "",
    "api_key": "",
    "metadata": {"workdir": "/tmp", "timeout_seconds": 600},
}


def _fake_runtime_resolver(requested_model, cfg):
    """A fake runtime resolver that mimics what a plugin would register."""
    desc = dict(_FAKE_DESCRIPTOR)
    if requested_model:
        desc["model"] = requested_model
    return desc


# ---------------------------------------------------------------------------
# Test 1: Plugin register → runtime registry
# ---------------------------------------------------------------------------

class TestPluginRegisterPopulatesRegistry:
    def test_register_adds_to_runtime_registry(self):
        """When a plugin calls register_acp_runtime_provider, the resolver
        should be findable via get_acp_runtime_provider."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            get_acp_runtime_provider,
            list_acp_runtime_providers,
        )

        register_acp_runtime_provider("test-agent-acp", _fake_runtime_resolver)
        assert "test-agent-acp" in list_acp_runtime_providers()
        assert get_acp_runtime_provider("test-agent-acp") is _fake_runtime_resolver


# ---------------------------------------------------------------------------
# Test 2: acp_runtime_switch apply with registry-resolved provider
# ---------------------------------------------------------------------------

class TestSwitchApplyWithRegistry:
    def test_apply_resolves_via_runtime_registry(self):
        """apply() should resolve the command key via the runtime registry
        and write all descriptor fields into config."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli import acp_runtime_switch as ars

        register_acp_runtime_provider("test-agent-acp", _fake_runtime_resolver)

        cfg = {}
        r = ars.apply(cfg, "acp_client", acp_command="test-agent-acp")

        assert r.success
        assert r.new_value == "acp_client"
        assert cfg["model"]["provider"] == "fake-acp-agent"
        assert cfg["model"]["default"] == "opus[1m]"
        assert cfg["model"]["acp_command"] == "fake-agent-binary"
        assert cfg["model"]["acp_args"] == ["--flag", "value"]


# ---------------------------------------------------------------------------
# Test 3: off restore — backup-based full restore
# ---------------------------------------------------------------------------

class TestSwitchOffRestore:
    def test_enable_then_disable_restores_provider(self):
        """After enable → disable, the original provider should be restored."""
        from hermes_cli import acp_runtime_switch as ars

        original_provider = "openrouter"
        cfg = {"model": {"provider": original_provider, "default": "gpt-4o"}}

        ars.enable_runtime(cfg, "my-agent", ["--flag"],
                           display_provider="my-acp", model="opus[1m]")
        assert cfg["model"]["provider"] == "my-acp"
        assert cfg["model"]["default"] == "opus[1m]"

        ars.disable_runtime(cfg)
        # Provider should be restored to original
        assert cfg["model"]["provider"] == original_provider

    def test_enable_then_disable_restores_model_default(self):
        """After enable → disable, model.default should be restored."""
        from hermes_cli import acp_runtime_switch as ars

        original_model = "gpt-4o"
        cfg = {"model": {"provider": "openrouter", "default": original_model}}

        ars.enable_runtime(cfg, "my-agent", [],
                           display_provider="my-acp", model="opus[1m]")
        assert cfg["model"]["default"] == "opus[1m]"

        ars.disable_runtime(cfg)
        assert cfg["model"]["default"] == original_model

    def test_enable_then_disable_restores_acp_command_args(self):
        """After enable → disable, acp_command and acp_args are removed
        (they didn't exist before enable)."""
        from hermes_cli import acp_runtime_switch as ars

        cfg = {"model": {"provider": "openrouter", "default": "gpt-4o"}}

        ars.enable_runtime(cfg, "my-agent", ["--flag"],
                           display_provider="my-acp", model="opus[1m]")
        assert "acp_command" in cfg["model"]
        assert "acp_args" in cfg["model"]

        ars.disable_runtime(cfg)
        assert "acp_command" not in cfg["model"]
        assert "acp_args" not in cfg["model"]

    def test_off_via_apply_restores_full_config(self):
        """Full apply() flow: enable → off restores all original values."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli import acp_runtime_switch as ars

        register_acp_runtime_provider("test-agent-acp", _fake_runtime_resolver)

        original = {"model": {"provider": "anthropic", "default": "claude-3"}}
        cfg = dict(original)

        # Enable
        r1 = ars.apply(cfg, "acp_client", acp_command="test-agent-acp")
        assert r1.success
        assert cfg["model"]["provider"] == "fake-acp-agent"
        assert cfg["model"]["default"] == "opus[1m]"

        # Disable
        r2 = ars.apply(cfg, "auto")
        assert r2.success
        # Original values restored
        assert cfg["model"]["provider"] == "anthropic"
        assert cfg["model"]["default"] == "claude-3"
        assert "acp_command" not in cfg["model"]
        assert "acp_args" not in cfg["model"]

    def test_no_backup_falls_back_gracefully(self):
        """When config was hand-edited (no backup), disable should still
        remove ACP keys without crashing."""
        from hermes_cli import acp_runtime_switch as ars

        cfg = {"model": {
            "provider": "acp-client",
            "acp_command": "my-agent",
            "acp_args": [],
            "default": "opus[1m]",
        }}
        # No _acp_runtime_backup key (hand-edited or pre-fix config)
        old = ars.disable_runtime(cfg)
        assert old == "acp_client"
        assert "provider" not in cfg["model"]
        assert "acp_command" not in cfg["model"]
        assert "acp_args" not in cfg["model"]

    def test_double_enable_does_not_overwrite_backup(self):
        """Enable when already enabled should not overwrite the backup."""
        from hermes_cli import acp_runtime_switch as ars

        cfg = {"model": {"provider": "openrouter", "default": "gpt-4o"}}

        # First enable
        ars.enable_runtime(cfg, "agent1", [],
                           display_provider="acp1", model="m1")
        backup1 = dict(cfg.get("_acp_runtime_backup", {}))

        # Second enable (already in acp_client state)
        ars.enable_runtime(cfg, "agent2", [],
                           display_provider="acp2", model="m2")
        backup2 = dict(cfg.get("_acp_runtime_backup", {}))

        # Backup should be the same (not overwritten)
        assert backup1 == backup2


# ---------------------------------------------------------------------------
# Test 4: resolve_runtime_provider receives actual config
# ---------------------------------------------------------------------------

class TestResolveRuntimeProviderConfigPassing:
    def test_resolver_receives_model_from_config(self, temp_hermes_home):
        """When resolve_runtime_provider is called with a registered
        runtime provider, the resolver should receive the actual model
        from config, not None."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli.runtime_provider import resolve_runtime_provider

        captured = {}

        def capturing_resolver(requested_model, cfg):
            captured["model"] = requested_model
            captured["cfg"] = cfg
            return dict(_FAKE_DESCRIPTOR)

        register_acp_runtime_provider("capture-agent", capturing_resolver)

        # Patch resolve_requested_provider to return our key
        with patch("hermes_cli.runtime_provider.resolve_requested_provider",
                   return_value="capture-agent"):
            with patch("hermes_cli.runtime_provider.resolve_provider",
                       return_value="capture-agent"):
                # Also bypass the acp_client block by having the resolver
                # return acp_client, then mock the acp_client handling
                try:
                    result = resolve_runtime_provider()
                except Exception:
                    pass  # May fail downstream — we just care about capture

        assert captured["model"] is not None
        assert isinstance(captured["cfg"], dict)


# ---------------------------------------------------------------------------
# Test 5: get_current_state with runtime registry
# ---------------------------------------------------------------------------

class TestGetCurrentStateWithRegistry:
    def test_detects_runtime_provider_state(self):
        """get_current_state should detect acp_client when provider is a
        registered runtime provider."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli import acp_runtime_switch as ars

        register_acp_runtime_provider("my-acp", _fake_runtime_resolver)
        cfg = {"model": {"provider": "my-acp", "default": "opus[1m]"}}
        assert ars.get_current_state(cfg) == "acp_client"
