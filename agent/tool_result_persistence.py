"""Persistence-boundary helpers for tool results and session artifacts.

The model-facing message history remains untouched.  These helpers are used
only when a value crosses into a durable snapshot, trajectory JSONL file, or a
user-facing persistence diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from agent.redact import redact_sensitive_text
from agent.tool_dispatch_helpers import _trajectory_normalize_msg
from agent.trajectory import convert_scratchpad_to_think
from tools.tool_result_sanitization import (
    sanitize_tool_result_for_sink,
    sanitize_tool_result_projection_for_sink,
)
from utils import atomic_json_write

logger = logging.getLogger(__name__)


_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",
    "_dropped_toolcall_nudge",
)


def is_ephemeral_scaffolding(msg: Any) -> bool:
    """Return whether a message is internal retry scaffolding."""
    return isinstance(msg, dict) and any(
        msg.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS
    )


def safe_session_filename_component(session_id: str) -> str:
    """Return a stable, traversal-free filename component for a session ID."""
    raw = str(session_id or "").strip()
    sanitized = re.sub(r"[^\w-]", "_", raw).strip("._")
    sanitized = sanitized[:96] or "session"
    if raw and sanitized == raw:
        return sanitized
    digest = hashlib.sha256(
        raw.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    return f"{sanitized}_{digest}"


def sanitize_tool_message_value(value: Any) -> Any:
    """Sanitize a tool message field while preserving structured shape."""
    return sanitize_tool_result_projection_for_sink(value)


def sanitize_trajectory_tool_value(value: Any) -> str:
    """Return tool content safe to embed in a trajectory XML response."""
    return sanitize_tool_result_for_sink(value)


def sanitize_trajectory_for_sink(
    trajectory: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Copy a trajectory and sanitize every persisted content value."""
    safe_trajectory = []
    for row in trajectory:
        safe_row = dict(row)
        if safe_row.get("from") in {"gpt", "tool"} and "value" in safe_row:
            safe_row["value"] = sanitize_tool_result_for_sink(safe_row["value"])
        safe_trajectory.append(safe_row)
    return safe_trajectory


def convert_to_trajectory_format(
    agent: Any,
    messages: List[Dict[str, Any]],
    user_query: str,
    completed: bool,
) -> List[Dict[str, Any]]:
    """Convert internal messages to a sink-safe trajectory representation."""
    messages = [_trajectory_normalize_msg(m) for m in messages]
    trajectory = []
    system_msg = (
        "You are a function calling AI model. You are provided with function signatures within <tools> </tools> XML tags. "
        "You may call one or more functions to assist with the user query. If available tools are not relevant in assisting "
        "with user query, just respond in natural conversational language. Don't make assumptions about what values to plug "
        "into functions. After calling & executing the functions, you will be provided with function results within "
        "<tool_response> </tool_response> XML tags. Here are the available tools:\n"
        f"<tools>\n{agent._format_tools_for_system_message()}\n</tools>\n"
        "For each function call return a JSON object, with the following pydantic model json schema for each:\n"
        "{'title': 'FunctionCall', 'type': 'object', 'properties': {'name': {'title': 'Name', 'type': 'string'}, "
        "'arguments': {'title': 'Arguments', 'type': 'object'}}, 'required': ['name', 'arguments']}\n"
        "Each function call should be enclosed within <tool_call> </tool_call> XML tags.\n"
        "Example:\n<tool_call>\n{'name': <function-name>,'arguments': <args-dict>}\n</tool_call>"
    )
    trajectory.append({"from": "system", "value": system_msg})
    trajectory.append({"from": "human", "value": user_query})

    i = 1
    while i < len(messages):
        msg = messages[i]
        if msg["role"] == "assistant":
            if "tool_calls" in msg and msg["tool_calls"]:
                content = ""
                if msg.get("reasoning") and msg["reasoning"].strip():
                    content = f"<think>\n{msg['reasoning']}\n</think>\n"
                if msg.get("content") and msg["content"].strip():
                    content += convert_scratchpad_to_think(msg["content"]) + "\n"

                for tool_call in msg["tool_calls"]:
                    if not tool_call or not isinstance(tool_call, dict):
                        continue
                    try:
                        raw_arguments = tool_call["function"]["arguments"]
                        arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else raw_arguments
                        )
                    except json.JSONDecodeError:
                        # Invalid arguments can contain secrets or reusable bytes;
                        # only the bounded sink projection may reach the logger.
                        safe_diagnostic = sanitize_tool_result_for_sink(
                            raw_arguments
                        )[:100]
                        logger.warning(
                            "Unexpected invalid JSON in trajectory conversion: %s",
                            safe_diagnostic,
                        )
                        arguments = {}

                    tool_call_json = {
                        "name": tool_call["function"]["name"],
                        "arguments": sanitize_tool_result_projection_for_sink(arguments),
                    }
                    content += (
                        f"<tool_call>\n{json.dumps(tool_call_json, ensure_ascii=False)}\n"
                        "</tool_call>\n"
                    )

                if "<think>" not in content:
                    content = "<think>\n</think>\n" + content
                trajectory.append({"from": "gpt", "value": content.rstrip()})

                tool_responses = []
                j = i + 1
                while j < len(messages) and messages[j]["role"] == "tool":
                    tool_msg = messages[j]
                    tool_content = sanitize_trajectory_tool_value(tool_msg["content"])
                    try:
                        if tool_content.strip().startswith(("{", "[")):
                            tool_content = json.loads(tool_content)
                    except (json.JSONDecodeError, AttributeError):
                        pass

                    tool_index = len(tool_responses)
                    tool_name = (
                        msg["tool_calls"][tool_index]["function"]["name"]
                        if tool_index < len(msg["tool_calls"])
                        else "unknown"
                    )
                    tool_responses.append(
                        "<tool_response>\n"
                        + json.dumps(
                            {
                                "tool_call_id": tool_msg.get("tool_call_id", ""),
                                "name": tool_name,
                                "content": tool_content,
                            },
                            ensure_ascii=False,
                        )
                        + "\n</tool_response>"
                    )
                    j += 1

                if tool_responses:
                    trajectory.append(
                        {"from": "tool", "value": "\n".join(tool_responses)}
                    )
                    i = j - 1
            else:
                content = ""
                if msg.get("reasoning") and msg["reasoning"].strip():
                    content = f"<think>\n{msg['reasoning']}\n</think>\n"
                raw_content = msg["content"] or ""
                content += convert_scratchpad_to_think(raw_content)
                if "<think>" not in content:
                    content = "<think>\n</think>\n" + content
                trajectory.append({"from": "gpt", "value": content.strip()})
        elif msg["role"] == "user":
            trajectory.append({"from": "human", "value": msg["content"]})
        i += 1
    return trajectory


