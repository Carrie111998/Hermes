"""Triage-only top-level fallbacks must not become auxiliary continuations."""
from unittest.mock import MagicMock, patch

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


def test_auxiliary_valid_triage_terminates_main_and_builtin_continuation():
    from agent import auxiliary_client

    cfg = {
        "fallback_providers": [
            {
                "provider": "custom",
                "model": "local-emergency",
                "failure_policy": "triage_and_notify",
            },
            {
                "provider": "openrouter",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ]
    }
    built_in = MagicMock(return_value=(object(), "built-in-must-not-run"))
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch.object(auxiliary_client, "_read_main_provider", return_value="primary"),
        patch.object(auxiliary_client, "resolve_provider_client", return_value=(None, None)),
        patch.object(
            auxiliary_client,
            "_try_configured_fallback_chain",
            return_value=(None, None, ""),
        ),
        patch.object(
            auxiliary_client,
            "_resolve_fallback_entry",
            return_value=(object(), "must-not-run"),
        ) as resolve_later,
        patch.object(
            auxiliary_client,
            "_get_provider_chain",
            return_value=[("built-in", built_in)],
        ) as discover_built_in,
    ):
        client, model = auxiliary_client._resolve_auto(
            main_runtime={"provider": "primary", "model": "primary-model"},
            task="compression",
        )

    assert (client, model) == (None, None)
    resolve_later.assert_not_called()
    discover_built_in.assert_not_called()
    built_in.assert_not_called()
