"""First-class Hermes cross-profile handoff tools.

GXTD-390 introduces an explicit handoff contract so work routed from one
profile to another always has a durable return path.  This module implements
that contract's durable Kanban-backed mode first; the immediate profile-session
mode is intentionally fail-closed until the runtime has a session-spawn API that
can return a live target-session link.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from tools.registry import registry, tool_error


def _ok(**fields: Any) -> str:
    payload = {"success": True}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return tool_error(message, success=False)


def _current_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return os.environ.get("HERMES_PROFILE") or get_active_profile_name() or "default"
    except Exception:
        return os.environ.get("HERMES_PROFILE") or "default"


def _normalize_target_profile(raw: Any) -> str:
    if not raw:
        raise ValueError("target_profile is required")
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    target = normalize_profile_name(str(raw))
    if not profile_exists(target):
        raise ValueError(f"target profile {target!r} does not exist")
    return target


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _session_context_value(name: str, default: str = "") -> str:
    try:
        from gateway.session_context import get_session_env

        value = get_session_env(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return str(os.environ.get(name, default) or default)


def _resolve_origin_delivery(origin_delivery: Any, origin_profile: str) -> dict[str, Any]:
    """Return a concrete callback destination or raise before task creation.

    A handoff_task without a callback is just the old bridge-artifact failure in
    prettier shoes.  Prefer explicit origin_delivery when provided; otherwise
    bind to the current gateway/TUI session environment.  Plain CLI/cron/test
    invocations usually have no durable return channel and are rejected unless
    the caller supplies origin_delivery explicitly.
    """

    explicit = _coerce_mapping(origin_delivery)
    if explicit:
        platform = str(explicit.get("platform") or "").strip()
        chat_id = str(explicit.get("chat_id") or "").strip()
        if not platform or not chat_id:
            raise ValueError("origin_delivery requires non-empty platform and chat_id")
        metadata = _coerce_mapping(explicit.get("metadata"))
        thread_id = str(explicit.get("thread_id") or metadata.get("thread_id") or "").strip()
        chat_type = str(explicit.get("chat_type") or metadata.get("chat_type") or "dm").strip()
        delivery_mode = str(explicit.get("delivery_mode") or "").strip()
        if not delivery_mode:
            delivery_mode = "notify" if platform == "tui" else "notify+wake"
        return {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "user_id": str(explicit.get("user_id") or "").strip() or None,
            "user_id_alt": str(explicit.get("user_id_alt") or "").strip() or None,
            "chat_type": chat_type,
            "notifier_profile": str(explicit.get("notifier_profile") or origin_profile).strip(),
            "delivery_mode": delivery_mode,
            "delivery_metadata": metadata or None,
        }

    platform = _session_context_value("HERMES_SESSION_PLATFORM")
    chat_id = _session_context_value("HERMES_SESSION_CHAT_ID")
    if not platform or not chat_id:
        session_key = _session_context_value("HERMES_SESSION_KEY")
        if not session_key:
            raise ValueError(
                "handoff_task requires origin_delivery or an active gateway/TUI "
                "session; refusing to create a task with no callback path"
            )
        platform = "tui"
        chat_id = session_key

    thread_id = _session_context_value("HERMES_SESSION_THREAD_ID")
    chat_type = _session_context_value("HERMES_SESSION_CHAT_TYPE", "dm") or "dm"
    message_id = _session_context_value("HERMES_SESSION_MESSAGE_ID")
    delivery_metadata: dict[str, Any] = {}
    if thread_id:
        delivery_metadata["thread_id"] = thread_id
    if chat_type:
        delivery_metadata["chat_type"] = chat_type
    if (
        platform.lower() == "telegram"
        and thread_id
        and chat_type.lower() in {"dm", "direct", "private"}
    ):
        delivery_metadata["telegram_dm_topic_reply_fallback"] = True
        if thread_id != "1":
            delivery_metadata["direct_messages_topic_id"] = thread_id
        if message_id:
            delivery_metadata["telegram_reply_to_message_id"] = message_id

    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "user_id": _session_context_value("HERMES_SESSION_USER_ID") or None,
        "user_id_alt": _session_context_value("HERMES_SESSION_USER_ID_ALT") or None,
        "chat_type": chat_type,
        "notifier_profile": _session_context_value("HERMES_SESSION_PROFILE") or origin_profile,
        "delivery_mode": "notify" if platform == "tui" else "notify+wake",
        "delivery_metadata": delivery_metadata or None,
    }


def _origin_session_id(args: Mapping[str, Any], kw: Mapping[str, Any]) -> Optional[str]:
    raw = args.get("origin_session_id") or args.get("session_id") or kw.get("session_id")
    if raw:
        return str(raw)
    try:
        from tools.async_delegation import _current_origin_session_id

        raw = _current_origin_session_id()
        if raw:
            return str(raw)
    except Exception:
        pass
    raw = os.environ.get("HERMES_SESSION_ID")
    return str(raw) if raw else None


def _format_handoff_body(
    *,
    prompt: str,
    body: Optional[str],
    target_profile: str,
    origin_profile: str,
    origin_session_id: Optional[str],
    source_context_policy: str,
) -> str:
    metadata = {
        "mode": "handoff_task",
        "target_profile": target_profile,
        "origin_profile": origin_profile,
        "origin_session_id": origin_session_id,
        "source_context_policy": source_context_policy,
        "callback_required": True,
        "callback_terminal_events": ["completed", "blocked", "spawn_auto_blocked"],
    }
    chunks = [
        "# Hermes handoff_task",
        "",
        "```json",
        json.dumps(metadata, indent=2, sort_keys=True),
        "```",
        "",
        "## Handoff prompt",
        prompt.strip(),
    ]
    if body and str(body).strip():
        chunks.extend(["", "## Additional context", str(body).strip()])
    return "\n".join(chunks).strip() + "\n"


def _handle_handoff_task(args: dict, **kw: Any) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _err("prompt is required")
    try:
        target_profile = _normalize_target_profile(args.get("target_profile"))
        origin_profile = str(args.get("origin_profile") or _current_profile()).strip() or "default"
        origin_session_id = _origin_session_id(args, kw)
        origin_delivery = _resolve_origin_delivery(args.get("origin_delivery"), origin_profile)
    except ValueError as exc:
        return _err(str(exc))

    title = str(args.get("title") or "").strip()
    if not title:
        first_line = prompt.splitlines()[0].strip() if prompt.splitlines() else "handoff"
        title = f"Handoff to {target_profile}: {first_line[:80]}"
    source_context_policy = str(args.get("source_context_policy") or "summary").strip() or "summary"
    handoff_body = _format_handoff_body(
        prompt=prompt,
        body=args.get("body"),
        target_profile=target_profile,
        origin_profile=origin_profile,
        origin_session_id=origin_session_id,
        source_context_policy=source_context_policy,
    )

    parents = args.get("parents") or []
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return _err("parents must be a list of task ids")

    try:
        from hermes_cli import kanban_db as kb

        board = args.get("board")
        conn = kb.connect(board=board)
        try:
            task_id = kb.create_task(
                conn,
                title=title,
                body=handoff_body,
                assignee=target_profile,
                created_by=origin_profile,
                parents=tuple(str(parent) for parent in parents if parent),
                tenant=args.get("tenant") or os.environ.get("HERMES_TENANT"),
                priority=int(args.get("priority") or 0),
                workspace_kind=str(args.get("workspace_kind") or "scratch"),
                workspace_path=args.get("workspace_path"),
                project_id=args.get("project") or args.get("project_id"),
                triage=bool(args.get("triage") or False),
                idempotency_key=args.get("idempotency_key"),
                max_runtime_seconds=(
                    int(args["max_runtime_seconds"])
                    if args.get("max_runtime_seconds") is not None
                    else None
                ),
                skills=args.get("skills"),
                model_override=args.get("model"),
                provider_override=args.get("provider"),
                goal_mode=bool(args.get("goal_mode") or False),
                goal_max_turns=(
                    int(args["goal_max_turns"])
                    if args.get("goal_max_turns") is not None
                    else None
                ),
                initial_status=str(args.get("initial_status") or "running"),
                session_id=origin_session_id,
                board=board,
            )
            try:
                kb.add_notify_sub(conn, task_id=task_id, **origin_delivery)
            except Exception:
                # Fail closed: if the callback cannot be stored, remove the
                # just-created task so the handoff cannot silently strand work.
                try:
                    kb.delete_task(conn, task_id)
                finally:
                    raise
            task = kb.get_task(conn, task_id)
            return _ok(
                mode="handoff_task",
                task_id=task_id,
                status=task.status if task else None,
                target_profile=target_profile,
                origin_profile=origin_profile,
                origin_session_id=origin_session_id,
                callback_registered=True,
                callback_platform=origin_delivery["platform"],
                callback_delivery_mode=origin_delivery["delivery_mode"],
            )
        finally:
            conn.close()
    except Exception as exc:
        return _err(f"handoff_task: {exc}")


def _handle_handoff_profile(args: dict, **kw: Any) -> str:
    try:
        target_profile = _normalize_target_profile(args.get("target_profile"))
    except ValueError as exc:
        return _err(str(exc))
    return _err(
        "handoff_profile is contract-defined but this runtime has no safe "
        f"target-session spawn API wired yet for {target_profile!r}; use "
        "mode='handoff_task' for durable execution, or implement the session "
        "spawn/link slice before claiming immediate profile handoff support"
    )


def _handle_handoff(args: dict, **kw: Any) -> str:
    mode = str(args.get("mode") or "handoff_task").strip()
    if mode == "handoff_task":
        return _handle_handoff_task(args, **kw)
    if mode == "handoff_profile":
        return _handle_handoff_profile(args, **kw)
    return _err("mode must be one of: handoff_task, handoff_profile")


HANDOFF_SCHEMA = {
    "name": "handoff",
    "description": (
        "Create a first-class Hermes cross-profile handoff. "
        "mode='handoff_task' creates a durable Kanban task assigned to the "
        "target profile and registers a mandatory origin callback for terminal "
        "events. mode='handoff_profile' is reserved for immediate target-profile "
        "sessions and currently fails closed until a safe session-spawn backend "
        "is wired."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["handoff_task", "handoff_profile"],
                "description": "Handoff mode. Defaults to handoff_task.",
            },
            "target_profile": {
                "type": "string",
                "description": "Hermes profile that should receive or execute the handoff.",
            },
            "prompt": {
                "type": "string",
                "description": "Sanitized prompt/instructions for the target profile.",
            },
            "title": {"type": "string", "description": "Optional Kanban task title."},
            "body": {"type": "string", "description": "Optional additional context appended to the task body."},
            "origin_profile": {"type": "string", "description": "Originating Hermes profile. Defaults to the active profile."},
            "origin_session_id": {"type": "string", "description": "Origin session id for callback/wake context."},
            "origin_delivery": {
                "type": "object",
                "description": (
                    "Explicit callback destination. Required when the current "
                    "session has no gateway/TUI delivery context. Must include "
                    "platform and chat_id; may include thread_id, user_id, "
                    "user_id_alt, chat_type, notifier_profile, delivery_mode, "
                    "and metadata."
                ),
                "properties": {
                    "platform": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "user_id_alt": {"type": "string"},
                    "chat_type": {"type": "string"},
                    "notifier_profile": {"type": "string"},
                    "delivery_mode": {"type": "string", "enum": ["notify", "notify+wake", "wake"]},
                    "metadata": {"type": "object"},
                },
            },
            "source_context_policy": {
                "type": "string",
                "enum": ["summary", "current_turn", "full_allowed"],
                "description": "How much source context was included in the handoff prompt.",
            },
            "parents": {"type": "array", "items": {"type": "string"}},
            "tenant": {"type": "string"},
            "priority": {"type": "integer"},
            "workspace_kind": {"type": "string", "enum": ["scratch", "dir", "worktree"]},
            "workspace_path": {"type": "string"},
            "project": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "max_runtime_seconds": {"type": "integer"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "goal_mode": {"type": "boolean"},
            "goal_max_turns": {"type": "integer"},
            "model": {"type": "string"},
            "provider": {"type": "string"},
            "initial_status": {"type": "string", "enum": ["running", "blocked"]},
            "board": {"type": "string"},
        },
        "required": ["target_profile", "prompt"],
    },
}


registry.register(
    name="handoff",
    toolset="handoff",
    schema=HANDOFF_SCHEMA,
    handler=_handle_handoff,
    emoji="🔀",
)
