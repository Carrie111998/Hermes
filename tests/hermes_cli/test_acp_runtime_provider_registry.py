"""Tests for the generic ACP runtime provider registry.

Tests cover:
  - register / get / list basic operations
  - duplicate re-registration (overwrite, not error)
  - invalid key and non-callable resolver
  - resolve_acp_runtime_provider dispatch + return
  - case-insensitivity of provider keys
  - unknown provider returns None (fall-through)
  - PluginContext.register_acp_runtime_provider forwarding
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the registry before and after each test."""
    from hermes_cli.acp_runtime_provider_registry import _clear_for_testing
    _clear_for_testing()
    yield
    _clear_for_testing()


class TestRegistryBasics:
    def test_register_and_get(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            get_acp_runtime_provider,
        )

        def resolver(model, cfg):
            return {"provider": "test"}

        register_acp_runtime_provider("my-runtime", resolver)
        assert get_acp_runtime_provider("my-runtime") is resolver

    def test_list_empty(self):
        from hermes_cli.acp_runtime_provider_registry import list_acp_runtime_providers
        assert list_acp_runtime_providers() == []

    def test_list_after_register(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            list_acp_runtime_providers,
        )

        register_acp_runtime_provider("zzz", lambda m, c: {})
        register_acp_runtime_provider("aaa", lambda m, c: {})
        assert list_acp_runtime_providers() == ["aaa", "zzz"]

    def test_get_unknown_returns_none(self):
        from hermes_cli.acp_runtime_provider_registry import get_acp_runtime_provider
        assert get_acp_runtime_provider("nonexistent") is None

    def test_get_non_string_returns_none(self):
        from hermes_cli.acp_runtime_provider_registry import get_acp_runtime_provider
        assert get_acp_runtime_provider(None) is None
        assert get_acp_runtime_provider(123) is None


