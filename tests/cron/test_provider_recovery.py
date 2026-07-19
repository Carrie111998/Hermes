"""Tests for the deterministic provider recovery engine (no_agent)."""

from datetime import datetime, timedelta, timezone

from cron.provider_recovery import (
    FailureCategory,
    classify_cron_error,
    scan_provider_failures,
    evaluate_and_recover,
)


# ── Error classification ──────────────────────────────────────────────────

def test_classify_429():
    assert classify_cron_error("HTTP 429 Too Many Requests") == FailureCategory.provider_429
    assert classify_cron_error("rate limit exceeded") == FailureCategory.provider_429


def test_classify_auth():
    assert classify_cron_error("HTTP 401 Unauthorized") == FailureCategory.auth_blocked
    assert classify_cron_error("authentication failed") == FailureCategory.auth_blocked


def test_classify_5xx():
    assert classify_cron_error("HTTP 503 Service Unavailable") == FailureCategory.provider_5xx
    assert classify_cron_error("internal server error") == FailureCategory.provider_5xx


def test_classify_timeout():
    assert classify_cron_error("connection timed out") == FailureCategory.timeout


def test_classify_quota():
    assert classify_cron_error("quota exhausted") == FailureCategory.quota_exhausted


def test_classify_other():
    assert classify_cron_error("something weird happened") == FailureCategory.other
    assert classify_cron_error(None) == FailureCategory.other


# ── Helpers ────────────────────────────────────────────────────────────────

def _recent_ts(offset_minutes: int = 0) -> str:
    """ISO timestamp within the last hour for test data that survives the cutoff."""
    return (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)).isoformat()


def _mock_recovery_deps(monkeypatch, executions, job_provider="opencode-go", job_model="deepseek-v4-pro"):
    """Patch cron.executions.list_executions and cron.jobs.get_job."""
    monkeypatch.setattr("cron.executions.list_executions", lambda *, limit=500: executions)
    def _fake_get_job(job_id):
        return {"id": job_id, "provider": job_provider, "model": job_model}
    monkeypatch.setattr("cron.jobs.get_job", _fake_get_job)


# ── Recovery assessment ───────────────────────────────────────────────────

def test_scan_no_failures(monkeypatch):
    _mock_recovery_deps(monkeypatch, [])
    result = scan_provider_failures("opencode-go", window_minutes=60, min_consecutive=3)
    assert result.triggered is False
    assert result.failure_count == 0


def test_scan_below_threshold(monkeypatch):
    _mock_recovery_deps(monkeypatch, [
        {"id": "e1", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(0)},
        {"id": "e2", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(5)},
    ])
    result = scan_provider_failures("opencode-go", window_minutes=60, min_consecutive=3)
    assert result.triggered is False


def test_scan_above_threshold_triggered(monkeypatch):
    _mock_recovery_deps(monkeypatch, [
        {"id": "e1", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(0)},
        {"id": "e2", "job_id": "j2", "status": "failed", "error": "rate limit exceeded", "claimed_at": _recent_ts(10)},
        {"id": "e3", "job_id": "j1", "status": "failed", "error": "Too Many Requests 429", "claimed_at": _recent_ts(20)},
    ])
    result = scan_provider_failures("opencode-go", window_minutes=60, min_consecutive=3)
    assert result.triggered is True
    assert result.failure_count == 3
    assert result.category == FailureCategory.provider_429
    assert len(result.affected_job_ids) >= 1


def test_scan_ignores_non_recoverable(monkeypatch):
    _mock_recovery_deps(monkeypatch, [
        {"id": "e1", "job_id": "j1", "status": "failed", "error": "timeout", "claimed_at": _recent_ts(0)},
        {"id": "e2", "job_id": "j1", "status": "failed", "error": "timeout", "claimed_at": _recent_ts(5)},
        {"id": "e3", "job_id": "j1", "status": "failed", "error": "timeout", "claimed_at": _recent_ts(10)},
    ])
    result = scan_provider_failures("opencode-go", window_minutes=60, min_consecutive=3)
    assert result.triggered is False


def test_scan_ignores_wrong_provider(monkeypatch):
    _mock_recovery_deps(monkeypatch, [
        {"id": "e1", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(0)},
        {"id": "e2", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(5)},
        {"id": "e3", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(10)},
    ], job_provider="xai-oauth", job_model="grok-4.20")
    result = scan_provider_failures("opencode-go", window_minutes=60, min_consecutive=3)
    assert result.triggered is False


# ── Dry-run recovery (no live job mutation) ───────────────────────────────

def test_evaluate_and_recover_dry_run(monkeypatch):
    _mock_recovery_deps(monkeypatch, [
        {"id": "e1", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(0)},
        {"id": "e2", "job_id": "j2", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(10)},
        {"id": "e3", "job_id": "j1", "status": "failed", "error": "HTTP 429", "claimed_at": _recent_ts(20)},
    ])
    # find_fallback is a module-level function — patch at its source
    monkeypatch.setattr("cron.provider_recovery.find_fallback", lambda p: ("openai-codex", "gpt-5.4-mini") if p == "opencode-go" else None)
    records = evaluate_and_recover("opencode-go", dry_run=True)
    assert len(records) >= 1
    assert records[0].fallback_provider == "openai-codex"
    assert records[0].fallback_model == "gpt-5.4-mini"
