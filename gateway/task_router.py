"""Risk-based task router for incoming Telegram tasks.

Classifies an inbound Telegram task's risk (LOW/MEDIUM/HIGH/CRITICAL) and
selects which agent/model is allowed to run it:

  - LOW/MEDIUM    -> gpt-5.6-luna via the openai-codex provider. No approval
                      gate; the task proceeds immediately.
  - HIGH/CRITICAL -> paused pending explicit Telegram approval. Only after an
                      authorized approver approves does it route to
                      claude-sonnet-5 via the anthropic provider.

DeepSeek is never selected by this router, at any risk level, and never used
as a fallback (see ``_FORBIDDEN_PROVIDER_SUBSTRINGS``).

This module owns its own pending-approval registry (task_id keyed,
thread-safe, atomic approve/reject/consume). It is intentionally separate
from ``tools.approval``'s dangerous-shell-command queue: task-routing
approval (which *agent* runs a whole task) and in-turn command approval
(whether one shell command inside an already-running turn is allowed) are
different concerns with different payload shapes and lifecycles. Keeping
them apart means this module can be added, tested, and reasoned about
without touching the existing, heavily-exercised command-approval logic.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("gateway.task_router")

_AUDIT_TEXT_PREVIEW_CHARS = 200
_LONG_TASK_MEDIUM_THRESHOLD = 4000


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"


@dataclass(frozen=True)
class AgentSelection:
    provider: str
    model: str


# The only two agent selections this router may ever return. DeepSeek (or
# any other provider) must never be selected here, at any risk level, and
# never substituted as a fallback.
_LOW_MEDIUM_AGENT = AgentSelection(provider="openai-codex", model="gpt-5.6-luna")
_HIGH_CRITICAL_AGENT = AgentSelection(provider="anthropic", model="claude-sonnet-5")

_FORBIDDEN_PROVIDER_SUBSTRINGS = ("deepseek",)


def _assert_not_forbidden(selection: AgentSelection) -> AgentSelection:
    provider = (selection.provider or "").lower()
    model = (selection.model or "").lower()
    for bad in _FORBIDDEN_PROVIDER_SUBSTRINGS:
        if bad in provider or bad in model:
            raise RuntimeError(
                f"task_router: refusing forbidden agent selection {selection!r}"
            )
    return selection


# --- Risk classification ----------------------------------------------------
#
# Keyword/regex heuristics only. ``text`` is untrusted user input: it is
# matched read-only against these patterns and is never evaluated, executed,
# or interpolated into a shell/SQL/format string.

_CRITICAL_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bdrop\s+(table|database)\b",
    r"\bdelete\s+from\b",
    r"\bformat\s+c:",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bwipe\b",
    r"\brevoke\b",
    r"\bproduction\b.*\b(deploy|delete|migrat)",
    r"\bcredentials?\b",
    r"\bapi[_ ]?keys?\b",
    r"\bsecrets?\b",
    r"\bprivate[_ ]?keys?\b",
    r"\bpayment\b",
    r"\btransfer\s+(funds|money)\b",
    r"\bwire\s+transfer\b",
    r"\bpush\s+--force\b",
    r"\bforce[- ]push\b",
    r"\bdeploy\s+to\s+prod",
    r"\bchmod\s+777\b",
    r"\bsudo\s+rm\b",
    r"\bdisable\s+(firewall|auth)\b",
]

_HIGH_PATTERNS = [
    r"\bdeploy\b",
    r"\bmigrat(e|ion)\b",
    r"\bdrop\b",
    r"\bdelete\b",
    r"\boverwrite\b",
    r"\breset\s+(database|db)\b",
    r"\bgit\s+push\b",
    r"\bpublish\b",
    r"\brelease\b",
    r"\bgrant\s+access\b",
    r"\bchange\s+password\b",
    r"\brotate\s+(key|secret)\b",
    r"\bmerge\s+to\s+main\b",
    r"\bkill\s+process\b",
    r"\brls\b",
    r"\brow[- ]level\s+security\b",
    r"\bsecurity\s+polic(?:y|ies)\b",
    r"\brbac\b",
    r"\bauthentication\b",
]


def classify_task_risk(text: Optional[str]) -> RiskLevel:
    """Classify the risk of an inbound task's free-text request.

    Pure and read-only: pattern-matches ``text`` and never executes,
    evaluates, or shells out to it. Defaults to LOW when the text is empty
    or matches nothing risky — HIGH/CRITICAL tasks are recognizable by
    clearly destructive or sensitive verbs, so an unmatched task is the
    safe default rather than a missed one.
    """
    if not text or not text.strip():
        return RiskLevel.LOW
    lowered = text.lower()
    for pattern in _CRITICAL_PATTERNS:
        if re.search(pattern, lowered):
            return RiskLevel.CRITICAL
    for pattern in _HIGH_PATTERNS:
        if re.search(pattern, lowered):
            return RiskLevel.HIGH
    if len(text) > _LONG_TASK_MEDIUM_THRESHOLD:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def select_agent_for_risk(risk: RiskLevel) -> AgentSelection:
    """Map a risk level to the agent/model allowed to run it.

    LOW/MEDIUM route directly to gpt-5.6-luna (OpenAI Codex).
    HIGH/CRITICAL route to claude-sonnet-5 (Anthropic) — callers MUST NOT
    execute a HIGH/CRITICAL task with this selection until it has cleared
    the approval gate in :class:`TaskApprovalRegistry`.
    """
    if risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
        return _assert_not_forbidden(_LOW_MEDIUM_AGENT)
    if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        return _assert_not_forbidden(_HIGH_CRITICAL_AGENT)
    raise ValueError(f"unknown risk level: {risk!r}")


def requires_approval(risk: RiskLevel) -> bool:
    return risk in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def infer_environment(text: Optional[str]) -> str:
    """Infer the affected environment for a human approval summary."""
    lowered = (text or "").lower()
    if re.search(r"\bproduction\b|\bprod\b", lowered):
        return "production"
    if re.search(r"\bstaging\b|\bstage\b", lowered):
        return "staging"
    return "local"


def approval_reason(risk: RiskLevel) -> str:
    if risk == RiskLevel.CRITICAL:
        return "destructive, production-sensitive, or secret-bearing operation"
    if risk == RiskLevel.HIGH:
        return "architecture, security, access-control, database, or deployment change"
    return "routine task"


def classify_and_select(text: Optional[str]) -> tuple[RiskLevel, AgentSelection]:
    """Convenience: classify risk then select the corresponding agent."""
    risk = classify_task_risk(text)
    return risk, select_agent_for_risk(risk)


# --- Task approval registry --------------------------------------------------


def _preview(text: str, limit: int = _AUDIT_TEXT_PREVIEW_CHARS) -> str:
    text = (text or "").replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass
class TaskRoute:
    """One inbound Telegram task's risk-routing + approval state."""

    task_id: str
    session_key: str
    chat_id: str
    thread_id: str
    user_id: str
    request_text: str
    risk: RiskLevel
    agent: AgentSelection
    status: TaskApprovalStatus
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    executed_at: Optional[float] = None
    execution_status: str = "not_started"
    prompt_sent: bool = False


