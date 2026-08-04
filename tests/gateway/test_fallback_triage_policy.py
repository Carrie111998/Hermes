"""Gateway pre-agent safety contract for triage-only fallbacks."""
from unittest.mock import patch

import pytest


def test_gateway_runtime_resolution_skips_triage_only_fallback_without_provider_call():
    # Import inside the test so module-level gateway setup remains isolated.
    from gateway import run

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
        patch.object(run, "_load_gateway_runtime_config", return_value=cfg),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider") as resolve,
    ):
        runtime = run._try_resolve_fallback_provider()

    assert runtime is None
    resolve.assert_not_called()


def test_gateway_malformed_policy_stops_before_resolution_or_later_continuation():
    from gateway import run

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
        patch.object(run, "_load_gateway_runtime_config", return_value=cfg),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={"provider": "openrouter"},
        ) as resolve,
        pytest.raises(ValueError, match="invalid failure_policy"),
    ):
        run._try_resolve_fallback_provider()

    resolve.assert_not_called()


def test_gateway_valid_triage_terminates_before_later_continuation():
    from gateway import run

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
    with (
        patch.object(run, "_load_gateway_runtime_config", return_value=cfg),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={"provider": "openrouter", "model": "must-not-run"},
        ) as resolve,
    ):
        runtime = run._try_resolve_fallback_provider()

    assert runtime is None
    resolve.assert_not_called()
