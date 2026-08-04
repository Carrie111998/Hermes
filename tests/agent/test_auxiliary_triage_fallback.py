"""Triage-only top-level fallbacks must not become auxiliary continuations."""
from unittest.mock import patch

import pytest


def test_auxiliary_auto_chain_skips_triage_only_main_fallback_without_resolution():
    from agent import auxiliary_client

    cfg = {
        "fallback_providers": [
            {
                "provider": "custom",
                "model": "local-emergency",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "triage_and_notify",
            }
        ]
    }
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch.object(auxiliary_client, "_read_main_provider", return_value="primary"),
        patch.object(auxiliary_client, "_resolve_fallback_entry") as resolve,
    ):
        result = auxiliary_client._try_main_fallback_chain(
            task="compression",
            failed_provider="primary",
            reason="test",
        )

    assert result == (None, None, "")
    resolve.assert_not_called()


def test_auxiliary_malformed_policy_stops_before_resolution_or_later_continuation():
    from agent import auxiliary_client

    cfg = {
        "fallback_providers": [
            {
                "provider": "custom",
                "model": "malformed-boundary",
                "failure_policy": "triage_and_notfiy",
            },
            {
                "provider": "openrouter",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ]
    }
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch.object(auxiliary_client, "_read_main_provider", return_value="primary"),
        patch.object(
            auxiliary_client,
            "_resolve_fallback_entry",
            return_value=(object(), "must-not-run"),
        ) as resolve,
        pytest.raises(ValueError, match="invalid failure_policy"),
    ):
        auxiliary_client._try_main_fallback_chain(
            task="compression",
            failed_provider="primary",
            reason="test",
        )

    resolve.assert_not_called()
