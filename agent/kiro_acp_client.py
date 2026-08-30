"""OpenAI-shaped facade: Kiro is the model, Hermes is the agent.

kiro-cli has no inference-only HTTP API, so this client speaks ACP stdio
and treats the first structured ``session/update`` tool_call as Codex
``finish_reason=tool_calls``: cancel the Kiro turn, map execute/read/write
onto Hermes tools, and let ``conversation_loop`` execute them.

Each Hermes completion opens a new ACP ``session/new`` and rebuilds the
transcript in ``format_messages_as_prompt``. That is intentional: do not
keep a long-lived Kiro session that replays history (that inverts
agent/model). After a tool, ``conversation_loop`` calls the model again,
so the CLI paints another ⚕ Hermes box — same as a new Codex completion,
not an agent reinit. Empty execute stubs must never become tool_calls or
that loop looks like a restart.

Do not teach Kiro an XML ``<tool_call>`` protocol. Do not dump Hermes
schemas into the prompt. Do not let Kiro run tools or write through the
ACP fs bridge.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import threading
from types import SimpleNamespace
from typing import Any, Iterable

from agent.acp_tool_bridge import (
    completion_to_stream_chunks,
    make_delta_chunk,
    make_usage_chunk,
    hermes_tool_call_from_acp,
)
from agent.acp_stdio_transport import (
    AcpStdioTransport,
    acp_scheme_host,
    effective_timeout_seconds,
    is_acp_base_url,
    permission_denied,
    resolve_acp_cwd,
)

ACP_MARKER_BASE_URL = "acp://kiro"

_MISSING_HINT = (
    "Install Kiro CLI (`kiro-cli`) and run `kiro-cli login`, "
    "or set KIRO_CLI_PATH / HERMES_KIRO_ACP_COMMAND."
)


def resolve_kiro_command() -> str:
    return (
        os.getenv("HERMES_KIRO_ACP_COMMAND", "").strip()
        or os.getenv("KIRO_CLI_PATH", "").strip()
        or "kiro-cli"
    )


def resolve_kiro_args(*, model: str | None = None) -> list[str]:
    """Default argv is `acp --model <slug>`. No --trust-all-tools.

    Native Kiro tool permissions are denied. Hermes executes the mapped
    tool_calls in its own loop, same as openai-codex.
    """
    raw = os.getenv("HERMES_KIRO_ACP_ARGS", "").strip()
    args = shlex.split(raw) if raw else ["acp"]
    return _args_with_model(args, model)


def _args_with_model(args: list[str], model: str | None) -> list[str]:
    out = list(args)
    selected = (model or "").strip()
    if not selected:
        return out
    if "--model" in out:
        idx = out.index("--model")
        if idx + 1 < len(out):
            out[idx + 1] = selected
        else:
            out.append(selected)
        return out
    out.extend(["--model", selected])
    return out


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _tool_call_summaries(message: dict[str, Any]) -> list[str]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for tool_call in raw:
        if isinstance(tool_call, dict):
            fn = tool_call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else ""
        else:
            fn = getattr(tool_call, "function", None)
            name = getattr(fn, "name", "") if fn is not None else ""
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    *,
    allowlist: Iterable[str] | None = None,
) -> str:
    del tools, tool_choice, allowlist
    sections: list[str] = []
    if model:
        sections.append(f"Model: {model}")
    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role == "tool":
            role = "tool"
        elif role not in {"system", "user", "assistant"}:
            role = "context"
        rendered = _render_message_content(message.get("content"))
        if role == "assistant":
            names = _tool_call_summaries(message)
            if names:
                extra = "Hermes tools requested: " + ", ".join(names)
                rendered = f"{rendered}\n{extra}".strip() if rendered else extra
        elif role == "tool":
            name = str(message.get("name") or message.get("tool_name") or "tool").strip()
            call_id = str(message.get("tool_call_id") or "").strip()
            header = f"{name} ({call_id})" if call_id else name
            rendered = f"{header} result:\n{rendered or '(empty)'}"
        if not rendered:
            continue
        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
            "context": "Context",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")
    if transcript:
        sections.append("\n\n".join(transcript))
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


class _ACPChatCompletions:
    def __init__(self, client: "KiroACPClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ACPChatNamespace:
    def __init__(self, client: "KiroACPClient"):
        self.completions = _ACPChatCompletions(client)


class KiroACPClient:
    """Minimal OpenAI-client-compatible facade for Kiro ACP."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        model: str | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "kiro-acp"
        self.base_url = base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._requested_model = (model or "").strip()
        self._bound_model = self._requested_model
        self._acp_command = acp_command or command or resolve_kiro_command()
        self._acp_args = list(acp_args or args or resolve_kiro_args(model=self._requested_model))
        self._acp_cwd = resolve_acp_cwd(acp_cwd)
        self.chat = _ACPChatNamespace(self)
        self._transport = self._make_transport()

    def _make_transport(self) -> AcpStdioTransport:
        return AcpStdioTransport(
            command=self._acp_command,
            args=self._acp_args,
            cwd=self._acp_cwd,
            vendor_label="Kiro ACP",
            missing_hint=_MISSING_HINT,
            permission_handler=lambda message_id, _options: permission_denied(message_id),
            inherit_credentials=False,
        )

    def _rebuild_transport(self, model: str) -> None:
        self._transport.close()
        self._requested_model = model
        self._bound_model = model
        self._acp_args = _args_with_model(self._acp_args, model)
        self._transport = self._make_transport()

    def _ensure_model_binding(self, model: str | None) -> str:
        requested = (model or self._requested_model or "claude-opus-5").strip()
        if self._bound_model and requested != self._bound_model:
            self._rebuild_transport(requested)
        elif not self._bound_model:
            self._rebuild_transport(requested)
        return requested

    @property
    def is_closed(self) -> bool:
        return self._transport.is_closed

    @property
    def _active_process(self) -> Any:
        return self._transport.active_process

    def close(self) -> None:
        self._transport.close()

    def _args_ready(self, parsed: dict[str, Any]) -> bool:
        name = str(parsed.get("name") or "")
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        if name == "terminal":
            command = args.get("command")
            return isinstance(command, str) and bool(command.strip())
        if name == "read_file":
            return bool(args.get("path"))
        if name == "write_file":
            return bool(args.get("path") and args.get("content") is not None)
        if name == "patch":
            return bool(
                args.get("path")
                and args.get("old_string") is not None
                and args.get("new_string") is not None
            )
        if name == "search_files":
            return bool(args.get("pattern"))
        if name == "web_extract":
            return bool(args.get("url"))
        if name in {"memory", "mcp__qmd__query"}:
            return bool(args)
        return bool(args)

    @staticmethod
    def _call_is_terminal_stub(call: Any) -> bool:
        fn = getattr(call, "function", None)
        if fn is None or getattr(fn, "name", None) != "terminal":
            return False
        try:
            raw = json.loads(getattr(fn, "arguments", None) or "{}")
        except (TypeError, ValueError):
            return True
        command = raw.get("command") if isinstance(raw, dict) else None
        return not (isinstance(command, str) and command.strip())

    def _drop_terminal_stubs(self, bucket: list[Any]) -> None:
        """Remove command-less terminal calls so a later real id wins."""
        bucket[:] = [item for item in bucket if not self._call_is_terminal_stub(item)]

    def _intercept_tool(self, parsed: dict[str, Any], bucket: list[Any]) -> Any:
        """Map one ACP toolCallId onto at most one ready Hermes tool_call.

        Incomplete execute snapshots (kind=execute, no rawInput.command) must
        not enter the bucket. A later update with the same id replaces in
        place; a ready call with a different id drops any leftover stubs.
        Cancel the Kiro turn only once args are actually executable.
        """
        if not self._args_ready(parsed):
            # Same id may already sit in the bucket from a prior richer
            # snapshot; do not resurrect a command=None stub beside it.
            pending_id = str(parsed.get("id") or "").strip()
            if pending_id:
                bucket[:] = [
                    item
                    for item in bucket
                    if getattr(item, "id", None) != pending_id
                    or not self._call_is_terminal_stub(item)
                ]
            return None
        call = hermes_tool_call_from_acp(parsed)
        if call is None:
            return None
        self._drop_terminal_stubs(bucket)
        existing = next(
            (item for item in bucket if getattr(item, "id", None) == call.id),
            None,
        )
        if existing is None:
            bucket.append(call)
        else:
            existing.function.name = call.function.name
            existing.function.arguments = call.function.arguments
        self._transport.cancel_prompt()
        return call

    def _finish_completion(
        self,
        *,
        bound: str,
        response_text: str,
        reasoning_text: str,
        usage: Any = None,
        intercepted: list[Any] | None = None,
    ) -> SimpleNamespace:
        tool_calls = [
            call for call in (intercepted or []) if not self._call_is_terminal_stub(call)
        ]
        cleaned_text = (response_text or "").strip()
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=bound,
        )

    def _iter_live_chunks(
        self,
        *,
        bound: str,
        prompt_text: str,
        timeout: float | None,
    ) -> Any:
        """Yield OpenAI-shaped chunks as ACP session/update arrives.

        Native Kiro execute/read/write become Hermes tool_call deltas so the
        conversation loop runs them — same as openai-codex function calls.
        """
        events: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        box: dict[str, Any] = {
            "err": None,
            "text": "",
            "reasoning": "",
            "usage": None,
            "intercepted": [],
            "visible": "",
        }

        def on_update(kind: str, payload: Any) -> None:
            if kind == "tool" and isinstance(payload, dict):
                self._intercept_tool(payload, box["intercepted"])
            events.put((kind, payload))

        def worker() -> None:
            try:
                text, reasoning = self._transport.run_prompt(
                    prompt_text,
                    timeout_seconds=effective_timeout_seconds(timeout),
                    on_update=on_update,
                )
                box["text"] = text
                box["reasoning"] = reasoning
                box["usage"] = getattr(self._transport, "last_usage", None)
            except BaseException as exc:
                box["err"] = exc
            finally:
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        saw_live = False
        while True:
            item = events.get()
            if item is None:
                break
            kind, payload = item
            if kind == "text" and payload:
                if box["intercepted"]:
                    continue
                saw_live = True
                box["visible"] += str(payload)
                yield make_delta_chunk(bound, content=str(payload))
            elif kind == "reasoning" and payload:
                if box["intercepted"]:
                    continue
                saw_live = True
                yield make_delta_chunk(bound, reasoning=str(payload))
            elif kind == "tool":
                continue
            elif kind == "heartbeat":
                yield SimpleNamespace(choices=[], model=bound, usage=None)
            elif kind == "usage":
                box["usage"] = payload
        thread.join()
        if box["err"] is not None:
            raise box["err"]
        completion = self._finish_completion(
            bound=bound,
            response_text=str(box["visible"] if box["intercepted"] else (box["text"] or "")),
            reasoning_text=str(box["reasoning"] or ""),
            usage=box["usage"],
            intercepted=box["intercepted"],
        )
        if not saw_live:
            yield from completion_to_stream_chunks(completion)
            return
        # Text/reasoning already streamed. Flush Hermes-executable
        # tool_calls from intercepted ACP intents.
        # Always emit a terminal finish_reason so Hermes does not treat
        # iterator end as "stream ended before completion".
        tool_calls = completion.choices[0].message.tool_calls
        if tool_calls:
            deltas = []
            for index, tool_call in enumerate(tool_calls):
                deltas.append(
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
            yield make_delta_chunk(
                bound, tool_calls=deltas, finish_reason="tool_calls"
            )
        else:
            yield make_delta_chunk(
                bound,
                finish_reason=completion.choices[0].finish_reason or "stop",
            )
        if completion.usage is not None:
            yield make_usage_chunk(bound, completion.usage)

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        bound = self._ensure_model_binding(model)
        prompt_text = format_messages_as_prompt(
            messages or [],
            model=bound,
            tools=tools,
            tool_choice=tool_choice,
        )
        if stream:
            return self._iter_live_chunks(
                bound=bound,
                prompt_text=prompt_text,
                timeout=timeout,
            )
        intercepted: list[Any] = []
        visible: list[str] = []

        def on_update(kind: str, payload: Any) -> None:
            if kind == "text" and payload and not intercepted:
                visible.append(str(payload))
            elif kind == "tool" and isinstance(payload, dict):
                self._intercept_tool(payload, intercepted)

        response_text, reasoning_text = self._transport.run_prompt(
            prompt_text,
            timeout_seconds=effective_timeout_seconds(timeout),
            on_update=on_update,
        )
        return self._finish_completion(
            bound=bound,
            response_text="".join(visible) if intercepted else response_text,
            reasoning_text=reasoning_text,
            usage=getattr(self._transport, "last_usage", None),
            intercepted=intercepted,
        )


def build_acp_client(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> Any:
    """Dispatch an ACP OpenAI facade from scheme / provider, not one vendor.

    acp://kiro and provider=kiro-acp build KiroACPClient. Everything else that
    is still an ACP URL (including acp://copilot) stays on CopilotACPClient so
    Copilot behaviour is unchanged.
    """
    host = acp_scheme_host(base_url)
    slug = str(provider or "").strip().lower()
    if slug == "kiro-acp" or host == "kiro":
        return KiroACPClient(base_url=base_url or ACP_MARKER_BASE_URL, **kwargs)
    if slug == "copilot-acp" or host == "copilot":
        from agent.copilot_acp_client import CopilotACPClient

        return CopilotACPClient(base_url=base_url or "acp://copilot", **kwargs)
    raise ValueError(
        f"Unknown ACP vendor provider={provider!r} base_url={base_url!r}. "
        "Refusing to fall through to CopilotACPClient."
    )


def should_use_acp_client(*, provider: str | None = None, base_url: str | None = None) -> bool:
    """True for known ACP vendors. Keyed on provider slug OR acp:// host.

    acp://kiro must not fall through to the OpenAI HTTP client. Unknown
    acp:// hosts stay on the mocked/generic path so the scheme-based
    stream/Responses rails keep working for the next vendor.
    """
    slug = str(provider or "").strip().lower()
    if slug in {"kiro-acp", "copilot-acp"}:
        return True
    return acp_scheme_host(base_url) in {"kiro", "copilot"}
