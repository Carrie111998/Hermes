from events.failure_eligibility import failure_cluster_eligible
from events.schema import Event, EventType


def _event(event_type, payload=None, *, source="fixture", tags=None):
    return Event.create(
        event_type,
        source,
        payload or {},
        tags=tags or [],
    )


def mixed_replay_fixture():
    return [
        _event(
            EventType.AGENT_NOTE,
            {"headline": "ARM TEST", "detail": "transport only", "synthetic": True},
            tags=["arm-test", "verification-probe"],
        ),
        _event(
            EventType.AGENT_NOTE,
            {"headline": "ARM TEST", "detail": "state.db only", "synthetic": True},
            tags=["verification-probe"],
        ),
        _event(
            EventType.AGENT_NOTE,
            {"headline": "First fire VERIFIED", "detail": "all checks passed"},
        ),
        _event(
            EventType.APPLICATION_BLOCKED,
            {"status": "blocked", "question": "Which option?"},
            source="applier",
        ),
        _event(
            EventType.AGENT_ERROR,
            {"error": "real executor failure"},
            source="worker",
        ),
    ]


def test_mixed_replay_has_exactly_one_cluster_eligible_record():
    eligible = [event for event in mixed_replay_fixture() if failure_cluster_eligible(event)]
    assert len(eligible) == 1
    assert eligible[0].event_type is EventType.AGENT_ERROR


def test_structured_nonzero_workload_exit_is_eligible_despite_success_wrapper():
    event = _event(
        EventType.AGENT_ITERATION,
        {"summary": "wrapper returned", "counters": {"exit_code": 1}},
    )
    assert failure_cluster_eligible(event)


def test_malformed_non_synthetic_agent_error_fails_closed():
    assert failure_cluster_eligible(_event(EventType.AGENT_ERROR, {}))


def test_mapping_input_uses_serialized_event_shape():
    event = _event(EventType.CRON_FAILED, {"error": "boom"})
    assert failure_cluster_eligible(event.to_dict())
