"""Regression coverage for default agent guidance policy."""

from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_agent_guidance_defaults_apply_to_all_models():
    agent = DEFAULT_CONFIG["agent"]
    assert agent["tool_use_enforcement"] is True
    assert agent["execution_guidance"] is True


def test_related_continuation_defaults_remain_unchanged():
    agent = DEFAULT_CONFIG["agent"]
    assert agent["intent_ack_continuation"] == "auto"
    assert agent["verify_on_stop"] is False
