"""API-server agent execution helper.

Keeps request-scoped context binding out of the large API adapter file.
"""

from __future__ import annotations

import uuid
from typing import Any

from gateway.session_acl import has_principal_scope
from gateway.session_scope_store import bind_session_scope, inherit_or_bind_session_scope


def run_agent_sync(
    adapter: Any,
    *,
    user_message: Any,
    conversation_history: list[dict[str, Any]],
    ephemeral_system_prompt: str | None = None,
    session_id: str | None = None,
    stream_delta_callback: Any = None,
    tool_progress_callback: Any = None,
    tool_start_callback: Any = None,
    tool_complete_callback: Any = None,
    agent_ref: list | None = None,
    gateway_session_key: str | None = None,
    principal_scope: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    from gateway.session_context import clear_session_vars, set_session_vars

    scope = principal_scope or {}
    tokens = set_session_vars(
        platform="api_server",
        chat_id=session_id or "",
        session_key=gateway_session_key or session_id or "",
        session_id=session_id or "",
        tenant_id=str(scope.get("tenant_id") or ""),
        workspace_id=str(scope.get("workspace_id") or ""),
        project_id=str(scope.get("project_id") or ""),
        user_id=str(scope.get("user_id") or ""),
        roles=scope.get("roles"),
        sandbox_id=str(scope.get("sandbox_id") or ""),
    )
    try:
        agent = adapter._create_agent(
            ephemeral_system_prompt=ephemeral_system_prompt,
            session_id=session_id,
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            gateway_session_key=gateway_session_key,
        )
        if agent_ref is not None:
            agent_ref[0] = agent
        effective_task_id = session_id or str(uuid.uuid4())
        result = agent.run_conversation(
            user_message=user_message,
            conversation_history=conversation_history,
            task_id=effective_task_id,
        )
        usage = {
            "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
            "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
            "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
        }
        effective_session_id = getattr(agent, "session_id", session_id)
        if isinstance(result, dict) and isinstance(effective_session_id, str) and effective_session_id:
            result["session_id"] = effective_session_id
        if has_principal_scope(scope):
            db = adapter._ensure_session_db()
            if db is None:
                raise RuntimeError("Session database unavailable for scoped session binding")
            if session_id:
                bind_session_scope(db, session_id, scope)
            if effective_session_id and effective_session_id != session_id:
                inherit_or_bind_session_scope(
                    db,
                    effective_session_id,
                    scope=scope,
                    parent_session_id=session_id,
                )
        return result, usage
    finally:
        clear_session_vars(tokens)
