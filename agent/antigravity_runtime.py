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

AGY_BIN = "/home/admin/.local/bin/agy"


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
    """Map Hermes model slugs to the documented agy model families."""
    value = str(model or "").lower()
    if "pro" in value:
        return "pro"
    if "flash" in value:
        return "flash"
    return None


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

    usage = turn.usage or {}
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
