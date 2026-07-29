"""Tests for ``hermes cron audit-models`` command."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hermes_cli.cron_audit import audit_cron_models


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_global_config():
    """Mock the global config and model snapshot resolution."""
    with patch("hermes_cli.config.load_config") as mock_load_config, \
         patch("cron.jobs._resolve_default_model_snapshot") as mock_resolve:
        mock_load_config.return_value = {
            "model": {
                "default": "gpt-4o",
                "provider": "openai",
            }
        }
        mock_resolve.return_value = "gpt-4o"
        yield mock_load_config, mock_resolve


def _make_job(
    job_id: str = "test123",
    name: str = "Test Job",
    model=None,
    provider=None,
    no_agent=False,
):
    """Build a minimal job record matching the shape ``load_jobs`` returns."""
    job = {
        "id": job_id,
        "name": name,
        "enabled": True,
    }
    if model is not None:
        job["model"] = model
    if provider is not None:
        job["provider"] = provider
    if no_agent:
        job["no_agent"] = True
    return job


# ---------------------------------------------------------------------------
# Status classification tests
# ---------------------------------------------------------------------------

class TestAuditStatusClassification:
    """Verify the audit correctly classifies each pinning state."""

    def test_pinned_job(self, mock_global_config):
        """Both model and provider set → status 'pinned'."""
        jobs = [_make_job(model="claude-3", provider="anthropic")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "pinned"

    def test_inherited_job(self, mock_global_config):
        """Neither model nor provider set → status 'inherited'."""
        jobs = [_make_job()]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "inherited"
        assert data["jobs"][0]["model"] == "(inherited)"
        assert data["jobs"][0]["provider"] == "(inherited)"

    def test_script_only_job(self, mock_global_config):
        """no_agent=True → status 'script-only'."""
        jobs = [_make_job(no_agent=True)]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "script-only"
        assert data["jobs"][0]["no_agent"] is True

    def test_partial_job_model_only(self, mock_global_config):
        """Model set but provider not → status 'partial'."""
        jobs = [_make_job(model="claude-3")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "partial"

    def test_partial_job_provider_only(self, mock_global_config):
        """Provider set but model not → status 'partial'."""
        jobs = [_make_job(provider="anthropic")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "partial"

    def test_script_only_takes_precedence_over_model(self, mock_global_config):
        """no_agent=True with model set → still 'script-only'."""
        jobs = [_make_job(model="claude-3", provider="anthropic", no_agent=True)]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"][0]["status"] == "script-only"


# ---------------------------------------------------------------------------
# JSON output tests
# ---------------------------------------------------------------------------

class TestJsonOutput:
    """Verify the JSON output structure."""

    def test_json_is_valid(self, mock_global_config):
        jobs = [_make_job(job_id="abc123", name="My Job", model="x", provider="y")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)  # should not raise
        assert "global_model" in data
        assert "global_provider" in data
        assert "jobs" in data
        assert isinstance(data["jobs"], list)

    def test_json_global_model_and_provider(self, mock_global_config):
        with patch("cron.jobs.load_jobs", return_value=[]):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["global_model"] == "gpt-4o"
        assert data["global_provider"] == "openai"

    def test_json_job_fields(self, mock_global_config):
        jobs = [_make_job(job_id="j1", name="Job One", model="m1", provider="p1")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        job = data["jobs"][0]
        assert job["id"] == "j1"
        assert job["name"] == "Job One"
        assert job["model"] == "m1"
        assert job["provider"] == "p1"
        assert job["status"] == "pinned"
        assert job["no_agent"] is False

    def test_json_empty_jobs(self, mock_global_config):
        with patch("cron.jobs.load_jobs", return_value=[]):
            result = audit_cron_models(json_output=True)
        data = json.loads(result)
        assert data["jobs"] == []


# ---------------------------------------------------------------------------
# Table output tests
# ---------------------------------------------------------------------------

class TestTableOutput:
    """Verify the human-readable table output."""

    def test_table_contains_header(self, mock_global_config):
        with patch("cron.jobs.load_jobs", return_value=[]):
            result = audit_cron_models(json_output=False)
        assert "Cron Model Audit" in result
        assert "Global model:" in result
        assert "Global provider:" in result

    def test_table_contains_column_headers(self, mock_global_config):
        with patch("cron.jobs.load_jobs", return_value=[]):
            result = audit_cron_models(json_output=False)
        assert "ID" in result
        assert "Name" in result
        assert "Model" in result
        assert "Provider" in result
        assert "Status" in result

    def test_table_contains_summary(self, mock_global_config):
        jobs = [
            _make_job(job_id="a", name="A", model="m", provider="p"),
            _make_job(job_id="b", name="B"),
            _make_job(job_id="c", name="C", no_agent=True),
        ]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=False)
        assert "Summary:" in result
        assert "1 pinned" in result
        assert "1 inherited" in result
        assert "1 script-only" in result

    def test_table_warning_for_inherited(self, mock_global_config):
        jobs = [_make_job(job_id="x", name="Inherited Job")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=False)
        assert "silently fail" in result
        assert "hermes cron update" in result

    def test_table_no_warning_when_all_pinned(self, mock_global_config):
        jobs = [_make_job(job_id="x", name="Pinned", model="m", provider="p")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=False)
        assert "silently fail" not in result

    def test_table_shows_job_data(self, mock_global_config):
        jobs = [_make_job(job_id="abc123def456", name="My Test Job", model="claude-3", provider="anthropic")]
        with patch("cron.jobs.load_jobs", return_value=jobs):
            result = audit_cron_models(json_output=False)
        assert "abc123def456"[:12] in result
        assert "My Test Job" in result
        assert "claude-3" in result
        assert "anthropic" in result
        assert "pinned" in result