from events.routing_policy import ALERTS, DEVFLOW, _POLICY
from events.schema import EventType, Priority

NEW_DDP_TYPES = {
    "devflow.work_requested": Priority.NORMAL,
    "devflow.work_triaged": Priority.NORMAL,
    "devflow.work_planned": Priority.NORMAL,
    "devflow.work_duplicate": Priority.LOW,
    "devflow.work_declined": Priority.NORMAL,
    "devflow.work_suppressed": Priority.LOW,
    "devflow.merge_pending": Priority.HIGH,
    "devflow.merged": Priority.HIGH,
    "devflow.auto_merged": Priority.HIGH,
    "devflow.deploy_started": Priority.NORMAL,
    "devflow.deployed": Priority.NORMAL,
    "devflow.deploy_failed": Priority.HIGH,
}


def test_new_ddp_event_types_registered_with_expected_defaults():
    for type_string, prio in NEW_DDP_TYPES.items():
        et = EventType.from_string(type_string)
        assert et is not None, f"missing EventType for {type_string}"
        assert et.default_priority is prio, type_string


# Expected routing topic per new type. All land in the DevFlow firehose
# EXCEPT deploy_failed, which mirrors build_failed (WARN → watchdog_alerts +
# urgent WhatsApp) per this task's implementation. The brief's test asserted
# a blanket ``== DEVFLOW`` for all 12, which contradicted the brief's own
# routing block (deploy_failed → ALERTS); adapted here to the real policy.
_EXPECTED_TOPIC = {ts: DEVFLOW for ts in NEW_DDP_TYPES}
_EXPECTED_TOPIC["devflow.deploy_failed"] = ALERTS


def test_new_ddp_event_types_have_routing_entries():
    for type_string in NEW_DDP_TYPES:
        et = EventType.from_string(type_string)
        assert et in _POLICY, f"no _POLICY entry for {type_string}"
        # _Spec exposes the topic as ``topic_key`` (see events/routing_policy.py);
        # the brief's ``.topic`` name does not exist on the real dataclass.
        assert _POLICY[et].topic_key == _EXPECTED_TOPIC[type_string], type_string


def test_existing_devflow_telemetry_untouched():
    # Stage 1 REUSES these; they must not be renamed or re-prioritized.
    assert EventType.DEVFLOW_BUILD_STARTED.type_string == "devflow.build_started"
    assert EventType.DEVFLOW_BUILD_SUCCEEDED.type_string == "devflow.build_succeeded"
    assert EventType.DEVFLOW_BUILD_FAILED.type_string == "devflow.build_failed"
    assert EventType.DEVFLOW_PR_OPENED.type_string == "devflow.pr_opened"
