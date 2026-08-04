"""Triage-only fallbacks must not become auxiliary continuations."""
from contextlib import ExitStack, contextmanager
import logging
from types import SimpleNamespace
import traceback
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


HOLD_MESSAGE = "Auxiliary request held by triage policy; continuation is disabled."


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
        pytest.raises(auxiliary_client.AuxiliaryTriageHold, match=HOLD_MESSAGE),
    ):
        auxiliary_client._resolve_auto(
            main_runtime={"provider": "primary", "model": "primary-model"},
            task="compression",
        )

    resolve_later.assert_not_called()
    discover_built_in.assert_not_called()
    built_in.assert_not_called()


def _task_fallback_chain(*, failure_policy="triage_and_notify"):
    first = {
        "provider": "custom",
        "model": "local-emergency",
        "api_key": "fixture-not-a-secret",
    }
    if failure_policy is not None:
        first["failure_policy"] = failure_policy
    return [
        first,
        {
            "provider": "openrouter",
            "model": "must-not-run",
            "failure_policy": "continue",
        },
    ]


def _original_auxiliary_request():
    return (
        [
            {"role": "system", "content": "PRIMARY_SYSTEM_CONTEXT"},
            {"role": "user", "content": "CONSEQUENTIAL_AUXILIARY_WORK"},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "must_not_reach_triage",
                    "parameters": {},
                },
            }
        ],
    )


def _classified_capacity_failure(auxiliary_client):
    """Patch one deterministic payment/capacity failure without SDK behavior."""
    return patch.multiple(
        auxiliary_client,
        _is_transient_transport_error=MagicMock(return_value=False),
        _is_unsupported_temperature_error=MagicMock(return_value=False),
        _is_payment_error=MagicMock(return_value=True),
        _is_auth_error=MagicMock(return_value=False),
        _is_connection_error=MagicMock(return_value=False),
        _is_rate_limit_error=MagicMock(return_value=False),
        _is_model_incompatible_error=MagicMock(return_value=False),
        _is_invalid_aux_response_error=MagicMock(return_value=False),
        _is_model_not_found_error=MagicMock(return_value=False),
        _recoverable_pool_provider=MagicMock(return_value=None),
        _mark_provider_unhealthy=MagicMock(),
    )


@contextmanager
def _patch_objects(module, replacements):
    with ExitStack() as stack:
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(module, name, new=replacement))
        yield


@contextmanager
def _capacity_path(
    auxiliary_client,
    *,
    async_mode=False,
    provider="auto",
    failure_policy="triage_and_notify",
    fallback_chain=None,
    candidate_results=None,
):
    """Patch the shared request seam while leaving policy routing real."""
    primary = MagicMock()
    primary.base_url = "https://primary.invalid/v1"
    fallback = MagicMock()
    messages, tools = _original_auxiliary_request()
    observed = {}
    observed_calls = []
    primary_error = RuntimeError("PROVIDER_EXCEPTION_SENTINEL credential material")
    fallback_result = object()
    candidate_results = iter(candidate_results) if candidate_results is not None else None

    def capture_fallback(_client, _model, _label, **kwargs):
        observed_calls.append(kwargs)
        observed.update(kwargs)
        if candidate_results is not None:
            try:
                return next(candidate_results)
            except StopIteration:
                pass
        return fallback_result

    relay = (
        AsyncMock(side_effect=primary_error)
        if async_mode
        else MagicMock(side_effect=primary_error)
    )
    candidate = (
        AsyncMock(side_effect=capture_fallback)
        if async_mode
        else MagicMock(side_effect=capture_fallback)
    )
    resolver = MagicMock(return_value=(fallback, "local-emergency"))
    to_async = MagicMock(return_value=(fallback, "local-emergency"))
    top_level = MagicMock(return_value=(object(), "must-not-run", "top-level"))
    main_agent = MagicMock(return_value=(object(), "must-not-run", "main-agent"))
    payment = MagicMock(return_value=(object(), "must-not-run", "payment"))
    built_in = MagicMock(return_value=[("built-in", MagicMock())])

    patches = {
        "_resolve_task_provider_model": MagicMock(
            return_value=(provider, "primary-model", None, None, None)
        ),
        "_get_task_extra_body": MagicMock(return_value={}),
        "_get_cached_client": MagicMock(return_value=(primary, "primary-model")),
        "_effective_aux_timeout": MagicMock(return_value=20),
        "_build_call_kwargs": MagicMock(return_value={}),
        "_relay_async_completion" if async_mode else "_relay_sync_completion": relay,
        "_get_auxiliary_task_config": MagicMock(
            return_value={
                "fallback_chain": (
                    fallback_chain
                    if fallback_chain is not None
                    else _task_fallback_chain(failure_policy=failure_policy)
                )
            }
        ),
        "resolve_provider_client": resolver,
        "_to_async_client": to_async,
        (
            "_call_fallback_candidate_async"
            if async_mode
            else "_call_fallback_candidate_sync"
        ): candidate,
        "_try_main_fallback_chain": top_level,
        "_try_main_agent_model_fallback": main_agent,
        "_try_payment_fallback": payment,
        "_get_provider_chain": built_in,
    }
    with (
        _classified_capacity_failure(auxiliary_client),
        _patch_objects(auxiliary_client, patches),
    ):
        yield SimpleNamespace(
            async_mode=async_mode,
            messages=messages,
            tools=tools,
            observed=observed,
            observed_calls=observed_calls,
            primary_error=primary_error,
            fallback_result=fallback_result,
            resolver=resolver,
            to_async=to_async,
            candidate=candidate,
            top_level=top_level,
            main_agent=main_agent,
            payment=payment,
            built_in=built_in,
        )


