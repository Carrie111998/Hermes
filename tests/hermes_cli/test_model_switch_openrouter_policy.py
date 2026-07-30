"""OpenRouter policy-authority regression tests for model switching."""

from unittest.mock import patch

from hermes_cli.model_switch import switch_model


_REJECTED = {
    "accepted": False,
    "persist": False,
    "recognized": False,
    "message": "blocked by OpenRouter policy",
}


def test_saved_openrouter_model_cannot_override_policy_rejection():
    """A local provider declaration must not bypass /models/user authority."""
    with (
        patch("hermes_cli.model_switch.resolve_alias", return_value=None),
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        ),
        patch("hermes_cli.models.validate_requested_model", return_value=_REJECTED),
        patch("hermes_cli.model_switch.get_model_info", return_value=None),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
        patch("hermes_cli.models.detect_provider_for_model", return_value=None),
    ):
        result = switch_model(
            raw_input="blocked/model",
            current_provider="openrouter",
            current_model="allowed/model",
            explicit_provider="openrouter",
            user_providers={
                "openrouter": {
                    "enabled": True,
                    "models": ["blocked/model"],
                }
            },
        )

    assert result.success is False
    assert result.error_message == "blocked by OpenRouter policy"
