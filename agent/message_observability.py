"""Secret-safe message-shape diagnostics for provider payload tracing."""

from __future__ import annotations

import logging
from typing import Any, Iterable


def _content_length(content: Any) -> int:
    """Return a useful payload length without rendering or logging content."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    if isinstance(content, list):
        return sum(
            _content_length(part.get("text") if isinstance(part, dict) else part)
            for part in content
        )
    return len(str(content))


def log_message_shape(
    logger: logging.Logger,
    stage: str,
    messages: Iterable[Any] | None,
    *,
    conversation_history_count: int | None = None,
) -> None:
    """Log counts, roles, and content lengths only; never message content."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    materialized = list(messages or [])
    roles = [
        str(message.get("role", "<missing>"))
        if isinstance(message, dict)
        else f"<{type(message).__name__}>"
        for message in materialized
    ]
    content_lengths = [
        _content_length(message.get("content")) if isinstance(message, dict) else 0
        for message in materialized
    ]
    logger.debug(
        "provider payload shape: stage=%s conversation_history_count=%s "
        "message_count=%d roles=%s content_lengths=%s",
        stage,
        conversation_history_count,
        len(materialized),
        roles,
        content_lengths,
    )
