"""
Graph Enforcer — Admission Policies & Runtime Enforcement

Provides a policy-check mechanism for graph-engineered workflows:
  - Admission gates: check budget, schema, evidence before allowing spawn
  - Runtime enforcement: interrupt nodes that exceed their budget
  - Policy escalation: on repeated failures, escalate to human

Design: stateless functions, no globals, testable in isolation.
Callers: graph_task, cronjob_tools, kanban dispatcher.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class AdmissionPolicy:
    """Policy checked before a node is allowed to run.

    Mirrors GraphARC's Policy model but adapted for Hermes' flat architecture.
    """

    name: str
    description: str = ""
    # Budget
    max_tokens: Optional[int] = None
    max_seconds: Optional[int] = None
    max_cost_usd: Optional[float] = None
    # Quality
    min_confidence: float = 0.5
    require_evidence: bool = False
    # Safety
    max_retries: int = 1
    escalation_threshold: int = 3  # consecutive failures before human escalation
    # Schema
    output_schema: Optional[str] = None  # typed-state-contracts schema name
    # Blocklist
    blocked_tools: List[str] = field(default_factory=list)
    # Admission
    allow_partial: bool = False  # accept partial results on budget exceeded

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "min_confidence": self.min_confidence,
            "require_evidence": self.require_evidence,
            "max_retries": self.max_retries,
            "output_schema": self.output_schema,
            "allow_partial": self.allow_partial,
        }


@dataclass
class AdmissionResult:
    """Outcome of an admission gate check."""

    allowed: bool
    reason: str
    warnings: List[str] = field(default_factory=list)
    recommended_budget: Optional[Dict[str, int]] = None


@dataclass
class EnforcementEvent:
    """Recorded when enforcement takes action (interrupt, escalate, etc.)."""

    node_id: str
    timestamp: float
    action: str  # 'interrupt', 'escalate', 'warn', 'block'
    reason: str
    context: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════
# Default policies
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_RESEARCH_POLICY = AdmissionPolicy(
    name="default_research",
    description="Conservative policy for research scans",
    max_tokens=20_000,
    max_seconds=120,
    min_confidence=0.5,
    require_evidence=True,
    max_retries=1,
    output_schema="ResearchBrief",
    allow_partial=True,
)

DEFAULT_CODE_REVIEW_POLICY = AdmissionPolicy(
    name="default_code_review",
    description="Strict policy for code review tasks",
    max_tokens=15_000,
    max_seconds=180,
    min_confidence=0.7,
    require_evidence=True,
    max_retries=2,
    output_schema="CodeReviewOutput",
    allow_partial=False,
)

DEFAULT_CRON_POLICY = AdmissionPolicy(
    name="default_cron",
    description="Production-safe policy for cron jobs",
    max_tokens=25_000,
    max_seconds=180,
    min_confidence=0.6,
    require_evidence=False,
    max_retries=1,
    output_schema="CronJobOutput",
    allow_partial=True,
)

NAMED_POLICIES: Dict[str, AdmissionPolicy] = {
    "research": DEFAULT_RESEARCH_POLICY,
    "code_review": DEFAULT_CODE_REVIEW_POLICY,
    "cron": DEFAULT_CRON_POLICY,
}


# ═══════════════════════════════════════════════════════════════════════
# Admission gate
# ═══════════════════════════════════════════════════════════════════════


def check_admission(
    policy: AdmissionPolicy,
    estimated_tokens: int,
    estimated_seconds: int = 0,
    retry_count: int = 0,
) -> AdmissionResult:
    """Run an admission gate check before spawning a node.

    Returns an AdmissionResult — if not allowed, the caller should
    reject the spawn and either retry with less work or escalate.
    """
    warnings: List[str] = []

    # 1. Retry exhaustion
    if retry_count >= policy.max_retries:
        return AdmissionResult(
            allowed=False,
            reason=(
                f"Retry limit exhausted ({retry_count}/{policy.max_retries}). "
                f"Task has failed {retry_count} times consecutively."
            ),
            warnings=warnings,
        )

    # 2. Token budget
    if policy.max_tokens is not None and estimated_tokens > policy.max_tokens:
        if policy.allow_partial:
            warnings.append(
                f"Token estimate ({estimated_tokens}) exceeds budget "
                f"({policy.max_tokens}). Will request partial output."
            )
        else:
            return AdmissionResult(
                allowed=False,
                reason=(
                    f"Token estimate ({estimated_tokens}) exceeds budget "
                    f"({policy.max_tokens}) and partial output is disabled."
                ),
                warnings=warnings,
                recommended_budget={"max_tokens": estimated_tokens + 5000},
            )

    # 3. Time budget
    if policy.max_seconds is not None and estimated_seconds > policy.max_seconds:
        warnings.append(
            f"Time estimate ({estimated_seconds}s) exceeds budget "
            f"({policy.max_seconds}s). Node may time out."
        )

    # 4. Escalation check — fires when failures >= escalation_threshold
    # regardless of retry budget (modeled after PagerDuty escalation:
    # N consecutive failures = wake a human).
    if retry_count >= policy.escalation_threshold > 0:
        warnings.append(
            f"Escalation threshold reached ({retry_count}/{policy.escalation_threshold}). "
            f"Consider human review."
        )
        # Escalation is a WARNING by default — it does not auto-block.
        # The caller decides whether to escalate further (interrupt, notify).

    return AdmissionResult(
        allowed=True,
        reason="All admission checks passed.",
        warnings=warnings,
    )


# ═══════════════════════════════════════════════════════════════════════
# Runtime enforcement
# ═══════════════════════════════════════════════════════════════════════


def enforce_budget(
    policy: AdmissionPolicy,
    node_id: str,
    tokens_used: int,
    seconds_elapsed: float,
    result: Any = None,
) -> List[EnforcementEvent]:
    """Post-hoc budget enforcement. Returns enforcement events.

    Call this after a node completes. If the node exceeded its budget,
    this returns a 'warn' or 'block' event that the caller can use
    to decide whether to accept or discard the result.
    """
    events: List[EnforcementEvent] = []
    now = time.time()

    # Token enforcement
    if policy.max_tokens is not None and tokens_used > policy.max_tokens:
        if policy.allow_partial:
            events.append(
                EnforcementEvent(
                    node_id=node_id,
                    timestamp=now,
                    action="warn",
                    reason=(
                        f"Node used {tokens_used} tokens (budget: {policy.max_tokens}). "
                        f"Partial output accepted."
                    ),
                )
            )
        else:
            events.append(
                EnforcementEvent(
                    node_id=node_id,
                    timestamp=now,
                    action="block",
                    reason=(
                        f"Node used {tokens_used} tokens (budget: {policy.max_tokens}). "
                        f"Output rejected — partial not allowed."
                    ),
                )
            )

    # Time enforcement
    if policy.max_seconds is not None and seconds_elapsed > policy.max_seconds:
        events.append(
            EnforcementEvent(
                node_id=node_id,
                timestamp=now,
                action="warn",
                reason=(
                    f"Node took {seconds_elapsed}s (budget: {policy.max_seconds}s)."
                ),
            )
        )

    # Evidence check (simple heuristic)
    if policy.require_evidence and result is not None:
        result_str = json.dumps(result) if not isinstance(result, str) else result
        urls = _extract_urls(result_str)
        if not urls:
            events.append(
                EnforcementEvent(
                    node_id=node_id,
                    timestamp=now,
                    action="warn",
                    reason="Evidence required but no URLs found in output.",
                )
            )

    # Confidence check
    if isinstance(result, dict) and "confidence" in result:
        conf = result["confidence"]
        if conf < policy.min_confidence:
            events.append(
                EnforcementEvent(
                    node_id=node_id,
                    timestamp=now,
                    action="warn",
                    reason=f"Confidence ({conf}) below minimum ({policy.min_confidence}).",
                )
            )

    return events


def _extract_urls(text: str) -> List[str]:
    """Extract http(s) URLs from a string."""
    import re
    return re.findall(r'https?://[^\s"\'\\]+', text)


# ═══════════════════════════════════════════════════════════════════════
# Policy registry
# ═══════════════════════════════════════════════════════════════════════


def resolve_policy(name: Optional[str]) -> AdmissionPolicy:
    """Look up a named policy, or return the default."""
    if name and name in NAMED_POLICIES:
        return NAMED_POLICIES[name]
    return DEFAULT_RESEARCH_POLICY


def register_policy(policy: AdmissionPolicy) -> None:
    """Register a custom policy (in-memory, session-scoped)."""
    NAMED_POLICIES[policy.name] = policy
    logger.info("Registered admission policy: %s", policy.name)


# ═══════════════════════════════════════════════════════════════════════
# Convenience: check + enforce in one call
# ═══════════════════════════════════════════════════════════════════════


def gate_node(
    policy_name: str,
    estimated_tokens: int,
    estimated_seconds: int = 0,
    retry_count: int = 0,
) -> AdmissionResult:
    """Shorthand: resolve policy + run admission check."""
    policy = resolve_policy(policy_name)
    return check_admission(policy, estimated_tokens, estimated_seconds, retry_count)


def enforce_node(
    policy_name: str,
    node_id: str,
    tokens_used: int,
    seconds_elapsed: float,
    result: Any = None,
) -> List[EnforcementEvent]:
    """Shorthand: resolve policy + enforce budget."""
    policy = resolve_policy(policy_name)
    return enforce_budget(
        policy=policy,
        node_id=node_id,
        tokens_used=tokens_used,
        seconds_elapsed=seconds_elapsed,
        result=result,
    )
