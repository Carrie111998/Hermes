from types import SimpleNamespace
from unittest.mock import patch

from agent.tool_executor import _budget_for_agent
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _agent(platform: str):
    return SimpleNamespace(
        platform=platform,
        context_compressor=SimpleNamespace(context_length=32_768),
    )


def test_cron_agent_uses_cron_tool_result_budget():
    config = {
        "cron": {
            "tool_result_budget": {
                "max_result_chars": 8_000,
                "max_turn_chars": 16_000,
                "preview_chars": 1_500,
            }
        }
    }

    with patch("hermes_cli.config.load_config", return_value=config):
        budget = _budget_for_agent(_agent("cron"))

    assert budget.default_result_size == 8_000
    assert budget.turn_budget == 16_000
    assert budget.preview_size == 1_500


def test_default_config_preserves_existing_budget_behavior():
    assert DEFAULT_CONFIG["cron"]["api_max_retries"] is None
    assert DEFAULT_CONFIG["cron"]["tool_result_budget"] == {
        "max_result_chars": None,
        "max_turn_chars": None,
        "preview_chars": None,
    }


def test_interactive_agent_ignores_cron_tool_result_budget():
    with patch(
        "hermes_cli.config.load_config",
        side_effect=AssertionError("interactive path must not read cron overrides"),
    ):
        budget = _budget_for_agent(_agent("cli"))

    assert budget.default_result_size == 19_660
    assert budget.turn_budget == 39_321
    assert budget.preview_size == 1_500
