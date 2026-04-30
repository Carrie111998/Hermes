"""Tests for the STAGE_TRANSITION event emit added to PipelineManager
in 2026-04-30 Phase 4 (Dashboard ↔ comms wiring).

PipelineManager.update_stage() is the single choke point for all stage
mutations from Control Center, JobFlow Dashboard, LangGraph, and the
agent crons. This test pins that the event emits correctly with a
populated payload, and that an EventBus failure never blocks the
canonical pipeline.json write.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline_state import PipelineManager


@pytest.fixture
def pipeline_with_one_job(tmp_path: Path):
    """Build a PipelineManager pointed at a temp pipeline.json with 1 job."""
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps({
        "jobs": [{
            "job_id": "linkedin-test-1",
            "title": "VP Finance",
            "company": "Acme",
            "score": 8.5,
            "stage": "scored",
            "history": [],
        }],
        "stats": {},
    }))
    mgr = PipelineManager(path=pipeline_path)
    return mgr


class TestStageTransitionEmit:
    """STAGE_TRANSITION event emission from update_stage()."""

    def test_emit_called_with_correct_payload(self, pipeline_with_one_job):
        """Happy path: bus.emit gets called with stage transition fields."""
        mock_bus = MagicMock()
        with patch("pipeline_state.manager._get_event_bus", return_value=mock_bus):
            pipeline_with_one_job.update_stage(
                job_id="linkedin-test-1",
                new_stage="approved",
                actor="diego",
                source="control_center",
                notes="reviewed via dashboard",
            )
        assert mock_bus.emit.call_count == 1
        kwargs = mock_bus.emit.call_args.kwargs
        from events.schema import EventType
        assert kwargs["event_type"] == EventType.STAGE_TRANSITION
        assert kwargs["source"] == "pipeline:control_center"
        assert kwargs["correlation_id"] == "linkedin-test-1"
        assert kwargs["job_id"] == "linkedin-test-1"
        payload = kwargs["payload"]
        assert payload["job_id"] == "linkedin-test-1"
        assert payload["prior_stage"] == "scored"
        assert payload["new_stage"] == "approved"
        assert payload["actor"] == "diego"
        assert payload["source_surface"] == "control_center"
        assert payload["notes"] == "reviewed via dashboard"
        assert payload["title"] == "VP Finance"
        assert payload["company"] == "Acme"
        assert payload["score"] == 8.5

    def test_emit_failure_does_not_propagate(self, pipeline_with_one_job):
        """A bus.emit() exception must be swallowed — pipeline.json write
        is canonical and must never be blocked by an observability hop."""
        mock_bus = MagicMock()
        mock_bus.emit.side_effect = RuntimeError("simulated DB lock")
        with patch("pipeline_state.manager._get_event_bus", return_value=mock_bus):
            # Must NOT raise
            pipeline_with_one_job.update_stage(
                job_id="linkedin-test-1",
                new_stage="approved",
                actor="diego",
                source="legacy_dashboard",
            )
        # And the pipeline file should still have been updated
        job = pipeline_with_one_job.get_job("linkedin-test-1")
        assert job["stage"] == "approved"

    def test_bus_unavailable_silent_no_op(self, pipeline_with_one_job):
        """When EventBus can't be imported (CLI context), update_stage
        still works — no emit happens, no error."""
        with patch("pipeline_state.manager._get_event_bus", return_value=None):
            pipeline_with_one_job.update_stage(
                job_id="linkedin-test-1",
                new_stage="approved",
                actor="diego",
                source="control_center",
            )
        job = pipeline_with_one_job.get_job("linkedin-test-1")
        assert job["stage"] == "approved"

    def test_prior_stage_in_payload_uses_pre_update_value(
        self, pipeline_with_one_job,
    ):
        """The emit's prior_stage must reflect the stage BEFORE this call,
        not the new stage. Important for downstream consumers that need to
        know the actual transition."""
        mock_bus = MagicMock()
        with patch("pipeline_state.manager._get_event_bus", return_value=mock_bus):
            pipeline_with_one_job.update_stage(
                job_id="linkedin-test-1",
                new_stage="ready_to_submit",
                actor="diego",
                source="control_center",
            )
        kwargs = mock_bus.emit.call_args.kwargs
        # Initial fixture has stage="scored"
        assert kwargs["payload"]["prior_stage"] == "scored"
        assert kwargs["payload"]["new_stage"] == "ready_to_submit"

    def test_metadata_passes_through_to_payload(self, pipeline_with_one_job):
        """Caller-provided metadata (title, company, score, url) should
        ride along on the event payload so subscribers don't need to
        re-fetch from pipeline.json."""
        mock_bus = MagicMock()
        with patch("pipeline_state.manager._get_event_bus", return_value=mock_bus):
            pipeline_with_one_job.update_stage(
                job_id="linkedin-test-1",
                new_stage="approved",
                actor="diego",
                source="control_center",
                metadata={
                    "url": "https://acme.example/jobs/123",
                    "score": 9.2,  # bumped score
                },
            )
        payload = mock_bus.emit.call_args.kwargs["payload"]
        assert payload["url"] == "https://acme.example/jobs/123"
        assert payload["score"] == 9.2
        assert payload["title"] == "VP Finance"  # unchanged

    def test_unknown_job_upsert_still_emits(self, pipeline_with_one_job):
        """An unknown job_id (upsert path) should still emit — the dashboard
        can drive new jobs into the pipeline. Prior_stage will be None."""
        mock_bus = MagicMock()
        with patch("pipeline_state.manager._get_event_bus", return_value=mock_bus):
            pipeline_with_one_job.update_stage(
                job_id="brand-new-job",
                new_stage="discovered",
                actor="scout",
                source="scout",
                metadata={"title": "Something New", "company": "Newco"},
            )
        assert mock_bus.emit.call_count == 1
        payload = mock_bus.emit.call_args.kwargs["payload"]
        assert payload["job_id"] == "brand-new-job"
        assert payload["prior_stage"] is None
        assert payload["new_stage"] == "discovered"
        assert payload["title"] == "Something New"