class TestRegistryValidation:
    def test_empty_key_raises(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        with pytest.raises(ValueError):
            register_acp_runtime_provider("", lambda m, c: {})

    def test_whitespace_key_raises(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        with pytest.raises(ValueError):
            register_acp_runtime_provider("   ", lambda m, c: {})

    def test_non_callable_resolver_raises(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        with pytest.raises(TypeError):
            register_acp_runtime_provider("test", "not_callable")


class TestRegistryReRegistration:
    def test_duplicate_overwrites(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            get_acp_runtime_provider,
        )

        r1 = lambda m, c: {"v": 1}
        r2 = lambda m, c: {"v": 2}
        register_acp_runtime_provider("dup", r1)
        register_acp_runtime_provider("dup", r2)
        assert get_acp_runtime_provider("dup") is r2

    def test_case_insensitive_lookup(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            get_acp_runtime_provider,
        )

        resolver = lambda m, c: {}
        register_acp_runtime_provider("MyRuntime", resolver)
        assert get_acp_runtime_provider("myruntime") is resolver
        assert get_acp_runtime_provider("MYRUNTIME") is resolver
        assert get_acp_runtime_provider("MyRuntime") is resolver


# ---------------------------------------------------------------------------
# resolve_acp_runtime_provider
# ---------------------------------------------------------------------------

class TestResolveViaRegistry:
    def test_unknown_provider_returns_none(self):
        from hermes_cli.acp_runtime_provider_registry import resolve_acp_runtime_provider
        result = resolve_acp_runtime_provider("unknown", None, {})
        assert result is None

    def test_known_provider_returns_descriptor(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            resolve_acp_runtime_provider,
        )

        def resolver(model, cfg):
            return {
                "provider": "acp_client",
                "display_provider": "my-agent",
                "model": model or "default-model",
                "api_mode": "acp_client",
                "command": "npx",
                "args": ["-y", "some-agent"],
            }

        register_acp_runtime_provider("my-acp", resolver)
        result = resolve_acp_runtime_provider("my-acp", "opus[1m]", {})
        assert result["provider"] == "acp_client"
        assert result["display_provider"] == "my-agent"
        assert result["model"] == "opus[1m]"
        assert result["command"] == "npx"

    def test_resolver_receives_model_and_cfg(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            resolve_acp_runtime_provider,
        )

        captured = {}

        def resolver(model, cfg):
            captured["model"] = model
            captured["cfg"] = cfg
            return {}

        register_acp_runtime_provider("capture", resolver)
        resolve_acp_runtime_provider("capture", "opus", {"key": "val"})
        assert captured["model"] == "opus"
        assert captured["cfg"]["key"] == "val"

    def test_non_dict_return_raises(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            resolve_acp_runtime_provider,
        )

        register_acp_runtime_provider("bad", lambda m, c: "not_a_dict")
        with pytest.raises(TypeError, match="expected dict"):
            resolve_acp_runtime_provider("bad", None, {})

    def test_resolver_exception_propagates(self):
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
            resolve_acp_runtime_provider,
        )

        def boom(model, cfg):
            raise RuntimeError("resolver failure")

        register_acp_runtime_provider("boom", boom)
        with pytest.raises(RuntimeError, match="resolver failure"):
            resolve_acp_runtime_provider("boom", None, {})


# ---------------------------------------------------------------------------
# PluginContext forwarding
# ---------------------------------------------------------------------------

class TestPluginContextForwarding:
    def test_register_acp_runtime_provider_forwards(self):
        from hermes_cli.acp_runtime_provider_registry import (
            get_acp_runtime_provider,
            _clear_for_testing,
        )
        from hermes_cli.plugins import PluginContext

        _clear_for_testing()

        manager = MagicMock()
        manifest = MagicMock()
        manifest.name = "test-plugin"
        ctx = PluginContext.__new__(PluginContext)
        ctx.manifest = manifest
        ctx._manager = manager

        def my_resolver(model, cfg):
            return {"provider": "test"}

        ctx.register_acp_runtime_provider("test-key", my_resolver)
        assert get_acp_runtime_provider("test-key") is my_resolver

        _clear_for_testing()

    def test_register_invalid_key_raises_through_context(self):
        from hermes_cli.plugins import PluginContext

        manager = MagicMock()
        manifest = MagicMock()
        manifest.name = "test-plugin"
        ctx = PluginContext.__new__(PluginContext)
        ctx.manifest = manifest
        ctx._manager = manager

        with pytest.raises(ValueError):
            ctx.register_acp_runtime_provider("", lambda m, c: {})


# ---------------------------------------------------------------------------
# Integration: acp_runtime_switch uses the runtime registry
# ---------------------------------------------------------------------------

class TestSwitchUsesRuntimeRegistry:
    """Verify that acp_runtime_switch._try_registry_resolve uses the
    runtime registry for descriptor resolution."""

    def test_runtime_registry_hit_skips_path_check(self):
        """When a runtime provider is registered, apply() should resolve
        the descriptor and skip the PATH check."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli import acp_runtime_switch as ars

        def resolver(model, cfg):
            return {
                "provider": "acp_client",
                "api_mode": "acp_client",
                "display_provider": "my-agent",
                "model": "opus[1m]",
                "command": "my-binary",
                "args": ["--flag"],
                "base_url": "",
                "api_key": "",
            }

        register_acp_runtime_provider("my-agent-runtime", resolver)

        cfg = {}
        r = ars.apply(cfg, "acp_client", acp_command="my-agent-runtime")
        assert r.success
        assert r.new_value == "acp_client"
        # Should have written display_provider and model
        assert cfg["model"]["provider"] == "my-agent"
        assert cfg["model"]["default"] == "opus[1m]"
        assert cfg["model"]["acp_command"] == "my-binary"
        assert cfg["model"]["acp_args"] == ["--flag"]

    def test_unknown_command_still_does_path_check(self):
        """When no runtime provider matches, PATH check runs normally."""
        from hermes_cli import acp_runtime_switch as ars
        from unittest.mock import patch

        cfg = {}
        with patch.object(ars, "check_acp_command_ok",
                          return_value=(False, "not found")):
            r = ars.apply(cfg, "acp_client", acp_command="nonexistent-cmd")
        assert r.success is False
        assert "Cannot enable" in r.message

    def test_get_current_state_detects_runtime_provider(self):
        """get_current_state should detect acp_client when provider is a
        registered runtime provider that resolves to acp_client."""
        from hermes_cli.acp_runtime_provider_registry import (
            register_acp_runtime_provider,
        )
        from hermes_cli import acp_runtime_switch as ars

        def resolver(model, cfg):
            return {"api_mode": "acp_client", "command": "npx"}

        register_acp_runtime_provider("my-runtime", resolver)
        cfg = {"model": {"provider": "my-runtime"}}
        assert ars.get_current_state(cfg) == "acp_client"
