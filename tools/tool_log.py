"""Shared helpers for bounding the text written into tool logs."""

from __future__ import annotations

# Upper bound for a single error message written to the log. Provider error
# bodies (JSON payloads, HTTP dumps) can reach megabytes; without a cap a
# single failed tool call floods agent.log / errors.log / gateway.log and
# bloats disk usage far beyond the one-line diagnostic it is meant to be.
LOG_ERROR_PREVIEW_LIMIT = 2000


def truncate_for_log(text: str) -> str:
    """Return ``text`` bounded to a single log line.

    Long upstream error bodies are shortened so a failed tool call cannot
    balloon the log. The full error text is still returned to the agent in
    the tool result and in debug data; only the log line is truncated.
    """
    if text is None:
        return ""
    if len(text) <= LOG_ERROR_PREVIEW_LIMIT:
        return text
    return text[:LOG_ERROR_PREVIEW_LIMIT] + "…"
