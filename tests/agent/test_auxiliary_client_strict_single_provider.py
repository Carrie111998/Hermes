from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import call_llm


def test_strict_single_provider_rejects_auto_without_discovery_or_fallback():
    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"provider": "auto", "model": "classifier-model"},
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", "classifier-model", None, None, None),
        ),
        patch("agent.auxiliary_client._get_cached_client") as get_client,
        patch("agent.auxiliary_client._try_configured_fallback_chain") as task_fallback,
        patch("agent.auxiliary_client._try_main_fallback_chain") as main_fallback,
        patch("agent.auxiliary_client._try_payment_fallback") as discovery_fallback,
    ):
        with pytest.raises(RuntimeError, match="explicit provider and model"):
            call_llm(
                task="smart_router",
                messages=[{"role": "user", "content": "synthetic input"}],
                strict_single_provider=True,
            )

    get_client.assert_not_called()
    task_fallback.assert_not_called()
    main_fallback.assert_not_called()
    discovery_fallback.assert_not_called()


@pytest.mark.parametrize(
    "task_config",
    [
        {"provider": "main", "model": "dedicated-classifier"},
        {"provider": "dedicated-provider", "model": ""},
        {"provider": "dedicated-provider", "model": "auto"},
    ],
)
def test_strict_single_provider_rejects_ambiguous_raw_config_before_resolution(
    task_config,
):
    primary = MagicMock()
    primary.base_url = "https://classifier.invalid/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=task_config,
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "dedicated-provider",
                "dedicated-classifier",
                None,
                None,
                None,
            ),
        ) as resolver,
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "dedicated-classifier"),
        ) as get_client,
    ):
        with pytest.raises(RuntimeError, match="explicit provider and model"):
            call_llm(
                task="smart_router",
                messages=[{"role": "user", "content": "synthetic input"}],
                strict_single_provider=True,
            )

    resolver.assert_not_called()
    get_client.assert_not_called()
    primary.chat.completions.create.assert_not_called()


def test_strict_single_provider_rejects_literal_custom_without_explicit_endpoint():
    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"provider": "custom", "model": "dedicated-classifier"},
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "custom",
                "dedicated-classifier",
                None,
                None,
                None,
            ),
        ) as resolver,
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(None, None),
        ) as get_client,
    ):
        with pytest.raises(RuntimeError, match="explicit base_url"):
            call_llm(
                task="smart_router",
                messages=[{"role": "user", "content": "synthetic input"}],
                strict_single_provider=True,
            )

    resolver.assert_not_called()
    get_client.assert_not_called()


def test_strict_single_provider_does_not_read_ambient_main_runtime():
    primary = MagicMock()
    primary.base_url = "https://classifier.invalid/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "dedicated-provider",
                "model": "dedicated-classifier",
            },
        ),
        patch(
            "agent.auxiliary_client._normalize_main_runtime",
            side_effect=AssertionError("strict route must not inspect the main runtime"),
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "dedicated-provider",
                "dedicated-classifier",
                None,
                None,
                None,
            ),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "dedicated-classifier"),
        ),
    ):
        response = call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "synthetic input"}],
            strict_single_provider=True,
        )

    assert response.choices[0].message.content == "synthetic result"
    assert primary.chat.completions.create.call_count == 1


def test_smart_router_default_slot_is_unconfigured_instead_of_auto():
    from hermes_cli.config import DEFAULT_CONFIG

    slot = DEFAULT_CONFIG["auxiliary"]["smart_router"]

    assert slot["provider"] == ""
    assert slot["model"] == ""
    assert slot["timeout"] == 12


def test_strict_single_provider_failure_has_one_attempt_and_no_fallback():
    primary = MagicMock()
    primary.base_url = "https://classifier.invalid/v1"
    primary.chat.completions.create.side_effect = TimeoutError("synthetic timeout")

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "dedicated-provider",
                "model": "dedicated-classifier",
            },
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "dedicated-provider",
                "dedicated-classifier",
                None,
                None,
                None,
            ),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "dedicated-classifier"),
        ) as get_client,
        patch("agent.auxiliary_client._try_configured_fallback_chain") as task_fallback,
        patch(
            "agent.auxiliary_client._try_configured_fallback_for_unavailable_client"
        ) as unavailable_fallback,
        patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fallback,
        patch("agent.auxiliary_client._try_payment_fallback") as discovery_fallback,
        patch("agent.auxiliary_client._resolve_auto") as provider_discovery,
        patch("agent.auxiliary_client._refresh_provider_credentials") as credential_refresh,
        patch("agent.auxiliary_client._retry_same_provider_sync") as credential_replay,
        patch("agent.auxiliary_client._refresh_nous_recommended_model") as model_self_heal,
        patch("agent.auxiliary_client._refresh_nous_auxiliary_client") as nous_refresh,
    ):
        with pytest.raises(TimeoutError, match="synthetic timeout"):
            call_llm(
                task="smart_router",
                messages=[{"role": "user", "content": "synthetic input"}],
                strict_single_provider=True,
            )

    assert primary.chat.completions.create.call_count == 1
    assert get_client.call_count == 1
    task_fallback.assert_not_called()
    unavailable_fallback.assert_not_called()
    main_fallback.assert_not_called()
    discovery_fallback.assert_not_called()
    provider_discovery.assert_not_called()
    credential_refresh.assert_not_called()
    credential_replay.assert_not_called()
    model_self_heal.assert_not_called()
    nous_refresh.assert_not_called()


def test_strict_single_provider_accepts_one_valid_response():
    primary = MagicMock()
    primary.base_url = "https://classifier.invalid/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "dedicated-provider",
                "model": "dedicated-classifier",
            },
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "dedicated-provider",
                "dedicated-classifier",
                None,
                None,
                None,
            ),
        ) as resolver,
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "dedicated-classifier"),
        ),
    ):
        response = call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "synthetic input"}],
            strict_single_provider=True,
        )

    assert response.choices[0].message.content == "synthetic result"
    assert primary.chat.completions.create.call_count == 1
    resolver.assert_called_once_with(
        None,
        "dedicated-provider",
        "dedicated-classifier",
        None,
        None,
    )


def test_non_strict_call_keeps_legacy_runtime_and_task_resolution_path():
    primary = MagicMock()
    primary.base_url = "https://legacy.invalid/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="legacy result"))]
    )
    supplied_runtime = {
        "provider": "legacy-main",
        "model": "legacy-main-model",
    }
    normalized_runtime = dict(supplied_runtime)

    with (
        patch(
            "agent.auxiliary_client._normalize_main_runtime",
            return_value=normalized_runtime,
        ) as normalize_runtime,
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("legacy-provider", "legacy-model", None, None, None),
        ) as resolver,
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "legacy-model"),
        ) as get_client,
    ):
        response = call_llm(
            task="legacy-task",
            main_runtime=supplied_runtime,
            messages=[{"role": "user", "content": "synthetic input"}],
        )

    assert response.choices[0].message.content == "legacy result"
    normalize_runtime.assert_called_once_with(supplied_runtime)
    resolver.assert_called_once_with("legacy-task", None, None, None, None)
    get_client.assert_called_once_with(
        "legacy-provider",
        "legacy-model",
        base_url=None,
        api_key=None,
        api_mode=None,
        main_runtime=normalized_runtime,
    )
