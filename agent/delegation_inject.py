"""Same-turn delivery of completed background delegations.

The async registry and its durable claims remain the authority for delivery.
This module only drains already-ready ``result_delivery=inject`` events at
conversation-loop safe boundaries. It never waits for a child.
"""

from __future__ import annotations

import logging
import os
import queue
from typing import Any

logger = logging.getLogger(__name__)


def _grant_reconciliation_grace_if_needed(agent: Any) -> None:
    """Allow one request only when the normal loop budget is exhausted."""

    api_calls = int(getattr(agent, "_api_call_count", 0) or 0)
    max_iterations = int(getattr(agent, "max_iterations", 0) or 0)
    budget = getattr(agent, "iteration_budget", None)
    remaining = int(getattr(budget, "remaining", 0) or 0)
    if (max_iterations and api_calls >= max_iterations) or remaining <= 0:
        agent._budget_grace_call = True


def reconcile_provisional_final(
    agent: Any,
    messages: list[dict[str, Any]],
    final_message: dict[str, Any],
    *,
    turn_id: str,
) -> bool:
    """Append a provisional assistant final and reconcile ready injects.

    Returns ``True`` only when at least one result was appended after the
    assistant message. The caller must then continue the normal model loop
    instead of committing the provisional final. The message object is never
    mutated after append, preserving prompt-cache history.
    """

    messages.append(final_message)
    ready = drain_ready_injects(agent, messages, turn_id=turn_id)
    if not ready:
        return False
    # One reconciliation request must remain possible if the provisional final
    # consumed the nominal iteration budget. This grants no waiting and does not
    # bypass a budget that still has room.
    _grant_reconciliation_grace_if_needed(agent)
    return True


def drain_ready_injects(agent: Any, messages: list[dict[str, Any]], turn_id: str) -> int:
    """Append one grouped synthetic user message for ready events from *turn_id*.

    The queue is scanned once using its size at entry. Non-matching events are
    requeued in their original order, so terminal/watch notifications and
    ``after_turn`` delegation results are left to their existing consumers.
    Returns the number of delegation results appended. No blocking calls are
    made.
    """

    if not turn_id:
        return 0
    # A previous inject may already be the tail while its reconciliation API
    # request is being retried after a transport failure. Appending another
    # synthetic user message here would create user→user history and force the
    # sequence repairer to rewrite cached context. Leave all events queued until
    # an assistant response establishes the next append-only boundary.
    if messages and messages[-1].get("role") == "user":
        return 0

    from tools.async_delegation import (
        claim_event_delivery,
        complete_event_delivery,
        release_event_delivery,
    )
    from tools.process_registry import _format_async_delegation, process_registry

    completion_queue = process_registry.completion_queue
    try:
        scan_count = completion_queue.qsize()
    except Exception:
        return 0

    accepted: list[tuple[dict[str, Any], str, str]] = []
    for _ in range(max(0, scan_count)):
        try:
            event = completion_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            break

        delivery = str(event.get("result_delivery") or "after_turn").strip().lower()
        event_turn_id = str(event.get("parent_turn_id") or "")
        if (
            event.get("type") != "async_delegation"
            or delivery != "inject"
            or event_turn_id != str(turn_id)
        ):
            completion_queue.put(event)
            continue

        claim_id = claim_event_delivery(event, f"conversation-loop:{os.getpid()}")
        if claim_id is None:
            # A competing CLI/gateway process already owns this durable event,
            # or it was delivered from a duplicate restored queue entry.
            continue

        try:
            text = _format_async_delegation(event)
        except Exception:
            logger.debug("Failed to format inject delegation event", exc_info=True)
            release_event_delivery(event, claim_id)
            completion_queue.put(event)
            continue
        if not text:
            release_event_delivery(event, claim_id)
            completion_queue.put(event)
            continue
        accepted.append((event, claim_id, text))

    if not accepted:
        return 0

    content = "\n\n".join(item[2] for item in accepted)
    delegation_ids = [str(item[0].get("delegation_id") or "") for item in accepted]
    event_ids = [
        f"{item[0].get('delegation_id') or ''}:{item[0].get('delivery_event_key') or 'aggregate'}"
        for item in accepted
    ]
    try:
        messages.append(
            {
                "role": "user",
                "content": content,
                "_synthetic_delegation_inject": True,
                "_delegation_ids": delegation_ids,
                "_delegation_event_ids": event_ids,
            }
        )
        agent._session_messages = messages
    except Exception:
        for event, claim_id, _text in accepted:
            release_event_delivery(event, claim_id)
            completion_queue.put(event)
        raise

    for event, claim_id, _text in accepted:
        complete_event_delivery(event, claim_id)
    _grant_reconciliation_grace_if_needed(agent)
    return len(accepted)
