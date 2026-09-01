"""Execute one prepared provider request; the caller owns recovery."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from agent.message_sanitization import (
    _sanitize_structure_non_ascii,
    _sanitize_structure_surrogates,
)
from utils import env_var_enabled

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ProviderCallContext:
    """Turn metadata needed for one outbound provider request."""

    task_id: str
    turn_id: str
    api_request_id: str
    original_user_message: Any
    conversation_messages: List[Dict[str, Any]]
    api_call_count: int
    retry_count: int
    approx_input_tokens: int
    request_char_count: int
    started_at: float


@dataclass
class ProviderCallResult:
    """Provider response and request details consumed by the retry loop."""

    response: Any
    api_kwargs: Dict[str, Any]
    redirect_crossed_response: bool


def _moa_client_consumes_prepared_request(client: Any) -> bool:
    """Whether ``client`` is the in-process MoA facade.

    ``_moa_prepared_request`` is private to ``MoAChatCompletions``.  A client
    rebuilt during credential rotation or fallback must never receive it.
    """
    completions = getattr(getattr(client, "chat", None), "completions", None)
    return callable(getattr(completions, "prepare", None))


def _system_prompt_for_hooks(api_kwargs: Any, request_messages: Any) -> Any:
    """Return the system prompt actually sent to the provider."""
    system_prompt = api_kwargs.get("system")
    if system_prompt is None:
        system_prompt = api_kwargs.get("instructions")
    if system_prompt is None and isinstance(request_messages, list) and request_messages:
        first = request_messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            system_prompt = first.get("content")
    return system_prompt


def _streaming_allowed(agent: Any) -> bool:
    """Preserve the loop's existing single-call streaming selection."""
    if getattr(agent, "_host_streaming_allowed", True) is False:
        return False
    if getattr(agent, "_disable_streaming", False):
        return False
    base_url = str(agent.base_url or "").lower()
    if (
        agent.provider == "copilot-acp"
        or base_url.startswith("acp://")
        or base_url.startswith("acp+tcp://")
    ):
        return False
    if agent.provider == "moa" and not agent._has_stream_consumers():
        return False
    if not agent._has_stream_consumers():
        from unittest.mock import Mock

        if isinstance(getattr(agent, "client", None), Mock):
            return False
    return True


def _set_model_request_active(agent: Any) -> tuple[Any, Any]:
    """Mark a request active under the same lock used by redirects."""
    active = getattr(agent, "_model_request_active", None)
    redirect_lock = getattr(agent, "_pending_redirect_lock", None)
    if redirect_lock is not None:
        with redirect_lock:
            if active is not None:
                active.set()
    elif active is not None:
        active.set()
    return active, redirect_lock


def _clear_model_request_active(agent: Any, active: Any, redirect_lock: Any) -> bool:
    """Clear active state and atomically observe a crossed redirect."""
    if redirect_lock is not None:
        with redirect_lock:
            if active is not None:
                active.clear()
            return bool(agent._pending_redirect)
    if active is not None:
        active.clear()
    return agent._has_pending_redirect()


