"""Tests for the generic delegation provider registry.

Tests cover:
  - register / get / list basic operations
  - duplicate re-registration (overwrite, not error)
  - invalid key and non-callable resolver
  - resolve_via_registry dispatch + return
  - case-insensitivity of provider keys
  - unknown provider returns None (fall-through)
  - PluginContext.register_delegation_provider forwarding
  - integration: _resolve_delegation_credentials consults registry
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the registry before and after each test."""
    from hermes_cli.delegation_provider_registry import _clear_for_testing
    _clear_for_testing()
    yield
    _clear_for_testing()


class TestRegistryBasics:
    def test_register_and_get(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            get_delegation_provider,
        )

        def resolver(model, cfg):
            return {"provider": "test"}

        register_delegation_provider("my-provider", resolver)
        assert get_delegation_provider("my-provider") is resolver

    def test_list_empty(self):
        from hermes_cli.delegation_provider_registry import list_delegation_providers
        assert list_delegation_providers() == []

    def test_list_after_register(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            list_delegation_providers,
        )

        register_delegation_provider("zzz", lambda m, c: {})
        register_delegation_provider("aaa", lambda m, c: {})
        assert list_delegation_providers() == ["aaa", "zzz"]

    def test_get_unknown_returns_none(self):
        from hermes_cli.delegation_provider_registry import get_delegation_provider
        assert get_delegation_provider("nonexistent") is None

    def test_get_non_string_returns_none(self):
        from hermes_cli.delegation_provider_registry import get_delegation_provider
        assert get_delegation_provider(None) is None
        assert get_delegation_provider(123) is None


