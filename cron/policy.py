"""Registered fail-closed policies for restricted cron jobs."""

from __future__ import annotations


STRICT_UNATTENDED_POLICY_ID = "strict-unattended-v1"

_CRON_OPERATOR_CAPABILITY = object()

_POLICY_RUNTIME_PINS = frozenset({"enabled_toolsets", "provider", "model", "base_url"})


class CronPolicyError(ValueError):
    """Raised when a cron job violates its registered policy."""


def cron_operator_capability() -> object:
    """Return the non-serializable capability used by the direct CLI."""
    return _CRON_OPERATOR_CAPABILITY


def is_trusted_cron_operator(capability: object) -> bool:
    """Validate direct CLI authority by object identity."""
    return capability is _CRON_OPERATOR_CAPABILITY


def require_trusted_policy_operator(job: dict, capability: object, action: str) -> None:
    """Reject protected lifecycle actions without direct CLI authority."""
    if job.get("policy_id") is not None and not is_trusted_cron_operator(capability):
        raise CronPolicyError(
            f"policy cron job {action} requires a trusted operator CLI"
        )


def validate_job_policy(job: dict) -> None:
    """Validate a persisted cron job's registered policy, if any."""
    policy_id = job.get("policy_id")
    if policy_id is None:
        return
    if policy_id != STRICT_UNATTENDED_POLICY_ID:
        raise CronPolicyError(f"unknown cron policy: {policy_id!r}")

    required_true = (
        "strict_toolsets",
        "no_mcp",
        "no_fallback",
        "created_paused",
    )
    for field in required_true:
        if job.get(field) is not True:
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} requires {field}=true"
            )

    if not isinstance(job.get("enabled_toolsets"), list):
        raise CronPolicyError(
            f"policy {STRICT_UNATTENDED_POLICY_ID!r} requires an explicit enabled_toolsets list"
        )
    from toolsets import get_toolset, resolve_toolset

    for toolset_name in job["enabled_toolsets"]:
        if not isinstance(toolset_name, str) or not toolset_name.strip():
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} has an invalid strict toolset name"
            )
        try:
            known_toolset = get_toolset(toolset_name)
            resolved_tools = resolve_toolset(toolset_name)
        except Exception as exc:
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} could not resolve strict toolset {toolset_name!r}"
            ) from exc
        if known_toolset is None:
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} references unknown strict toolset {toolset_name!r}"
            )
        if "memory" in resolved_tools:
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} forbids the persistent memory toolset"
            )
    for field in ("provider", "model"):
        value = job.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CronPolicyError(
                f"policy {STRICT_UNATTENDED_POLICY_ID!r} requires a non-empty {field} pin"
            )
    if job.get("no_agent") is True:
        raise CronPolicyError(
            f"policy {STRICT_UNATTENDED_POLICY_ID!r} requires an agent-backed job"
        )


def validate_policy_update(before: dict, after: dict) -> None:
    """Reject changes to capability and inference pins on policy jobs."""
    validate_job_policy(before)
    validate_job_policy(after)
    if before.get("policy_id") is None:
        return
    for field in _POLICY_RUNTIME_PINS:
        if before.get(field) != after.get(field):
            raise CronPolicyError(f"policy field {field!r} cannot be updated")
