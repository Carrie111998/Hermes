"""Gateway pre-agent safety contract for triage-only fallbacks."""
from unittest.mock import patch


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
