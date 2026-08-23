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


def test_derived_failure_cluster_alert_is_never_raw_evidence():
    """AGENT_FAILURE_CLUSTER is a DERIVED aggregate alert, not raw failure
    evidence. Feeding it back into clustering let the watchdog count its own
    emissions: cluster_size grew +1 per 5-minute sweep (7 -> 44 on
    2026-08-23) and 5 phantom notifications reached Telegram's watchdog-alerts
    topic. Mission Control still renders these red via evaluate_outcome, and
    the Critic trigger subscribes by event type directly -- excluding them
    here only stops audit-tail consumers from re-ingesting the alarm as data.
    """
    derived = _event(
        EventType.AGENT_FAILURE_CLUSTER,
        {
            "watchdog_type": "agent_failure_cluster",
            "source": "watchdog",
            "cluster_size": 44,
            "last_event_type": "agent_failure_cluster",
        },
        source="watchdog",
    )
    assert not failure_cluster_eligible(derived)