async def _invoke_capacity_path(auxiliary_client, harness):
    kwargs = {
        "task": "compression",
        "messages": harness.messages,
        "tools": harness.tools,
    }
    if harness.async_mode:
        return await auxiliary_client._async_call_llm_impl(**kwargs)
    return auxiliary_client._call_llm_impl(**kwargs)


def _assert_no_later_continuation(harness):
    harness.resolver.assert_not_called()
    harness.to_async.assert_not_called()
    if harness.async_mode:
        harness.candidate.assert_not_awaited()
    else:
        harness.candidate.assert_not_called()
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()
    assert harness.observed == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("async_mode", "provider"),
    [(False, "openai-codex"), (True, "auto")],
    ids=["sync-explicit", "async-auto"],
)
async def test_task_specific_capacity_triage_raises_sanitized_terminal_hold(
    async_mode,
    provider,
):
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        async_mode=async_mode,
        provider=provider,
    ) as harness:
        with pytest.raises(auxiliary_client.AuxiliaryTriageHold) as caught:
            await _invoke_capacity_path(auxiliary_client, harness)

    assert str(caught.value) == HOLD_MESSAGE
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(caught.value))
    for forbidden in (
        "PROVIDER_EXCEPTION_SENTINEL",
        "CONSEQUENTIAL_AUXILIARY_WORK",
        "must_not_reach_triage",
        "fixture-not-a-secret",
        "local-emergency",
        "primary-model",
    ):
        assert forbidden not in str(caught.value)
        assert forbidden not in rendered
    _assert_no_later_continuation(harness)


def test_task_specific_triage_raises_through_initial_auto_resolution():
    from agent import auxiliary_client

    def resolve(provider, *_args, **_kwargs):
        if provider == "primary":
            return None, None
        return MagicMock(), "must-not-run"

    with (
        patch.object(auxiliary_client, "_read_main_provider", return_value="primary"),
        patch.object(auxiliary_client, "_read_main_model", return_value="primary-model"),
        patch.object(
            auxiliary_client,
            "_get_auxiliary_task_config",
            return_value={"fallback_chain": _task_fallback_chain()},
        ),
        patch.object(
            auxiliary_client,
            "resolve_provider_client",
            side_effect=resolve,
        ) as resolve_client,
        patch.object(auxiliary_client, "_try_main_fallback_chain") as top_level,
        patch.object(auxiliary_client, "_try_payment_fallback") as payment,
        patch.object(auxiliary_client, "_get_provider_chain") as built_in,
        pytest.raises(auxiliary_client.AuxiliaryTriageHold, match=HOLD_MESSAGE),
    ):
        auxiliary_client._resolve_auto(
            main_runtime={"provider": "primary", "model": "primary-model"},
            task="compression",
        )

    assert resolve_client.call_count == 1
    top_level.assert_not_called()
    payment.assert_not_called()
    built_in.assert_not_called()