class TestRegistryValidation:
    def test_empty_key_raises(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        with pytest.raises(ValueError):
            register_delegation_provider("", lambda m, c: {})

    def test_whitespace_key_raises(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        with pytest.raises(ValueError):
            register_delegation_provider("   ", lambda m, c: {})

    def test_non_callable_resolver_raises(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        with pytest.raises(TypeError):
            register_delegation_provider("test", "not_callable")


class TestRegistryReRegistration:
    def test_duplicate_overwrites(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            get_delegation_provider,
        )

        r1 = lambda m, c: {"v": 1}
        r2 = lambda m, c: {"v": 2}
        register_delegation_provider("dup", r1)
        register_delegation_provider("dup", r2)
        assert get_delegation_provider("dup") is r2

    def test_case_insensitive_lookup(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            get_delegation_provider,
        )

        resolver = lambda m, c: {}
        register_delegation_provider("MyProvider", resolver)
        # Lookup should work with any case
        assert get_delegation_provider("myprovider") is resolver
        assert get_delegation_provider("MYPROVIDER") is resolver
        assert get_delegation_provider("MyProvider") is resolver


# ---------------------------------------------------------------------------
# resolve_via_registry
# ---------------------------------------------------------------------------

class TestResolveViaRegistry:
    def test_unknown_provider_returns_none(self):
        from hermes_cli.delegation_provider_registry import resolve_via_registry
        result = resolve_via_registry("unknown", None, {})
        assert result is None

    def test_known_provider_returns_descriptor(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            resolve_via_registry,
        )

        def resolver(model, cfg):
            return {
                "provider": "acp_client",
                "model": model or "default-model",
                "api_mode": "acp_client",
                "command": "npx",
                "args": ["-y", "some-agent"],
            }

        register_delegation_provider("my-acp", resolver)
        result = resolve_via_registry("my-acp", "opus", {})
        assert result["provider"] == "acp_client"
        assert result["model"] == "opus"
        assert result["command"] == "npx"

    def test_resolver_receives_model_and_cfg(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            resolve_via_registry,
        )

        captured = {}

        def resolver(model, cfg):
            captured["model"] = model
            captured["cfg"] = cfg
            return {}

        register_delegation_provider("capture", resolver)
        resolve_via_registry("capture", "sonnet", {"delegation_key": "val"})
        assert captured["model"] == "sonnet"
        assert captured["cfg"]["delegation_key"] == "val"

    def test_non_dict_return_raises(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            resolve_via_registry,
        )

        register_delegation_provider("bad", lambda m, c: "not_a_dict")
        with pytest.raises(TypeError, match="expected dict"):
            resolve_via_registry("bad", None, {})

    def test_resolver_exception_propagates(self):
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
            resolve_via_registry,
        )

        def boom(model, cfg):
            raise RuntimeError("resolver failure")

        register_delegation_provider("boom", boom)
        with pytest.raises(RuntimeError, match="resolver failure"):
            resolve_via_registry("boom", None, {})


# ---------------------------------------------------------------------------
# PluginContext forwarding
# ---------------------------------------------------------------------------

class TestPluginContextForwarding:
    def test_register_delegation_provider_forwards(self):
        from hermes_cli.delegation_provider_registry import (
            get_delegation_provider,
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

        ctx.register_delegation_provider("test-key", my_resolver)
        assert get_delegation_provider("test-key") is my_resolver

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
            ctx.register_delegation_provider("", lambda m, c: {})


# ---------------------------------------------------------------------------
# Integration: _resolve_delegation_credentials consults registry
# ---------------------------------------------------------------------------

class TestDelegationCredentialResolution:
    """Verify that _resolve_delegation_credentials uses the registry when
    a registered provider key is encountered."""

    def test_registered_provider_resolved_without_runtime(self):
        """When a provider is registered, _resolve_delegation_credentials
        uses the resolver and never calls resolve_runtime_provider."""
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        from tools.delegate_tool import _resolve_delegation_credentials

        def resolver(model, cfg):
            return {
                "provider": "acp_client",
                "model": model or "default",
                "api_mode": "acp_client",
                "base_url": "",
                "api_key": "",
                "command": "my-binary",
                "args": ["--flag"],
            }

        register_delegation_provider("my-custom", resolver)

        cfg = {"provider": "my-custom", "model": "opus"}
        creds = _resolve_delegation_credentials(cfg, parent_agent=None)

        assert creds["provider"] == "acp_client"
        assert creds["model"] == "opus"
        assert creds["api_mode"] == "acp_client"
        assert creds["command"] == "my-binary"
        assert creds["args"] == ["--flag"]
        assert creds["api_key"] == ""

    def test_unknown_provider_falls_through_to_runtime(self):
        """When no resolver is registered, the built-in path runs (and may
        fail — that's the existing behavior, unchanged)."""
        from tools.delegate_tool import _resolve_delegation_credentials

        # Use a known built-in provider with a mocked resolve_runtime_provider
        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider"
        ) as mock_rt:
            mock_rt.return_value = {
                "provider": "custom",
                "api_mode": "chat_completions",
                "base_url": "https://example.com",
                "api_key": "test-key",
            }
            cfg = {"provider": "custom", "model": "gpt-4"}
            creds = _resolve_delegation_credentials(cfg, parent_agent=None)
            assert creds["provider"] == "custom"
            assert creds["api_key"] == "test-key"
            mock_rt.assert_called_once()

    def test_resolver_model_fallback_to_delegation_model(self):
        """When the resolver doesn't specify a model, the delegation-level
        model from config is used."""
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        from tools.delegate_tool import _resolve_delegation_credentials

        def resolver(model, cfg):
            return {
                "provider": "acp_client",
                # no model key — core should fall back
            }

        register_delegation_provider("no-model", resolver)

        cfg = {"provider": "no-model", "model": "delegation-model"}
        creds = _resolve_delegation_credentials(cfg, parent_agent=None)
        assert creds["model"] == "delegation-model"

    def test_resolver_descriptor_keys_normalised(self):
        """Missing descriptor keys should not cause KeyError in the caller."""
        from hermes_cli.delegation_provider_registry import (
            register_delegation_provider,
        )
        from tools.delegate_tool import _resolve_delegation_credentials

        def resolver(model, cfg):
            return {"provider": "acp_client"}  # minimal

        register_delegation_provider("minimal", resolver)

        cfg = {"provider": "minimal"}
        creds = _resolve_delegation_credentials(cfg, parent_agent=None)
        # All expected keys must be present (even if None)
        for key in ("model", "provider", "base_url", "api_key", "api_mode",
                     "request_overrides", "max_output_tokens", "command", "args"):
            assert key in creds, f"Missing key: {key}"
        assert creds["args"] == []
        assert creds["request_overrides"] == {}
