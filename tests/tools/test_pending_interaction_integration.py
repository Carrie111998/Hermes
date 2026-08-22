from __future__ import annotations

import threading

from agent.pending_interactions import (
    PendingInteractionResponse,
    get_pending_interaction_service,
)


def _registered_for(request_id: str):
    service = get_pending_interaction_service()
    ready = threading.Event()
    events = []

    def subscriber(event):
        if (
            event.event == "pending_interaction.registered"
            and event.target.request_id == request_id
        ):
            events.append(event)
            ready.set()

    return service, service.subscribe(subscriber), ready, events


def test_gateway_approval_exposes_exact_id_and_rejects_missing_choice():
    from tools import approval

    session_key = "pending-service-approval-session"
    entry = approval._ApprovalEntry(
        {
            "request_id": "pending-service-approval",
            "command": "secret command must not escape",
            "allow_permanent": False,
        }
    )
    service, unsubscribe, ready, events = _registered_for(entry.data["request_id"])
    try:
        with approval._lock:
            approval._gateway_queues[session_key] = [entry]
        approval._register_pending_approval(session_key, entry, "desktop")

        assert ready.wait(2)
        target = events[0].target
        assert target.runtime_session_id == session_key
        assert events[0].metadata["choices"] == ("once", "session", "deny")
        assert "command" not in events[0].metadata

        assert service.resolve(
            target, PendingInteractionResponse("answer", "once")
        ).status == "invalid_response"
        assert service.resolve(
            target, PendingInteractionResponse("always")
        ).status == "invalid_response"
        assert service.resolve(
            target, PendingInteractionResponse("once", resolved_by="test-plugin")
        ).status == "accepted"
        assert entry.event.is_set()
        assert entry.result == "once"
        assert approval.resolve_gateway_approval(
            session_key, "deny", request_id=entry.data["request_id"]
        ) == 0
    finally:
        unsubscribe()
        with approval._lock:
            approval._gateway_queues.pop(session_key, None)


def test_gateway_clarify_plugin_and_native_race_has_one_winner():
    from tools import clarify_gateway

    request_id = "pending-service-clarify"
    service, unsubscribe, ready, events = _registered_for(request_id)
    entry = clarify_gateway.register(
        request_id,
        "pending-service-clarify-session",
        "Deploy where?",
        ["staging", "production"],
    )
    try:
        assert ready.wait(2)
        target = events[0].target
        barrier = threading.Barrier(3)
        outcomes = []

        def plugin_resolve():
            barrier.wait()
            outcomes.append(
                service.resolve(
                    target,
                    PendingInteractionResponse(
                        "answer", "staging", resolved_by="test-plugin"
                    ),
                ).status
            )

        def native_resolve():
            barrier.wait()
            outcomes.append(
                "accepted"
                if clarify_gateway.resolve_gateway_clarify(request_id, "production")
                else "already_resolved"
            )

        threads = [threading.Thread(target=plugin_resolve), threading.Thread(target=native_resolve)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["accepted", "already_resolved"]
        assert clarify_gateway.wait_for_response(request_id, 0.1) in {
            "staging",
            "production",
        }
    finally:
        unsubscribe()
        clarify_gateway.clear_session(entry.session_key)
