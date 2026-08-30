"""ACP ↔ Hermes tool/stream bridge.

This maps ACP tool intents (and Copilot's text bridge) onto Hermes
``tool_calls``. Not an OpenAI/ChatGPT vendor adapter. Kiro uses ACP;
Hermes owns tools.

Hermes' conversation loop speaks ``ChatCompletionMessageToolCall`` — that
is the existing agent-loop contract, not a ChatGPT product API.

Two ACP clients share this module and must not be collapsed into one protocol:

* Copilot still uses the text ``<tool_call>`` bridge
  (:func:`render_tool_bridge_sections` / :func:`extract_tool_calls_from_text`).
* Kiro is a model: native ACP ``tool_call`` intents are mapped to Hermes
  ``tool_calls`` via :func:`hermes_tool_call_from_acp`. Do not dump schemas
  into the Kiro prompt or parse XML out of Kiro text.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any, Iterable

from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
TOOL_CALL_JSON_RE = re.compile(
    r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)
_TOOL_CALL_OPEN = "<tool_call>"
_TOOL_CALL_CLOSE = "</tool_call>"

# The contract sentence shared by every ACP client: how to emit a call.
TOOL_CALL_CONTRACT = (
    "Available tools (OpenAI function schema). "
    "When using a tool, emit ONLY <tool_call>{...}</tool_call> with one JSON object "
    "containing id/type/function{name,arguments}. arguments must be a JSON string."
)

__all__ = [
    "TOOL_CALL_BLOCK_RE",
    "TOOL_CALL_JSON_RE",
    "TOOL_CALL_CONTRACT",
    "StreamChunks",
    "build_hermes_tool_call",
    "tool_specs_from_openai_tools",
    "render_tool_bridge_sections",
    "extract_tool_calls_from_text",
    "LiveToolCallTextFilter",
    "completion_to_stream_chunks",
    "empty_usage",
    "make_delta_chunk",
    "make_usage_chunk",
    "extract_acp_usage",
    "parse_acp_tool_update",
    "format_acp_tool_progress_line",
    "hermes_tool_call_from_acp",
]


class StreamChunks(list):
    """Stream chunks that can still carry response-level attributes.

    Hermes reads provider-level extras off the object returned by
    ``chat.completions.create`` (e.g. ``hermes_projected_messages``, consumed by
    ``agent/provider_projection.py``). A plain list of chunks would silently drop
    them on the ``stream=True`` path, so ACP clients return this instead and copy
    the extras onto it.
    """


def completion_to_stream_chunks(completion: SimpleNamespace) -> StreamChunks:
    """Convert a one-shot ACP response into OpenAI-style stream chunks.

    Response-level attributes other than ``choices``/``usage``/``model`` are
    copied onto the returned object so nothing a caller reads off the completion
    is lost when it asked to stream.
    """
    choice = completion.choices[0]
    message = choice.message
    tool_call_deltas = None
    if message.tool_calls:
        tool_call_deltas = []
        for index, tool_call in enumerate(message.tool_calls):
            tool_call_deltas.append(
                SimpleNamespace(
                    index=index,
                    id=getattr(tool_call, "id", None),
                    type=getattr(tool_call, "type", "function"),
                    function=SimpleNamespace(
                        name=getattr(tool_call.function, "name", None),
                        arguments=getattr(tool_call.function, "arguments", None),
                    ),
                )
            )

    delta = SimpleNamespace(
        role="assistant",
        content=message.content or None,
        tool_calls=tool_call_deltas,
        reasoning_content=getattr(message, "reasoning_content", None),
        reasoning=getattr(message, "reasoning", None),
    )
    data_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=delta,
                finish_reason=choice.finish_reason,
            )
        ],
        model=completion.model,
        usage=None,
    )
    usage_chunk = SimpleNamespace(
        choices=[],
        model=completion.model,
        usage=completion.usage,
    )
    chunks = StreamChunks([data_chunk, usage_chunk])
    for key, value in vars(completion).items():
        if key not in ("choices", "usage", "model"):
            setattr(chunks, key, value)
    return chunks


def empty_usage() -> SimpleNamespace:
    """Zeroed usage for vendors that never report tokens. Do not invent counts."""
    return SimpleNamespace(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )


def make_delta_chunk(
    model: str,
    *,
    content: str | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    """One OpenAI-shaped stream chunk. Used to flush ACP session/update live."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning,
                    reasoning=reasoning,
                ),
                finish_reason=finish_reason,
            )
        ],
        model=model,
        usage=None,
    )


