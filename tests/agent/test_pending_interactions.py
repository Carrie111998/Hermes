from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from agent.pending_interactions import (
    PROCESS_INSTANCE_ID,
    PendingInteractionResponse,
    PendingInteractionService,
    current_interaction_metadata,
    pending_interaction_source,
)


def _register(service, resolver, validator=lambda response: True, **kwargs):
    return service.register(
        runtime_session_id=kwargs.get("runtime_session_id", "session-1"),
        request_id=kwargs.get("request_id", "request-1"),
        interaction_type=kwargs.get("interaction_type", "clarify"),
        question_id=kwargs.get("question_id"),
        resolver=resolver,
        validator=validator,
        metadata={"surface": "test"},
    )


def test_registered_event_has_exact_immutable_target_and_no_transport_side_effect():
    service = PendingInteractionService()
    received = []
    delivered = threading.Event()

    def subscriber(event):
        received.append(event)
        delivered.set()

    unsubscribe = service.subscribe(subscriber)
    target = _register(service, lambda _response: True)

    assert delivered.wait(2)
    event = received[0]
    assert event.event == "pending_interaction.registered"
    assert event.target == target
    assert target.process_instance_id == PROCESS_INSTANCE_ID
    assert target.runtime_session_id == "session-1"
    assert target.request_id == "request-1"
    assert event.metadata == {"surface": "test"}
    with pytest.raises(TypeError):
        event.metadata["surface"] = "mutated"
    with pytest.raises(FrozenInstanceError):
        event.status = "resolved"

    unsubscribe()
    service.terminal(target, "cancelled")
    service.shutdown()


def test_resolution_is_exact_validated_and_at_most_once_under_race():
    service = PendingInteractionService()
    native_calls = []
    target = _register(
        service,
        lambda response: native_calls.append(response.value) or True,
        validator=lambda response: response.kind == "answer" and response.value == "yes",
    )

    assert service.resolve(target, PendingInteractionResponse("answer", "no")).status == "invalid_response"

    barrier = threading.Barrier(3)
    statuses = []

    def resolve():
        barrier.wait()
        statuses.append(
            service.resolve(target, PendingInteractionResponse("answer", "yes")).status
        )

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == ["accepted", "already_resolved"]
    assert native_calls == ["yes"]
    service.shutdown()


def test_process_and_profile_identity_fail_closed(monkeypatch):
    service = PendingInteractionService()
    target = _register(service, lambda _response: True)

    stale = replace(target, process_instance_id="previous-process")
    assert service.resolve(stale, PendingInteractionResponse("answer", "x")).status == "process_mismatch"

    other_profile = replace(target, profile_id="other-profile")
    assert service.resolve(other_profile, PendingInteractionResponse("answer", "x")).status == "policy_denied"

    assert service.terminal(target, "cancelled") is True
    service.shutdown()


def test_subscriber_failure_isolated_from_registration_and_resolution():
    service = PendingInteractionService()
    delivered = threading.Event()

    def broken(_event):
        delivered.set()
        raise RuntimeError("subscriber failed")

    service.subscribe(broken)
    target = _register(service, lambda _response: True)
    assert delivered.wait(2)
    assert service.resolve(target, PendingInteractionResponse("answer", "ok")).status == "accepted"
    service.shutdown()


def test_batch_question_targets_share_parent_but_resolve_independently():
    service = PendingInteractionService()
    answers = {}
    targets = []
    for question_id in ("deployment", "region"):
        targets.append(
            _register(
                service,
                lambda response, qid=question_id: answers.setdefault(qid, response.value) is not None,
                request_id="batch-request",
                question_id=question_id,
            )
        )

    assert targets[0].request_id == targets[1].request_id == "batch-request"
    assert targets[0].question_id != targets[1].question_id
    assert service.resolve(targets[0], PendingInteractionResponse("answer", "staging")).status == "accepted"
    assert service.resolve(targets[1], PendingInteractionResponse("answer", "us-east")).status == "accepted"
    assert answers == {"deployment": "staging", "region": "us-east"}
    service.shutdown()


@pytest.mark.parametrize("kind", ["background_review", "curator"])
def test_internal_fork_metadata_is_authoritative(kind):
    with pending_interaction_source(kind):
        metadata = current_interaction_metadata(surface="internal-test")

    assert metadata["primary_user_turn"] is False
    assert metadata["internal"] is True
    assert metadata["fork_kind"] == kind
    assert metadata["surface"] == "internal-test"
