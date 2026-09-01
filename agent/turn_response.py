"""Normalize one provider response before the conversation loop advances it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from agent.provider_projection import splice_provider_projection


@dataclass
class NormalizedTurnResponse:
    """Provider-neutral assistant message plus its normalized finish reason."""

    assistant_message: Any
    finish_reason: str


def normalize_turn_response(
    agent: Any,
    response: Any,
    messages: List[Dict[str, Any]],
    *,
    task_id: str,
    turn_id: str,
    api_request_id: str,
    api_call_count: int,
    api_start_time: float,
    api_duration: float,
    api_message_count: int,
    moa_references: Any,
) -> NormalizedTurnResponse:
    """Normalize response content, project agent providers, and notify hooks.

    No retry, tool dispatch, persistence, or final-response decision belongs
    here.  Exceptions from the transport deliberately propagate to the caller
    so its established turn-level error handling remains authoritative.
    """
    transport = agent._get_transport()
    normalize_kwargs = {}
    if agent.api_mode == "anthropic_messages":
        normalize_kwargs["strip_tool_prefix"] = agent._is_anthropic_oauth
    assistant_message = transport.normalize_response(response, **normalize_kwargs)
    finish_reason = assistant_message.finish_reason
    _normalize_content(assistant_message)
    splice_provider_projection(agent, response, messages)
    _emit_post_api_request_hook(
        agent,
        response,
        assistant_message,
        finish_reason=finish_reason,
        task_id=task_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        api_call_count=api_call_count,
        api_start_time=api_start_time,
        api_duration=api_duration,
        api_message_count=api_message_count,
        moa_references=moa_references,
    )
    return NormalizedTurnResponse(assistant_message, finish_reason)


def _normalize_content(assistant_message: Any) -> None:
    """Coerce nonstandard provider content into the string expected downstream."""
    content = getattr(assistant_message, "content", None)
    if content is None or isinstance(content, str):
        return
    if isinstance(content, dict):
        assistant_message.content = (
            content.get("text", "") or content.get("content", "") or json.dumps(content)
        )
        return
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(str(part["text"]))
        assistant_message.content = "\n".join(text_parts)
        return
    assistant_message.content = str(content)


def _emit_post_api_request_hook(
    agent: Any,
    response: Any,
    assistant_message: Any,
    *,
    finish_reason: str,
    task_id: str,
    turn_id: str,
    api_request_id: str,
    api_call_count: int,
    api_start_time: float,
    api_duration: float,
    api_message_count: int,
    moa_references: Any,
) -> None:
    """Emit the best-effort observability hook without affecting delivery."""
    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook

        if not has_hook("post_api_request"):
            return
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        content = assistant_message.content or ""
        invoke_hook(
            "post_api_request",
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            session_id=agent.session_id or "",
            platform=agent.platform or "",
            model=agent.model,
            provider=agent.provider,
            base_url=agent.base_url,
            api_mode=agent.api_mode,
            api_call_count=api_call_count,
            api_duration=api_duration,
            started_at=api_start_time,
            ended_at=api_start_time + api_duration,
            finish_reason=finish_reason,
            message_count=api_message_count,
            response_model=getattr(response, "model", None),
            response=agent._api_response_payload_for_hook(
                response, assistant_message, finish_reason=finish_reason
            ),
            usage=agent._usage_summary_for_api_request_hook(response),
            assistant_message=assistant_message,
            assistant_content_chars=len(content),
            assistant_tool_call_count=len(tool_calls),
            moa_references=moa_references,
        )
    except Exception:
        pass