def make_usage_chunk(model: str, usage: Any) -> SimpleNamespace:
    return SimpleNamespace(choices=[], model=model, usage=usage)


def extract_acp_usage(payload: Any) -> SimpleNamespace | None:
    """Return usage only when the vendor sent at least one real token count.

    Accepts OpenAI-ish fields, Kiro ``PromptResponse.usage``
    (``inputTokens`` / ``outputTokens``), and native ACP ``usage_update``
    (``used`` / ``size`` — ``size`` is the window, not a token count).
    """
    if not isinstance(payload, dict):
        return None
    for key in ("usage", "tokenUsage", "tokens", "currentUsage", "update"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            nested = extract_acp_usage(inner)
            if nested is not None:
                return nested

    def _num(*keys: str) -> int:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0

    prompt = _num(
        "promptTokens",
        "inputTokens",
        "prompt_tokens",
        "input_tokens",
        "usedTokens",
        "used",
    )
    completion = _num(
        "completionTokens",
        "outputTokens",
        "completion_tokens",
        "output_tokens",
    )
    total = _num("totalTokens", "total_tokens")
    cached = _num(
        "cachedTokens",
        "cached_tokens",
        "cacheReadTokens",
        "cachedWriteTokens",
    )
    if prompt == 0 and completion == 0 and total == 0:
        return None
    if total == 0:
        total = prompt + completion
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


_ACP_KIND_TO_HERMES = {
    "execute": "terminal",
    "read": "read_file",
    "write": "write_file",
    "edit": "patch",
    "search": "search_files",
    "fetch": "web_extract",
}

_FILE_OP_COMMANDS = {
    "create": "write_file",
    "append": "write_file",
    "write": "write_file",
    "str_replace": "patch",
    "replace": "patch",
    "insert": "patch",
}
_NON_SHELL_COMMANDS = set(_FILE_OP_COMMANDS) | {"delete_file", "delete", "move"}

_ACP_NAME_HINTS = (
    (("execute", "exec", "shell", "bash", "command", "terminal"), "terminal"),
    (("read", "fsread", "readfile", "read_file"), "read_file"),
    (("write", "fswrite", "writefile", "write_file"), "write_file"),
    (("replace", "strreplace", "patch", "edit"), "patch"),
    (("grep", "search", "glob", "find"), "search_files"),
    (("qmd",), "mcp__qmd__query"),
    (("memory", "recall"), "memory"),
    (("process", "poll", "wait"), "process"),
)


_SHELL_PIPELINE_LEAD = re.compile(
    r"^(?:running:\s*)?(?:sudo\s+)?(?:"
    r"ps|pgrep|pidof|grep|egrep|fgrep|rg|find|ls|awk|sed|head|tail|cat|echo"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_shell_pipeline(text: str) -> bool:
    """True for a process/shell pipeline, not a file-search regex like ``foo|bar``."""
    s = (text or "").strip()
    if not s:
        return False
    body = s.split(":", 1)[1].strip() if s.lower().startswith("running:") else s
    if re.match(r"^(?:sudo\s+)?ps\b", body, re.IGNORECASE):
        return True
    return "|" in body and bool(_SHELL_PIPELINE_LEAD.match(body))


def _strip_running_prefix(text: str) -> str:
    s = (text or "").strip()
    if s.lower().startswith("running:"):
        return s.split(":", 1)[1].strip()
    return s


def _effective_acp_command(raw_input: dict[str, Any], title: str) -> str:
    """Prefer an explicit shell field; never treat a pipeline as a file-search pattern."""
    for key in ("command", "cmd", "shellCommand"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for candidate in (raw_input.get("pattern"), raw_input.get("query"), title):
        if isinstance(candidate, str) and _looks_like_shell_pipeline(candidate):
            return _strip_running_prefix(candidate)
    return ""


def _map_acp_tool_name(raw_name: str, acp_kind: str, command: str, path: str) -> str:
    op = (command or "").strip().lower()
    if op in _FILE_OP_COMMANDS:
        return _FILE_OP_COMMANDS[op]
    # A real shell string wins over kind=search / grep-named titles.
    if (command or "").strip() and op not in _NON_SHELL_COMMANDS:
        return "terminal"
    kind_mapped = _ACP_KIND_TO_HERMES.get(acp_kind)
    if kind_mapped:
        if kind_mapped == "patch" and not path and raw_name.lower().find("write") >= 0:
            return "write_file"
        return kind_mapped
    lowered = (raw_name or "").replace("-", "").replace("_", "").lower()
    for needles, mapped in _ACP_NAME_HINTS:
        if any(needle in lowered for needle in needles):
            return mapped
    if path:
        return "read_file"
    return (raw_name or "tool").strip() or "tool"


def hermes_tool_call_from_acp(
    parsed: dict[str, Any] | None,
) -> ChatCompletionMessageToolCall | None:
    """Turn a parsed ACP tool intent into a Hermes-executable tool_call.

    Only in-flight proposals (pending / in_progress / unset) become calls.
    Completed/failed/cancelled updates mean the vendor already finished — or
    we already denied — and must not be re-dispatched.
    """
    if not isinstance(parsed, dict):
        return None
    status = str(parsed.get("status") or "").strip().lower()
    if status in {"completed", "failed", "error", "cancelled"}:
        return None
    name = str(parsed.get("name") or "").strip()
    if not name or name == "tool":
        return None
    args = parsed.get("args")
    if not isinstance(args, dict):
        args = {}
    if name == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
    call_id = str(parsed.get("id") or "").strip() or f"acp_{name}"
    return build_hermes_tool_call(
        call_id=call_id,
        name=name,
        arguments=json.dumps(args, ensure_ascii=False),
    )


def parse_acp_tool_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize ACP tool_call / tool_call_update into a Hermes tool event.

    Kind/name/args are mapped onto Hermes tool names (``terminal``,
    ``read_file``, …) so the conversation loop can dispatch them the same
    way openai-codex function calls are dispatched.
    """
    if not isinstance(update, dict):
        return None
    kind = str(
        update.get("sessionUpdate")
        or update.get("type")
        or update.get("updateType")
        or ""
    ).strip()
    kind_norm = kind.replace("-", "_")
    tool_like = kind_norm in {
        "tool_call",
        "tool_call_update",
        "toolcall",
        "toolcallupdate",
        "current_tool_use",
        "tool_use",
        "tool_use_update",
    }
    if not tool_like and not (
        update.get("toolCallId")
        or update.get("tool_call_id")
        or update.get("kind")
        or update.get("toolName")
    ):
        return None
    if not kind:
        kind = "tool_call"
    raw_input = (
        update.get("rawInput")
        or update.get("raw_input")
        or update.get("input")
        or {}
    )
    if not isinstance(raw_input, dict):
        raw_input = {"raw": raw_input}
    path = ""
    locations = update.get("locations") or []
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, dict):
            path = str(first.get("path") or "")
    path = path or str(
        raw_input.get("path")
        or raw_input.get("filePath")
        or raw_input.get("file_path")
        or ""
    )
    title = str(update.get("title") or "").strip()
    command = _effective_acp_command(raw_input, title)
    acp_kind = str(update.get("kind") or "").strip().lower()
    raw_name = str(
        update.get("toolName")
        or update.get("name")
        or title
        or acp_kind
        or "tool"
    ).strip()
    if acp_kind in {"delete", "move", "think", "switch_mode"}:
        return None
    hermes_name = _map_acp_tool_name(raw_name, acp_kind, command, path)
    args: dict[str, Any] = {}
    if hermes_name == "terminal":
        # Keep the snapshot so the same toolCallId can be enriched later.
        # Never put command=None in args — that becomes a Hermes terminal
        # call that fails with "expected string, got NoneType".
        if isinstance(command, str) and command.strip():
            args["command"] = command
    elif hermes_name == "read_file":
        args["path"] = path or title
    elif hermes_name == "write_file":
        args["path"] = path or title
        content = (
            raw_input.get("content")
            or raw_input.get("file_text")
            or raw_input.get("newText")
            or raw_input.get("text")
        )
        if content is not None:
            args["content"] = content
    elif hermes_name == "patch":
        args["path"] = path or title
        old = (
            raw_input.get("oldText")
            or raw_input.get("old_string")
            or raw_input.get("old_str")
        )
        new = (
            raw_input.get("newText")
            or raw_input.get("new_string")
            or raw_input.get("new_str")
        )
        if old is not None:
            args["old_string"] = old
        if new is not None:
            args["new_string"] = new
    elif hermes_name == "search_files":
        pattern = raw_input.get("pattern") or raw_input.get("query")
        if not isinstance(pattern, str) or not pattern.strip() or _looks_like_shell_pipeline(pattern):
            pattern = title
        if (
            isinstance(pattern, str)
            and pattern.strip()
            and not _looks_like_shell_pipeline(pattern)
            and pattern.strip().lower() not in {"grep", "search", "search_files", "glob", "find"}
        ):
            args["pattern"] = pattern.strip()
    elif hermes_name == "web_extract":
        args["url"] = str(
            raw_input.get("url") or raw_input.get("uri") or title
        )
    elif hermes_name == "process":
        args["action"] = str(raw_input.get("action") or "poll")
        args["session_id"] = str(
            raw_input.get("session_id") or raw_input.get("id") or ""
        )
    else:
        args = dict(raw_input)
        if path:
            args.setdefault("path", path)
        if command:
            args.setdefault("command", command)
    status = str(update.get("status") or "").strip().lower()
    if kind == "tool_call" and not status:
        status = "in_progress"
    result = update.get("rawOutput") or update.get("raw_output")
    if result is not None and not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False)
        except Exception:
            result = str(result)
    preview = command or path or title or raw_name
    return {
        "id": str(update.get("toolCallId") or update.get("tool_call_id") or raw_name),
        "name": hermes_name,
        "raw_name": raw_name,
        "preview": preview,
        "args": args,
        "status": status,
        "result": result,
        "is_error": status in {"failed", "error", "cancelled"},
    }


def format_acp_tool_progress_line(parsed: dict[str, Any]) -> str:
    """Honest one-line fallback when the CLI tool-progress callback is unbound."""
    name = str(parsed.get("name") or "tool")
    preview = str(parsed.get("preview") or "")
    status = str(parsed.get("status") or "")
    suffix = ""
    if status and status not in {"in_progress", "pending"}:
        suffix = f" ({status})"
    if name == "terminal":
        return f"💻 $ {preview}{suffix}".rstrip()
    if name == "process":
        return f"⚙️ proc {preview}{suffix}".rstrip()
    if name == "read_file":
        return f"📖 read {preview}{suffix}".rstrip()
    if name == "write_file":
        return f"✍️ write {preview}{suffix}".rstrip()
    if name == "patch":
        return f"🔧 patch {preview}{suffix}".rstrip()
    if name == "search_files":
        return f"🔎 grep {preview}{suffix}".rstrip()
    return f"⚡ {name} {preview}{suffix}".strip()


def build_hermes_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: str,
) -> ChatCompletionMessageToolCall:
    """Build a Hermes loop tool-call (``ChatCompletionMessageToolCall``)."""
    return ChatCompletionMessageToolCall(
        id=call_id,
        call_id=call_id,
        response_item_id=None,
        type="function",
        function=Function(name=name, arguments=arguments),
    )


def tool_specs_from_openai_tools(
    tools: list[dict[str, Any]] | None,
    *,
    allowlist: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten OpenAI ``tools`` into ``{name, description, parameters}`` specs.

    Malformed entries are skipped. When ``allowlist`` is given, only tools whose
    name is in it survive — that is how a client forwards just Hermes'
    agent-level tools instead of the whole toolset.
    """
    allowed = {str(n).strip() for n in allowlist} if allowlist is not None else None
    specs: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if allowed is not None and name not in allowed:
            continue
        specs.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    return specs


def render_tool_bridge_sections(
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
    *,
    allowlist: Iterable[str] | None = None,
) -> list[str]:
    """Prompt sections that carry the forwarded tool schemas + choice hint.

    Returns an empty list when no tool survives filtering and no choice hint was
    requested, so callers can splice the result into their section list
    unconditionally.
    """
    specs = tool_specs_from_openai_tools(tools, allowlist=allowlist)
    sections: list[str] = []
    if specs:
        sections.append(
            TOOL_CALL_CONTRACT + "\n" + json.dumps(specs, ensure_ascii=False)
        )
    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")
    return sections


def extract_tool_calls_from_text(
    text: str,
) -> tuple[list[ChatCompletionMessageToolCall], str]:
    """Pull ``<tool_call>`` blocks out of an ACP response.

    Returns ``(tool_calls, cleaned_text)`` where ``cleaned_text`` is the
    response with the consumed blocks removed, so the assistant message doesn't
    show raw JSON to the user.
    """
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[ChatCompletionMessageToolCall] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"acp_call_{len(extracted)+1}"

        extracted.append(
            build_hermes_tool_call(
                call_id=call_id,
                name=fn_name.strip(),
                arguments=fn_args,
            )
        )

    for m in TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        _try_add_tool_call(raw)
        consumed_spans.append((m.start(), m.end()))

    # Only try bare-JSON fallback when no XML blocks were found.
    if not extracted:
        for m in TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned


def _partial_open_tag_hold(text: str) -> int:
    for i in range(1, len(_TOOL_CALL_OPEN)):
        if text.endswith(_TOOL_CALL_OPEN[:i]):
            return i
    return 0


class LiveToolCallTextFilter:
    """Hold ``<tool_call>`` spans out of streamed assistant text.

    Codex paints function calls as tool cards, never as JSON in the bubble.
    ACP vendors emit the bridge contract as ordinary ``agent_message_chunk``
    text, then often keep talking as if the call was ignored. Buffer the
    tag, parse complete blocks, and drop everything after the first call.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._calls: list[ChatCompletionMessageToolCall] = []
        self._saw_call = False
        self.visible_parts: list[str] = []

    @property
    def visible_text(self) -> str:
        return "".join(self.visible_parts)

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        visible: list[str] = []
        while True:
            start = self._buf.find(_TOOL_CALL_OPEN)
            if start < 0:
                if self._saw_call:
                    hold = _partial_open_tag_hold(self._buf)
                    self._buf = self._buf[-hold:] if hold else ""
                    break
                hold = _partial_open_tag_hold(self._buf)
                if hold:
                    visible.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                else:
                    visible.append(self._buf)
                    self._buf = ""
                break
            if start > 0:
                if not self._saw_call:
                    visible.append(self._buf[:start])
                self._buf = self._buf[start:]
            close = self._buf.find(_TOOL_CALL_CLOSE)
            if close < 0:
                break
            block = self._buf[: close + len(_TOOL_CALL_CLOSE)]
            calls, _ = extract_tool_calls_from_text(block)
            self._calls.extend(calls)
            self._saw_call = True
            self._buf = self._buf[close + len(_TOOL_CALL_CLOSE) :]
        out = "".join(visible)
        if out:
            self.visible_parts.append(out)
        return out

    def flush(self) -> tuple[str, list[ChatCompletionMessageToolCall]]:
        leftover = self._buf
        self._buf = ""
        more, _ = extract_tool_calls_from_text(leftover)
        self._calls.extend(more)
        if more:
            self._saw_call = True
        if self._saw_call or leftover.strip().startswith(_TOOL_CALL_OPEN):
            return "", list(self._calls)
        if leftover:
            self.visible_parts.append(leftover)
        return leftover, list(self._calls)
