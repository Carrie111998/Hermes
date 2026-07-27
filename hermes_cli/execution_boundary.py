"""Deterministic checks between probabilistic planning and real execution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional


class ExecutionBoundaryError(PermissionError):
    """Raised when an action is unsafe or stale at execution time."""


@dataclass(frozen=True)
class PayloadContract:
    required: Mapping[str, type]
    optional: Mapping[str, type]
    allow_additional: bool = False


def validate_payload(
    payload: Mapping[str, Any],
    contract: Optional[PayloadContract],
) -> None:
    if contract is None:
        return
    public = {key: value for key, value in payload.items() if not key.startswith("_")}
    missing = sorted(set(contract.required) - set(public))
    if missing:
        raise ExecutionBoundaryError(
            f"action payload is missing required fields: {', '.join(missing)}"
        )
    known = set(contract.required) | set(contract.optional)
    additional = sorted(set(public) - known)
    if additional and not contract.allow_additional:
        raise ExecutionBoundaryError(
            f"action payload contains unknown fields: {', '.join(additional)}"
        )
    for key, expected in {**contract.required, **contract.optional}.items():
        if key in public and not isinstance(public[key], expected):
            raise ExecutionBoundaryError(
                f"action payload field {key!r} must be {expected.__name__}"
            )


def validate_temporal_preconditions(
    payload: Mapping[str, Any],
    *,
    now: Optional[int] = None,
) -> None:
    """Reject actions based on stale observations or an active change freeze."""
    ts = int(time.time()) if now is None else int(now)
    not_before = payload.get("not_before")
    not_after = payload.get("not_after")
    if not_before is not None and ts < int(not_before):
        raise ExecutionBoundaryError("action execution window has not opened")
    if not_after is not None and ts > int(not_after):
        raise ExecutionBoundaryError("action execution window has expired")
    observed_at = payload.get("observed_state_at")
    max_age = payload.get("max_state_age_seconds")
    if (observed_at is None) != (max_age is None):
        raise ExecutionBoundaryError(
            "observed_state_at and max_state_age_seconds must be supplied together"
        )
    if observed_at is not None:
        age = ts - int(observed_at)
        if age < 0:
            raise ExecutionBoundaryError("external state observation is in the future")
        if int(max_age) < 0 or age > int(max_age):
            raise ExecutionBoundaryError("external state observation is stale")
    freeze = payload.get("change_freeze")
    if isinstance(freeze, Mapping) and bool(freeze.get("active")):
        raise ExecutionBoundaryError(
            f"change freeze is active: {freeze.get('reason') or 'unspecified'}"
        )
