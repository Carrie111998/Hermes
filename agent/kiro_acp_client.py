"""Thin OpenAI-shaped facade for `kiro-cli acp`.

Reuses the shared ACP stdio transport and acp_openai_bridge. Kiro already has
its own read/edit/exec tools — only Hermes agent-level tools are forwarded
through the text tool-bridge so Hermes does not re-run work Kiro finished.
"""

from __future__ import annotations

import json
import os
import shlex
from types import SimpleNamespace
from typing import Any, Iterable

from agent.acp_openai_bridge import (
    completion_to_stream_chunks,
    extract_tool_calls_from_text,
    render_tool_bridge_sections,
)
from agent.acp_stdio_transport import (
    AcpStdioTransport,
    acp_scheme_host,
    effective_timeout_seconds,
    is_acp_base_url,
    permission_allowed,
    resolve_acp_cwd,
)

ACP_MARKER_BASE_URL = "acp://kiro"

# Hermes agent-level tools only. Kiro owns read/edit/exec; re-offering those
# makes Hermes re-run work Kiro already finished (see acp_openai_bridge).
KIRO_ACP_TOOL_ALLOWLIST: tuple[str, ...] = (
    "memory",
    "todo",
    "skill_manage",
    "skill_view",
    "skills_list",
    "messaging",
    "react_to_message",
)

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

    Kiro permission prompts are answered with allow_once only. That is not
    Hermes taking over Kiro's exec tools — Kiro still owns read/edit/exec.
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


def format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    *,
    allowlist: Iterable[str] | None = KIRO_ACP_TOOL_ALLOWLIST,
) -> str:
    sections: list[str] = [
        "You are being used as the active ACP agent backend for Hermes.",
        "Use ACP capabilities to complete tasks.",
        "IMPORTANT: If you take an action with a Hermes agent-level tool, "
        "you MUST output tool calls using <tool_call>{...}</tool_call> blocks "
        "with JSON exactly in OpenAI function-call shape.",
        "If no Hermes tool is needed, answer normally using your own tools.",
    ]
    if model:
        sections.append(f"Hermes requested model hint: {model}")
    sections.extend(
        render_tool_bridge_sections(tools, tool_choice, allowlist=allowlist)
    )
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
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    sections.append("Continue the conversation from the latest user request.")
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
            permission_handler=permission_allowed,
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
            allowlist=KIRO_ACP_TOOL_ALLOWLIST,
        )
        response_text, reasoning_text = self._transport.run_prompt(
            prompt_text,
            timeout_seconds=effective_timeout_seconds(timeout),
        )
        tool_calls, cleaned_text = extract_tool_calls_from_text(response_text)
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        completion = SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=bound,
        )
        if stream:
            return completion_to_stream_chunks(completion)
        return completion


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
