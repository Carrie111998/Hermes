"""Bootstrap email policy: AgentMail free tier first, paid changes via procurement."""

from __future__ import annotations

from dataclasses import dataclass


AGENTMAIL_FREE_LIMITS = {
    "inboxes": 3,
    "emails_month": 3_000,
    "emails_day": 100,
    "storage_mb": 3_072,
    "webhook_endpoints": 2,
}


@dataclass(frozen=True)
class EmailCapacityDecision:
    status: str
    reason: str
    utilization_basis_points: int
    requires_procurement_review: bool


def evaluate_agentmail_free_capacity(
    *,
    inboxes: int,
    emails_month: int,
    emails_day: int,
    storage_mb: int,
    webhook_endpoints: int,
    warning_basis_points: int = 8_000,
) -> EmailCapacityDecision:
    usage = {
        "inboxes": inboxes,
        "emails_month": emails_month,
        "emails_day": emails_day,
        "storage_mb": storage_mb,
        "webhook_endpoints": webhook_endpoints,
    }
    if any(value < 0 for value in usage.values()):
        raise ValueError("email usage cannot be negative")
    ratios = {
        key: (usage[key] * 10_000 // limit)
        for key, limit in AGENTMAIL_FREE_LIMITS.items()
    }
    peak_key = max(ratios, key=ratios.get)
    peak = ratios[peak_key]
    if peak >= 10_000:
        return EmailCapacityDecision(
            "capacity_exceeded",
            f"AgentMail free-tier {peak_key} limit reached",
            peak,
            True,
        )
    if peak >= warning_basis_points:
        return EmailCapacityDecision(
            "review_required",
            f"AgentMail free-tier {peak_key} usage is approaching its limit",
            peak,
            True,
        )
    return EmailCapacityDecision(
        "within_free_tier",
        "AgentMail free tier remains sufficient",
        peak,
        False,
    )
