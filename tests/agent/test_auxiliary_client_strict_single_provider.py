import logging
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


def test_endpoint_logging_strips_userinfo_query_and_fragment():
    from agent.auxiliary_client import _safe_endpoint_for_log

    rendered = _safe_endpoint_for_log(
        "https://synthetic-user:synthetic-pass@example.invalid:8443/v1"
        "?api_key=synthetic-token#fragment"
    )

    assert rendered == "https://example.invalid:8443/v1"
    assert "synthetic" not in rendered
    assert "?" not in rendered
    assert "#" not in rendered


def test_strict_single_provider_diagnostics_are_metadata_only(caplog):
    primary = MagicMock()
    primary.base_url = (
        "https://privacy.invalid/tenant-private/classifier"
        "?payload=private-query#private-fragment"
    )
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "private-provider",
                "model": "private-model",
            },
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "private-provider",
                "private-model",
                primary.base_url,
                "synthetic-private-key",
                "chat_completions",
            ),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary, "private-model"),
        ),
        caplog.at_level(logging.INFO, logger="agent.auxiliary_client"),
    ):
        call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "private-payload"}],
            strict_single_provider=True,
        )

    rendered = caplog.text
    for private_value in (
        "privacy.invalid",
        "tenant-private",
        "private-provider",
        "private-model",
        "private-payload",
        "private-query",
        "private-fragment",
        "synthetic-private-key",
    ):
        assert private_value not in rendered
    assert "state=dispatch" in rendered


def test_strict_named_custom_rewrite_diagnostic_is_metadata_only(caplog):
    import agent.auxiliary_client as auxiliary

    primary = MagicMock()
    primary.base_url = "https://privacy.invalid/tenant-private/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )
    with (
        patch.object(
            auxiliary,
            "_get_auxiliary_task_config",
            return_value={
                "provider": "custom:private-classifier",
                "model": "private-model",
            },
        ),
        patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            return_value={
                "name": "private-classifier",
                "base_url": "https://privacy.invalid/tenant-private/anthropic",
                "api_key": "synthetic-private-key",
                "api_mode": "chat_completions",
            },
        ),
        patch.object(
            auxiliary,
            "_get_cached_client",
            return_value=(primary, "private-model"),
        ),
        caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"),
    ):
        auxiliary.call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "private-payload"}],
            strict_single_provider=True,
        )

    rendered = caplog.text
    for private_value in (
        "privacy.invalid",
        "tenant-private",
        "private-classifier",
        "private-model",
        "private-payload",
        "synthetic-private-key",
    ):
        assert private_value not in rendered


def test_strict_single_provider_suppresses_shared_transport_diagnostics(caplog):
    primary = MagicMock()
    primary.base_url = "https://privacy.invalid/tenant-private/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))]
    )

    def _noisy_shared_transport(*_args, **_kwargs):
        transport_logger = logging.getLogger("synthetic.shared_transport")
        try:
            raise RuntimeError("private-exception")
        except RuntimeError:
            transport_logger.error(
                "provider=private-provider model=private-model "
                "endpoint=https://privacy.invalid/tenant-private/v1",
                exc_info=True,
            )
        return primary, "private-model"

    with (
        patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "private-provider",
                "model": "private-model",
            },
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=(
                "private-provider",
                "private-model",
                primary.base_url,
                "synthetic-private-key",
                "chat_completions",
            ),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            side_effect=_noisy_shared_transport,
        ),
        caplog.at_level(logging.DEBUG),
    ):
        call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "private-payload"}],
            strict_single_provider=True,
        )

    rendered = caplog.text
    for private_value in (
        "privacy.invalid",
        "tenant-private",
        "private-provider",
        "private-model",
        "private-payload",
        "private-exception",
        "Traceback",
    ):
        assert private_value not in rendered
    assert "state=dispatch" in rendered


def test_auxiliary_client_cache_is_scoped_to_active_profile_home(tmp_path, monkeypatch):
    """Equal custom aliases in two profiles must never share one HTTP client."""

    import agent.auxiliary_client as auxiliary

    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    current = [homes[0]]
    client_a = MagicMock(name="profile-a-client")
    client_b = MagicMock(name="profile-b-client")

    monkeypatch.setattr(auxiliary, "get_hermes_home", lambda: current[0])
    with auxiliary._client_cache_lock:
        auxiliary._client_cache.clear()

    try:
        with patch.object(
            auxiliary,
            "resolve_provider_client",
            side_effect=[(client_a, "classifier"), (client_b, "classifier")],
        ) as builder:
            first, _ = auxiliary._get_cached_client(
                "custom:classifier",
                "classifier",
                task="smart_router",
            )
            current[0] = homes[1]
            second, _ = auxiliary._get_cached_client(
                "custom:classifier",
                "classifier",
                task="smart_router",
            )

        assert first is client_a
        assert second is client_b
        assert builder.call_count == 2
        with auxiliary._client_cache_lock:
            assert len(auxiliary._client_cache) == 2
    finally:
        with auxiliary._client_cache_lock:
            auxiliary._client_cache.clear()