def test_task_specific_triage_raises_for_initial_unavailable_client():
    from agent import auxiliary_client

    messages, tools = _original_auxiliary_request()
    with (
        patch.object(
            auxiliary_client,
            "_resolve_task_provider_model",
            return_value=("ollama-cloud", "primary-model", None, None, None),
        ),
        patch.object(auxiliary_client, "_get_task_extra_body", return_value={}),
        patch.object(auxiliary_client, "_get_cached_client", return_value=(None, None)),
        patch.object(
            auxiliary_client,
            "_get_auxiliary_task_config",
            return_value={"fallback_chain": _task_fallback_chain()},
        ),
        patch.object(auxiliary_client, "resolve_provider_client") as resolve,
        patch.object(auxiliary_client, "_build_call_kwargs") as build,
        patch.object(auxiliary_client, "_relay_sync_completion") as relay,
        patch.object(auxiliary_client, "_call_fallback_candidate_sync") as candidate,
        patch.object(auxiliary_client, "_try_main_fallback_chain") as top_level,
        patch.object(auxiliary_client, "_try_payment_fallback") as payment,
        patch.object(auxiliary_client, "_get_provider_chain") as built_in,
        pytest.raises(auxiliary_client.AuxiliaryTriageHold, match=HOLD_MESSAGE),
    ):
        auxiliary_client._call_llm_impl(
            task="compression",
            messages=messages,
            tools=tools,
        )

    for blocked in (
        resolve,
        build,
        relay,
        candidate,
        top_level,
        payment,
        built_in,
    ):
        blocked.assert_not_called()


def test_compression_feasibility_preserves_terminal_hold_boundary():
    from agent import auxiliary_client, conversation_compression

    statuses = []
    agent = SimpleNamespace(
        compression_enabled=True,
        _compression_warning=None,
        _current_main_runtime=lambda: {},
        _emit_status=statuses.append,
    )
    with (
        patch.object(
            auxiliary_client,
            "_resolve_task_provider_model",
            return_value=("auto", "primary-model", None, None, None),
        ),
        patch.object(
            auxiliary_client,
            "get_text_auxiliary_client",
            side_effect=auxiliary_client.AuxiliaryTriageHold(),
        ),
        patch.object(
            auxiliary_client,
            "_try_configured_fallback_for_unavailable_client",
        ) as later_fallback,
    ):
        conversation_compression.check_compression_model_feasibility(agent)

    later_fallback.assert_not_called()
    assert statuses == [agent._compression_warning]
    assert "held by triage policy" in agent._compression_warning


def test_task_specific_invalid_policy_fails_closed_before_continuation():
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        failure_policy="triage_and_notfiy",
    ) as harness:
        with pytest.raises(ValueError, match="invalid failure_policy"):
            auxiliary_client._call_llm_impl(
                task="compression",
                messages=harness.messages,
                tools=harness.tools,
            )

    _assert_no_later_continuation(harness)


@pytest.mark.parametrize(
    "failure_policy",
    [None, "continue"],
    ids=["missing-policy", "explicit-continue"],
)
def test_task_specific_compatibility_policies_continue_with_original_request(
    failure_policy,
):
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        failure_policy=failure_policy,
    ) as harness:
        result = auxiliary_client._call_llm_impl(
            task="compression",
            messages=harness.messages,
            tools=harness.tools,
        )

    assert result is harness.fallback_result
    harness.resolver.assert_called_once()
    harness.candidate.assert_called_once()
    assert harness.observed["messages"] is harness.messages
    assert harness.observed["tools"] is harness.tools
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()


def _ordered_task_chain(*failure_policies):
    providers = ("custom", "openrouter", "openai-codex")
    return [
        {
            "provider": providers[index],
            "model": f"ordered-candidate-{index}",
            "api_key": "fixture-not-a-secret",
            "failure_policy": failure_policy,
        }
        for index, failure_policy in enumerate(failure_policies)
    ]


