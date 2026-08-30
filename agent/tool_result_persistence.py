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
    """Copy a trajectory and sanitize every tool-originated value."""
    safe_trajectory = []
    for row in trajectory:
        safe_row = dict(row)
        if safe_row.get("from") == "tool" and "value" in safe_row:
            safe_row["value"] = sanitize_tool_result_for_sink(safe_row["value"])
        safe_trajectory.append(safe_row)
    return safe_trajectory


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
