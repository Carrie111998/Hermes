"""Tests for events.producers.devflow_pr_build emitter helpers.

Added 2026-04-30 alongside the new DEVFLOW_PR_* / DEVFLOW_BUILD_* event
types. Spec at docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.

The emitter helpers are thin wrappers around ``EventBus.emit`` that
enforce the payload contract for each PR / build event. They exist so a
future producer (GitHub poller, DevFlow API extension, webhook receiver,
or manual trigger) can emit type-correct events without re-deriving the
schema.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


class TestEmitPrEvents:
    def test_emit_pr_opened_emits_correct_event(self, bus):
        from events.producers.devflow_pr_build import emit_pr_opened
        event_id = emit_pr_opened(
            bus,
            run_id="run-abc",
            pr_number=42,
            repo="hermes-agent",
            title="Add DevFlow PR events",
            author="diego",
            branch="feature/devflow-pr-events",
            base_branch="main",
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_PR_OPENED)
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == EventType.DEVFLOW_PR_OPENED
        assert evt.priority == Priority.NORMAL
        assert evt.payload["run_id"] == "run-abc"
        assert evt.payload["pr_number"] == 42
        assert evt.payload["repo"] == "hermes-agent"
        assert evt.payload["title"] == "Add DevFlow PR events"
        assert evt.payload["author"] == "diego"
        assert evt.payload["branch"] == "feature/devflow-pr-events"

    def test_emit_pr_merged_uses_high_priority(self, bus):
        from events.producers.devflow_pr_build import emit_pr_merged
        event_id = emit_pr_merged(
            bus,
            run_id="run-abc",
            pr_number=42,
            repo="hermes-agent",
            title="Add DevFlow PR events",
            merged_by="diego",
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_PR_MERGED)
        assert len(events) == 1
        assert events[0].priority == Priority.HIGH
        assert events[0].payload["merged_by"] == "diego"

    def test_emit_pr_closed_carries_reason(self, bus):
        from events.producers.devflow_pr_build import emit_pr_closed
        event_id = emit_pr_closed(
            bus,
            run_id="run-abc",
            pr_number=42,
            repo="hermes-agent",
            title="Add DevFlow PR events",
            closed_by="diego",
            reason="superseded",
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_PR_CLOSED)
        assert len(events) == 1
        evt = events[0]
        assert evt.priority == Priority.NORMAL
        assert evt.payload["reason"] == "superseded"
        assert evt.payload["closed_by"] == "diego"

    def test_emit_pr_review_requested_lists_reviewers(self, bus):
        from events.producers.devflow_pr_build import emit_pr_review_requested
        event_id = emit_pr_review_requested(
            bus,
            run_id="run-abc",
            pr_number=42,
            repo="hermes-agent",
            title="Add DevFlow PR events",
            reviewers=["alice", "bob"],
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_PR_REVIEW_REQUESTED)
        assert len(events) == 1
        evt = events[0]
        assert evt.priority == Priority.HIGH
        assert evt.payload["reviewers"] == ["alice", "bob"]

    def test_emit_pr_opened_validates_required_fields(self, bus):
        from events.producers.devflow_pr_build import (
            emit_pr_opened, DevflowProducerPayloadError,
        )
        # run_id missing → should raise
        with pytest.raises(DevflowProducerPayloadError):
            emit_pr_opened(
                bus,
                run_id="",  # empty
                pr_number=42,
                repo="hermes-agent",
                title="t",
                author="diego",
            )

    def test_emit_pr_opened_correlation_id_defaults_to_run_id(self, bus):
        from events.producers.devflow_pr_build import emit_pr_opened
        emit_pr_opened(
            bus,
            run_id="run-xyz",
            pr_number=7,
            repo="hermes-agent",
            title="t",
            author="diego",
        )
        evt = bus.query(event_type=EventType.DEVFLOW_PR_OPENED)[0]
        # correlation_id ties PR events to the WorkflowRun on the bridge side
        assert evt.correlation_id == "run-xyz"


class TestEmitBuildEvents:
    def test_emit_build_started_low_priority(self, bus):
        from events.producers.devflow_pr_build import emit_build_started
        event_id = emit_build_started(
            bus,
            run_id="run-abc",
            build_id="build-1",
            build_name="ci/lint",
            repo="hermes-agent",
            branch="main",
            commit_sha="abc123",
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_BUILD_STARTED)
        assert len(events) == 1
        evt = events[0]
        assert evt.priority == Priority.LOW
        assert evt.payload["build_id"] == "build-1"
        assert evt.payload["build_name"] == "ci/lint"
        assert evt.payload["commit_sha"] == "abc123"

    def test_emit_build_succeeded_normal_priority_with_duration(self, bus):
        from events.producers.devflow_pr_build import emit_build_succeeded
        event_id = emit_build_succeeded(
            bus,
            run_id="run-abc",
            build_id="build-1",
            build_name="ci/lint",
            repo="hermes-agent",
            duration_seconds=42.7,
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_BUILD_SUCCEEDED)
        assert len(events) == 1
        evt = events[0]
        assert evt.priority == Priority.NORMAL
        assert evt.payload["duration_seconds"] == 42.7

    def test_emit_build_failed_high_priority_with_error_summary(self, bus):
        from events.producers.devflow_pr_build import emit_build_failed
        event_id = emit_build_failed(
            bus,
            run_id="run-abc",
            build_id="build-2",
            build_name="ci/test",
            repo="hermes-agent",
            duration_seconds=12.4,
            error_summary="3 tests failed: tests/foo, tests/bar, tests/baz",
            exit_code=1,
        )
        assert event_id
        events = bus.query(event_type=EventType.DEVFLOW_BUILD_FAILED)
        assert len(events) == 1
        evt = events[0]
        assert evt.priority == Priority.HIGH
        assert evt.payload["error_summary"].startswith("3 tests failed")
        assert evt.payload["exit_code"] == 1

    def test_emit_build_failed_validates_required_fields(self, bus):
        from events.producers.devflow_pr_build import (
            emit_build_failed, DevflowProducerPayloadError,
        )
        with pytest.raises(DevflowProducerPayloadError):
            emit_build_failed(
                bus,
                run_id="run-abc",
                build_id="",  # empty
                build_name="ci/test",
                repo="hermes-agent",
                duration_seconds=1.0,
                error_summary="oops",
            )


class TestStateTransitionTracker:
    """The producer module ships an optional state tracker so a polling
    producer (cron job, webhook reconciler) can detect PR/build state
    transitions and emit only on transition — avoiding duplicate Telegram
    deliveries when the same state is observed across ticks.

    The tracker is a thin JSON file: { "pr:{repo}:{pr_number}": "<state>", ... }.
    It does NOT impose a polling cadence — callers decide when to invoke it.
    """

    def test_track_pr_state_emits_only_on_transition(self, bus, tmp_path):
        from events.producers.devflow_pr_build import PrBuildStateTracker
        state_path = tmp_path / "devflow_pr_state.json"
        tracker = PrBuildStateTracker(state_path)

        # Tick 1: PR seen for first time as "open" — should emit pr_opened
        emitted = tracker.observe_pr(
            bus,
            repo="hermes-agent", pr_number=42,
            state="open", run_id="run-abc",
            title="t", author="diego",
        )
        assert emitted == "devflow.pr_opened"
        assert len(bus.query(event_type=EventType.DEVFLOW_PR_OPENED)) == 1

        # Tick 2: same PR observed as "open" again — should NOT re-emit
        emitted = tracker.observe_pr(
            bus,
            repo="hermes-agent", pr_number=42,
            state="open", run_id="run-abc",
            title="t", author="diego",
        )
        assert emitted is None
        assert len(bus.query(event_type=EventType.DEVFLOW_PR_OPENED)) == 1

        # Tick 3: PR transitions to "merged" — should emit pr_merged
        emitted = tracker.observe_pr(
            bus,
            repo="hermes-agent", pr_number=42,
            state="merged", run_id="run-abc",
            title="t", author="diego",
            merged_by="diego",
        )
        assert emitted == "devflow.pr_merged"
        assert len(bus.query(event_type=EventType.DEVFLOW_PR_MERGED)) == 1

    def test_track_build_state_emits_only_on_transition(self, bus, tmp_path):
        from events.producers.devflow_pr_build import PrBuildStateTracker
        state_path = tmp_path / "devflow_build_state.json"
        tracker = PrBuildStateTracker(state_path)

        # Tick 1: build seen as "running" — emits build_started
        emitted = tracker.observe_build(
            bus,
            repo="hermes-agent", build_id="b1",
            state="running", run_id="run-abc",
            build_name="ci/test",
        )
        assert emitted == "devflow.build_started"

        # Tick 2: still running — no re-emit
        emitted = tracker.observe_build(
            bus,
            repo="hermes-agent", build_id="b1",
            state="running", run_id="run-abc",
            build_name="ci/test",
        )
        assert emitted is None

        # Tick 3: succeeded — emits build_succeeded
        emitted = tracker.observe_build(
            bus,
            repo="hermes-agent", build_id="b1",
            state="succeeded", run_id="run-abc",
            build_name="ci/test", duration_seconds=10.0,
        )
        assert emitted == "devflow.build_succeeded"

    def test_state_tracker_persists_across_instances(self, bus, tmp_path):
        """Tracker reads/writes its state file each call so a fresh
        instance (e.g., next cron tick) sees prior state and avoids
        re-emission."""
        from events.producers.devflow_pr_build import PrBuildStateTracker
        state_path = tmp_path / "devflow_state.json"

        t1 = PrBuildStateTracker(state_path)
        t1.observe_pr(
            bus,
            repo="r", pr_number=1, state="open", run_id="run-1",
            title="t", author="a",
        )
        # Drop t1, create fresh tracker pointing at same file
        t2 = PrBuildStateTracker(state_path)
        emitted = t2.observe_pr(
            bus,
            repo="r", pr_number=1, state="open", run_id="run-1",
            title="t", author="a",
        )
        assert emitted is None, "fresh instance must read prior state and skip"