def _assert_sanitized_hold(caught):
    assert str(caught.value) == HOLD_MESSAGE
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(caught.value))
    for forbidden in (
        "PROVIDER_EXCEPTION_SENTINEL",
        "CONSEQUENTIAL_AUXILIARY_WORK",
        "must_not_reach_triage",
        "fixture-not-a-secret",
        "ordered-candidate",
        "primary-model",
    ):
        assert forbidden not in str(caught.value)
        assert forbidden not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_stale_fallback_auth_log_excludes_exception_and_route_text(
    async_mode,
    caplog,
):
    from agent import auxiliary_client

    error_text = "STALE_AUTH_RESPONSE_WITH_CREDENTIAL_SENTINEL"
    route_text = "ROUTE_CONFIGURATION_SENTINEL"
    relay = (
        AsyncMock(side_effect=RuntimeError(error_text))
        if async_mode
        else MagicMock(side_effect=RuntimeError(error_text))
    )
    destination = auxiliary_client._FallbackDestination(
        "openrouter",
        "https://fallback.invalid/v1",
        None,
        "fallback-model",
    )

    with (
        patch.object(auxiliary_client, "_fallback_entry_timeout", return_value=None),
        patch.object(auxiliary_client, "_fallback_destination", return_value=destination),
        patch.object(
            auxiliary_client,
            "_replan_synchronous_cache_sections",
            return_value=([], []),
        ),
        patch.object(auxiliary_client, "_build_call_kwargs", return_value={}),
        patch.object(
            auxiliary_client,
            "_relay_async_completion" if async_mode else "_relay_sync_completion",
            new=relay,
        ),
        patch.object(auxiliary_client, "_is_auth_error", return_value=True),
        patch.object(
            auxiliary_client,
            "_auth_refresh_provider_for_route",
            return_value="openrouter",
        ),
        patch.object(
            auxiliary_client,
            "_refresh_provider_credentials",
            return_value=False,
        ),
        patch.object(auxiliary_client, "_mark_provider_unhealthy"),
        caplog.at_level(logging.WARNING, logger=auxiliary_client.__name__),
    ):
        call_kwargs = {
            "task": "compression",
            "messages": [],
            "temperature": None,
            "max_tokens": None,
            "tools": None,
            "effective_timeout": 20,
            "effective_extra_body": {},
            "reasoning_config": None,
        }
        if async_mode:
            result = await auxiliary_client._call_fallback_candidate_async(
                object(),
                "fallback-model",
                route_text,
                **call_kwargs,
            )
        else:
            result = auxiliary_client._call_fallback_candidate_sync(
                object(),
                "fallback-model",
                route_text,
                **call_kwargs,
            )

    assert result is None
    assert "stale/unrefreshable" in caplog.text
    assert error_text not in caplog.text
    assert route_text not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_later_task_triage_holds_after_stale_continue_candidate(async_mode):
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        async_mode=async_mode,
        fallback_chain=_ordered_task_chain("continue", "triage_and_notify"),
        candidate_results=[None],
    ) as harness:
        with pytest.raises(auxiliary_client.AuxiliaryTriageHold) as caught:
            await _invoke_capacity_path(auxiliary_client, harness)

    _assert_sanitized_hold(caught)
    assert harness.resolver.call_count == 1
    if async_mode:
        harness.candidate.assert_awaited_once()
        assert harness.to_async.call_count == 1
    else:
        harness.candidate.assert_called_once()
        harness.to_async.assert_not_called()
    assert len(harness.observed_calls) == 1
    assert harness.observed_calls[0]["messages"] is harness.messages
    assert harness.observed_calls[0]["tools"] is harness.tools
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_later_task_continue_resumes_after_stale_continue_candidate(async_mode):
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        async_mode=async_mode,
        fallback_chain=_ordered_task_chain("continue", "continue"),
        candidate_results=[None],
    ) as harness:
        result = await _invoke_capacity_path(auxiliary_client, harness)

    assert result is harness.fallback_result
    assert harness.resolver.call_count == 2
    assert len(harness.observed_calls) == 2
    for forwarded in harness.observed_calls:
        assert forwarded["messages"] is harness.messages
        assert forwarded["tools"] is harness.tools
    if async_mode:
        assert harness.candidate.await_count == 2
        assert harness.to_async.call_count == 2
    else:
        assert harness.candidate.call_count == 2
        harness.to_async.assert_not_called()
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("provider", ["auto", "openai-codex"], ids=["auto", "explicit"])
async def test_exhausted_stale_task_chain_retains_post_chain_fallbacks(
    async_mode,
    provider,
):
    from agent import auxiliary_client

    with _capacity_path(
        auxiliary_client,
        async_mode=async_mode,
        provider=provider,
        fallback_chain=_ordered_task_chain("continue"),
        candidate_results=[None],
    ) as harness:
        if provider == "auto":
            harness.top_level.return_value = (None, None, "")
        result = await _invoke_capacity_path(auxiliary_client, harness)

    assert result is harness.fallback_result
    assert harness.resolver.call_count == 1
    if provider == "auto":
        harness.top_level.assert_called_once()
        harness.payment.assert_called_once()
        harness.main_agent.assert_not_called()
    else:
        harness.top_level.assert_not_called()
        harness.payment.assert_not_called()
        harness.main_agent.assert_called_once()
    harness.built_in.assert_not_called()
    if async_mode:
        assert harness.candidate.await_count == 2
        assert harness.to_async.call_count == 2
    else:
        assert harness.candidate.call_count == 2
        harness.to_async.assert_not_called()


