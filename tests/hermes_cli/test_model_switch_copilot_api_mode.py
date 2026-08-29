"""Regression tests for Copilot api_mode recomputation during /model switch.

When switching models within the Copilot provider (e.g. GPT-5 → Claude),
the stale api_mode from resolve_runtime_provider must be overridden with
a fresh value computed from the *new* model.  Without the fix, Claude
requests went through the Responses API and failed with
``unsupported_api_for_model``.
"""

from unittest.mock import patch

from hermes_cli.model_switch import switch_model


_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


def _run_copilot_switch(
    raw_input: str,
    current_provider: str = "copilot",
    current_model: str = "gpt-5.4",
    explicit_provider: str = "",
    runtime_api_mode: str = "codex_responses",
    base_url: str = "https://api.githubcopilot.com",
):
    """Run switch_model with Copilot mocks and return the result."""
    with (
        patch("hermes_cli.model_switch.resolve_alias", return_value=None),
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "ghu_test_token",
                "base_url": base_url,
                "api_mode": runtime_api_mode,
            },
        ),
        patch(
            "hermes_cli.models.validate_requested_model",
            return_value=_MOCK_VALIDATION,
        ),
        patch("hermes_cli.model_switch.get_model_info", return_value=None),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
        patch("hermes_cli.models.detect_provider_for_model", return_value=None),
    ):
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model=current_model,
            explicit_provider=explicit_provider,
        )


def test_same_provider_copilot_switch_recomputes_api_mode():
    """GPT-5 to Claude must select Copilot's Anthropic Messages contract.

    The switch targets ``POST /v1/messages`` through ``anthropic_messages``.
    The Anthropic adapter supplies Copilot's required Bearer authentication
    and identity headers instead of native Anthropic ``x-api-key`` auth.
    """
    result = _run_copilot_switch(
        raw_input="claude-opus-4.6",
        current_provider="copilot",
        current_model="gpt-5.4",
    )

    assert result.success, f"switch_model failed: {result.error_message}"
    assert result.new_model == "claude-opus-4.6"
    assert result.target_provider == "copilot"
    assert result.api_mode == "anthropic_messages"


def test_copilot_switch_rejects_lookalike_host_for_messages_routing():
    """A Copilot-labelled custom runtime cannot broaden host trust by path."""
    result = _run_copilot_switch(
        raw_input="claude-opus-4.6",
        base_url="https://evil.example/api.githubcopilot.com",
    )

    assert result.success, f"switch_model failed: {result.error_message}"
    assert result.api_mode == "chat_completions"


