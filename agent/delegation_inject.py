"""Same-turn delivery of completed background delegations.

The async registry and its durable claims remain the authority for delivery.
This module only drains already-ready ``result_delivery=inject`` events at
conversation-loop safe boundaries. It never waits for a child.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any

logger = logging.getLogger(__name__)


_GRACE_TURN_ATTR = "_delegation_reconciliation_grace_turn_id"
_PENDING_CLAIMS_ATTR = "_pending_delegation_inject_claims"
_CLAIM_HEARTBEAT_ATTR = "_delegation_inject_claim_heartbeat"
_CLAIM_HEARTBEAT_INTERVAL_SECONDS = 60.0


def _event_identity(event: dict[str, Any]) -> str:
    return (
        f"{event.get('delegation_id') or ''}:"
        f"{event.get('delivery_event_key') or 'aggregate'}"
    )


def _message_event_ids(message: dict[str, Any]) -> set[str]:
    metadata = message.get("display_metadata")
    if isinstance(metadata, dict):
        values = metadata.get("delegation_event_ids") or []
    else:
        values = message.get("_delegation_event_ids") or []
    return {str(value) for value in values if value}


def _durable_event_is_in_history(
    messages: list[dict[str, Any]], event_id: str
) -> bool:
    return any(
        message.get("_db_persisted") is True
        and event_id in _message_event_ids(message)
        for message in messages
        if isinstance(message, dict)
    )


def _stop_claim_heartbeat_if_idle(agent: Any) -> None:
    if getattr(agent, _PENDING_CLAIMS_ATTR, None):
        return
    heartbeat = getattr(agent, _CLAIM_HEARTBEAT_ATTR, None)
    if isinstance(heartbeat, dict):
        stop = heartbeat.get("stop")
        if isinstance(stop, threading.Event):
            stop.set()


def ensure_pending_inject_heartbeat(agent: Any) -> bool:
    """Renew live same-turn claims throughout provider retries and backoff."""

    if not getattr(agent, _PENDING_CLAIMS_ATTR, None):
        return False
    existing = getattr(agent, _CLAIM_HEARTBEAT_ATTR, None)
    if isinstance(existing, dict):
        thread = existing.get("thread")
        existing_stop = existing.get("stop")
        if (
            isinstance(thread, threading.Thread)
            and thread.is_alive()
            and isinstance(existing_stop, threading.Event)
            and not existing_stop.is_set()
        ):
            return True

    stop = threading.Event()

    def _heartbeat() -> None:
        from tools.async_delegation import renew_event_delivery

        while not stop.wait(_CLAIM_HEARTBEAT_INTERVAL_SECONDS):
            pending = list(getattr(agent, _PENDING_CLAIMS_ATTR, []) or [])
            if not pending:
                break
            for entry in pending:
                try:
                    if not renew_event_delivery(entry["event"], entry["claim_id"]):
                        logger.warning(
                            "Could not renew same-turn delegation claim %s",
                            entry.get("event_id"),
                        )
                except Exception:
                    logger.warning(
                        "Failed to renew same-turn delegation claim %s",
                        entry.get("event_id"),
                        exc_info=True,
                    )

    thread = threading.Thread(
        target=_heartbeat,
        daemon=True,
        name="delegation-inject-claim-heartbeat",
    )
    setattr(agent, _CLAIM_HEARTBEAT_ATTR, {"stop": stop, "thread": thread})
    thread.start()
    return True


def acknowledge_pending_injects(agent: Any, *, turn_id: str | None = None) -> int:
    """Acknowledge inject claims after a provider consumed their message."""

    from tools.async_delegation import complete_event_delivery

    pending = list(getattr(agent, _PENDING_CLAIMS_ATTR, []) or [])
    keep: list[dict[str, Any]] = []
    acknowledged = 0
    for entry in pending:
        if turn_id is not None and str(entry.get("turn_id") or "") != str(turn_id):
            keep.append(entry)
            continue
        if complete_event_delivery(entry["event"], entry["claim_id"]):
            acknowledged += 1
        else:
            keep.append(entry)
            logger.warning(
                "Provider consumed delegation inject %s but durable ack did not commit",
                entry.get("event_id"),
            )
    setattr(agent, _PENDING_CLAIMS_ATTR, keep)
    _stop_claim_heartbeat_if_idle(agent)
    return acknowledged


def release_pending_injects(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    turn_id: str | None = None,
) -> int:
    """Roll back unconsumed RAM injects, preserving already-durable copies."""

    from tools.async_delegation import (
        complete_event_delivery,
        get_event_delivery_state,
        release_event_delivery,
    )
    from tools.process_registry import process_registry

    pending = list(getattr(agent, _PENDING_CLAIMS_ATTR, []) or [])
    keep: list[dict[str, Any]] = []
    removable_event_ids: set[str] = set()
    settled = 0
    for entry in pending:
        if turn_id is not None and str(entry.get("turn_id") or "") != str(turn_id):
            keep.append(entry)
            continue
        event = entry["event"]
        event_id = str(entry["event_id"])
        if _durable_event_is_in_history(messages, event_id):
            if complete_event_delivery(event, entry["claim_id"]):
                settled += 1
            else:
                keep.append(entry)
        else:
            # Remove the unconsumed marker by durable identity even when
            # compression replaced the Python dict object.
            removable_event_ids.add(event_id)
            committed = release_event_delivery(event, entry["claim_id"])
            state = get_event_delivery_state(event)
            if committed:
                # At the attempt cap release transitions to dropped, not pending.
                if state == "pending":
                    process_registry.completion_queue.put(event)
                settled += 1
            elif state == "delivered":
                settled += 1
            else:
                keep.append(entry)

    if removable_event_ids:
        messages[:] = [
            message
            for message in messages
            if not (_message_event_ids(message) & removable_event_ids)
        ]
        agent._session_messages = messages
    setattr(agent, _PENDING_CLAIMS_ATTR, keep)
    _stop_claim_heartbeat_if_idle(agent)
    return settled


def _normal_budget_available(agent: Any) -> bool:
    """Mirror the conversation-loop's normal iteration-budget predicate."""

    max_iterations = getattr(agent, "max_iterations", None)
    budget = getattr(agent, "iteration_budget", None)
    # Lightweight helper users/tests do not necessarily expose loop-budget
    # state. In production both attributes exist; absent state must not make a
    # non-blocking queue drain manufacture a grace-call contract of its own.
    if max_iterations is None or budget is None:
        return True
    api_calls = int(getattr(agent, "_api_call_count", 0) or 0)
    remaining = int(getattr(budget, "remaining", 0) or 0)
    return api_calls < int(max_iterations or 0) and remaining > 0


