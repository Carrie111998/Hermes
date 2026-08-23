"""Pure failure-cluster eligibility shared by event consumers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from events.outcomes import OutcomeState, evaluate_outcome
from events.schema import Event, EventType

_PROBE_TAGS = frozenset({"arm-test", "verification-probe", "transport-probe"})

# Derived aggregate alerts are OUTPUTS of failure analysis, not raw failure
# evidence. Feeding them back into clustering lets a producer count its own
# alarm as fresh input: the watchdog's audit-tail scan re-detected its own
# agent_failure_cluster emissions every sweep and grew cluster_size by one
# per pass (7 -> 44 on 2026-08-23, five phantom notifications delivered).
# Mission Control still renders these red via evaluate_outcome, and the
# Critic trigger subscribes to the type directly; excluding them here only
# stops audit-tail consumers from re-ingesting the alarm as data.
_DERIVED_ALERT_TYPES = frozenset({EventType.AGENT_FAILURE_CLUSTER})


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def failure_cluster_eligible(event: Event | Mapping[str, Any]) -> bool:
    """Return whether one event is genuine failure evidence for clustering.

    Explicit probes are never incidents, and neither are derived aggregate
    alerts (they summarize other events rather than carrying their own
    outcome). Pending/successful records are likewise excluded. Unknown
    non-synthetic AGENT_ERROR remains actionable because a malformed error
    envelope must fail closed rather than silently disappear.
    """
    try:
        candidate = event if isinstance(event, Event) else Event.from_dict(dict(event))
    except (KeyError, TypeError, ValueError):
        return False

    payload = candidate.payload if isinstance(candidate.payload, dict) else {}
    tags = {str(tag).strip().lower() for tag in candidate.tags}
    if _truthy(payload.get("synthetic")) or tags.intersection(_PROBE_TAGS):
        return False
    if candidate.event_type in _DERIVED_ALERT_TYPES:
        return False

    return evaluate_outcome(candidate).state is OutcomeState.FAILED
