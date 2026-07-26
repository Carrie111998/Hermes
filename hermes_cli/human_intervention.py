"""Fail-closed bridge for optional plugin-backed human intervention.

The CLI owns modal UI state, local input queues, deadlines, command risk, and
all final approval decisions. An optional provider may only create an external
wait record and return a constrained candidate signal for the CLI to validate.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

InterventionKind = Literal["approval", "sudo", "clarify", "computer_use"]
InterventionAction = Literal["deny", "extend", "approve_once"]
InterventionOutcome = Literal[
    "local_response",
    "remote_deny",
    "remote_approve_once",
    "timeout",
    "cancelled",
]

# This is a non-configurable authorization floor. A plugin record or user
# configuration may narrow permissions, but may never make critical commands
# remotely approvable.
NEVER_REMOTE_APPROVE_LEVELS = frozenset({"critical"})


@dataclass(frozen=True)
class HumanInterventionRequest:
    """Core-sanitized description of a local wait exposed to a provider."""

    kind: InterventionKind
    session_key: str
    title: str
    preview: str
    timeout_seconds: int | None
    risk_level: str = ""
    allowed_actions: frozenset[InterventionAction] = field(default_factory=frozenset)


@dataclass(frozen=True)
class HumanInterventionSignal:
    """A provider's candidate decision, subject to core validation."""

    action: InterventionAction
    extend_seconds: int = 0
    source: str = ""


class HumanInterventionProvider(ABC):
    """Optional external control channel for a CLI-owned local wait.

    Providers must never receive passwords, clarify answers, local queue
    objects, or authority to return session/permanent approvals.
    """

    @abstractmethod
    def begin(self, request: HumanInterventionRequest) -> object | None:
        """Best-effort open; return a provider-private handle or ``None``."""

    @abstractmethod
    def poll(self, handle: object) -> HumanInterventionSignal | None:
        """Return at most one non-blocking candidate signal."""

    @abstractmethod
    def finish(self, handle: object, outcome: InterventionOutcome) -> None:
        """Best-effort idempotent cleanup; exceptions are always contained."""


def begin_human_intervention(
    provider: HumanInterventionProvider | None,
    request: HumanInterventionRequest,
) -> object | None:
    """Open a provider record without allowing provider failure to affect UI."""
    if provider is None:
        return None
    try:
        return provider.begin(request)
    except Exception:
        return None


def take_remote_signal(
    provider: HumanInterventionProvider | None,
    handle: object | None,
    request: HumanInterventionRequest,
) -> HumanInterventionSignal | None:
    """Poll and fail-closed validate one provider candidate signal.

    This function deliberately does not make a local UI decision. Callers must
    check their local response queue before claiming a remote signal so local
    input always wins the race.
    """
    if provider is None or handle is None:
        return None
    try:
        signal = provider.poll(handle)
    except Exception:
        return None
    if not isinstance(signal, HumanInterventionSignal):
        return None
    if signal.action not in request.allowed_actions:
        return None
    if signal.action == "extend":
        return signal if signal.extend_seconds > 0 else None
    if signal.action == "deny":
        return signal
    if signal.action != "approve_once":
        return None
    if request.kind != "approval":
        return None
    if request.risk_level.strip().lower() in NEVER_REMOTE_APPROVE_LEVELS:
        return None
    return signal


def finish_human_intervention(
    provider: HumanInterventionProvider | None,
    handle: object | None,
    outcome: InterventionOutcome,
) -> None:
    """Best-effort provider cleanup that cannot change local wait semantics."""
    if provider is None or handle is None:
        return
    try:
        provider.finish(handle, outcome)
    except Exception:
        return