def _continue_chain_for(failed_provider):
    provider = "openrouter" if failed_provider == "custom" else "custom"
    return [
        {
            "provider": provider,
            "model": "configured-continue-candidate",
            "api_key": "fixture-not-a-secret",
            "failure_policy": "continue",
        }
    ]


@contextmanager
def _initial_unavailable_path(
    auxiliary_client,
    *,
    async_mode,
    kind,
    provider,
    base_url=None,
    fallback_chain=None,
    promoted_request_error=None,
):
    task = "vision" if kind == "vision" else "compression"
    messages, tools = _original_auxiliary_request()
    fallback_client = MagicMock()
    fallback_client.base_url = "https://configured-fallback.invalid/v1"
    auto_client = MagicMock()
    auto_client.base_url = "https://auto-fallback.invalid/v1"
    result = object()

    def cached_client(route, *_args, **_kwargs):
        if route == "auto":
            return auto_client, "auto-model"
        return None, None

    def vision_client(*_args, **kwargs):
        route = kwargs.get("provider")
        if route == "auto":
            return "auto-vision", auto_client, "auto-vision-model"
        return route, None, None

    cached = MagicMock(side_effect=cached_client)
    vision = MagicMock(side_effect=vision_client)
    resolver = MagicMock(
        side_effect=lambda _provider, *_args, **kwargs: (
            fallback_client,
            kwargs.get("model") or "configured-continue-candidate",
        )
    )
    to_async = MagicMock(
        side_effect=lambda client, selected_model, **_kwargs: (
            client,
            selected_model,
        )
    )
    build = MagicMock(return_value={})
    relay = (
        AsyncMock(
            side_effect=promoted_request_error,
            return_value=None if promoted_request_error else object(),
        )
        if async_mode
        else MagicMock(
            side_effect=promoted_request_error,
            return_value=None if promoted_request_error else object(),
        )
    )
    candidate = AsyncMock(return_value=None) if async_mode else MagicMock(return_value=None)
    top_level = MagicMock(return_value=(object(), "must-not-run", "top-level"))
    main_agent = MagicMock(return_value=(object(), "must-not-run", "main-agent"))
    payment = MagicMock(return_value=(object(), "must-not-run", "payment"))
    built_in = MagicMock(return_value=[("built-in", MagicMock())])
    set_route = MagicMock()
    mark_unhealthy = MagicMock()
    chain_walk = MagicMock(wraps=auxiliary_client._try_configured_fallback_chain)
    patches = {
        "_resolve_task_provider_model": MagicMock(
            return_value=(provider, "primary-model", base_url, None, None)
        ),
        "_get_task_extra_body": MagicMock(return_value={}),
        "_get_cached_client": cached,
        "resolve_vision_provider_client": vision,
        "_get_auxiliary_task_config": MagicMock(
            return_value={"fallback_chain": fallback_chain or []}
        ),
        "_task_minimum_context_length": MagicMock(return_value=None),
        "resolve_provider_client": resolver,
        "_to_async_client": to_async,
        "_effective_aux_timeout": MagicMock(return_value=20),
        "_set_relay_auxiliary_route": set_route,
        "_build_call_kwargs": build,
        "_relay_async_completion" if async_mode else "_relay_sync_completion": relay,
        (
            "_call_fallback_candidate_async"
            if async_mode
            else "_call_fallback_candidate_sync"
        ): candidate,
        "_validate_llm_response": MagicMock(return_value=result),
        "_is_anthropic_compat_endpoint": MagicMock(return_value=False),
        "_provider_requires_stream": MagicMock(return_value=False),
        "_try_main_fallback_chain": top_level,
        "_try_main_agent_model_fallback": main_agent,
        "_try_payment_fallback": payment,
        "_get_provider_chain": built_in,
        "_try_configured_fallback_chain": chain_walk,
    }
    if promoted_request_error is not None:
        patches.update(
            {
                "_is_transient_transport_error": MagicMock(return_value=False),
                "_is_unsupported_temperature_error": MagicMock(return_value=False),
                "_is_payment_error": MagicMock(return_value=True),
                "_is_auth_error": MagicMock(return_value=False),
                "_is_connection_error": MagicMock(return_value=False),
                "_is_rate_limit_error": MagicMock(return_value=False),
                "_is_model_incompatible_error": MagicMock(return_value=False),
                "_is_invalid_aux_response_error": MagicMock(return_value=False),
                "_is_model_not_found_error": MagicMock(return_value=False),
                "_recoverable_pool_provider": MagicMock(return_value=None),
                "_mark_provider_unhealthy": mark_unhealthy,
            }
        )
    with _patch_objects(auxiliary_client, patches):
        yield SimpleNamespace(
            async_mode=async_mode,
            kind=kind,
            task=task,
            provider=provider,
            base_url=base_url,
            messages=messages,
            tools=tools,
            result=result,
            cached=cached,
            vision=vision,
            resolver=resolver,
            to_async=to_async,
            build=build,
            relay=relay,
            candidate=candidate,
            set_route=set_route,
            mark_unhealthy=mark_unhealthy,
            chain_walk=chain_walk,
            top_level=top_level,
            main_agent=main_agent,
            payment=payment,
            built_in=built_in,
        )