def save_session_log(agent: Any, messages: Optional[List[Dict[str, Any]]] = None) -> None:
    """Write the optional session JSON snapshot through sink-safe projections."""
    if not getattr(agent, "_session_json_enabled", False):
        return
    messages = messages or agent._session_messages
    if not messages:
        return

    try:
        safe_sid = safe_session_filename_component(agent.session_id)
        log_file = agent.logs_dir / f"session_{safe_sid}.json"
    except Exception:
        return

    try:
        cleaned = []
        for msg in messages:
            if is_ephemeral_scaffolding(msg):
                continue
            msg = dict(msg)
            if msg.get("role") == "assistant" and msg.get("content"):
                msg["content"] = agent._clean_session_content(msg["content"])
            if msg.get("role") == "assistant" and msg.get("tool_calls") is not None:
                msg["tool_calls"] = sanitize_tool_message_value(msg["tool_calls"])
            if msg.get("role") == "tool":
                if "content" in msg:
                    msg["content"] = sanitize_tool_message_value(msg["content"])
                if "api_content" in msg and msg["api_content"] is not None:
                    msg["api_content"] = sanitize_tool_message_value(
                        msg["api_content"]
                    )
                if msg.get("tool_calls") is not None:
                    msg["tool_calls"] = sanitize_tool_message_value(
                        msg["tool_calls"]
                    )
            elif "content" in msg:
                msg["content"] = agent._redact_message_content(msg.get("content"))
            cleaned.append(msg)

        if log_file.exists():
            try:
                existing = json.loads(log_file.read_text(encoding="utf-8"))
                existing_count = existing.get(
                    "message_count", len(existing.get("messages", []))
                )
                if existing_count > len(cleaned):
                    logging.debug(
                        "Skipping session log overwrite: existing has %d messages, current has %d",
                        existing_count,
                        len(cleaned),
                    )
                    return
            except Exception:
                pass

        entry = {
            "session_id": agent.session_id,
            "model": agent.model,
            "base_url": agent.base_url,
            "platform": agent.platform,
            "session_start": agent.session_start.isoformat(),
            "last_updated": datetime.now().isoformat(),
            "system_prompt": redact_sensitive_text(agent._cached_system_prompt or ""),
            "tools": agent.tools or [],
            "message_count": len(cleaned),
            "messages": cleaned,
        }
        atomic_json_write(log_file, entry, indent=2, default=str)
    except Exception as exc:
        if getattr(agent, "verbose_logging", False):
            logging.warning("Failed to save session log: %s", sanitize_tool_result_for_sink(exc))


def format_file_mutation_failure_footer(
    failed: Dict[str, Dict[str, Any]],
    neutralize_paths: Callable[[str], str],
) -> str:
    """Render failed mutation diagnostics without leaking retained previews."""
    if not failed:
        return ""
    lines = [
        "⚠️ File-mutation verifier: "
        f"{len(failed)} file(s) were NOT modified this turn despite any "
        "wording above that may suggest otherwise. Run `git status` or "
        "`read_file` to confirm."
    ]
    shown = 0
    for path, info in failed.items():
        if shown >= 10:
            break
        preview = sanitize_tool_result_for_sink(
            info.get("error_preview") or ""
        ).strip()
        tool = info.get("tool") or "patch"
        if preview:
            lines.append(f"  • `{path}` — [{tool}] {preview}")
        else:
            lines.append(f"  • `{path}` — [{tool}] failed")
        shown += 1
    remaining = len(failed) - shown
    if remaining > 0:
        lines.append(f"  • … and {remaining} more")
    return neutralize_paths("\n".join(lines))


__all__ = [
    "_EPHEMERAL_SCAFFOLDING_FLAGS",
    "convert_to_trajectory_format",
    "format_file_mutation_failure_footer",
    "is_ephemeral_scaffolding",
    "safe_session_filename_component",
    "sanitize_tool_message_value",
    "sanitize_tool_result_for_sink",
    "sanitize_tool_result_projection_for_sink",
    "sanitize_trajectory_for_sink",
    "sanitize_trajectory_tool_value",
    "save_session_log",
]
