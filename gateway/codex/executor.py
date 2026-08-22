"""Codex SDK adapter and structured user-question boundary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway.codex.protocol import BridgeExecutionResult, BridgeRequest
from gateway.codex.settings import CodexBridgeSettings


class CodexUserQuestion(RuntimeError):
    """A blocking structured question emitted by the Codex app server."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


_CODEX_USER_INPUT_METHODS = frozenset(
    {"item/tool/requestUserInput", "tool/requestUserInput"}
)


def _structured_codex_user_question(
    method: str, params: Mapping[str, Any] | None
) -> str | None:
    """Render a blocking Codex user-input request without parsing assistant text."""

    if method not in _CODEX_USER_INPUT_METHODS or not isinstance(params, Mapping):
        return None
    if params.get("isBlocking") is not True:
        return None
    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None

    rendered: list[str] = []
    for raw_question in raw_questions[:3]:
        if not isinstance(raw_question, Mapping):
            continue
        question = str(raw_question.get("question") or "").strip()
        if not question:
            continue
        header = str(raw_question.get("header") or "").strip()
        line = f"{header}: {question}" if header else question
        raw_options = raw_question.get("options")
        options: list[str] = []
        if isinstance(raw_options, list):
            for raw_option in raw_options[:3]:
                if not isinstance(raw_option, Mapping):
                    continue
                label = str(raw_option.get("label") or "").strip()
                description = str(raw_option.get("description") or "").strip()
                if label:
                    options.append(
                        f"{label} ({description})" if description else label
                    )
        if raw_question.get("isOther") is True:
            options.append("Câu trả lời khác")
        if options:
            line = f"{line}\nLựa chọn: {'; '.join(options)}"
        rendered.append(line)

    if not rendered:
        return None
    return "\n\n".join(rendered)[:4000]


def _unwrap_thread_item(item: Any) -> Any:
    return getattr(item, "root", item)


def _public_progress_for_item(item: Any) -> tuple[str, str] | None:
    """Map SDK item types to fixed summaries; never expose reasoning content."""

    item = _unwrap_thread_item(item)
    item_type = getattr(item, "type", None)
    if item_type == "commandExecution":
        return "execution", "Codex đang chạy và kiểm tra các lệnh trong workspace."
    if item_type == "fileChange":
        return "implementation", "Codex đã áp dụng một thay đổi tệp trong workspace."
    if item_type == "plan":
        return "planning", "Codex đã cập nhật kế hoạch thực thi."
    if item_type in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
        return "tooling", "Codex đang sử dụng một công cụ để tiếp tục task."
    return None


class CodexSdkExecutor:
    """Lazy Codex SDK adapter. Importing Hermes does not start app-server."""

    def __init__(self, settings: CodexBridgeSettings):
        self.settings = settings

    def execute(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        on_thread: Callable[[str], None],
        on_progress: Callable[[str, str], None],
    ) -> BridgeExecutionResult:
        try:
            from openai_codex import ApprovalMode, Codex, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "Codex bridge requires the 'codex-bridge' package extra"
            ) from exc

        sandbox = (
            Sandbox.read_only
            if self.settings.sandbox == "read-only"
            else Sandbox.workspace_write
        )
        def handle_server_request(
            method: str, params: Mapping[str, Any] | None
        ) -> dict[str, Any]:
            question = _structured_codex_user_question(method, params)
            if question:
                # Abort the SDK stream instead of supplying a fabricated
                # answer. Closing this app-server process stops the in-flight
                # turn; the durable bridge resumes the persisted thread only
                # after a correlated Hermes reply arrives.
                raise CodexUserQuestion(question)
            if method in _CODEX_USER_INPUT_METHODS:
                raise RuntimeError(
                    "Codex user-input request was non-blocking or invalid; "
                    "refusing to fabricate an answer"
                )
            return {}

        with Codex() as codex:
            sdk_client = getattr(codex, "_client", None)
            if sdk_client is None or not hasattr(sdk_client, "_approval_handler"):
                raise RuntimeError(
                    "Pinned Codex SDK does not expose server-request handling"
                )
            sdk_client._approval_handler = handle_server_request
            if codex_thread_id:
                thread = codex.thread_resume(
                    codex_thread_id,
                    cwd=request.workspace,
                    model=self.settings.model,
                    sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                )
            else:
                thread = codex.thread_start(
                    cwd=request.workspace,
                    model=self.settings.model,
                    sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                    developer_instructions=(
                        "You are executing a Hermes-originated Codex task. Keep progress "
                        "user-facing, do not reveal private reasoning, and finish with a "
                        "concise result suitable for delivery to the origin channel."
                    ),
                )
            on_thread(thread.id)
            on_progress("codex_start", "Codex thread đã bắt đầu xử lý request.")

            if self.settings.collaboration_mode == "plan":
                from openai_codex.api import TurnHandle

                model = self.settings.model
                if not model:
                    models = codex.models().data
                    model = next(item.model for item in models if item.is_default)
                started = codex._client.turn_start(
                    thread.id,
                    request.prompt,
                    params={
                        "approvalPolicy": "never",
                        "cwd": request.workspace,
                        "sandboxPolicy": {
                            "type": (
                                "readOnly"
                                if self.settings.sandbox == "read-only"
                                else "workspaceWrite"
                            )
                        },
                        "collaborationMode": {
                            "mode": "plan",
                            "settings": {
                                "model": model,
                                "reasoning_effort": "low",
                                "developer_instructions": None,
                            },
                        },
                    },
                )
                handle = TurnHandle(codex._client, thread.id, started.turn.id)
            else:
                handle = thread.turn(request.prompt)
            final_response: str | None = None
            artifacts: set[str] = set()
            for notification in handle.stream():
                # Reasoning notifications and reasoning thread items are ignored.
                if notification.method not in {"item/started", "item/completed"}:
                    continue
                item = getattr(notification.payload, "item", None)
                public_progress = _public_progress_for_item(item)
                if public_progress and notification.method == "item/started":
                    on_progress(*public_progress)
                unwrapped = _unwrap_thread_item(item)
                if (
                    notification.method == "item/completed"
                    and getattr(unwrapped, "type", None) == "fileChange"
                ):
                    root = Path(request.workspace).resolve()
                    for change in getattr(unwrapped, "changes", ()):
                        raw_path = str(getattr(change, "path", "") or "").strip()
                        if not raw_path:
                            continue
                        candidate = Path(raw_path)
                        if not candidate.is_absolute():
                            candidate = root / candidate
                        try:
                            resolved = candidate.resolve()
                            root_norm = os.path.normcase(str(root))
                            resolved_norm = os.path.normcase(str(resolved))
                            if os.path.commonpath((root_norm, resolved_norm)) == root_norm:
                                artifacts.add(str(resolved))
                        except (OSError, ValueError):
                            continue
                if (
                    notification.method == "item/completed"
                    and getattr(unwrapped, "type", None) == "agentMessage"
                ):
                    phase = getattr(getattr(unwrapped, "phase", None), "value", None)
                    if phase == "final_answer" or phase == "finalAnswer":
                        final_response = getattr(unwrapped, "text", None)
                    elif phase is None:
                        final_response = getattr(unwrapped, "text", None)
            if not final_response:
                raise RuntimeError("Codex turn completed without a final response")
            return BridgeExecutionResult(final_response, tuple(sorted(artifacts)))
