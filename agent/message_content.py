from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_PART_TYPES = {"text", "input_text", "output_text", "summary_text"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")

MAX_ITERATIONS_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools."
)
NETWORK_STREAM_CONTINUATION_REQUEST = (
    "[System: The previous response was cut off by a network error mid-stream. "
    "Continue exactly where you left off. Do not restart or repeat prior text. "
    "Finish the answer directly.]"
)
OUTPUT_LIMIT_CONTINUATION_REQUEST = (
    "[System: Your previous response was truncated by the output length limit. "
    "Continue exactly where you left off. Do not restart or repeat prior text. "
    "Finish the answer directly.]"
)
CODEX_INCOMPLETE_CONTINUATION_REQUEST = (
    "[System: Your previous response contained only internal reasoning and "
    "never produced a visible answer or tool call. Do not keep thinking. "
    "Produce your final answer as plain text now (or make the tool call "
    "you were planning).]"
)
INTENT_ACK_CONTINUATION_REQUEST = (
    "[System: Continue now. Execute the required tool calls and only "
    "send your final answer after completing the task.]"
)
EMPTY_TOOL_RESPONSE_CONTINUATION_REQUEST = (
    "You just executed tool calls but returned an empty response. Please process "
    "the tool results above and continue with the task."
)
DROPPED_TOOL_CALL_CONTINUATION_REQUEST = (
    "Your previous turn indicated a tool call but none was included. Do not "
    "narrate a plan or restate intent — issue the actual tool call now to "
    "continue the task."
)

EXACT_INTERNAL_USER_REQUESTS = frozenset(
    {
        MAX_ITERATIONS_SUMMARY_REQUEST,
        NETWORK_STREAM_CONTINUATION_REQUEST,
        OUTPUT_LIMIT_CONTINUATION_REQUEST,
        CODEX_INCOMPLETE_CONTINUATION_REQUEST,
        INTENT_ACK_CONTINUATION_REQUEST,
        EMPTY_TOOL_RESPONSE_CONTINUATION_REQUEST,
        DROPPED_TOOL_CALL_CONTINUATION_REQUEST,
    }
)

