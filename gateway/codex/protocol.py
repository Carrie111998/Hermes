"""Stable bridge protocol constants, records, and executor contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


BRIDGE_PHASES = frozenset(
    {"captured", "working", "needs_user", "output_ready", "done", "failed"}
)
TERMINAL_PHASES = frozenset({"done", "failed"})
_DEFAULT_COMMAND_PREFIX = "/codex"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def request_fingerprint(prompt: str) -> str:
    """Return a non-reversible identity for idempotency collision checks."""

    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class BridgeOrigin:
    type: str
    conversation_id: str
    message_id: str
    user_id: str | None = None
    thread_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "type": self.type,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
        }
        if self.user_id:
            result["user_id"] = self.user_id
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result


@dataclass(frozen=True)
class BridgeRequest:
    hermes_job_id: str
    idempotency_key: str
    origin: BridgeOrigin
    workspace: str
    prompt: str


@dataclass(frozen=True)
class BridgeReply:
    prompt_id: str
    idempotency_key: str
    origin: BridgeOrigin
    answer: str


@dataclass(frozen=True)
class ProgressEvent:
    event_id: str
    task_id: str
    executor: str
    phase: str
    summary: str
    origin: dict[str, str]
    created_at: str
    idempotency_key: str
    progress: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase not in BRIDGE_PHASES:
            raise ValueError(f"Unsupported bridge phase: {self.phase}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "executor": self.executor,
            "phase": self.phase,
            "summary": self.summary,
            "progress": self.progress,
            "origin": self.origin,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class BridgeMapping:
    hermes_job_id: str
    idempotency_key: str
    request_fingerprint: str
    codex_thread_id: str | None
    origin: dict[str, str]
    workspace: str
    phase: str
    final_result: str | None
    artifacts: tuple[str, ...]
    owner_instance_id: str
    updated_at: str


@dataclass(frozen=True)
class CaptureResult:
    mapping: BridgeMapping
    should_execute: bool
    recovered: bool = False


@dataclass(frozen=True)
class PendingQuestion:
    prompt_id: str
    hermes_job_id: str
    question: str
    origin: dict[str, str]
    status: str


@dataclass(frozen=True)
class BridgeReplyMapping:
    reply_id: str
    hermes_job_id: str
    prompt_id: str
    idempotency_key: str
    origin: dict[str, str]
    answer: str
    phase: str
    final_result: str | None
    owner_instance_id: str
    updated_at: str


@dataclass(frozen=True)
class ReplyCaptureResult:
    mapping: BridgeReplyMapping
    job: BridgeMapping
    should_execute: bool
    recovered: bool = False


@dataclass(frozen=True)
class BridgeExecutionResult:
    final_response: str
    artifacts: tuple[str, ...] = ()


class CodexExecutor(Protocol):
    def execute(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        on_thread: Callable[[str], None],
        on_progress: Callable[[str, str], None],
    ) -> str | BridgeExecutionResult: ...


class BridgeEventProjector(Protocol):
    """Optional non-critical consumer of durable public bridge events."""

    def project_pending(self) -> int: ...
