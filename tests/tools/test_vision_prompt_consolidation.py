"""Vision pre-analysis prompt is a single source of truth."""

from unittest.mock import AsyncMock, patch

import pytest

from tools.vision_tools import get_vision_analysis_prompt


def test_get_vision_analysis_prompt_returns_concise_default():
    with patch("hermes_cli.config.load_config") as mock_load:
        mock_load.side_effect = Exception("no config")
        prompt = get_vision_analysis_prompt()

    assert "Concisely describe this image in 2-4 sentences" in prompt
    assert "Skip decorative details." in prompt


def test_get_vision_analysis_prompt_reads_config_override():
    with patch(
        "hermes_cli.config.load_config",
        return_value={"auxiliary": {"vision": {"analysis_prompt": "Custom prompt"}}},
    ):
        prompt = get_vision_analysis_prompt()

    assert prompt == "Custom prompt"


def test_get_vision_analysis_prompt_ignores_blank_override():
    with patch(
        "hermes_cli.config.load_config",
        return_value={"auxiliary": {"vision": {"analysis_prompt": "   "}}},
    ):
        prompt = get_vision_analysis_prompt()

    assert "Concisely describe this image in 2-4 sentences" in prompt


def test_get_vision_analysis_prompt_ignores_missing_key():
    with patch("hermes_cli.config.load_config", return_value={}):
        prompt = get_vision_analysis_prompt()

    assert "Concisely describe this image in 2-4 sentences" in prompt


@pytest.mark.asyncio
async def test_gateway_call_site_uses_shared_prompt():
    """The gateway entry point forwards the shared prompt, not a private copy."""
    import json

    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": "A cat on a chair."}),
    ) as mock_vision:
        await runner._enrich_message_with_vision(
            user_text="What is happening here?",
            image_paths=["/tmp/cat.png"],
        )

    assert (
        "Concisely describe this image in 2-4 sentences"
        in mock_vision.await_args.kwargs["user_prompt"]
    )
    assert "Skip decorative details." in mock_vision.await_args.kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_gateway_call_site_honors_config_override():
    """A config override must flow through the gateway entry point too."""
    import json

    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    with patch(
        "hermes_cli.config.load_config",
        return_value={"auxiliary": {"vision": {"analysis_prompt": "Custom prompt"}}},
    ), patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": "A cat on a chair."}),
    ) as mock_vision:
        await runner._enrich_message_with_vision(
            user_text="What is happening here?",
            image_paths=["/tmp/cat.png"],
        )

    assert mock_vision.await_args.kwargs["user_prompt"] == "Custom prompt"