def _has_reconciliation_capacity(agent: Any, turn_id: str) -> bool:
    """Return whether one more model request can consume an inject event."""

    if _normal_budget_available(agent):
        return True
    # A generic budget grace already granted by the loop can carry this inject;
    # record it as this turn's sole reconciliation boundary after acceptance.
    if bool(getattr(agent, "_budget_grace_call", False)):
        return True
    return str(getattr(agent, _GRACE_TURN_ATTR, "") or "") != str(turn_id)


def _grant_reconciliation_grace_if_needed(agent: Any, turn_id: str) -> None:
    """Reserve at most one exhausted-budget reconciliation call per turn."""

    if _normal_budget_available(agent):
        return
    setattr(agent, _GRACE_TURN_ATTR, str(turn_id))
    if not bool(getattr(agent, "_budget_grace_call", False)):
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
    _grant_reconciliation_grace_if_needed(agent, turn_id)
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
    # A cached agent may enter a new turn after an early-return path that never
    # reached the common finalizer.  Settle only claims from older turns; claims
    # from this turn must survive compression/API retries without duplication.
    stale_turn_ids = {
        str(entry.get("turn_id") or "")
        for entry in (getattr(agent, _PENDING_CLAIMS_ATTR, []) or [])
        if str(entry.get("turn_id") or "") != str(turn_id)
    }
    for stale_turn_id in stale_turn_ids:
        release_pending_injects(agent, messages, turn_id=stale_turn_id)
    # A previous inject may already be the tail while its reconciliation API
    # request is being retried after a transport failure. Appending another
    # synthetic user message here would create user→user history and force the
    # sequence repairer to rewrite cached context. Leave all events queued until
    # an assistant response establishes the next append-only boundary.
    if messages and messages[-1].get("role") == "user":
        return 0
    # Once this turn has consumed its sole exhausted-budget reconciliation
    # request, a later child must remain pending. The gateway/idle watcher will
    # deliver it through the normal late-result turn after the parent exits;
    # appending it here would mark it delivered even though no model call could
    # read it.
    if not _has_reconciliation_capacity(agent, turn_id):
        return 0

    from tools.async_delegation import (
        claim_event_delivery,
        complete_event_delivery,
        get_event_delivery_state,
        release_event_delivery,
    )
    from tools.process_registry import _format_async_delegation, process_registry

    completion_queue = process_registry.completion_queue
    accepted: list[tuple[dict[str, Any], str, str, str]] = []
    # Atomic with TUI dequeue -> route/requeue/claim.  Without this guard the
    # TUI poller can temporarily hold a ready event outside the queue while this
    # bounded snapshot reports zero, nondeterministically degrading inject to a
    # later synthetic turn.
    with process_registry.completion_routing_lock:
        try:
            scan_count = completion_queue.qsize()
        except Exception:
            return 0

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

            # Formatting is local preparation, not a delivery attempt.  Do it before
            # the durable claim so a broken spill/formatter cannot exhaust the
            # bounded delivery-attempt budget without ever showing the result.
            try:
                text = _format_async_delegation(event)
            except Exception:
                logger.debug("Failed to format inject delegation event", exc_info=True)
                completion_queue.put(event)
                continue
            if not text:
                completion_queue.put(event)
                continue

            claim_id = claim_event_delivery(event, f"conversation-loop:{os.getpid()}")
            if claim_id is None:
                # A competing CLI/gateway process already owns this durable event,
                # or it was delivered from a duplicate restored queue entry.
                continue
            event_id = _event_identity(event)
            if _durable_event_is_in_history(messages, event_id):
                # A previous process persisted the synthetic message before it
                # crashed.  The active transcript is now the durable handoff.
                complete_event_delivery(event, claim_id)
                continue
            accepted.append((event, claim_id, text, event_id))

    if not accepted:
        return 0

    content = "\n\n".join(item[2] for item in accepted)
    delegation_ids = [str(item[0].get("delegation_id") or "") for item in accepted]
    event_ids = [item[3] for item in accepted]
    try:
        synthetic_message = {
            "role": "user",
            "content": content,
            "display_kind": "delegation_inject",
            "display_metadata": {
                "delegation_ids": delegation_ids,
                "delegation_event_ids": event_ids,
            },
            "_synthetic_delegation_inject": True,
            "_delegation_ids": delegation_ids,
            "_delegation_event_ids": event_ids,
        }
        messages.append(synthetic_message)
        agent._session_messages = messages
    except Exception:
        for event, claim_id, _text, _event_id in accepted:
            if release_event_delivery(event, claim_id):
                if get_event_delivery_state(event) == "pending":
                    completion_queue.put(event)
        raise

    pending = list(getattr(agent, _PENDING_CLAIMS_ATTR, []) or [])
    pending.extend(
        {
            "event": event,
            "claim_id": claim_id,
            "event_id": event_id,
            "message": synthetic_message,
            "turn_id": str(turn_id),
        }
        for event, claim_id, _text, event_id in accepted
    )
    setattr(agent, _PENDING_CLAIMS_ATTR, pending)
    _grant_reconciliation_grace_if_needed(agent, turn_id)
    return len(accepted)
