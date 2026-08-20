"""Tests for ClinePass provider integration.

Verifies that ClinePass appears in all the right places:
- Provider plugin loads and registers
- PROVIDER_REGISTRY has the correct config
- CANONICAL_PROVIDERS includes the slug
- OPTIONAL_ENV_VARS knows the API key
- Unified provider catalog surfaces it on the "keys" tab
"""

from __future__ import annotations

import pytest


def test_cline_pass_plugin_loads():
    """The cline-pass provider plugin registers a ProviderProfile."""
    from providers import list_providers

    profiles = {p.name: p for p in list_providers()}
    assert "cline-pass" in profiles
    prof = profiles["cline-pass"]
    assert prof.base_url == "https://api.cline.bot/api/v1"
    assert "CLINE_API_KEY" in prof.env_vars


def test_cline_pass_aliases_resolve():
    """Aliases (clinepass, cline_pass) resolve to the same profile."""
    from providers import get_provider_profile

    main = get_provider_profile("cline-pass")
    alias1 = get_provider_profile("clinepass")
    alias2 = get_provider_profile("cline_pass")
    assert main is alias1 is alias2


def test_cline_pass_in_provider_registry():
    """PROVIDER_REGISTRY entry has correct auth type and URLs."""
    from hermes_cli.auth import PROVIDER_REGISTRY

    cfg = PROVIDER_REGISTRY.get("cline-pass")
    assert cfg is not None
    assert cfg.auth_type == "api_key"
    assert cfg.inference_base_url == "https://api.cline.bot/api/v1"
    assert "CLINE_API_KEY" in cfg.api_key_env_vars
    assert cfg.base_url_env_var == "CLINE_BASE_URL"


def test_cline_pass_in_canonical_providers():
    """CANONICAL_PROVIDERS includes cline-pass."""
    from hermes_cli.models import CANONICAL_PROVIDERS

    slugs = {p.slug for p in CANONICAL_PROVIDERS}
    assert "cline-pass" in slugs


def test_cline_pass_in_optional_env_vars():
    """OPTIONAL_ENV_VARS knows about CLINE_API_KEY and CLINE_BASE_URL."""
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

    assert "CLINE_API_KEY" in OPTIONAL_ENV_VARS
    info = OPTIONAL_ENV_VARS["CLINE_API_KEY"]
    assert info["category"] == "provider"
    assert info["password"] is True
    assert "cline.bot" in info["url"]

    assert "CLINE_BASE_URL" in OPTIONAL_ENV_VARS
    base = OPTIONAL_ENV_VARS["CLINE_BASE_URL"]
    assert base["password"] is False


def test_cline_pass_in_provider_catalog():
    """Unified provider catalog surfaces cline-pass on the 'keys' tab."""
    from hermes_cli.provider_catalog import provider_catalog_by_slug

    cat = provider_catalog_by_slug()
    assert "cline-pass" in cat
    desc = cat["cline-pass"]
    assert desc.tab == "keys"  # api_key auth type
    assert "CLINE_API_KEY" in desc.api_key_env_vars
    assert desc.auth_type == "api_key"


def test_cline_pass_has_plugin_yaml():
    """The plugin directory contains a plugin.yaml manifest."""
    from pathlib import Path

    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "model-providers" / "cline-pass"
    assert (plugin_dir / "__init__.py").exists()
    assert (plugin_dir / "plugin.yaml").exists()
