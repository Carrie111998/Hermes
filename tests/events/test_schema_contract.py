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


class TestResourcePressureEnumEntry:
    """RESOURCE_PRESSURE is the system-resource exhaustion early-warning,
    added after the 2026-06-11 pagefile-expansion disk burst (commit charge
    98.4%, pagefile 36->54.4 GB in ~22 min, ZERO alerting). Emitted by
    events.producers.resource_monitor.ResourcePressureMonitor and routed
    high-priority to watchdog_alerts. Must remain a first-class EventType."""

    def test_enum_entry_exists(self):
        assert hasattr(EventType, "RESOURCE_PRESSURE")

    def test_type_string_is_stable(self):
        assert EventType.RESOURCE_PRESSURE.type_string == "resource_pressure"

    def test_default_priority_is_high(self):
        # High-priority so it survives significant_only / digest_only
        # verbosity and reaches Telegram while the disk is still bleeding.
        assert EventType.RESOURCE_PRESSURE.default_priority == Priority.HIGH

    def test_resolvable_from_string(self):
        resolved = EventType.from_string("resource_pressure")
        assert resolved is EventType.RESOURCE_PRESSURE


class TestTrackerPartialBacklogEnumEntry:
    """TRACKER_PARTIAL_BACKLOG is the tracker partial/ pileup early-warning
    (2026-07-14; the 07-13 storm's 13 partials sat ~a day unnoticed). Emitted by
    events.producers.partial_backlog_monitor.PartialBacklogMonitor and routed to
    jobflow_decisions (the human-action lane). Must remain a first-class EventType."""

    def test_enum_entry_exists(self):
        assert hasattr(EventType, "TRACKER_PARTIAL_BACKLOG")

    def test_type_string_is_stable(self):
        assert EventType.TRACKER_PARTIAL_BACKLOG.type_string == "tracker_partial_backlog"

    def test_default_priority_is_high(self):
        assert EventType.TRACKER_PARTIAL_BACKLOG.default_priority == Priority.HIGH

    def test_resolvable_from_string(self):
        resolved = EventType.from_string("tracker_partial_backlog")
        assert resolved is EventType.TRACKER_PARTIAL_BACKLOG
