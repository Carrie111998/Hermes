"""Shared helpers for bounding the text written into tool logs."""

from __future__ import annotations

# Upper bound for a single error message written to the log. Provider error
# bodies (JSON payloads, HTTP dumps) can reach megabytes; without a cap a
# single failed tool call floods agent.log / errors.log / gateway.log and
# bloats disk usage far beyond the one-line diagnostic it is meant to be.
LOG_ERROR_PREVIEW_LIMIT = 2000


def _collapse_line_separators(text: str) -> str:
    # A physical log line ends at the first \n or \r. A multiline upstream
    # error body must never become several physical lines, no matter how
    # short, so every line separator is collapsed to a single space first.
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def truncate_for_log(text: str) -> str:
    """Return ``text`` bounded to a single log line.

    Long upstream error bodies are shortened so a failed tool call cannot
    balloon the log, and embedded line separators are collapsed so a
    multiline body still lands as one physical log line. The full error text
    is still returned to the agent in the tool result and in debug data;
    only the log line is truncated.
    """
    if text is None:
        return ""
    text = _collapse_line_separators(text)
    if len(text) <= LOG_ERROR_PREVIEW_LIMIT:
        return text
    return text[:LOG_ERROR_PREVIEW_LIMIT] + "…"
