"""Hermes SessionDB ownership and runtime-resume history helpers."""

from __future__ import annotations

import json
from typing import Any

SESSION_DB_HISTORY_MODE = "session_db"
_RUNTIME_ATTACHMENT_TEXT_PREFIXES = ("[Attached image:", "[Attached video:")
_RUNTIME_MEDIA_CONTEXT_OPEN = "<runtime_generated_media_context>"
_RUNTIME_MEDIA_CONTEXT_CLOSE = "</runtime_generated_media_context>"


class RuntimeSessionStateError(RuntimeError):
    """SessionDB state is unavailable or conflicts with a resume request."""

    def __init__(self, code: str, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def resume_runtime_history(
    messages: list[dict[str, Any]],
    checkpoint: Any,
    tool_results: Any,
) -> list[dict[str, Any]]:
    """Rebuild the legacy checkpoint continuation used during migration."""
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("message"), dict):
        raise ValueError("runtime_checkpoint.message is required for tool-result resume")
    assistant = checkpoint["message"]
    calls = assistant.get("tool_calls")
    if assistant.get("role") != "assistant" or not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("runtime checkpoint must contain exactly one platform tool call")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
    tool_name = str(function.get("name") or "") if isinstance(function, dict) else ""
    if not call_id or not tool_name:
        raise ValueError("runtime checkpoint tool call id and name are required")
    if (
        not isinstance(tool_results, list)
        or len(tool_results) != 1
        or not isinstance(tool_results[0], dict)
    ):
        raise ValueError("exactly one tool_result is required for runtime resume")
    result = tool_results[0]
    if str(result.get("tool_call_id") or "") != call_id:
        raise ValueError("tool_result does not match runtime checkpoint")
    status = str(result.get("status") or "")
    if status == "succeeded":
        content = result.get("output")
        if content is None and result.get("output_ref") is not None:
            content = {"status": "externalized", "output_ref": result["output_ref"]}
    elif status == "failed" and isinstance(result.get("error"), dict):
        content = {"error": result["error"]}
    else:
        raise ValueError("tool_result status must be succeeded or failed")
    return [
        *messages,
        json.loads(json.dumps(assistant, ensure_ascii=False)),
        {
            "role": "tool",
            "name": tool_name,
            "tool_call_id": call_id,
            "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _anchor_content(content: Any) -> str:
    if isinstance(content, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in content
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and not str(part.get("text") or "").startswith(
                    _RUNTIME_ATTACHMENT_TEXT_PREFIXES,
                )
            )
        )
    else:
        text = "\n".join(
            line
            for line in _message_text(content).splitlines()
            if not line.strip().startswith(_RUNTIME_ATTACHMENT_TEXT_PREFIXES)
        )
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() not in {"[screenshot]", "[Attached media]"}
    ).strip()


def _inject_runtime_attachment_context(
    history: list[dict[str, Any]],
    attachment_parts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose ephemeral generated media on the new tool-result tail only."""
    context_lines = [
        str(part.get("text") or "").strip()
        for part in attachment_parts
        if isinstance(part, dict)
        and (part.get("_runtime_image_path") or part.get("_runtime_video_path"))
        and str(part.get("text") or "").strip()
    ]
    if not context_lines:
        return history
    if not history or history[-1].get("role") != "tool":
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "generated output attachments require a resumed tool result",
            status=409,
        )
    result = list(history)
    tail = dict(result[-1])
    content = str(tail.get("content") or "")
    tail["content"] = (
        content
        + "\n\n"
        + _RUNTIME_MEDIA_CONTEXT_OPEN
        + "\n"
        + "\n".join(context_lines)
        + "\n"
        + _RUNTIME_MEDIA_CONTEXT_CLOSE
    )
    result[-1] = tail
    return result


def _public_message_key(message: Any) -> tuple[str, str] | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "")
    if role not in {"user", "assistant"} or message.get("tool_calls"):
        return None
    return role, _anchor_content(message.get("content"))


def merge_runtime_session_history(
    caller_messages: list[dict[str, Any]],
    session_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a public caller prefix with the authoritative private DB suffix."""
    if not session_messages:
        return list(caller_messages)
    if not caller_messages:
        return list(session_messages)

    session_public = [
        key
        for key in (_public_message_key(item) for item in session_messages)
        if key is not None
    ]
    if not session_public:
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "SessionDB history has no public anchor for caller history",
            status=409,
        )

    caller_public = [
        (index, key)
        for index, item in enumerate(caller_messages)
        if (key := _public_message_key(item)) is not None
    ]
    alignments: list[list[int]] = []
    for public_index, (message_index, key) in enumerate(caller_public):
        if key != session_public[0]:
            continue
        matched = [message_index]
        search_from = public_index + 1
        for session_key in session_public[1:]:
            match = next(
                (
                    (candidate_index, candidate_message_index)
                    for candidate_index, (candidate_message_index, candidate_key) in enumerate(
                        caller_public[search_from:],
                        start=search_from,
                    )
                    if candidate_key == session_key
                ),
                None,
            )
            if match is None:
                break
            search_from, matched_message_index = match
            matched.append(matched_message_index)
            search_from += 1
        if len(matched) == len(session_public):
            alignments.append(matched)
    if not alignments:
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "caller history does not overlap SessionDB history",
            status=409,
        )

    # The latest complete alignment avoids binding repeated prompts to an older
    # occurrence. Public turns after the DB tail may come from another Runtime;
    # retain them instead of silently discarding valid product history.
    matched = alignments[-1]
    return [
        *caller_messages[: matched[0]],
        *session_messages,
        *caller_messages[matched[-1] + 1 :],
    ]


