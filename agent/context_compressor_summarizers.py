"""Tool-result one-line summarizers extracted from context_compressor.

Summarize tool calls/results into compact single-line forms for summaries.
_logger used via lazy round-trip import to preserve the godfile's logger
identity (zero behavior change, same pattern as auth s2 seam).

Part of #78645 + #78647.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from agent.context_compressor_skill_prune import (
    _SKILL_VIEW_PRUNE_MIN_CHARS,
    _skill_pruned_marker,
)


def _str_arg(args: dict, key: str, default: str = "") -> str:
    """Safely get a string argument from parsed tool args.

    LLMs sometimes return non-string parameter values (e.g. bool, int) for
    tool calls.  Calling ``len()`` / ``.count()`` / slicing on those causes
    ``TypeError`` / ``AttributeError`` which crashes context compression.
    This helper coerces any value to ``str`` so downstream code can assume
    a string is always returned.
    """
    val = args.get(key, default)
    if isinstance(val, str):
        return val
    return str(val) if val is not None else default


def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    """Create an informative 1-line summary of a tool call + result.

    Used during the pre-compression pruning pass to replace large tool
    outputs with a short but useful description of what the tool did,
    rather than a generic placeholder that carries zero information.

    Returns strings like::

        [terminal] ran `npm test` -> exit 0, 47 lines output
        [read_file] read config.py from line 1 (1,200 chars)
        [search_files] content search for 'compress' in agent/ -> 12 matches

    Never raises: models sometimes emit non-string argument values (bool,
    int, None) and the args here come from persisted session history, so a
    single malformed historical call must not crash compression — which
    retries on the same history and would crash-loop. Individual branches
    coerce the values they slice/measure (keeping summaries informative);
    this wrapper is the backstop for anything they miss.
    """
    try:
        return _summarize_tool_result_unguarded(tool_name, tool_args, tool_content)
    except Exception as exc:  # noqa: BLE001 — a summary must never crash compression
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        logger.debug("Tool-result summary failed for %s: %s", tool_name, exc)
        _len = len(tool_content) if isinstance(tool_content, str) else 0
        return f"[{tool_name}] ({_len:,} chars result)"


def _summarize_tool_result_unguarded(tool_name: str, tool_args: str, tool_content: str) -> str:
    """Build the summary line (unguarded; see ``_summarize_tool_result``)."""
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    content = tool_content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "terminal":
        cmd = _str_arg(args, "command")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return f"[terminal] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"

    if tool_name == "read_file":
        path = args.get("path", "?")
        offset = args.get("offset", 1)
        return f"[read_file] read {path} from line {offset} ({content_len:,} chars)"

    if tool_name == "write_file":
        path = args.get("path", "?")
        written_lines = _str_arg(args, "content").count("\n") + 1 if args.get("content") else "?"
        return f"[write_file] wrote to {path} ({written_lines} lines)"

    if tool_name == "search_files":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        target = args.get("target", "content")
        match_count = re.search(r'"total_count"\s*:\s*(\d+)', content)
        count = match_count.group(1) if match_count else "?"
        return f"[search_files] {target} search for '{pattern}' in {path} -> {count} matches"

    if tool_name == "patch":
        path = args.get("path", "?")
        mode = args.get("mode", "replace")
        return f"[patch] {mode} in {path} ({content_len:,} chars result)"

    if tool_name in {"browser_navigate", "browser_click", "browser_snapshot",
                     "browser_type", "browser_scroll", "browser_vision"}:
        url = args.get("url", "")
        ref = args.get("ref", "")
        detail = f" {url}" if url else (f" ref={ref}" if ref else "")
        return f"[{tool_name}]{detail} ({content_len:,} chars)"

    if tool_name == "web_search":
        query = args.get("query", "?")
        return f"[web_search] query='{query}' ({content_len:,} chars result)"

    if tool_name == "web_extract":
        urls = args.get("urls", [])
        first = urls[0] if isinstance(urls, list) and urls else "?"
        # web_search results are dicts ({"url"/"href": ...}) and models often
        # forward them straight into web_extract. Unwrap to the URL string so
        # the summary stays readable and the ``+=`` below never hits the
        # ``dict + str`` TypeError that would abort pre-compression pruning.
        if isinstance(first, dict):
            first = first.get("url") or first.get("href") or "?"
        elif not isinstance(first, str):
            first = "?"
        url_desc = first
        if isinstance(urls, list) and len(urls) > 1:
            url_desc += f" (+{len(urls) - 1} more)"
        return f"[web_extract] {url_desc} ({content_len:,} chars)"

    if tool_name == "delegate_task":
        goal = _str_arg(args, "goal")
        if len(goal) > 60:
            goal = goal[:57] + "..."
        return f"[delegate_task] '{goal}' ({content_len:,} chars result)"

    if tool_name == "execute_code":
        code_str = _str_arg(args, "code")
        code_preview = code_str[:60].replace("\n", " ")
        if len(code_str) > 60:
            code_preview += "..."
        return f"[execute_code] `{code_preview}` ({line_count} lines output)"

    if tool_name == "skill_view":
        name = args.get("name", "?")
        if content_len > _SKILL_VIEW_PRUNE_MIN_CHARS:
            # Ghost-skill defense (#32106): a metadata-only summary makes the
            # model believe the skill is still loaded. The canonical marker
            # tells it the instructions are gone AND how to get them back.
            return (
                f"[skill_view] name={name} ({content_len:,} chars) "
                + _skill_pruned_marker(str(name))
            )
        return f"[skill_view] name={name} ({content_len:,} chars)"

    if tool_name in {"skills_list", "skill_manage"}:
        name = args.get("name", "?")
        return f"[{tool_name}] name={name} ({content_len:,} chars)"

    if tool_name == "vision_analyze":
        question = _str_arg(args, "question")[:50]
        return f"[vision_analyze] '{question}' ({content_len:,} chars)"

    if tool_name == "memory":
        action = args.get("action", "?")
        target = args.get("target", "?")
        return f"[memory] {action} on {target}"

    if tool_name == "todo":
        return "[todo] updated task list"

    if tool_name == "clarify":
        return "[clarify] asked user a question"

    if tool_name == "text_to_speech":
        return f"[text_to_speech] generated audio ({content_len:,} chars)"

    if tool_name == "cronjob":
        action = args.get("action", "?")
        return f"[cronjob] {action}"

    if tool_name == "process":
        action = args.get("action", "?")
        sid = args.get("session_id", "?")
        return f"[process] {action} session={sid}"

    # Generic fallback
    first_arg = ""
    for k, v in list(args.items())[:2]:
        sv = str(v)[:40]
        first_arg += f" {k}={sv}"
    return f"[{tool_name}]{first_arg} ({content_len:,} chars result)"