def _copy(route: TaskRoute) -> TaskRoute:
    return dataclasses.replace(route)


class TaskApprovalRegistry:
    """Thread-safe registry for task risk-routing + approval state.

    Mirrors the atomic pop/consume idempotency pattern used by
    ``tools.approval``'s gateway approval queue, but keyed by ``task_id`` and
    carrying task-routing fields (risk, selected agent) rather than
    shell-command payloads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRoute] = {}
        self._dedupe: dict[str, str] = {}  # dedupe_key -> task_id

    def create_or_get(
        self,
        *,
        dedupe_key: Optional[str],
        session_key: str,
        chat_id: str,
        thread_id: str,
        user_id: str,
        request_text: str,
        risk: RiskLevel,
        agent: AgentSelection,
    ) -> TaskRoute:
        """Create a new task route, or return the existing one for ``dedupe_key``.

        Idempotent duplicate protection: a redelivered Telegram update (same
        ``dedupe_key``, e.g. ``f"{session_key}:{message_id}"``) resolves to
        the SAME task rather than spawning a second approval request or a
        second execution.
        """
        with self._lock:
            if dedupe_key:
                existing_id = self._dedupe.get(dedupe_key)
                if existing_id is not None:
                    existing = self._tasks.get(existing_id)
                    # Telegram retries preserve the original payload.  Do
                    # not let a reused/malformed message id make a different
                    # request inherit the earlier route in this process.
                    if existing is not None and existing.request_text == request_text:
                        return _copy(existing)
            task_id = uuid.uuid4().hex
            auto_approved = not requires_approval(risk)
            status = TaskApprovalStatus.APPROVED if auto_approved else TaskApprovalStatus.PENDING
            route = TaskRoute(
                task_id=task_id,
                session_key=session_key,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                request_text=request_text,
                risk=risk,
                agent=agent,
                status=status,
            )
            if auto_approved:
                route.decided_at = time.time()
                route.decided_by = "auto:risk_router"
            self._tasks[task_id] = route
            if dedupe_key:
                self._dedupe[dedupe_key] = task_id
        _audit("created", route)
        return _copy(route)

    def get(self, task_id: str) -> Optional[TaskRoute]:
        with self._lock:
            route = self._tasks.get(task_id)
            return _copy(route) if route else None

    def claim_prompt(self, task_id: str) -> Optional[TaskRoute]:
        """Atomically claim the one approval prompt for a pending task."""
        with self._lock:
            route = self._tasks.get(task_id)
            if route is None or route.status != TaskApprovalStatus.PENDING:
                return None
            if route.prompt_sent:
                return None
            route.prompt_sent = True
            snapshot = _copy(route)
        _audit("prompt_claimed", snapshot)
        return snapshot

    def release_prompt_claim(self, task_id: str) -> None:
        """Allow a failed delivery attempt to be retried safely."""
        with self._lock:
            route = self._tasks.get(task_id)
            if route is not None and route.status == TaskApprovalStatus.PENDING:
                route.prompt_sent = False

    def approve(self, task_id: str, decided_by: str) -> Optional[TaskRoute]:
        """Atomically verify PENDING and transition to APPROVED.

        Returns ``None`` if the task is unknown or not pending — including
        a second call for a task already approved/rejected. Callers MUST
        treat ``None`` as "already handled", never retry as a fresh
        approval. This is the sole authorization + atomic-consume point for
        the approval decision itself (execution is gated separately by
        :meth:`consume_for_execution`).
        """
        with self._lock:
            route = self._tasks.get(task_id)
            if route is None or route.status != TaskApprovalStatus.PENDING:
                return None
            route.status = TaskApprovalStatus.APPROVED
            route.decided_at = time.time()
            route.decided_by = decided_by
        _audit("approved", route)
        return _copy(route)

    def reject(self, task_id: str, decided_by: str) -> Optional[TaskRoute]:
        """Atomically verify PENDING and transition to REJECTED.

        Returns ``None`` if the task is unknown or not pending. A rejected
        task can never later be approved or executed — :meth:`approve` and
        :meth:`consume_for_execution` both require the prior state
        (PENDING / APPROVED respectively), which REJECTED never satisfies.
        """
        with self._lock:
            route = self._tasks.get(task_id)
            if route is None or route.status != TaskApprovalStatus.PENDING:
                return None
            route.status = TaskApprovalStatus.REJECTED
            route.decided_at = time.time()
            route.decided_by = decided_by
            route.execution_status = "rejected"
        _audit("rejected", route)
        return _copy(route)

    def consume_for_execution(self, task_id: str) -> Optional[TaskRoute]:
        """Atomically verify APPROVED and transition to EXECUTING.

        A task can be consumed for execution at most once — the single
        execution gate. Returns ``None`` for anything not currently
        APPROVED (unknown, still pending, rejected, or already consumed),
        so "approve twice" or "run twice" can never execute twice.
        """
        with self._lock:
            route = self._tasks.get(task_id)
            if route is None or route.status != TaskApprovalStatus.APPROVED:
                return None
            route.status = TaskApprovalStatus.EXECUTING
            route.execution_status = "executing"
        _audit("executing", route)
        return _copy(route)

    def mark_executed(self, task_id: str, *, success: bool) -> None:
        with self._lock:
            route = self._tasks.get(task_id)
            if route is None:
                return
            route.status = TaskApprovalStatus.EXECUTED
            route.execution_status = "executed" if success else "failed"
            route.executed_at = time.time()
            snapshot = _copy(route)
        _audit("executed", snapshot)


_registry = TaskApprovalRegistry()


def get_task_registry() -> TaskApprovalRegistry:
    return _registry


def _audit(event: str, route: TaskRoute) -> None:
    """Emit one structured audit log line for a task-routing lifecycle event.

    Deliberately a single ``logger.info`` call with lazy ``%s`` args (not an
    f-string) so the standard logging pipeline's ``RedactingFormatter``
    (see ``hermes_logging.py``) gets a chance to scrub secret-shaped
    substrings before anything reaches disk.
    """
    logger.info(
        "task_audit event=%s task_id=%s chat_id=%s message_thread_id=%s "
        "risk=%s agent_provider=%s agent_model=%s approval_status=%s "
        "execution_status=%s request_preview=%r created_at=%s decided_at=%s "
        "decided_by=%s executed_at=%s",
        event,
        route.task_id,
        route.chat_id,
        route.thread_id or "",
        route.risk.value,
        route.agent.provider,
        route.agent.model,
        route.status.value,
        route.execution_status,
        _preview(route.request_text),
        route.created_at,
        route.decided_at,
        route.decided_by,
        route.executed_at,
    )
