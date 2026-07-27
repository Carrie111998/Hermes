"""Unit tests for the Databricks provider profile.

Pins the profile's identity, registration, auth, and model-fetching
behaviour without going live.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Patch target: the import inside fetch_models uses
# ``from hermes_cli.urllib_security import open_credentialed_url``,
# so we patch at the source location.
_PATCH_TARGET = "hermes_cli.urllib_security.open_credentialed_url"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def databricks_profile():
    """Resolve the registered Databricks profile through the real discovery path."""
    import model_tools  # noqa: F401 -- triggers plugin discovery
    import providers

    profile = providers.get_provider_profile("databricks")
    assert profile is not None, "databricks provider profile must be registered"
    return profile


# ---------------------------------------------------------------------------
# Identity & registration
# ---------------------------------------------------------------------------


class TestDatabricksIdentity:
    def test_core_fields(self, databricks_profile):
        p = databricks_profile
        assert p.name == "databricks"
        assert p.auth_type == "api_key"
        assert "DATABRICKS_TOKEN" in p.env_vars
        assert "DATABRICKS_BASE_URL" in p.env_vars

    def test_base_url_empty_by_default(self, databricks_profile):
        """Users must configure their own workspace URL -- no default."""
        assert databricks_profile.base_url == ""

    def test_display_metadata_present(self, databricks_profile):
        assert databricks_profile.display_name
        assert databricks_profile.description
        assert databricks_profile.signup_url.startswith("https://")

    def test_supports_vision_false(self, databricks_profile):
        assert databricks_profile.supports_vision is False

    def test_supports_health_check_true(self, databricks_profile):
        assert databricks_profile.supports_health_check is True


class TestDatabricksAliases:
    @pytest.mark.parametrize(
        "alias", ["databricks-serving", "databricks-gateway", "dbx"]
    )
    def test_alias_resolves_via_registry(self, alias):
        import providers

        resolved = providers.get_provider_profile(alias)
        assert resolved is not None
        assert resolved.name == "databricks"

    def test_aliases_declared_on_profile(self, databricks_profile):
        assert "databricks-serving" in databricks_profile.aliases
        assert "databricks-gateway" in databricks_profile.aliases
        assert "dbx" in databricks_profile.aliases


# ---------------------------------------------------------------------------
# _strip_v1_suffix helper -- accessed via sys.modules since the plugin is
# loaded into a synthetic package (plugins.model_providers) by the discovery
# system, not importable via the dotted filesystem path.
# ---------------------------------------------------------------------------


def _get_databricks_module():
    """Return the loaded databricks provider module from sys.modules."""
    # Trigger discovery first
    import providers  # noqa: F401
    providers.list_providers()
    return sys.modules.get("plugins.model_providers.databricks")


class TestStripV1Suffix:
    def test_strips_v1(self):
        mod = _get_databricks_module()
        assert mod is not None
        assert mod._strip_v1_suffix("https://dbc-abc123.cloud.databricks.com/v1") == (
            "https://dbc-abc123.cloud.databricks.com"
        )

    def test_strips_v1_with_trailing_slash(self):
        mod = _get_databricks_module()
        assert mod is not None
        assert mod._strip_v1_suffix("https://dbc-abc123.cloud.databricks.com/v1/") == (
            "https://dbc-abc123.cloud.databricks.com"
        )

    def test_no_v1_returns_unchanged(self):
        mod = _get_databricks_module()
        assert mod is not None
        assert (
            mod._strip_v1_suffix("https://dbc-abc123.cloud.databricks.com")
            == "https://dbc-abc123.cloud.databricks.com"
        )

    def test_custom_path_without_v1_unchanged(self):
        mod = _get_databricks_module()
        assert mod is not None
        assert (
            mod._strip_v1_suffix("https://example.databricks.com/custom")
            == "https://example.databricks.com/custom"
        )


# ---------------------------------------------------------------------------
# fetch_models -- Databricks serving endpoints
# ---------------------------------------------------------------------------


class TestDatabricksFetchModels:
    def test_returns_none_when_no_api_key(self, databricks_profile):
        result = databricks_profile.fetch_models(api_key=None)
        assert result is None

    def test_returns_none_when_no_base_url(self, databricks_profile):
        result = databricks_profile.fetch_models(api_key="dapi-test123", base_url="")
        assert result is None

    @patch(_PATCH_TARGET)
    def test_fetches_from_serving_endpoints_list(
        self, mock_open_url, databricks_profile
    ):
        """Verify the correct API path is called: /api/2.0/serving-endpoints."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"endpoints": [{"name": "my-llm-endpoint"}, {"name": "embedding-service"}]}'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_open_url.return_value = mock_resp

        result = databricks_profile.fetch_models(
            api_key="dapi-test456",
            base_url="https://dbc-xyz.cloud.databricks.com/v1",
        )

        assert result == ["my-llm-endpoint", "embedding-service"]

        # Verify the request was constructed correctly
        call_args, call_kwargs = mock_open_url.call_args
        req = call_args[0]
        assert req.get_full_url() == (
            "https://dbc-xyz.cloud.databricks.com/api/2.0/serving-endpoints"
        )
        assert req.get_header("Authorization") == "Bearer dapi-test456"
        assert req.get_header("Accept") == "application/json"

    @patch(_PATCH_TARGET)
    def test_handles_list_response_shape(self, mock_open_url, databricks_profile):
        """The API may return a bare list instead of {'endpoints': [...]}."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'[{"name": "model-a"}, {"name": "model-b"}]'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_open_url.return_value = mock_resp

        result = databricks_profile.fetch_models(
            api_key="dapi-test789",
            base_url="https://dbc-xyz.cloud.databricks.com/v1",
        )

        assert result == ["model-a", "model-b"]

    @patch(_PATCH_TARGET)
    def test_skips_non_dict_entries(self, mock_open_url, databricks_profile):
        """Gracefully handle malformed entries in the response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"endpoints": [{"name": "good"}, "bad", null, {"name": "also-good"}]}'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_open_url.return_value = mock_resp

        result = databricks_profile.fetch_models(
            api_key="dapi-test789",
            base_url="https://dbc-xyz.cloud.databricks.com/v1",
        )

        assert result == ["good", "also-good"]

    @patch(_PATCH_TARGET)
    def test_returns_none_on_api_error(self, mock_open_url, databricks_profile):
        """Network errors or HTTP 4xx/5xx shouldn't crash the provider."""
        mock_open_url.side_effect = Exception("Connection refused")

        result = databricks_profile.fetch_models(
            api_key="dapi-test999",
            base_url="https://dbc-xyz.cloud.databricks.com/v1",
        )

        assert result is None

    @patch(_PATCH_TARGET)
    def test_workspace_url_without_v1(self, mock_open_url, databricks_profile):
        """When base_url doesn't include /v1, use it as-is for the API call."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"endpoints": []}'
        mock_resp.__enter__.return_value = mock_resp
        mock_open_url.return_value = mock_resp

        databricks_profile.fetch_models(
            api_key="dapi-test000",
            base_url="https://dbc-xyz.cloud.databricks.com",
        )

        call_args, _ = mock_open_url.call_args
        req = call_args[0]
        assert req.get_full_url() == (
            "https://dbc-xyz.cloud.databricks.com/api/2.0/serving-endpoints"
        )


# ---------------------------------------------------------------------------
# Provider auto-registration -- Databricks should appear in the provider
# universe without manual PROVIDER_REGISTRY or CANONICAL_PROVIDERS edits
# ---------------------------------------------------------------------------


class TestDatabricksRegistration:
    def test_appears_in_provider_catalog(self):
        """Databricks should appear in the unified provider catalog."""
        from hermes_cli.provider_catalog import provider_catalog

        catalog = provider_catalog()
        slugs = [d.slug for d in catalog]
        assert "databricks" in slugs

    def test_catalog_auth_type_is_api_key(self):
        from hermes_cli.provider_catalog import provider_catalog_by_slug

        catalog = provider_catalog_by_slug()
        descriptor = catalog.get("databricks")
        assert descriptor is not None
        assert descriptor.auth_type == "api_key"
        assert descriptor.tab == "keys"
        assert "DATABRICKS_TOKEN" in descriptor.api_key_env_vars
        assert descriptor.base_url_env_var == "DATABRICKS_BASE_URL"

    def test_appears_in_list_providers(self):
        from providers import list_providers

        names = [p.name for p in list_providers()]
        assert "databricks" in names

    def test_appears_in_PROVIDER_REGISTRY(self):
        """Auto-extension in auth.py should pick up Databricks."""
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert "databricks" in PROVIDER_REGISTRY
        cfg = PROVIDER_REGISTRY["databricks"]
        assert cfg.auth_type == "api_key"
        assert "DATABRICKS_TOKEN" in cfg.api_key_env_vars
        assert cfg.base_url_env_var == "DATABRICKS_BASE_URL"
