"""Build the API-only message projection for one turn iteration.

The durable transcript belongs to ``run_conversation``.  This module creates
the structurally isolated, provider-facing projection and applies every
request-only transform in its required order.  It does not call the acting
provider, compact history, or own retry control.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_sanitization import (
    _repair_tool_call_arguments,
    _sanitize_messages_surrogates,
)
from agent.prompt_caching import build_prompt_cache_plan, effective_cache_ttl
from agent.turn_context import compose_user_api_content


logger = logging.getLogger(__name__)


# Shared with the active-turn redirect writer in conversation_loop.  Keeping
# the marker here makes the request projection the owner of its wire cleanup.
INTERRUPT_SCAFFOLD_MARKER = (
    "[This response was interrupted by a user correction.]"
)


@dataclass
class PreparedTurnRequest:
    """The request projection consumed by the remaining turn loop."""

    messages: List[Dict[str, Any]]
    api_messages: List[Dict[str, Any]]
    tools_for_api: Optional[List[Dict[str, Any]]]


# Bounded memo for deterministic tool-call argument canonicalization.
_CANON_ARGS_CACHE: Dict[str, str] = {}
_CANON_ARGS_CACHE_MAX = 4096
_CANON_ARGS_CACHE_MAX_BYTES = 32 * 1024 * 1024
_canon_args_cache_bytes = 0


def _canonicalize_tool_call_arguments(arg_str: str) -> str:
    """Return the deterministic wire form of a tool argument JSON string."""
    global _canon_args_cache_bytes
    cached = _CANON_ARGS_CACHE.get(arg_str)
    if cached is not None:
        return cached
    canonical = json.dumps(
        json.loads(arg_str), separators=(",", ":"), sort_keys=True
    )
    _CANON_ARGS_CACHE[arg_str] = canonical
    _canon_args_cache_bytes += len(arg_str) + len(canonical)
    while len(_CANON_ARGS_CACHE) > _CANON_ARGS_CACHE_MAX or (
        _canon_args_cache_bytes > _CANON_ARGS_CACHE_MAX_BYTES
        and len(_CANON_ARGS_CACHE) > 1
    ):
        try:
            key = next(iter(_CANON_ARGS_CACHE))
            value = _CANON_ARGS_CACHE.pop(key)
            _canon_args_cache_bytes -= len(key) + len(value)
        except (StopIteration, KeyError, RuntimeError):
            break
    return canonical


def _clone_message_for_send(msg):
    """Clone JSON containers while sharing immutable payload leaves."""
    if isinstance(msg, dict):
        return {
            key: _clone_message_for_send(value)
            if isinstance(value, (dict, list))
            else value
            for key, value in msg.items()
        }
    if isinstance(msg, list):
        return [
            _clone_message_for_send(value)
            if isinstance(value, (dict, list))
            else value
            for value in msg
        ]
    return msg


def _canonicalize_api_tool_calls(api_messages) -> None:
    """Canonicalize tool-call JSON on the isolated request copy."""
    for message in api_messages:
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            continue
        normalized = []
        for tool_call in tool_calls:
            if isinstance(tool_call, dict) and "function" in tool_call:
                try:
                    tool_call = {
                        **tool_call,
                        "function": {
                            **tool_call["function"],
                            "arguments": _canonicalize_tool_call_arguments(
                                tool_call["function"]["arguments"]
                            ),
                        },
                    }
                except Exception:
                    tool_call = {
                        **tool_call,
                        "function": {
                            **tool_call["function"],
                            "arguments": _repair_tool_call_arguments(
                                tool_call["function"]["arguments"],
                                tool_call["function"].get("name", "?"),
                            ),
                        },
                    }
            normalized.append(tool_call)
        message["tool_calls"] = normalized


def _apply_context_engine_selection(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    conversation_messages: List[Dict[str, Any]],
    incoming_message: Optional[Dict[str, Any]],
    *,
    logger: Any,
) -> List[Dict[str, Any]]:
    """Apply optional request-only context selection, failing open."""
    engine = getattr(agent, "context_compressor", None)
    if engine is None or not hasattr(engine, "select_context"):
        return api_messages

    try:
        from agent.context_engine import ContextEngine

        if (
            getattr(engine.select_context, "__func__", None)
            is ContextEngine.select_context
        ):
            return api_messages
    except Exception:
        pass

    conversation_copy = (
        [_clone_message_for_send(msg) for msg in conversation_messages]
        if conversation_messages is not None
        else None
    )
    incoming_copy = (
        _clone_message_for_send(incoming_message)
        if isinstance(incoming_message, dict)
        else incoming_message
    )
    session_label = getattr(agent, "session_id", None) or "-"
    try:
        selected = engine.select_context(
            api_messages,
            conversation_messages=conversation_copy,
            incoming_message=incoming_copy,
            budget_tokens=getattr(engine, "context_length", 0) or 0,
        )
    except Exception:
        logger.warning(
            "Context engine select_context hook failed; using unmodified "
            "request messages (session=%s)",
            session_label,
            exc_info=True,
        )
        return api_messages

    if selected is None:
        return api_messages
    if (
        isinstance(selected, list)
        and selected
        and all(isinstance(message, dict) for message in selected)
    ):
        return selected
    logger.warning(
        "Context engine select_context returned an invalid value "
        "(not a non-empty list of dicts); ignoring (session=%s)",
        session_label,
    )
    return api_messages


def _append_moa_context(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    original_user_message: Any,
    moa_config: Dict[str, Any],
    request_logger: logging.Logger,
) -> None:
    """Append ephemeral reference-model guidance to the latest user message."""
    try:
        from agent.message_content import flatten_message_text
        from agent.moa_loop import _preset_temperature, aggregate_moa_context

        context = aggregate_moa_context(
            user_prompt=(
                original_user_message
                if isinstance(original_user_message, str)
                else flatten_message_text(original_user_message)
            ),
            api_messages=api_messages,
            reference_models=moa_config.get("reference_models") or [],
            aggregator=moa_config.get("aggregator") or {},
            temperature=_preset_temperature(moa_config, "reference_temperature"),
            aggregator_temperature=_preset_temperature(
                moa_config, "aggregator_temperature"
            ),
            reference_max_tokens=moa_config.get("reference_max_tokens"),
            reference_timeout=(
                float(moa_config["reference_timeout"])
                if moa_config.get("reference_timeout")
                else None
            ),
            degraded_reference_policy=str(
                moa_config.get("degraded_reference_policy") or "loud"
            ),
            agent=agent,
        )
        if not context:
            return
        for message in reversed(api_messages):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                message["content"] = content + "\n\n" + context
            elif isinstance(content, list):
                message["content"] = [
                    *content,
                    {"type": "text", "text": "\n\n" + context},
                ]
            break
    except Exception as exc:
        request_logger.warning("MoA context aggregation failed: %s", exc)


def build_turn_request(
    agent: Any,
    messages: List[Dict[str, Any]],
    *,
    current_turn_user_idx: int,
    active_system_prompt: str,
    ext_prefetch_cache: str,
    plugin_user_context: str,
    original_user_message: Any,
    moa_config: Optional[Dict[str, Any]],
    request_logger: Optional[logging.Logger] = None,
) -> PreparedTurnRequest:
    """Create the API-only request projection for the current iteration."""
    loop_logger = request_logger or logger
    request_logger = getattr(agent, "logger", None) or loop_logger
    sanitize_cursor = getattr(agent, "_sanitize_args_cursor", None)
    if sanitize_cursor is None:
        sanitize_cursor = {}
        try:
            agent._sanitize_args_cursor = sanitize_cursor
        except Exception:
            pass
    repaired_tool_calls = agent._sanitize_tool_call_arguments(
        messages,
        logger=request_logger,
        session_id=agent.session_id,
        cursor=sanitize_cursor,
    )
    if repaired_tool_calls > 0:
        request_logger.info(
            "Sanitized %s corrupted tool_call arguments before request "
            "(session=%s)",
            repaired_tool_calls,
            agent.session_id or "-",
        )

    messages = [
        message
        for message in messages
        if not (
            message.get("display_kind") == "hidden"
            and message.get("role") == "assistant"
            and (
                (
                    isinstance(message.get("content"), str)
                    and message["content"].strip() == INTERRUPT_SCAFFOLD_MARKER
                )
                or (
                    isinstance(message.get("api_content"), str)
                    and message["api_content"].strip()
                    == INTERRUPT_SCAFFOLD_MARKER
                )
            )
        )
    ]

    from agent.agent_runtime_helpers import repair_message_sequence_with_cursor

    repaired_sequence = repair_message_sequence_with_cursor(agent, messages)
    if repaired_sequence > 0:
        request_logger.info(
            "Repaired %s message-alternation violations before request "
            "(session=%s)",
            repaired_sequence,
            agent.session_id or "-",
        )

    api_messages = []
    for index, message in enumerate(messages):
        api_message = _clone_message_for_send(message)
        api_content = api_message.pop("api_content", None)
        display_kind = api_message.pop("display_kind", None)
        api_message.pop("display_metadata", None)
        if (
            display_kind == "hidden"
            and api_message.get("role") == "assistant"
            and not api_content
            and not (api_message.get("content") or "").strip()
            and not api_message.get("tool_calls")
        ):
            from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER

            api_message["content"] = _INTERRUPTED_PLACEHOLDER
        api_message.pop("_row_id", None)

        if index == current_turn_user_idx and message.get("role") == "user":
            if isinstance(api_content, str) and api_content:
                api_message["content"] = api_content
            else:
                composed = compose_user_api_content(
                    api_message.get("content", ""),
                    ext_prefetch_cache,
                    plugin_user_context,
                )
                if composed is not None:
                    api_message["content"] = composed
        elif (
            isinstance(api_content, str)
            and api_content
            and message.get("role") in ("user", "assistant")
        ):
            api_message["content"] = api_content

        agent._copy_reasoning_content_for_api(message, api_message)
        api_message.pop("reasoning", None)
        api_message.pop("finish_reason", None)
        api_message.pop("_length_continuation_fragment", None)
        api_message.pop("_length_continuation_nudge", None)
        if agent._should_sanitize_tool_calls():
            sanitize_model = agent.model
            if agent.provider == "moa":
                aggregator = (moa_config or {}).get("aggregator") or {}
                if aggregator.get("model"):
                    sanitize_model = aggregator["model"]
                if sanitize_model == agent.model:
                    moa_client = getattr(agent, "client", None)
                    slot = getattr(moa_client, "last_aggregator_slot", None)
                    if slot and slot.get("model"):
                        sanitize_model = slot["model"]
            agent._sanitize_tool_calls_for_strict_api(
                api_message, model=sanitize_model
            )
        api_messages.append(api_message)

    effective_system = active_system_prompt or ""
    if agent.ephemeral_system_prompt:
        effective_system = (
            effective_system + "\n\n" + agent.ephemeral_system_prompt
        ).strip()
    if effective_system:
        api_messages = [
            {"role": "system", "content": effective_system},
            *api_messages,
        ]

    if moa_config:
        _append_moa_context(
            agent,
            api_messages,
            original_user_message,
            moa_config,
            loop_logger,
        )

    if agent.prefill_messages:
        system_offset = (
            1
            if api_messages and api_messages[0].get("role") == "system"
            else 0
        )
        for index, prefill in enumerate(agent.prefill_messages):
            api_messages.insert(
                system_offset + index, _clone_message_for_send(prefill)
            )

    incoming = (
        messages[current_turn_user_idx]
        if 0 <= current_turn_user_idx < len(messages)
        else None
    )
    api_messages = _apply_context_engine_selection(
        agent,
        api_messages,
        messages,
        incoming,
        logger=request_logger,
    )
    api_messages = agent._sanitize_api_messages(api_messages)
    api_messages = agent._drop_thinking_only_and_merge_users(
        api_messages,
        drop_codex_reasoning_items=agent.api_mode != "codex_responses",
    )
    for message in api_messages:
        if isinstance(message.get("content"), str):
            message["content"] = message["content"].strip()
    _canonicalize_api_tool_calls(api_messages)
    _sanitize_messages_surrogates(api_messages)

    tools_for_api = agent.tools
    if agent._use_prompt_caching and agent.provider != "moa":
        from agent.prompt_caching import (
            envelope_tool_part_cache_markers_supported,
        )

        static_system_prefix = getattr(
            agent, "_cached_system_prompt_static", None
        )
        cache_plan = build_prompt_cache_plan(
            api_messages,
            tools_for_api,
            cache_ttl=effective_cache_ttl(
                agent._cache_ttl,
                provider=agent.provider,
                model=agent.model,
            ),
            native_anthropic=agent._use_native_cache_layout,
            static_system_prefix=(
                static_system_prefix
                if isinstance(static_system_prefix, str)
                else None
            ),
            direct_native_tool_cache=(
                agent._direct_native_anthropic_tool_cache_capability()
            ),
            tool_part_markers=envelope_tool_part_cache_markers_supported(
                getattr(agent, "provider", ""),
                getattr(agent, "base_url", ""),
            ),
        )
        api_messages = cache_plan.messages
        tools_for_api = cache_plan.tools

    return PreparedTurnRequest(
        messages=messages,
        api_messages=api_messages,
        tools_for_api=tools_for_api,
    )


__all__ = ["PreparedTurnRequest", "build_turn_request"]
