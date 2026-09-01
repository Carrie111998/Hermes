"""Advance a conversation after one already-dispatched tool-call round."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from agent.conversation_compression import (
    compression_skipped_due_to_lock,
    conversation_history_after_compression,
)
from agent.model_metadata import estimate_request_tokens_rough

logger = logging.getLogger(__name__)


@dataclass
class PostToolAdvance:
    """State returned to the loop after deterministic post-tool maintenance."""

    messages: list[dict]
    conversation_history: list[dict]
    active_system_prompt: str
    compression_attempts: int
    final_response: str
    exit_reason: str | None = None


def advance_after_tool_execution(
    agent: Any,
    tool_calls: list[Any],
    messages: list[dict],
    conversation_history: list[dict],
    *,
    system_message: dict,
    active_system_prompt: str,
    user_message: str,
    task_id: str,
    api_call_count: int,
    compression_attempts: int,
    max_compression_attempts: int,
    final_response: str,
    should_skip_handoff: Callable[[list[dict], str], bool],
    handoff_final_response: str,
) -> PostToolAdvance:
    """Maintain context and schedule the next model round after tool results."""
    agent._stream_needs_break = True
    if {call.function.name for call in tool_calls} == {"execute_code"}:
        agent.iteration_budget.refund()

    compressor = agent.context_compressor
    if compressor.last_prompt_tokens > 0:
        real_tokens = compressor.last_prompt_tokens
    elif compressor.last_prompt_tokens == -1:
        real_tokens = 0
    else:
        real_tokens = estimate_request_tokens_rough(messages, tools=agent.tools or None)

    exit_reason = None
    if (
        agent.compression_enabled
        and compression_attempts < max_compression_attempts
        and compressor.should_compress(real_tokens)
    ):
        compression_attempts += 1
        clear_warn = getattr(agent, "_clear_context_overflow_warn", None)
        if callable(clear_warn):
            clear_warn()
        agent._safe_print("  ⟳ compacting context…")
        before_compression = messages
        messages, active_system_prompt = agent._compress_context(
            messages,
            system_message,
            approx_tokens=real_tokens,
            task_id=task_id,
        )
        if messages is before_compression and compression_skipped_due_to_lock(agent):
            compression_attempts -= 1
        else:
            conversation_history = conversation_history_after_compression(
                agent, messages, conversation_history
            )
            if should_skip_handoff(messages, user_message):
                logger.info("Skipping post-tool compaction model call: reference-only handoff")
                if not final_response:
                    final_response = handoff_final_response
                exit_reason = "compaction_handoff_not_actionable"
    elif agent.compression_enabled:
        _warn_when_compression_is_blocked(agent, compressor, real_tokens)
        messages = _prune_tool_results(compressor, messages, real_tokens)

    if exit_reason is None:
        agent._session_messages = messages
        agent._touch_activity(
            f"tool results posted, continuing iteration #{api_call_count}"
        )
    return PostToolAdvance(
        messages,
        conversation_history,
        active_system_prompt,
        compression_attempts,
        final_response,
        exit_reason,
    )


def _warn_when_compression_is_blocked(agent: Any, compressor: Any, tokens: int) -> None:
    reason = None
    info = getattr(compressor, "should_compress_info", None)
    if info is not None:
        try:
            reason = info(tokens)[1]
        except Exception:
            reason = None
    if reason:
        agent._warn_context_overflow_blocked(
            reason, tokens, int(getattr(compressor, "threshold_tokens", 0) or 0)
        )


def _prune_tool_results(
    compressor: Any, messages: list[dict], tokens: int
) -> list[dict]:
    prune = getattr(compressor, "prune_tool_results_only", None)
    if not callable(prune):
        return messages
    try:
        pruned, count = prune(messages, current_tokens=tokens)
    except Exception:
        logger.debug("proactive tool-result prune failed; skipping", exc_info=True)
        return messages
    return pruned if count and pruned is not messages else messages
