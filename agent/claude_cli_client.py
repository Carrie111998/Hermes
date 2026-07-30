"""OpenAI-compatible facade backed by the official Claude Code process."""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from agent.claude_cli_process import (
    ClaudeCLIProcessRunner,
    ClaudeCLIStaleSessionError,
)
from agent.claude_cli_protocol import (
    build_bootstrap_prompt,
    build_resume_prompt,
    decision_schema_json,
    parse_decision,
    to_chat_completion,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fingerprint(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _resolve_session_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        from gateway.session_context import get_session_env

        contextual = get_session_env("HERMES_SESSION_ID", "")
        if contextual:
            return contextual
    except Exception:
        pass
    inherited = os.environ.get("HERMES_SESSION_ID", "").strip()
    return inherited or f"claude-cli-{uuid.uuid4()}"


def _system_fingerprint(messages: Iterable[Mapping[str, Any]]) -> str:
    systems = [
        message
        for message in messages
        if isinstance(message, Mapping) and message.get("role") == "system"
    ]
    return _fingerprint(systems)


def _tool_fingerprint(tools: Iterable[Mapping[str, Any]]) -> str:
    return _fingerprint(list(tools or []))


def _semantic_delta(messages: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if not messages:
        return []
    if messages[-1].get("role") == "tool":
        start = len(messages) - 1
        while start > 0 and messages[start - 1].get("role") == "tool":
            start -= 1
        return messages[start:]
    for message in reversed(messages):
        if message.get("role") != "system":
            return [message]
    return []


class _ClaudeCLICompletions:
    def __init__(self, owner: "ClaudeCLIClient"):
        self._owner = owner

    def create(self, **request_kwargs):
        return self._owner._create_completion(**request_kwargs)


class ClaudeCLIClient:
    """Expose Claude CLI through Hermes's existing completions client seam."""

    def __init__(
        self,
        *,
        model: str = "opus",
        session_db=None,
        session_id: str | None = None,
        executable: str = "claude",
        executable_args: list[str] | None = None,
        timeout_seconds: float = 600,
        runner: ClaudeCLIProcessRunner | None = None,
    ):
        self.model = model or "opus"
        self.session_db = session_db
        self.session_id = _resolve_session_id(session_id)
        self.runner = runner or ClaudeCLIProcessRunner(
            executable=executable,
            executable_args=executable_args,
            timeout_seconds=timeout_seconds,
        )
        self.api_key = "claude-cli-process"
        self.base_url = "claude-cli://local"
        self.chat = SimpleNamespace(completions=_ClaudeCLICompletions(self))
        self._closed = False
        self.provider_reported_model: str | None = None

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self.runner.close()
        self._closed = True

    def _get_attachment(self) -> dict[str, Any] | None:
        if self.session_db is None:
            return None
        return self.session_db.get_provider_attachment(
            self.session_id,
            "claude-cli",
        )

    def _delete_attachment(self) -> None:
        if self.session_db is not None:
            self.session_db.delete_provider_attachment(
                self.session_id,
                "claude-cli",
            )

    def _save_attachment(
        self,
        *,
        provider_session_id: str,
        model_requested: str,
        model_reported: str,
        tool_fingerprint: str,
        system_fingerprint: str,
    ) -> None:
        if self.session_db is None:
            return
        if self.session_db.get_session(self.session_id) is None:
            self.session_db.create_session(self.session_id, "claude-cli")
        self.session_db.upsert_provider_attachment(
            hermes_session_id=self.session_id,
            provider="claude-cli",
            provider_session_id=provider_session_id,
            model_requested=model_requested,
            model_reported=model_reported,
            tool_catalog_fingerprint=tool_fingerprint,
            system_prompt_fingerprint=system_fingerprint,
            last_success_at=time.time(),
        )

    @staticmethod
    def _attachment_is_compatible(
        attachment: Mapping[str, Any] | None,
        *,
        model: str,
        tool_fingerprint: str,
        system_fingerprint: str,
    ) -> bool:
        return bool(
            attachment
            and attachment.get("provider") == "claude-cli"
            and attachment.get("provider_session_id")
            and attachment.get("model_requested") == model
            and attachment.get("tool_catalog_fingerprint") == tool_fingerprint
            and attachment.get("system_prompt_fingerprint") == system_fingerprint
        )

    def _run_fresh(
        self,
        *,
        messages: list[Mapping[str, Any]],
        tools: list[Mapping[str, Any]],
        model: str,
    ):
        provider_session_id = str(uuid.uuid4())
        return self.runner.complete(
            prompt=build_bootstrap_prompt(messages=messages, tools=tools),
            schema_json=decision_schema_json(),
            model=model,
            new_session_id=provider_session_id,
        )

    def _create_completion(
        self,
        *,
        messages,
        tools=None,
        model=None,
        stream=False,
        **_ignored,
    ):
        if self._closed:
            raise RuntimeError("Claude CLI client is closed")
        message_list = list(messages or [])
        tool_list = list(tools or [])
        requested_model = model or self.model
        tool_fp = _tool_fingerprint(tool_list)
        system_fp = _system_fingerprint(message_list)
        attachment = self._get_attachment()

        if self._attachment_is_compatible(
            attachment,
            model=requested_model,
            tool_fingerprint=tool_fp,
            system_fingerprint=system_fp,
        ):
            try:
                result = self.runner.complete(
                    prompt=build_resume_prompt(
                        messages=_semantic_delta(message_list)
                    ),
                    schema_json=decision_schema_json(),
                    model=requested_model,
                    resume_session_id=attachment["provider_session_id"],
                )
            except ClaudeCLIStaleSessionError:
                self._delete_attachment()
                result = self._run_fresh(
                    messages=message_list,
                    tools=tool_list,
                    model=requested_model,
                )
        else:
            result = self._run_fresh(
                messages=message_list,
                tools=tool_list,
                model=requested_model,
            )

        decision = parse_decision(result.decision, tools=tool_list)
        self.provider_reported_model = result.model_reported
        self._save_attachment(
            provider_session_id=result.session_id,
            model_requested=requested_model,
            model_reported=result.model_reported or "",
            tool_fingerprint=tool_fp,
            system_fingerprint=system_fp,
        )
        return to_chat_completion(
            decision,
            model=requested_model,
            model_reported=result.model_reported,
        )


class _AsyncClaudeCLICompletions:
    def __init__(self, owner: "AsyncClaudeCLIClient"):
        self._owner = owner

    async def create(self, **request_kwargs):
        return await asyncio.to_thread(
            self._owner._sync_client.chat.completions.create,
            **request_kwargs,
        )


class AsyncClaudeCLIClient:
    """Thread-backed async view over a cancellation-aware Claude CLI client."""

    def __init__(self, sync_client: ClaudeCLIClient):
        self._sync_client = sync_client
        self._real_client = sync_client
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self.chat = SimpleNamespace(completions=_AsyncClaudeCLICompletions(self))

    async def close(self) -> None:
        await asyncio.to_thread(self._sync_client.close)

    def is_closed(self) -> bool:
        return self._sync_client.is_closed()