_TOOL_CALL_STREAM_CONTINUATION_PREFIX = "[System: Your previous tool call ("
_TOOL_CALL_STREAM_CONTINUATION_SUFFIX = (
    ") was too large and the stream timed out before it could be delivered. "
    "Do NOT retry the same tool call with the same large content. Instead, "
    "break the content into multiple smaller tool calls (e.g. use multiple "
    "patch calls or write smaller files). Each tool call's arguments must be "
    "under ~8K tokens to avoid stream timeouts.]"
)
_TOOL_NAMES_WIRE_RE = re.compile(r"[^\r\n)]{1,256}\Z")
_ASYNC_SINGLE_HEADER_RE = re.compile(
    r"\[ASYNC DELEGATION COMPLETE — [^\]\r\n]{1,256}\]\Z"
)
_ASYNC_BATCH_HEADER_RE = re.compile(
    r"\[ASYNC DELEGATION BATCH COMPLETE — [^\]\r\n]{1,256}\]\Z"
)
_ASYNC_SINGLE_INTRO = (
    "A background subagent you dispatched earlier has finished. You may have "
    "moved on since dispatching it; the full task source is below so you can "
    "act on the result or re-dispatch if things have changed."
)
_ASYNC_BATCH_INTRO_RE = re.compile(
    r"A background fan-out of \d+ subagent\(s\) you dispatched earlier has "
    r"finished\. All ran in parallel and waited on each other; their "
    r"consolidated results are below\. You may have moved on since dispatching "
    r"— act on these or re-dispatch if things have changed\.\Z"
)
_BACKGROUND_WATCH_HEADER_RE = re.compile(
    r'\[IMPORTANT: Background process [^\r\n]+ matched watch pattern "[^\r\n]*"\.\Z'
)
_BACKGROUND_COMPLETION_HEADER_RE = re.compile(
    r"\[IMPORTANT: Background process [^\r\n]+ \(exit code [^\r\n]+\)\.\Z"
)
_WATCH_DISABLED_RE = re.compile(
    r"\[IMPORTANT: Watch patterns disabled for process [^\r\n]+ — \d+ "
    r"consecutive rate-limit windows triggered \(min spacing [0-9.]+s\)\. "
    r"Falling back to notify_on_complete semantics; you'll get exactly one "
    r"notification when the process exits\.\]\Z"
)
_CLI_HANDOFF_PREFIX = '[Session was just handed off from CLI ("'
_CLI_HANDOFF_SUFFIX = (
    '") to this channel. The full prior conversation history is loaded above. '
    "Briefly confirm you're working here and summarize what we were working on, "
    "so the user can continue from this device.]"
)
_CLI_HANDOFF_TITLE_RE = re.compile(r"[^\r\n]{1,512}\Z")


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part

    part_type = str(_field(part, "type") or "").strip().lower()
    if part_type in _NON_TEXT_PART_TYPES:
        return ""

    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def flatten_message_text(content: Any, *, sep: str = "\n") -> str:
    """Return the visible text from common chat/Responses message content shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)

    text = _text_from_part(content)
    if text:
        return text
    if isinstance(content, Mapping) or any(
        hasattr(content, key) for key in ("type", *_TEXT_KEYS)
    ):
        return ""
    try:
        return str(content)
    except Exception:
        return ""


def has_non_text_content(content: Any) -> bool:
    """Return whether content carries a structured non-text input part."""

    parts = content if isinstance(content, list) else [content]
    for part in parts:
        if part is None or isinstance(part, str):
            continue
        part_type = str(_field(part, "type") or "").strip().lower()
        if part_type and part_type not in _TEXT_PART_TYPES:
            return True
    return False


def build_tool_call_stream_continuation_request(tool_names: Iterable[str]) -> str:
    """Build the bounded recovery request for a tool call cut off in flight."""

    tool_list = ", ".join(str(name) for name in list(tool_names)[:3])
    return (
        _TOOL_CALL_STREAM_CONTINUATION_PREFIX
        + tool_list
        + _TOOL_CALL_STREAM_CONTINUATION_SUFFIX
    )


def build_resume_recovery_note(
    reason: str | None,
    message: str = "",
    *,
    interactive: bool = True,
) -> str:
    """Build the recovery note for an interrupted gateway turn."""

    reason_phrase = (
        "a gateway restart"
        if reason == "restart_timeout"
        else "a gateway shutdown"
        if reason == "shutdown_timeout"
        else "a gateway interruption"
    )
    if message:
        resume_guidance = (
            "Address the user's NEW message below FIRST and focus "
            "on what the user is asking now."
        )
        tail_guidance = (
            "Do NOT re-execute old tool calls — skip any "
            "unfinished work from the conversation history."
        )
    elif interactive:
        resume_guidance = (
            "Report to the user that the session was restored "
            "successfully and ask what they would like to do next."
        )
        tail_guidance = (
            "Do NOT re-execute old tool calls — skip any "
            "unfinished work from the conversation history."
        )
    else:
        resume_guidance = (
            "No user is present on this non-interactive platform, "
            "so do NOT emit a 'session restored' acknowledgement "
            "or ask questions. Review the conversation history and "
            "CONTINUE the interrupted task to completion."
        )
        tail_guidance = (
            "Do NOT re-run tool calls whose results already "
            "appear in the history — resume from the first step "
            "that has no recorded result."
        )
    return (
        f"[System note: The previous turn was interrupted by "
        f"{reason_phrase}; the gateway is now back online. "
        f"Any restart/shutdown command in the history has already "
        f"run — do NOT re-execute or verify it. {resume_guidance} "
        f"{tail_guidance}]"
        + (f"\n\n{message}" if message else "")
    )


_PURE_RESUME_RECOVERY_NOTES = frozenset(
    build_resume_recovery_note(reason, "", interactive=interactive)
    for reason in ("restart_timeout", "shutdown_timeout", None)
    for interactive in (True, False)
)


def build_cli_handoff_notice(cli_title: str) -> str:
    """Build the synthetic user-role notice for a CLI-to-gateway handoff."""

    return _CLI_HANDOFF_PREFIX + str(cli_title) + _CLI_HANDOFF_SUFFIX


def _is_tool_call_stream_continuation(text: str) -> bool:
    if not (
        text.startswith(_TOOL_CALL_STREAM_CONTINUATION_PREFIX)
        and text.endswith(_TOOL_CALL_STREAM_CONTINUATION_SUFFIX)
    ):
        return False
    tool_names = text[
        len(_TOOL_CALL_STREAM_CONTINUATION_PREFIX) :
        -len(_TOOL_CALL_STREAM_CONTINUATION_SUFFIX)
    ]
    return bool(_TOOL_NAMES_WIRE_RE.fullmatch(tool_names))


def _is_async_delegation_notification(text: str) -> bool:
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    if _ASYNC_SINGLE_HEADER_RE.fullmatch(lines[0]):
        return (
            lines[1] == _ASYNC_SINGLE_INTRO
            and any(line.startswith("Original goal:") for line in lines[2:])
            and "--- RESULT ---" in lines[2:]
        )
    if _ASYNC_BATCH_HEADER_RE.fullmatch(lines[0]):
        return bool(
            _ASYNC_BATCH_INTRO_RE.fullmatch(lines[1])
            and any(line.startswith("Role: ") for line in lines[2:])
        )
    return False


def _is_background_process_notification(text: str) -> bool:
    if _WATCH_DISABLED_RE.fullmatch(text):
        return True
    header, separator, body = text.partition("\n")
    if not separator or not body.startswith("Command: ") or not text.endswith("]"):
        return False
    if _BACKGROUND_WATCH_HEADER_RE.fullmatch(header):
        return "\nMatched output:\n" in body
    if _BACKGROUND_COMPLETION_HEADER_RE.fullmatch(header):
        return "\nOutput:\n" in body
    return False


def _is_pure_resume_recovery_note(text: str) -> bool:
    return text in _PURE_RESUME_RECOVERY_NOTES


def _is_cli_handoff_notice(text: str) -> bool:
    if not (
        text.startswith(_CLI_HANDOFF_PREFIX)
        and text.endswith(_CLI_HANDOFF_SUFFIX)
    ):
        return False
    title = text[len(_CLI_HANDOFF_PREFIX) : -len(_CLI_HANDOFF_SUFFIX)]
    return bool(title.strip() and _CLI_HANDOFF_TITLE_RE.fullmatch(title))


def _is_goal_continuation_request(text: str) -> bool:
    # Lazy import avoids coupling the lightweight content helpers to goal
    # state/database setup during module initialization.
    try:
        from hermes_cli.goals import is_goal_continuation_prompt

        return is_goal_continuation_prompt(text)
    except Exception:
        return False


def is_internal_user_scaffolding_text(text: str) -> bool:
    """Recognize stable Hermes-authored user-role wire formats narrowly."""

    normalized = (text or "").strip()
    return (
        normalized in EXACT_INTERNAL_USER_REQUESTS
        or _is_tool_call_stream_continuation(normalized)
        or _is_async_delegation_notification(normalized)
        or _is_background_process_notification(normalized)
        or _is_pure_resume_recovery_note(normalized)
        or _is_cli_handoff_notice(normalized)
        or _is_goal_continuation_request(normalized)
    )
