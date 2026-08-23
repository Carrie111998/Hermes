"""Pure failure-cluster eligibility shared by event consumers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from events.outcomes import OutcomeState, evaluate_outcome
from events.schema import Event

_PROBE_TAGS = frozenset({"arm-test", "verification-probe", "transport-probe"})


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def failure_cluster_eligible(event: Event | Mapping[str, Any]) -> bool:
    """Return whether one event is genuine failure evidence for clustering.

    Explicit probes are never incidents. Pending/successful records are likewise
    excluded. Unknown non-synthetic AGENT_ERROR remains actionable because a
    malformed error envelope must fail closed rather than silently disappear.
    """
    try:
        candidate = event if isinstance(event, Event) else Event.from_dict(dict(event))
    except (KeyError, TypeError, ValueError):
        return False

    payload = candidate.payload if isinstance(candidate.payload, dict) else {}
    tags = {str(tag).strip().lower() for tag in candidate.tags}
    if _truthy(payload.get("synthetic")) or tags.intersection(_PROBE_TAGS):
        return False

    return evaluate_outcome(candidate).state is OutcomeState.FAILED
