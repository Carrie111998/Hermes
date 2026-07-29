from __future__ import annotations

from enum import Enum

from capability.policy import get_policy_decision


class PermissionDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


def check_capability(
    *,
    profile: str,
    capability: str | None,
) -> PermissionDecision:

    if capability is None:
        return PermissionDecision.APPROVAL_REQUIRED

    policy_decision = get_policy_decision(
        profile,
        capability,
    )

    if policy_decision is None:
        return PermissionDecision.APPROVAL_REQUIRED

    try:
        return PermissionDecision(policy_decision)
    except ValueError:
        return PermissionDecision.APPROVAL_REQUIRED
