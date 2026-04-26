"""Contract tests for events/schema.py.

These tests pin down EventType enum entries that other parts of the system
(producers, subscribers, watchdog, Critic trigger) depend on.  Removing or
renaming an entry without updating these tests must fail CI.
"""

from events.schema import EventType, Priority


class TestAgentFailureClusterEnumEntry:
    """AGENT_FAILURE_CLUSTER is the post-hoc Critic trigger from Hermes
    Revival §6. Wired by cron_emitter.on_job_completed and consumed by
    CriticSubscriber. Must remain a first-class EventType."""

    def test_enum_entry_exists(self):
        assert hasattr(EventType, "AGENT_FAILURE_CLUSTER")

    def test_type_string_is_stable(self):
        assert EventType.AGENT_FAILURE_CLUSTER.type_string == "agent_failure_cluster"

    def test_default_priority_is_high(self):
        assert EventType.AGENT_FAILURE_CLUSTER.default_priority == Priority.HIGH

    def test_resolvable_from_string(self):
        resolved = EventType.from_string("agent_failure_cluster")
        assert resolved is EventType.AGENT_FAILURE_CLUSTER
