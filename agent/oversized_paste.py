"""Offload an oversized pasted user message to one retrievable file.

The full input is persisted before downstream context handling can elide it.
The replacement message contains the backend-visible path that the task's file
operations can read.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CHAR_THRESHOLD = 50_000


def _stable_paste_filename(content: str) -> str:
    """Return a content-addressed filename for an input paste."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"paste_{digest}.txt"


def _build_reference(path: str, content: str) -> str:
    """Build the single resolvable reference that replaces the paste."""
    n_chars = len(content)
    n_lines = content.count("\n") + 1
    return (
        f"[Large pasted content was saved to a file to keep the conversation "
        f"readable ({n_chars:,} characters, {n_lines:,} lines). This is the "
        f"COMPLETE, unabridged paste, nothing was elided or truncated.\n"
        f"Full content file (absolute path): {path}\n"
        f"To read it, call the read_file tool with path=\"{path}\". "
        f"read_file paginates via offset/limit, so use those to page through "
        f"it if it is very large. Prefer search_files over read_file if you "
        f"only need to find a specific section.]"
    )


def _threshold(agent: Any) -> int:
    """Return the effective character threshold for this agent."""
    raw = getattr(agent, "_oversized_input_char_threshold", None)
    if raw is None:
        return DEFAULT_CHAR_THRESHOLD
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CHAR_THRESHOLD


def _enabled(agent: Any) -> bool:
    """Return whether ingestion offloading is enabled for this agent."""
    return bool(getattr(agent, "_oversized_input_enabled", True))


def should_offload(agent: Any, content: Any) -> bool:
    """Return whether plain string content is large enough to offload."""
    if not _enabled(agent) or not isinstance(content, str) or not content:
        return False
    threshold = _threshold(agent)
    return threshold > 0 and len(content) >= threshold


def _resolve_active_environment(task_id: str):
    """Return or lazily create the environment selected by the task id."""
    try:
        from tools.terminal_tool import ensure_task_env, get_active_env

        return get_active_env(task_id) or ensure_task_env(task_id)
    except Exception as exc:
        logger.debug("oversized-paste environment resolution failed: %s", exc)
        return None


def write_paste_file(content: str, task_id: str = "default") -> Optional[str | Path]:
    """Persist the full input and return its backend-visible path."""
    try:
        from tools.tool_result_storage import persist_backend_visible_content

        env = _resolve_active_environment(task_id)
        path = persist_backend_visible_content(
            content,
            _stable_paste_filename(content),
            env=env,
            host_subdir="pastes",
            encoding_errors="strict",
        )
        if path is None:
            return None
        if env is None:
            return Path(path)
        return path
    except Exception as exc:
        logger.warning("oversized-paste offload write failed: %s", exc)
        return None


def maybe_offload_oversized_message(
    agent: Any,
    user_message: Any,
    persist_user_message: Any = None,
    *,
    task_id: str = "default",
) -> Tuple[Any, Any, Optional[str | Path]]:
    """Replace oversized input with one task-readable file reference."""
    if not should_offload(agent, user_message):
        return user_message, persist_user_message, None

    if "task_id" in inspect.signature(write_paste_file).parameters:
        path = write_paste_file(user_message, task_id=task_id)
    else:
        # Preserve compatibility with one-argument wrappers of the original
        # local-only helper.
        path = write_paste_file(user_message)
    if path is None:
        return user_message, persist_user_message, None

    reference = _build_reference(str(path), user_message)
    logger.info(
        "Offloaded oversized paste (%d chars) to %s",
        len(user_message),
        path,
    )
    new_persist = reference if isinstance(persist_user_message, str) else persist_user_message
    return reference, new_persist, path
