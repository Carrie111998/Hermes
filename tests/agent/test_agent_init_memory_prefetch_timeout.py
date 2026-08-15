"""Regression coverage for the external-memory prefetch config handoff."""

from agent.agent_init import _external_prefetch_timeout_from_config


def test_configured_external_prefetch_timeout_is_parsed():
    assert _external_prefetch_timeout_from_config(
        {"external_prefetch_timeout": "0.5"}
    ) == 0.5


def test_missing_or_invalid_external_prefetch_timeout_uses_manager_default():
    assert _external_prefetch_timeout_from_config({}) is None
    assert _external_prefetch_timeout_from_config(
        {"external_prefetch_timeout": "invalid"}
    ) is None
    assert _external_prefetch_timeout_from_config(
        {"external_prefetch_timeout": 0}
    ) is None