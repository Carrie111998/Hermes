"""Read-only, no-send replay of bounded event audit samples."""

from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from typing import Any

from events.formatting import format_header, header_dot
from events.outcomes import OutcomeState
from events.routing_policy import ACTION_REQUIRED, ALERTS, Attention, classify
from events.schema import Event


_FAILURE_STATES = frozenset({OutcomeState.FAILED, OutcomeState.DEGRADED})


def _bounded_records(path: Path, limit: int) -> list[dict[str, Any]]:
    records: deque[dict[str, Any]] = deque(maxlen=max(0, limit))
    if limit <= 0 or not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return list(records)


def replay_audit(path: Path, *, limit: int = 500) -> dict[str, Any]:
    """Classify a bounded audit tail entirely in memory.

    This module imports no delivery subscriber and constructs no adapter.  Each
    accepted event therefore yields exactly one policy route and one formatted
    header without sending or mutating notification state.
    """
    raw_records = _bounded_records(Path(path), limit)
    destinations: Counter[str] = Counter()
    attentions: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    whatsapp_tiers: Counter[str] = Counter()
    unknown_event_types: Counter[str] = Counter()
    violations: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []

    for raw in raw_records:
        try:
            event = Event.from_dict(raw)
        except ValueError:
            unknown_event_types[str(raw.get("event_type", "<missing>"))] += 1
            continue
        except (KeyError, TypeError):
            unknown_event_types["<malformed>"] += 1
            continue

        route = classify(event)
        marker = header_dot(event, route.verdict)
        header = format_header(event, route.verdict)
        destinations[route.topic_key] += 1
        attentions[route.attention.value] += 1
        outcomes[route.verdict.state.value] += 1
        if route.wa_tier:
            whatsapp_tiers[route.wa_tier] += 1

        row = {
            "event_id": event.event_id,
            "event_type": event.event_type.type_string,
            "payload": event.payload,
            "outcome": route.verdict.state.value,
            "attention": route.attention.value,
            "topic": route.topic_key,
            "priority": route.priority.label,
            "whatsapp_tier": route.wa_tier,
            "batch": route.batch,
            "marker": marker,
            "header": header,
            "destination_count": 1,
        }
        rows.append(row)

        if route.verdict.state in _FAILURE_STATES and marker == "🟢":
            violations.append({"event_id": event.event_id, "reason": "failure_is_green"})
        if route.attention is Attention.ACT and route.topic_key != ACTION_REQUIRED:
            violations.append({"event_id": event.event_id, "reason": "act_outside_action_required"})
        if route.attention is Attention.ACT and route.wa_tier is None:
            violations.append({"event_id": event.event_id, "reason": "act_without_whatsapp"})
        if (
            route.verdict.state in _FAILURE_STATES
            and route.attention is not Attention.ACT
            and route.topic_key not in {ALERTS, "security_and_system", "jobflow_firehose"}
        ):
            violations.append({"event_id": event.event_id, "reason": "failure_outside_alert_domain"})

    return {
        "sample_size": len(raw_records),
        "classified_count": len(rows),
        "destinations": dict(destinations),
        "attentions": dict(attentions),
        "outcomes": dict(outcomes),
        "whatsapp_tiers": dict(whatsapp_tiers),
        "unknown_event_types": dict(unknown_event_types),
        "violations": violations,
        "rows": rows,
    }
