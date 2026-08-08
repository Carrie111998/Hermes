"""Delivery-failure degradation rule (kanban t_6887f404).

Verifies that a *persistent* delivery failure — the canonical case being a
Discord/Slack 404 "Unknown Channel" where the configured delivery target (an
ephemeral thread id spawned when the job was created) has stopped resolving —
is persisted onto the job record as ``delivery_degraded`` instead of being an
invisible ERROR log.  57 of these failures once shipped to a dead thread before
anyone noticed.

These tests run the *real* persistence path (``mark_job_degraded`` -> jobs.json)
under a temp HERMES_HOME, not a mock, so they prove the field is actually
written and survives a reload.
"""
import json
import sys
from pathlib import Path

import pytest

# Ensure the package root is importable when run via pytest discovery.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cron.jobs import (  # noqa: E402
    CRON_DIR,
    JOBS_FILE,
    load_jobs,
    mark_job_degraded,
    classify_delivery_failure,
)


def _write_jobs(home: Path, jobs: list) -> None:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs, "updated_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )


def _make_job(job_id: str) -> dict:
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "prompt": "report",
        "deliver": "discord:#anuncios-e-status",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 30, "display": "every 30m"},
        "origin": {
            "platform": "discord",
            "chat_id": "1533284402493263934",  # dead thread id from the incident
            "thread_id": "1533284402493263934",
        },
    }


def test_classify_persistent_unknown_channel():
    """Discord 404 Unknown Channel / code 10003 is classified persistent."""
    err = 'Discord API error (404): {"message": "Unknown Channel", "code": 10003}'
    assert classify_delivery_failure(err) == "persistent"


def test_classify_transient_rate_limit():
    """A rate-limit / timeout error must stay transient (not degrade the job)."""
    assert classify_delivery_failure("429 Too Many Requests") == "transient"
    assert classify_delivery_failure(None) == "transient"


def test_mark_job_degraded_persists_to_jobs_json(tmp_path, monkeypatch):
    """mark_job_degraded writes delivery_degraded + timestamp into jobs.json."""
    job = _make_job("jj01")
    _write_jobs(tmp_path, [job])
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(
        "cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json"
    )

    err = 'Discord API error (404): {"message": "Unknown Channel", "code": 10003}'
    mark_job_degraded("jj01", err)

    # Reload from disk (real file, not the in-memory dict).
    reloaded = load_jobs()
    assert len(reloaded) == 1
    saved = reloaded[0]
    assert saved.get("delivery_degraded") is True
    assert saved.get("delivery_degraded_class") == "persistent"
    assert saved.get("delivery_degraded_at")
    assert err in (saved.get("last_delivery_error") or "")


def test_mark_job_degraded_does_not_disable_job(tmp_path, monkeypatch):
    """Degradation flags delivery only — the job stays enabled and scheduled."""
    job = _make_job("jj02")
    _write_jobs(tmp_path, [job])
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")

    mark_job_degraded(
        "jj02",
        "live adapter delivery to discord:1533284402493263934 failed: "
        "404 Not Found (error code: 10003): Unknown Channel",
    )
    saved = load_jobs()[0]
    assert saved.get("enabled") is True      # not silently disabled
    assert saved.get("state") == "scheduled"  # recurring job stays scheduled
    assert saved.get("delivery_degraded") is True
