"""Deterministic recovery decisions for autonomous orchestration."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

from agent.provider_route_policy import Capability, RouteRole, SubscriptionRoutePolicy


class FailureClass(enum.Enum):
    SAFETY_REFUSAL = "safety_refusal"
    TEMPORARY_RATE_LIMIT = "temporary_rate_limit"
    BROWSER_OAUTH_TIMEOUT = "browser_oauth_timeout"
    EXPIRED_CREDENTIAL = "expired_credential"
    REVOKED_CREDENTIAL = "revoked_credential"
    NO_REFRESH_CREDENTIAL = "no_refresh_credential"
    UNAVAILABLE_MODEL = "unavailable_model"
    PROVIDER_OUTAGE = "provider_outage"
    WORKER_CRASH = "worker_crash"
    LOST_WORKER_SESSION = "lost_worker_session"
    CONTEXT_EXHAUSTION = "context_exhaustion"
    FAILED_VERIFICATION = "failed_verification"
    PARTIAL_CHANGES = "partial_changes"
    DIRTY_WORKTREE = "dirty_worktree"
    INTERRUPTED_REPORT = "interrupted_report"
    ACTIVE_JOB_QUEUE = "active_job_queue"


@dataclass(frozen=True)
class RecoveryContext:
    failure: FailureClass
    current_route: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    role: RouteRole = RouteRole.BUILDER
    capability: Capability = Capability.WRITE
    total_attempts: int = 0
    route_attempts: dict[str, int] = field(default_factory=dict)
    cooldown_until: float | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDecision:
    failure: str
    action: str
    next_route: dict[str, Any] | None = None
    wait_until: float | None = None
    checkpoint_required: bool = False
    escalation: str | None = None
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure": self.failure,
            "action": self.action,
            "next_route": self.next_route,
            "wait_until": self.wait_until,
            "checkpoint_required": self.checkpoint_required,
            "escalation": self.escalation,
            "blocked_reason": self.blocked_reason,
        }


def _route_key(route: dict[str, Any] | None) -> str:
    if not route:
        return ""
    return f"{str(route.get('provider') or '').strip().lower()}:{str(route.get('model') or '').strip().lower()}"


def _limits(config: dict[str, Any]) -> tuple[int, int]:
    orch = config.get("orchestrator") if isinstance(config, dict) else {}
    if not isinstance(orch, dict):
        orch = {}
    return int(orch.get("max_total_attempts") or 8), int(orch.get("max_route_attempts") or 2)


def _approved_alternative(ctx: RecoveryContext) -> dict[str, Any] | None:
    policy = SubscriptionRoutePolicy(ctx.config)
    _, max_route = _limits(ctx.config)
    current_key = _route_key(ctx.current_route)
    for route in ctx.alternatives:
        key = _route_key(route)
        if key == current_key and ctx.route_attempts.get(key, 0) >= max_route:
            continue
        if policy.evaluate(route, role=ctx.role, capability=ctx.capability).allowed:
            return dict(route)
    return None


def decide_recovery(ctx: RecoveryContext) -> RecoveryDecision:
    max_total, _ = _limits(ctx.config)
    failure = ctx.failure.value

    if ctx.total_attempts >= max_total:
        return RecoveryDecision(failure, "escalate", escalation="no_approved_route", blocked_reason="attempt_limit_exceeded")

    if ctx.failure == FailureClass.SAFETY_REFUSAL:
        return RecoveryDecision(failure, "escalate", escalation="strategic_business_info", blocked_reason="safety_refusal")
    if ctx.failure in {FailureClass.BROWSER_OAUTH_TIMEOUT, FailureClass.REVOKED_CREDENTIAL, FailureClass.NO_REFRESH_CREDENTIAL}:
        return RecoveryDecision(failure, "escalate", escalation="fresh_interactive_auth", blocked_reason=failure)
    if ctx.failure == FailureClass.ACTIVE_JOB_QUEUE:
        return RecoveryDecision(failure, "queue", blocked_reason="active_job_in_progress")
    if ctx.failure == FailureClass.DIRTY_WORKTREE:
        return RecoveryDecision(failure, "checkpoint_then_escalate", checkpoint_required=True, escalation="destructive_rollback", blocked_reason="dirty_worktree")
    if ctx.failure == FailureClass.CONTEXT_EXHAUSTION:
        return RecoveryDecision(failure, "checkpoint_then_rotate_context", checkpoint_required=True)
    if ctx.failure in {FailureClass.WORKER_CRASH, FailureClass.LOST_WORKER_SESSION, FailureClass.INTERRUPTED_REPORT, FailureClass.PARTIAL_CHANGES}:
        return RecoveryDecision(failure, "resume_from_checkpoint", checkpoint_required=True)
    if ctx.failure == FailureClass.EXPIRED_CREDENTIAL:
        return RecoveryDecision(failure, "refresh_saved_credential")
    if ctx.failure == FailureClass.TEMPORARY_RATE_LIMIT:
        if ctx.cooldown_until and ctx.cooldown_until > time.time():
            return RecoveryDecision(failure, "wait", wait_until=ctx.cooldown_until)

    route = _approved_alternative(ctx)
    if route is not None:
        return RecoveryDecision(failure, "switch_route", next_route=route)

    if ctx.failure == FailureClass.FAILED_VERIFICATION:
        return RecoveryDecision(failure, "retry_changed_strategy", checkpoint_required=True)
    if ctx.failure in {FailureClass.UNAVAILABLE_MODEL, FailureClass.PROVIDER_OUTAGE, FailureClass.TEMPORARY_RATE_LIMIT}:
        return RecoveryDecision(failure, "escalate", escalation="no_approved_route", blocked_reason=failure)

    return RecoveryDecision(failure, "escalate", escalation="no_approved_route", blocked_reason=failure)
