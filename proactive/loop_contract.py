"""Fail-closed Loop Contract validation for Grace -> ClawOps delegation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping


CONTRACT_VERSION = "1.0"


class LoopContractError(ValueError):
    """Raised when Grace has not supplied an executable contract."""


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Return the immutable scope fingerprint used by approval and callbacks.

    The persisted fingerprint field is excluded from its own digest so the
    approval provenance can carry the exact value without creating a recursive
    hash definition.
    """
    canonical_value = deepcopy(dict(contract or {}))
    canonical_value.pop("approval_provenance", None)
    canonical = json.dumps(
        canonical_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_loop_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized contract or reject it before a task is created."""
    value = deepcopy(dict(contract or {}))
    errors: list[str] = []

    def required_text(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if not isinstance(cur, str) or not cur.strip():
            errors.append(f"{path} is required")

    def required_list(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if (
            not isinstance(cur, list)
            or not cur
            or any(not isinstance(item, str) or not item.strip() for item in cur)
        ):
            errors.append(f"{path} must contain only non-empty strings")

    for path in (
        "identity.project",
        "identity.topic_name",
        "identity.request_instance_id",
        "original_request",
        "grace_interpretation",
        "trigger",
        "goal.objective",
        "memory.namespace",
        "completion_mode",
    ):
        required_text(path)
    if value.get("completion_mode") not in {"terminal", "intermediate"}:
        errors.append("completion_mode must be terminal or intermediate")
    thread_id = (value.get("identity") or {}).get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        errors.append("identity.thread_id must be a string")
    for path in (
        "goal.deliverables",
        "goal.non_goals",
        "scope.allowed",
        "scope.forbidden",
        "verification.checks",
        "verification.evidence_required",
        "verification.acceptance_criteria",
        "stop_rules.success",
        "stop_rules.blocked",
        "stop_rules.no_progress",
        "memory.working",
        "memory.promote_on_acceptance",
    ):
        required_list(path)
    if "external_targets" in value:
        required_list("external_targets")

    max_iterations = value.get("stop_rules", {}).get("max_iterations")
    if not isinstance(max_iterations, int) or not 1 <= max_iterations <= 20:
        errors.append("stop_rules.max_iterations must be an integer from 1 to 20")
    max_runtime = value.get("stop_rules", {}).get("max_runtime_seconds")
    if not isinstance(max_runtime, int) or not 60 <= max_runtime <= 14400:
        errors.append("stop_rules.max_runtime_seconds must be 60..14400")

    if errors:
        raise LoopContractError("; ".join(errors))
    value["contract_version"] = CONTRACT_VERSION
    return value
