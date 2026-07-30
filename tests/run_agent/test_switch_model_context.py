"""Tests that switch_model does not inherit stale context_length overrides.

Includes regression tests for the global max_context_length ceiling:
- Ceiling survives /model switch
- Ceiling is applied as min(native, ceiling), not as absolute override
- Ceiling at or below native context is honoured
- Ceiling above native context does NOT inflate (native window wins)
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.context_compressor import ContextCompressor


def _make_agent_with_compressor(config_context_length=None, max_context_length=None) -> AIAgent:
    """Build a minimal AIAgent with a context_compressor, skipping __init__."""
    agent = AIAgent.__new__(AIAgent)

    # Primary model settings
    agent.model = "primary-model"
    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    agent.quiet_mode = True

    # Store the initial config_context_length override used at agent construction.
    agent._config_context_length = config_context_length

    # Global context-length ceiling (agent.max_context_length in config.yaml).
    agent._max_context_length = max_context_length

    # Context compressor with primary model values
    compressor = ContextCompressor(
        model="primary-model",
        threshold_percent=0.50,
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-primary",
        provider="openrouter",
        quiet_mode=True,
        config_context_length=config_context_length,
    )
    agent.context_compressor = compressor

    # For switch_model
    agent._primary_runtime = {}

    return agent


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_model_clears_previous_config_context_length(mock_ctx_len):
    """Switching models must not reuse the previous model.context_length override."""
    agent = _make_agent_with_compressor(config_context_length=32_768)

    assert agent.context_compressor.model == "primary-model"
    assert agent.context_compressor.context_length == 32_768  # From config override

    # Switch model
    agent.switch_model("new-model", "openrouter", api_key="sk-new", base_url="https://openrouter.ai/api/v1")

    # Verify the old config override is not passed to the new model.
    mock_ctx_len.assert_called_once()
    call_kwargs = mock_ctx_len.call_args.kwargs
    assert call_kwargs.get("config_context_length") is None

    # Verify compressor was updated from the newly resolved model metadata.
    assert agent.context_compressor.model == "new-model"
    assert agent.context_compressor.context_length == 131_072


def test_switch_model_without_config_context_length():
    """When switching models without config override, config_context_length should be None."""
    agent = _make_agent_with_compressor(config_context_length=None)

    with patch("agent.model_metadata.get_model_context_length", return_value=128_000) as mock_ctx_len:
        # Switch model
        agent.switch_model("new-model", "openrouter", api_key="sk-new", base_url="https://openrouter.ai/api/v1")

        # Verify get_model_context_length was called with None
        mock_ctx_len.assert_called_once()
        call_kwargs = mock_ctx_len.call_args.kwargs
        assert call_kwargs.get("config_context_length") is None


# ---------------------------------------------------------------------------
# Regression tests: global max_context_length ceiling (PR #70242)
# ---------------------------------------------------------------------------


@patch("agent.model_metadata.get_model_context_length", return_value=1_048_576)
def test_ceiling_survives_model_switch(mock_ctx_len):
    """Global max_context_length ceiling must be applied after a /model switch.

    Scenario: 200K ceiling, switch to a 1M-context model.
    Expected: compressor is clamped to 200K, not inflated to 1M.
    """
    agent = _make_agent_with_compressor(max_context_length=200_000)

    agent.switch_model(
        "big-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )

    # Native resolution returns 1M; ceiling must clamp to 200K.
    assert agent.context_compressor.context_length == 200_000, (
        f"Expected 200000 (ceiling), got {agent.context_compressor.context_length}"
    )


@patch("agent.model_metadata.get_model_context_length", return_value=128_000)
def test_ceiling_does_not_inflate_smaller_native_context(mock_ctx_len):
    """A ceiling ABOVE the model's native window must NOT inflate the window.

    Scenario: 200K ceiling, model native context is 128K.
    Expected: compressor stays at 128K (native wins via min()).
    """
    agent = _make_agent_with_compressor(max_context_length=200_000)

    agent.switch_model(
        "small-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )

    # Ceiling (200K) > native (128K) → native must win.
    assert agent.context_compressor.context_length == 128_000, (
        f"Expected 128000 (native), got {agent.context_compressor.context_length} "
        "(ceiling must not inflate native context)"
    )


@patch("agent.model_metadata.get_model_context_length", return_value=200_000)
def test_ceiling_at_exact_native_context(mock_ctx_len):
    """Ceiling exactly equal to native context must be applied without change."""
    agent = _make_agent_with_compressor(max_context_length=200_000)

    agent.switch_model(
        "exact-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )

    assert agent.context_compressor.context_length == 200_000


@patch("agent.model_metadata.get_model_context_length", return_value=1_048_576)
def test_no_ceiling_leaves_native_context_unchanged(mock_ctx_len):
    """When no ceiling is set, the full native context must be used."""
    agent = _make_agent_with_compressor(max_context_length=None)

    agent.switch_model(
        "big-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )

    # No ceiling → native 1M context is untouched.
    assert agent.context_compressor.context_length == 1_048_576


@patch("agent.model_metadata.get_model_context_length", return_value=1_048_576)
def test_ceiling_is_min_of_native_and_ceiling(mock_ctx_len):
    """Ceiling is strictly min(native_context, ceiling) for both directions."""
    # Ceiling smaller than native → ceiling wins.
    agent_low = _make_agent_with_compressor(max_context_length=65_536)
    agent_low.switch_model(
        "big-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )
    assert agent_low.context_compressor.context_length == 65_536

    mock_ctx_len.return_value = 32_768
    # Ceiling larger than native → native wins.
    agent_high = _make_agent_with_compressor(max_context_length=200_000)
    agent_high.switch_model(
        "small-model", "openrouter",
        api_key="sk-new", base_url="https://openrouter.ai/api/v1",
    )
    assert agent_high.context_compressor.context_length == 32_768