def test_strict_named_custom_cache_key_freezes_cross_profile_route(tmp_path):
    """A strict custom alias is keyed by its resolved, secret-safe route."""

    import yaml

    import agent.auxiliary_client as auxiliary
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    routes = {
        profile_a: {
            "base_url": (
                "https://profile-a-user:profile-a-pass@PROFILE-A.invalid:443/v1/"
                "?api_key=profile-a-url-secret#ignored"
            ),
            "api_key": "profile-a-api-secret",
        },
        profile_b: {
            "base_url": (
                "https://profile-b-user:profile-b-pass@Profile-B.invalid/v1"
                "?api_key=profile-b-url-secret#ignored"
            ),
            "api_key": "profile-b-api-secret",
        },
    }

    for home, route in routes.items():
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "auxiliary": {
                        "smart_router": {
                            "provider": "custom:classifier",
                            "model": "classifier-model",
                        }
                    },
                    "custom_providers": [
                        {
                            "name": "classifier",
                            "base_url": route["base_url"],
                            "api_key": route["api_key"],
                            "api_mode": "chat_completions",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    built_clients = []

    def fake_openai_client(*, api_key, base_url, **_kwargs):
        client = MagicMock(name=f"client-{len(built_clients)}")
        client.api_key = api_key
        client.base_url = base_url
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=base_url))]
        )
        built_clients.append(client)
        return client

    with auxiliary._client_cache_lock:
        auxiliary._client_cache.clear()

    try:
        with patch.object(auxiliary, "_create_openai_client", side_effect=fake_openai_client):
            responses = []
            for home in (profile_a, profile_b):
                token = set_hermes_home_override(home)
                try:
                    responses.append(
                        auxiliary.call_llm(
                            task="smart_router",
                            messages=[{"role": "user", "content": "classify"}],
                            strict_single_provider=True,
                        )
                    )
                finally:
                    reset_hermes_home_override(token)

        assert len(built_clients) == 2
        assert built_clients[0] is not built_clients[1]
        assert [client.api_key for client in built_clients] == [
            routes[profile_a]["api_key"],
            routes[profile_b]["api_key"],
        ]
        assert responses[0].choices[0].message.content != responses[1].choices[0].message.content

        with auxiliary._client_cache_lock:
            cache_keys = list(auxiliary._client_cache)

        assert len(cache_keys) == 2
        key_a = next(key for key in cache_keys if str(profile_a.resolve()) in key)
        key_b = next(key for key in cache_keys if str(profile_b.resolve()) in key)

        for key, endpoint in (
            (key_a, "https://profile-a.invalid/v1"),
            (key_b, "https://profile-b.invalid/v1"),
        ):
            assert "custom:classifier" in key
            assert endpoint in key
            assert "chat_completions" in key
            assert "classifier-model" in key
            assert any(
                isinstance(component, tuple)
                and component[:1] == ("api-key-digest",)
                for component in key
            )

        rendered_keys = repr(cache_keys)
        for secret in (
            "profile-a-user",
            "profile-a-pass",
            "profile-a-url-secret",
            "profile-a-api-secret",
            "profile-b-user",
            "profile-b-pass",
            "profile-b-url-secret",
            "profile-b-api-secret",
        ):
            assert secret not in rendered_keys
    finally:
        with auxiliary._client_cache_lock:
            auxiliary._client_cache.clear()


def test_strict_named_custom_freeze_applies_call_api_mode_before_endpoint_rewrite():
    """The call-level wire override is part of the route frozen for the cache."""

    import agent.auxiliary_client as auxiliary

    primary = MagicMock()
    primary.base_url = "https://classifier.invalid/v1"
    primary.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
    )
    task_config = {
        "provider": "custom:classifier",
        "model": "classifier-model",
        "api_mode": "anthropic_messages",
    }

    with (
        patch.object(auxiliary, "_get_auxiliary_task_config", return_value=task_config),
        patch(
            "hermes_cli.runtime_provider._get_named_custom_provider",
            return_value={
                "name": "classifier",
                "base_url": "https://classifier.invalid/anthropic",
                "api_key": "synthetic-secret",
                "api_mode": "anthropic_messages",
            },
        ),
        patch.object(
            auxiliary,
            "_get_cached_client",
            return_value=(primary, "classifier-model"),
        ) as get_client,
    ):
        auxiliary.call_llm(
            task="smart_router",
            messages=[{"role": "user", "content": "classify"}],
            api_mode="chat_completions",
            strict_single_provider=True,
        )

    assert get_client.call_args.kwargs["base_url"] == "https://classifier.invalid/v1"
    assert get_client.call_args.kwargs["api_mode"] == "chat_completions"
