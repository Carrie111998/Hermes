"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
import re
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False


# ── HTTP status detection in terminal output ────────────────────────────
# `curl` without `-f` (and many other fetch tools) exits 0 even when the
# server answers 4xx/5xx: the HTTP error is only visible in the OUTPUT.
# Blind-retrying such a call is the classic "deterministic 404 loop" — the
# harness must classify the embedded status as a failure so the tool-loop
# guardrail can fire. Patterns are deliberately conservative (status-line /
# curl-error / JSON status / HTML title shapes) so ordinary output containing
# the string "404" (e.g. grep hits, log excerpts) is not misclassified.

_HTTP_STATUS_LINE_RE = re.compile(
    r"HTTP/\d(?:\.\d)?\s+([4-5]\d\d)"          # HTTP/1.1 404, HTTP/2 503
)
_HTTP_NAKED_STATUS_RE = re.compile(
    r"(?<![0-9])HTTP\s+([4-5]\d\d)(?![0-9])"    # "HTTP 404" without protocol version
)
_CURL_FAIL_RE = re.compile(
    r"curl:\s*\(\d+\)\s+[^\n]*?\berror\b[^\n]*?\b([4-5]\d\d)\b"
)
_JSON_STATUS_RE = re.compile(
    r'"(?:status|statusCode|status_code)"\s*:\s*([4-5]\d\d)'
)
_HTML_TITLE_STATUS_RE = re.compile(
    r"<title>[^<]*?([4-5]\d\d)[^<]*?</title>", re.IGNORECASE
)
_WGET_STATUS_RE = re.compile(
    r"HTTP request sent, awaiting response\.\.\.\s*([4-5]\d\d)"
)
# Plain-text error bodies from minimal servers: python http.server
# ("Error code: 404"), and line-start status phrases (nginx/apache h1-style
# bodies, `curl -w '%{http_code}'` bare output). Anchored to line starts so
# mid-line log excerpts / grep hits (e.g. "access.log:404 Not Found") are
# data, not an HTTP error response.
_ERROR_CODE_LINE_RE = re.compile(
    r"(?m)^\s*Error code:\s*([4-5]\d\d)\s*$", re.IGNORECASE
)
_BODY_STATUS_PHRASE_RE = re.compile(
    r"(?m)^\s*([4-5]\d\d)\s+(?:Not Found|Forbidden|Unauthorized|"
    r"Service Unavailable|Internal Server Error|Bad Gateway|Gateway Timeout)\b",
    re.IGNORECASE,
)
_HTML_H1_STATUS_RE = re.compile(
    r"<h1>\s*([4-5]\d\d)\b", re.IGNORECASE
)


def detect_http_status_in_output(output: str | None) -> tuple[str, str] | None:
    """Return ``(code, kind)`` when terminal output embeds an HTTP error status.

    ``kind`` is ``"permanent"`` for 4xx (wrong URL, resource gone — retrying
    unchanged never helps) and ``"transient"`` for 5xx (server-side flake —
    a bounded backoff retry is legitimate, but a persistent 5xx loop is not).
    Returns ``None`` when no HTTP error status is found.
    """
    if not output:
        return None
    head = output[:8000]
    for pattern in (
        _HTTP_STATUS_LINE_RE,
        _HTTP_NAKED_STATUS_RE,
        _CURL_FAIL_RE,
        _JSON_STATUS_RE,
        _HTML_TITLE_STATUS_RE,
        _WGET_STATUS_RE,
        _ERROR_CODE_LINE_RE,
        _BODY_STATUS_PHRASE_RE,
        _HTML_H1_STATUS_RE,
    ):
        match = pattern.search(head)
        if not match:
            continue
        code = match.group(1)
        return (code, "permanent" if code.startswith("4") else "transient")
    return None
