"""Tests for POST /api/v1/pipeline/jobs/{job_id}/stage.

2026-07-12: the endpoint routes through the JobOps intent lane (so the
tracker-intent-applier gives stage changes the full trio: legacy projection,
Postgres, canonical-store mirror) and falls back to the old direct
PipelineManager write only when the intent lane fails.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import intent_applier
import pipeline_state
from control_center.app import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_manager(monkeypatch):
    mgr = MagicMock()
    mgr.get_job.return_value = {"job_id": "job-1", "stage": "review"}
    monkeypatch.setattr(pipeline_state, "PipelineManager", MagicMock(return_value=mgr))
    return mgr


def test_stage_routes_through_intent_lane(client, fake_manager, monkeypatch):
    jobops = MagicMock()
    monkeypatch.setattr(intent_applier, "JobOpsClient", MagicMock(return_value=jobops))

    r = client.post(
        "/api/v1/pipeline/jobs/job-1/stage",
        json={"stage": "approved", "actor": "diego", "source": "control_center"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] is True
    kw = jobops.post_intent.call_args.kwargs
    assert kw["job_id"] == "job-1"
    assert kw["stage"] == "approved"
    assert kw["actor_id"] == "diego"
    assert kw["source"] == "control_center"
    # The direct write must NOT run when the intent lane accepted the change —
    # the tracker-intent-applier owns the projection write from here.
    fake_manager.update_stage.assert_not_called()


def test_stage_falls_back_to_direct_write_when_intent_lane_fails(
    client, fake_manager, monkeypatch
):
    jobops = MagicMock()
    jobops.post_intent.side_effect = RuntimeError("jobops down")
    monkeypatch.setattr(intent_applier, "JobOpsClient", MagicMock(return_value=jobops))

    r = client.post("/api/v1/pipeline/jobs/job-1/stage", json={"stage": "approved"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] is False
    fake_manager.update_stage.assert_called_once()


def test_stage_unknown_job_404(client, fake_manager):
    fake_manager.get_job.return_value = None
    r = client.post("/api/v1/pipeline/jobs/nope/stage", json={"stage": "approved"})
    assert r.status_code == 404


def test_stage_missing_stage_400(client, fake_manager):
    r = client.post("/api/v1/pipeline/jobs/job-1/stage", json={})
    assert r.status_code == 400