def execute_provider_call(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    *,
    tools_for_api: List[Dict[str, Any]] | None,
    moa_prepared_request: Any,
    context: ProviderCallContext,
    on_first_delta: Callable[[], None],
) -> ProviderCallResult:
    """Build and execute one provider request without retrying it.

    The caller has already rebuilt provider-specific request projection for
    this attempt.  It receives exceptions unchanged, then applies its
    established retry/fallback policy.
    """
    if tools_for_api == agent.tools:
        api_kwargs = agent._build_api_kwargs(api_messages)
    else:
        api_kwargs = agent._build_api_kwargs(api_messages, tools_for_api=tools_for_api)
    _sanitize_structure_surrogates(api_kwargs)
    if agent._force_ascii_payload:
        _sanitize_structure_non_ascii(api_kwargs)
    if agent.api_mode == "codex_responses":
        api_kwargs = agent._get_transport().preflight_kwargs(
            api_kwargs,
            allow_stream=False,
            is_github_responses=agent._is_copilot_url(),
            sanitize_harmony_tokens=agent._is_codex_backend(),
        )
    if agent._empty_content_retries > 0 and agent._is_openrouter_url():
        headers = dict(api_kwargs.get("extra_headers") or {})
        headers["X-OpenRouter-Cache"] = "false"
        api_kwargs["extra_headers"] = headers
    if getattr(agent, "_is_user_initiated_turn", False) and agent._is_copilot_url():
        headers = dict(api_kwargs.get("extra_headers") or {})
        headers["x-initiator"] = "user"
        api_kwargs["extra_headers"] = headers
        agent._is_user_initiated_turn = False

    try:
        from hermes_cli.middleware import apply_llm_request_middleware

        request_middleware = apply_llm_request_middleware(
            api_kwargs,
            task_id=context.task_id,
            turn_id=context.turn_id,
            api_request_id=context.api_request_id,
            session_id=agent.session_id or "",
            platform=agent.platform or "",
            model=agent.model,
            provider=agent.provider,
            base_url=agent.base_url,
            api_mode=agent.api_mode,
            api_call_count=context.api_call_count,
        )
        api_kwargs = request_middleware.payload
        original_api_kwargs = request_middleware.original_payload
        middleware_trace = request_middleware.trace
    except Exception:
        original_api_kwargs = dict(api_kwargs)
        middleware_trace = []

    _invoke_pre_api_request_hook(agent, api_messages, api_kwargs, context, middleware_trace)
    if env_var_enabled("HERMES_DUMP_REQUESTS"):
        agent._dump_api_request_debug(api_kwargs, reason="preflight")
    if moa_prepared_request is not None and agent.provider == "moa":
        if _moa_client_consumes_prepared_request(agent.client):
            api_kwargs["_moa_prepared_request"] = moa_prepared_request
        else:
            logger.warning(
                "MoA client replaced mid-turn (client=%s); sending the prepared "
                "prompt without the MoA handshake",
                type(agent.client).__name__,
            )

    use_streaming = _streaming_allowed(agent)

    def perform_api_call(next_api_kwargs: Dict[str, Any]) -> Any:
        if agent.api_mode == "codex_responses":
            next_api_kwargs = agent._get_transport().preflight_kwargs(
                next_api_kwargs,
                allow_stream=False,
                is_github_responses=agent._is_copilot_url(),
                sanitize_harmony_tokens=agent._is_codex_backend(),
            )
        if use_streaming:
            return agent._interruptible_streaming_api_call(
                next_api_kwargs, on_first_delta=on_first_delta
            )
        from agent import relay_llm

        return relay_llm.execute(
            next_api_kwargs,
            agent._interruptible_api_call,
            session_id=str(agent.session_id or ""),
            name=str(agent.provider or "provider"),
            model_name=str(agent.model or ""),
            metadata={
                "api_mode": agent.api_mode,
                "api_request_id": context.api_request_id,
                "call_role": _call_role(agent),
                "retry_count": context.retry_count,
            },
            defer_logical_completion=True,
        )

    from hermes_cli.middleware import run_llm_execution_middleware

    active, redirect_lock = _set_model_request_active(agent)
    try:
        response = run_llm_execution_middleware(
            api_kwargs,
            perform_api_call,
            original_request=original_api_kwargs,
            task_id=context.task_id,
            turn_id=context.turn_id,
            api_request_id=context.api_request_id,
            session_id=agent.session_id or "",
            platform=agent.platform or "",
            model=agent.model,
            provider=agent.provider,
            base_url=agent.base_url,
            api_mode=agent.api_mode,
            api_call_count=context.api_call_count,
            middleware_trace=list(middleware_trace),
        )
    finally:
        redirect_crossed_response = _clear_model_request_active(
            agent, active, redirect_lock
        )
    return ProviderCallResult(response, api_kwargs, redirect_crossed_response)


def _invoke_pre_api_request_hook(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    api_kwargs: Dict[str, Any],
    context: ProviderCallContext,
    middleware_trace: List[Any],
) -> None:
    """Invoke the optional observability hook without affecting delivery."""
    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook

        if not has_hook("pre_api_request"):
            return
        request_messages = api_kwargs.get("messages")
        if not isinstance(request_messages, list):
            request_messages = api_kwargs.get("input")
        if not isinstance(request_messages, list):
            request_messages = api_messages
        invoke_hook(
            "pre_api_request",
            task_id=context.task_id,
            turn_id=context.turn_id,
            api_request_id=context.api_request_id,
            session_id=agent.session_id or "",
            user_message=context.original_user_message,
            conversation_history=list(context.conversation_messages),
            platform=agent.platform or "",
            model=agent.model,
            provider=agent.provider,
            base_url=agent.base_url,
            api_mode=agent.api_mode,
            api_call_count=context.api_call_count,
            retry_count=context.retry_count,
            request_messages=list(request_messages),
            system_prompt=_system_prompt_for_hooks(api_kwargs, request_messages),
            message_count=len(api_messages),
            tool_count=len(agent.tools or []),
            approx_input_tokens=context.approx_input_tokens,
            request_char_count=context.request_char_count,
            max_tokens=agent.max_tokens,
            started_at=context.started_at,
            middleware_trace=list(middleware_trace),
            request=agent._api_request_payload_for_hook(api_kwargs),
        )
    except Exception:
        pass
def _call_role(agent: Any) -> str:
    if getattr(agent, "is_subagent", False):
        return "delegated"
    if int(getattr(agent, "_fallback_index", 0) or 0) > 0:
        return "fallback"
    return "primary"
