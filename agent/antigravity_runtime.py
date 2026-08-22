"""Antigravity structured stream-json runtime for AIAgent.

The old implementation used ``agy -p`` text output and treated stdout as a
successful assistant response.  This runtime consumes Antigravity's structured
stream, projects tool events into Hermes' transcript, and fails closed when the
provider does not emit a terminal successful result.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.transports.antigravity_stream_json import (
    AntigravityStreamJsonSession,
)

logger = logging.getLogger(__name__)

# AGY source carrier was retired. Keep the provider module importable for
# rollback compatibility, but fail closed instead of launching a source binary.
AGY_BIN: Optional[str] = None


def _coerce_prompt_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: list[str] = []
        for part in user_message:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text") or part.get("content") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return str(user_message or "")


def _resolve_agy_model(model: Any) -> Optional[str]:
    """Map Hermes model slugs to valid agy model names."""
    if not model:
        return None
    value = str(model).strip()
    known_models = {
        "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
        "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
        "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
        "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
        "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
        "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
        "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
        "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
        "claude-sonnet-4-6": "Claude Sonnet 4.6 (Thinking)",
        "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
        "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
    }
    val_clean = value.split('/')[-1].lower()
    if val_clean in known_models:
        return known_models[val_clean]

    for exact_name in known_models.values():
        if value.lower() == exact_name.lower():
            return exact_name

    if "3.6" in val_clean:
        return "Gemini 3.6 Flash (Medium)"
    if "3.5" in val_clean:
        return "Gemini 3.5 Flash (Medium)"
    if "pro" in val_clean:
        return "Gemini 3.1 Pro (High)"
    if "flash" in val_clean:
        return "Gemini 3.6 Flash (Medium)"
    if "sonnet" in val_clean:
        return "Claude Sonnet 4.6 (Thinking)"
    if "opus" in val_clean:
        return "Claude Opus 4.6 (Thinking)"
    if "gpt" in val_clean:
        return "GPT-OSS 120B (Medium)"

    return value


def _make_event_bridge(agent: Any):
    """Project structured Antigravity events into Hermes live callbacks."""

    def emit_stream_delta(text: str) -> None:
        callback = getattr(agent, "_fire_stream_delta", None)
        if callback is not None and text:
            try:
                callback(text)
            except Exception:
                logger.debug("Antigravity stream callback failed", exc_info=True)

    def emit_tool_progress(
        phase: str,
        name: str,
        preview: Any = None,
        args: Any = None,
        **kwargs: Any,
    ) -> None:
        callback = getattr(agent, "tool_progress_callback", None)
        if callback is not None:
            try:
                callback(phase, name, preview, args, **kwargs)
            except Exception:
                logger.debug("Antigravity tool progress callback failed", exc_info=True)

    def on_event(event: dict) -> None:
        if not isinstance(event, dict) or event.get("event") != "step_update":
            return
        step = event.get("step_update") or {}
        if not isinstance(step, dict):
            return
        step_type = step.get("step_type") or ""
        if step_type == "agent_response":
            emit_stream_delta(str(step.get("text_delta") or ""))
            return
        if step_type == "checkpoint":
            # This is an observed provider checkpoint event, not proof that a
            # Hermes checkpoint carrier was mutated. Keep it visible but avoid
            # the false "checkpoint complete" claim.
            status = getattr(agent, "_emit_status", None)
            if status is not None:
                try:
                    status("Antigravity checkpoint event observed; Hermes carrier verification pending")
                except Exception:
                    logger.debug("Antigravity checkpoint status callback failed", exc_info=True)
            return
        if step_type != "tool":
            return

        info = step.get("tool_info") or {}
        if not isinstance(info, dict):
            info = {}
        name = str(step.get("tool_name") or info.get("name") or "unknown")
        args = info.get("parameters") or {}
        state = step.get("state") or ""
        if state == "ACTIVE":
            emit_tool_progress("tool.started", name, None, args)
        elif state == "DONE":
            result = info.get("output")
            if result is None:
                result = step.get("output", "")
            emit_tool_progress(
                "tool.completed",
                name,
                None,
                None,
                duration=step.get("duration_seconds"),
                is_error=False,
                result=result,
            )

    return on_event


def _flush_projected_messages(agent: Any, messages: list[dict]) -> None:
    if getattr(agent, "_session_db", None) is None:
        return
    try:
        agent._flush_messages_to_session_db(messages)
    except Exception:
        logger.debug("Antigravity projected-message flush failed", exc_info=True)


def _coerce_usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _persist_antigravity_usage(
    agent: Any,
    *,
    usage: Any = None,
    api_call_count: int = 1,
    cost_result: Any = None,
) -> None:
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        return

    try:
        if not getattr(agent, "_session_db_created", False):
            ensure_db_session = getattr(agent, "_ensure_db_session", None)
            if ensure_db_session is not None:
                ensure_db_session()

        kwargs: dict[str, Any] = {
            "model": getattr(agent, "model", None),
            "billing_provider": getattr(agent, "provider", None),
            "billing_base_url": getattr(agent, "base_url", None),
            "billing_mode": "subscription_included",
            "api_call_count": api_call_count,
        }
        if usage is not None:
            kwargs.update({
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            })
            if cost_result is not None:
                kwargs.update({
                    "estimated_cost_usd": (
                        float(cost_result.amount_usd)
                        if cost_result.amount_usd is not None
                        else None
                    ),
                    "cost_status": cost_result.status,
                    "cost_source": cost_result.source,
                    "pricing_version": cost_result.pricing_version,
                })
        session_db.update_token_counts(session_id, **kwargs)
    except Exception as exc:
        logger.debug(
            "Antigravity token persistence failed (session=%s): %s",
            session_id,
            exc,
        )


def _record_antigravity_usage(agent: Any, raw_usage: Any) -> dict[str, Any]:
    """Record one Antigravity turn in Hermes accounting without overclaiming.

    Antigravity's terminal usage is a plain mapping rather than the typed usage
    object accepted by ``normalize_usage``. Convert the documented flat fields
    explicitly and keep missing usage distinct from a measured zero.
    """
    agent.session_api_calls = getattr(agent, "session_api_calls", 0) + 1
    if not isinstance(raw_usage, dict) or not raw_usage:
        compressor = getattr(agent, "context_compressor", None)
        if compressor is not None and getattr(
            compressor, "awaiting_real_usage_after_compression", False
        ):
            compressor.update_from_response({})
        _persist_antigravity_usage(agent, api_call_count=1)
        return {}

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    input_tokens = _coerce_usage_int(
        raw_usage.get("input_tokens", raw_usage.get("prompt_tokens"))
    )
    output_tokens = _coerce_usage_int(
        raw_usage.get("output_tokens", raw_usage.get("completion_tokens"))
    )
    cache_read_tokens = _coerce_usage_int(
        raw_usage.get("cache_read_tokens", raw_usage.get("cache_read_input_tokens"))
    )
    cache_write_tokens = _coerce_usage_int(
        raw_usage.get(
            "cache_write_tokens", raw_usage.get("cache_creation_input_tokens")
        )
    )
    reasoning_tokens = _coerce_usage_int(raw_usage.get("reasoning_tokens"))
    reported_total = _coerce_usage_int(raw_usage.get("total_tokens"))

    canonical_usage = CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        raw_usage=dict(raw_usage),
    )
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = reported_total or canonical_usage.total_tokens
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": canonical_usage.reasoning_tokens,
    }

    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        try:
            compressor.update_from_response(usage_dict)
        except Exception:
            logger.debug("Antigravity usage context update failed", exc_info=True)

    agent.session_prompt_tokens = (
        getattr(agent, "session_prompt_tokens", 0) + prompt_tokens
    )
    agent.session_completion_tokens = (
        getattr(agent, "session_completion_tokens", 0) + completion_tokens
    )
    agent.session_total_tokens = (
        getattr(agent, "session_total_tokens", 0) + total_tokens
    )
    agent.session_input_tokens = (
        getattr(agent, "session_input_tokens", 0) + canonical_usage.input_tokens
    )
    agent.session_output_tokens = (
        getattr(agent, "session_output_tokens", 0) + canonical_usage.output_tokens
    )
    agent.session_cache_read_tokens = (
        getattr(agent, "session_cache_read_tokens", 0)
        + canonical_usage.cache_read_tokens
    )
    agent.session_cache_write_tokens = (
        getattr(agent, "session_cache_write_tokens", 0)
        + canonical_usage.cache_write_tokens
    )
    agent.session_reasoning_tokens = (
        getattr(agent, "session_reasoning_tokens", 0) + canonical_usage.reasoning_tokens
    )

    cost_result = estimate_usage_cost(
        getattr(agent, "model", "") or "",
        canonical_usage,
        provider=getattr(agent, "provider", None),
        base_url=getattr(agent, "base_url", None),
        api_key=getattr(agent, "api_key", ""),
    )
    if cost_result.amount_usd is not None:
        agent.session_estimated_cost_usd = getattr(
            agent, "session_estimated_cost_usd", 0.0
        ) + float(cost_result.amount_usd)
    agent.session_cost_status = cost_result.status
    agent.session_cost_source = cost_result.source
    _persist_antigravity_usage(
        agent,
        usage=canonical_usage,
        api_call_count=1,
        cost_result=cost_result,
    )
    return usage_dict


def run_antigravity_mcp_turn(
    agent: Any,
    user_message: Any,
    original_user_message: Any = None,
    messages: List[Dict[str, Any]] = None,
    effective_task_id: Optional[str] = None,
    should_review_memory: Any = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one Antigravity structured turn and project it into Hermes."""
    if messages is None:
        messages = getattr(agent, "messages", [])

    if AGY_BIN is None:
        return {
            "final_response": "Antigravity provider is retired on this source host",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "error": "AGY source carrier retired",
            "agent_persisted": False,
        }

    if not hasattr(agent, "_antigravity_stream_session") or agent._antigravity_stream_session is None:
        cwd = getattr(agent, "session_cwd", None) or "/home/admin/antigravity-bot/workspace"
        agent._antigravity_stream_session = AntigravityStreamJsonSession(
            cwd=cwd,
            agy_bin=AGY_BIN,
            model=_resolve_agy_model(getattr(agent, "model", None)),
            on_event=_make_event_bridge(agent),
        )

    prompt_text = _coerce_prompt_text(user_message)
    try:
        turn = agent._antigravity_stream_session.run_turn(
            prompt_text,
            system_prompt=system_prompt,
        )
    except Exception as exc:
        logger.exception("Antigravity stream turn failed")
        try:
            agent._antigravity_stream_session.close()
        except Exception:
            pass
        agent._antigravity_stream_session = None
        return {
            "final_response": f"Antigravity structured turn failed: {exc}",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "error": str(exc),
            "agent_persisted": False,
        }

    if turn.projected_messages:
        messages.extend(turn.projected_messages)
        _flush_projected_messages(agent, messages)

    if not turn.completed:
        error = turn.error or "Antigravity stream did not complete"
        logger.warning("Antigravity turn incomplete: %s", error)
        return {
            "final_response": f"Antigravity turn incomplete: {error}",
            "messages": messages,
            "api_calls": 1,
            "completed": False,
            "partial": True,
            "error": error,
            "agent_persisted": bool(turn.projected_messages),
            "antigravity_conversation_id": turn.conversation_id,
            "antigravity_checkpoint_events": turn.checkpoint_events,
        }

    accounted_usage = _record_antigravity_usage(agent, turn.usage)

    agent._iters_since_skill = getattr(agent, "_iters_since_skill", 0) + turn.tool_iterations
    should_review_skills = False
    if (
        getattr(agent, "_skill_nudge_interval", 0) > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in getattr(agent, "valid_tool_names", [])
    ):
        should_review_skills = True
        agent._iters_since_skill = 0

    if not getattr(turn, "error", None):
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
                messages=messages,
            )
        except Exception:
            logger.debug("Antigravity external memory sync raised", exc_info=True)

    if (
        turn.final_text
        and (should_review_memory or should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=bool(should_review_memory),
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("Antigravity background review spawn raised", exc_info=True)

    usage = accounted_usage
    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": True,
        "partial": False,
        "error": None,
        "agent_persisted": bool(turn.projected_messages),
        "antigravity_conversation_id": turn.conversation_id,
        "antigravity_checkpoint_events": turn.checkpoint_events,
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