def runtime_history_tool_names(history: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            if name:
                names.add(name)
    return names


def _tool_result_content_equal(left: Any, right: Any) -> bool:
    try:
        return json.loads(str(left)) == json.loads(str(right))
    except (json.JSONDecodeError, TypeError, ValueError):
        return str(left or "") == str(right or "")


def resume_session_db_history(
    db: Any,
    session_id: str,
    history: list[dict[str, Any]],
    tool_results: Any,
) -> list[dict[str, Any]]:
    """Persist one real platform result at the unfinished SessionDB tool call."""
    if (
        not isinstance(tool_results, list)
        or len(tool_results) != 1
        or not isinstance(tool_results[0], dict)
    ):
        raise ValueError("exactly one tool_result is required for runtime resume")
    result = tool_results[0]
    call_id = str(result.get("tool_call_id") or "")
    if not call_id:
        raise ValueError("tool_result.tool_call_id is required")

    assistant_index = -1
    matching_call: dict[str, Any] | None = None
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        matches = [
            call
            for call in message.get("tool_calls") or []
            if isinstance(call, dict) and str(call.get("id") or "") == call_id
        ]
        if matches:
            assistant_index = index
            matching_call = matches[0]
            break
    if matching_call is None:
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "tool_result does not match an assistant tool call in SessionDB",
            status=409,
        )

    projected = resume_runtime_history(
        [],
        {"message": {"role": "assistant", "content": None, "tool_calls": [matching_call]}},
        [result],
    )[-1]
    later = history[assistant_index + 1 :]
    prior_results = [
        message
        for message in later
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and str(message.get("tool_call_id") or "") == call_id
    ]
    if prior_results:
        if (
            len(prior_results) == 1
            and _tool_result_content_equal(
                prior_results[0].get("content"),
                projected["content"],
            )
        ):
            return history
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "conflicting tool_result already exists in SessionDB",
            status=409,
        )
    if any(
        isinstance(message, dict) and message.get("role") != "tool"
        for message in later
    ):
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "SessionDB continued past the requested tool call",
            status=409,
        )

    sibling_ids = {
        str(call.get("id") or "")
        for call in (history[assistant_index].get("tool_calls") or [])
        if isinstance(call, dict) and str(call.get("id") or "") != call_id
    }
    completed_siblings = {
        str(message.get("tool_call_id") or "")
        for message in later
        if isinstance(message, dict) and message.get("role") == "tool"
    }
    if not sibling_ids.issubset(completed_siblings):
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "SessionDB contains more than one unfinished tool call",
            status=409,
        )

    tool_name = str((matching_call.get("function") or {}).get("name") or "")
    try:
        db.append_message(
            session_id=session_id,
            role="tool",
            content=projected["content"],
            tool_name=tool_name,
            tool_call_id=call_id,
        )
    except Exception as exc:
        raise RuntimeSessionStateError(
            "runtime_session_unavailable",
            "failed to persist resumed tool result in SessionDB",
        ) from exc
    return [*history, projected]


def load_runtime_session_history(
    adapter: Any,
    requested_session_id: str,
    *,
    require_existing: bool,
) -> tuple[Any, str, list[dict[str, Any]]]:
    try:
        db = adapter._ensure_session_db()
    except Exception as exc:
        raise RuntimeSessionStateError(
            "runtime_session_unavailable",
            "SessionDB is unavailable",
        ) from exc
    if db is None:
        raise RuntimeSessionStateError(
            "runtime_session_unavailable",
            "SessionDB is unavailable",
        )
    try:
        session = db.get_session(requested_session_id)
        if session is None:
            if require_existing:
                raise RuntimeSessionStateError(
                    "runtime_session_not_found",
                    "runtime SessionDB history does not exist",
                    status=409,
                )
            return db, requested_session_id, []
        resolved_session_id = db.resolve_resume_session_id(requested_session_id)
        history = db.get_messages_as_conversation(
            resolved_session_id,
            include_ancestors=True,
        )
    except RuntimeSessionStateError:
        raise
    except Exception as exc:
        raise RuntimeSessionStateError(
            "runtime_session_unavailable",
            "failed to load runtime SessionDB history",
        ) from exc
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise RuntimeSessionStateError(
            "runtime_history_conflict",
            "SessionDB returned invalid runtime history",
            status=409,
        )
    return db, str(resolved_session_id or requested_session_id), history


__all__ = [
    "RuntimeSessionStateError",
    "SESSION_DB_HISTORY_MODE",
    "load_runtime_session_history",
    "merge_runtime_session_history",
    "resume_runtime_history",
    "resume_session_db_history",
    "runtime_history_tool_names",
]
