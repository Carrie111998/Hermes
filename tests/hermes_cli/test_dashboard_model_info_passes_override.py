"""Regression for #88931: the /api/model/info endpoint must thread the
operator-configured context_length override into get_model_context_length
so the dashboard reports the same value the agent will actually use.

The previous implementation called get_model_context_length with
config_context_length=None (intending to surface the catalog/endpoint
answer separately), but that left effective_context_length inheriting
the catalog fallback for any custom model whose name was substring-
matched by a generic catch-all (e.g. "qwen" → 131072).

The fix: pass the override into the primary call, and only fall back
to a second catalog-only call when the override is set (so the UI can
still display the auto-detected value alongside the override).
"""

import importlib
from unittest.mock import patch

import pytest


def test_dashboard_passes_override_to_get_model_context_length():
    """The dashboard endpoint must call get_model_context_length with the
    operator-configured override (config_context_length=config_ctx_int)
    so the effective_context_length in the response matches the model
    switch dialog, instead of the catalog fallback."""
    web_server = importlib.import_module("hermes_cli.web_server")

    # Capture the kwargs each call receives.
    seen_calls = []

    def fake_resolve(model, base_url="", api_key="", config_context_length=None,
                     provider="", custom_providers=None):
        seen_calls.append({
            "model": model,
            "config_context_length": config_context_length,
        })
        # Return the effective value (override if set, else catalog answer).
        if config_context_length and config_context_length > 0:
            return config_context_length
        return 131072  # catalog fallback (the "qwen" catch-all scenario)

    # Provide a config that exercises the override path.
    fake_config = {
        "model": {
            "default": "qwen36-35b",
            "provider": "custom",
            "base_url": "https://example.invalid/v1",
            "context_length": 99000,
        }
    }

    with patch.object(web_server, "load_config", return_value=fake_config), \
         patch.object(web_server, "_profile_scope"), \
         patch(
             "agent.model_metadata.get_model_context_length",
             side_effect=fake_resolve,
         ):
        result = web_server.get_model_info()

    # The primary call must have received the override.
    assert any(
        c["config_context_length"] == 99000 for c in seen_calls
    ), (
        "/api/model/info did not pass config_context_length=99000 to "
        "get_model_context_length; effective_context_length will fall "
        "back to the catalog value. Calls seen: %r" % (seen_calls,)
    )
    # And the effective response value must be the override, not the catalog.
    assert result["effective_context_length"] == 99000, (
        "effective_context_length should reflect the operator override "
        "(99000), not the catalog fallback (131072). Got: %r"
        % (result,)
    )
    # config_context_length is surfaced as a separate field too.
    assert result["config_context_length"] == 99000


def test_dashboard_without_override_returns_catalog_answer():
    """When the user has not configured a context_length override, the
    effective value must match the catalog/endpoint answer."""
    web_server = importlib.import_module("hermes_cli.web_server")

    def fake_resolve(model, base_url="", api_key="", config_context_length=None,
                     provider="", custom_providers=None):
        return 131072  # catalog answer

    fake_config = {
        "model": {
            "default": "qwen36-35b",
            "provider": "custom",
            "base_url": "https://example.invalid/v1",
            # no context_length override
        }
    }

    with patch.object(web_server, "load_config", return_value=fake_config), \
         patch.object(web_server, "_profile_scope"), \
         patch(
             "agent.model_metadata.get_model_context_length",
             side_effect=fake_resolve,
         ):
        result = web_server.get_model_info()

    assert result["effective_context_length"] == 131072
    assert result["config_context_length"] == 0