async def _invoke_initial_unavailable(auxiliary_client, harness):
    kwargs = {
        "task": harness.task,
        "messages": harness.messages,
        "tools": harness.tools,
    }
    if harness.async_mode:
        return await auxiliary_client._async_call_llm_impl(**kwargs)
    return auxiliary_client._call_llm_impl(**kwargs)


def _assert_no_auto_recovery(harness):
    if harness.kind == "vision":
        assert harness.vision.call_count == 1
        assert harness.vision.call_args.kwargs["provider"] == harness.provider
        harness.cached.assert_not_called()
    else:
        assert harness.cached.call_count == 1
        assert harness.cached.call_args.args[0] == harness.provider
        harness.vision.assert_not_called()


UNAVAILABLE_TRIAGE_CASES = [
    (False, "text", "openrouter", None),
    (True, "text", "openrouter", None),
    (False, "text", "custom", None),
    (True, "text", "custom", None),
    (False, "text", "custom", "https://custom-primary.invalid/v1"),
    (True, "text", "custom", "https://custom-primary.invalid/v1"),
    (False, "vision", "openrouter", None),
    (True, "vision", "openrouter", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("async_mode", "kind", "provider", "base_url"),
    UNAVAILABLE_TRIAGE_CASES,
    ids=[
        "sync-openrouter",
        "async-openrouter",
        "sync-custom-no-base-url",
        "async-custom-no-base-url",
        "sync-custom-base-url",
        "async-custom-base-url",
        "sync-vision",
        "async-vision",
    ],
)
async def test_initial_unavailable_checks_task_triage_before_auto_recovery(
    async_mode,
    kind,
    provider,
    base_url,
):
    from agent import auxiliary_client

    with _initial_unavailable_path(
        auxiliary_client,
        async_mode=async_mode,
        kind=kind,
        provider=provider,
        base_url=base_url,
        fallback_chain=_ordered_task_chain("triage_and_notify"),
    ) as harness:
        with pytest.raises(auxiliary_client.AuxiliaryTriageHold) as caught:
            await _invoke_initial_unavailable(auxiliary_client, harness)

    _assert_sanitized_hold(caught)
    _assert_no_auto_recovery(harness)
    harness.resolver.assert_not_called()
    harness.to_async.assert_not_called()
    harness.build.assert_not_called()
    if async_mode:
        harness.relay.assert_not_awaited()
    else:
        harness.relay.assert_not_called()
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()


NO_CHAIN_COMPATIBILITY_CASES = [
    ("text", "openrouter", None, False),
    ("text", "custom", None, False),
    ("text", "custom", "https://custom-primary.invalid/v1", True),
    ("vision", "openrouter", None, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("kind", "provider", "base_url", "raises_unavailable"),
    NO_CHAIN_COMPATIBILITY_CASES,
    ids=["openrouter", "custom-no-base-url", "custom-base-url", "vision"],
)
async def test_initial_unavailable_without_task_chain_preserves_auto_behavior(
    async_mode,
    kind,
    provider,
    base_url,
    raises_unavailable,
):
    from agent import auxiliary_client

    with _initial_unavailable_path(
        auxiliary_client,
        async_mode=async_mode,
        kind=kind,
        provider=provider,
        base_url=base_url,
    ) as harness:
        if raises_unavailable:
            with pytest.raises(RuntimeError, match="No LLM provider configured"):
                await _invoke_initial_unavailable(auxiliary_client, harness)
        else:
            result = await _invoke_initial_unavailable(auxiliary_client, harness)
            assert result is harness.result

    harness.resolver.assert_not_called()
    harness.to_async.assert_not_called()
    expected_route_calls = 1 if raises_unavailable else 2
    if kind == "vision":
        assert harness.vision.call_count == expected_route_calls
        harness.cached.assert_not_called()
    else:
        assert harness.cached.call_count == expected_route_calls
        harness.vision.assert_not_called()


CONTINUE_COMPATIBILITY_CASES = [
    ("text", "openrouter", None),
    ("text", "custom", "https://custom-primary.invalid/v1"),
    ("vision", "openrouter", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("kind", "provider", "base_url"),
    CONTINUE_COMPATIBILITY_CASES,
    ids=["openrouter", "custom-base-url", "vision"],
)
async def test_initial_unavailable_explicit_continue_uses_configured_candidate(
    async_mode,
    kind,
    provider,
    base_url,
):
    from agent import auxiliary_client

    with _initial_unavailable_path(
        auxiliary_client,
        async_mode=async_mode,
        kind=kind,
        provider=provider,
        base_url=base_url,
        fallback_chain=_continue_chain_for(provider),
    ) as harness:
        result = await _invoke_initial_unavailable(auxiliary_client, harness)

    assert result is harness.result
    harness.resolver.assert_called_once()
    if async_mode:
        harness.to_async.assert_called_once()
        harness.relay.assert_awaited_once()
    else:
        harness.to_async.assert_not_called()
        harness.relay.assert_called_once()
    _assert_no_auto_recovery(harness)
    harness.build.assert_called_once()
    assert harness.build.call_args.args[2] is harness.messages
    assert harness.build.call_args.kwargs["tools"] is harness.tools
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("kind", ["text", "vision"], ids=["text", "vision"])
async def test_initial_unavailable_promoted_continue_resumes_at_typed_cursor(
    async_mode,
    kind,
):
    from agent import auxiliary_client

    with _initial_unavailable_path(
        auxiliary_client,
        async_mode=async_mode,
        kind=kind,
        provider="openrouter",
        fallback_chain=_ordered_task_chain("continue", "triage_and_notify"),
        promoted_request_error=RuntimeError("PROVIDER_EXCEPTION_SENTINEL"),
    ) as harness:
        with pytest.raises(auxiliary_client.AuxiliaryTriageHold) as caught:
            await _invoke_initial_unavailable(auxiliary_client, harness)

    _assert_sanitized_hold(caught)
    assert [
        call.kwargs.get("start_index", 0)
        for call in harness.chain_walk.call_args_list
    ] == [0, 1]
    assert [call.args[0] for call in harness.resolver.call_args_list] == ["custom"]
    assert [call.kwargs["model"] for call in harness.resolver.call_args_list] == [
        "ordered-candidate-0"
    ]
    assert harness.build.call_count == 1
    assert harness.build.call_args.args[:2] == (
        "custom",
        "ordered-candidate-0",
    )
    assert harness.build.call_args.args[2] is harness.messages
    assert harness.build.call_args.kwargs["tools"] is harness.tools
    assert harness.set_route.call_args.args[:2] == (
        "custom",
        "ordered-candidate-0",
    )
    assert harness.relay.call_args.kwargs["provider"] == "custom"
    harness.mark_unhealthy.assert_called_once_with("custom")
    if async_mode:
        harness.relay.assert_awaited_once()
        harness.candidate.assert_not_awaited()
        harness.to_async.assert_called_once()
    else:
        harness.relay.assert_called_once()
        harness.candidate.assert_not_called()
        harness.to_async.assert_not_called()
    _assert_no_auto_recovery(harness)
    harness.top_level.assert_not_called()
    harness.main_agent.assert_not_called()
    harness.payment.assert_not_called()
    harness.built_in.assert_not_called()
