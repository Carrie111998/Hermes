"""Behavior tests for no-agent cron delivery watchdog classification."""

from __future__ import annotations


def test_watchdog_classifies_latest_remote_delivery_evidence_only():
    from cron.delivery_watchdog import classify_delivery_events

    jobs = [
        {"id": "missing", "deliver": "telegram:123"},
        {"id": "failed", "deliver": "discord:456"},
        {"id": "uncertain", "deliver": "telegram:789"},
        {"id": "local", "deliver": "local"},
        {"id": "recovered", "deliver": "telegram:101"},
    ]
    executions = [
        {
            "id": "missing-exec",
            "job_id": "missing",
            "status": "completed",
            "delivery_state": None,
            "claimed_at": "2026-07-19T08:00:00+00:00",
        },
        {
            "id": "failed-exec",
            "job_id": "failed",
            "status": "completed",
            "delivery_state": "failed",
            "claimed_at": "2026-07-19T08:01:00+00:00",
        },
        {
            "id": "uncertain-exec",
            "job_id": "uncertain",
            "status": "completed",
            "delivery_state": "uncertain_in_flight",
            "claimed_at": "2026-07-19T08:02:00+00:00",
        },
        {
            "id": "local-exec",
            "job_id": "local",
            "status": "completed",
            "delivery_state": None,
            "claimed_at": "2026-07-19T08:03:00+00:00",
        },
        {
            "id": "recovered-old-failure",
            "job_id": "recovered",
            "status": "completed",
            "delivery_state": "failed",
            "claimed_at": "2026-07-19T08:04:00+00:00",
        },
        {
            "id": "recovered-latest-ok",
            "job_id": "recovered",
            "status": "completed",
            "delivery_state": "accepted",
            "claimed_at": "2026-07-19T08:05:00+00:00",
        },
    ]

    events = classify_delivery_events(jobs, executions)

    assert events == [
        {
            "event": "missing_delivery_receipt",
            "execution_id": "missing-exec",
            "job_id": "missing",
            "delivery_state": None,
        },
        {
            "event": "delivery_failed",
            "execution_id": "failed-exec",
            "job_id": "failed",
            "delivery_state": "failed",
        },
        {
            "event": "delivery_uncertain",
            "execution_id": "uncertain-exec",
            "job_id": "uncertain",
            "delivery_state": "uncertain_in_flight",
        },
    ]


def test_watchdog_treats_suppressed_external_delivery_as_healthy():
    from cron.delivery_watchdog import classify_delivery_events

    assert classify_delivery_events(
        [{"id": "quiet", "deliver": "telegram:123"}],
        [{
            "id": "quiet-exec",
            "job_id": "quiet",
            "status": "completed",
            "delivery_state": "suppressed",
            "claimed_at": "2026-07-19T08:00:00+00:00",
        }],
    ) == []


def test_watchdog_runtime_error_uses_latest_execution_identity():
    from cron.delivery_watchdog import classify_delivery_events

    events = classify_delivery_events(
        [{"id": "job-1", "deliver": "local", "last_status": "error", "last_error": "boom"}],
        [{
            "id": "failed-execution-2",
            "job_id": "job-1",
            "status": "failed",
            "claimed_at": "2026-07-19T08:00:00+00:00",
        }],
    )

    assert events[0]["execution_id"] == "failed-execution-2"


def test_watchdog_flags_unresolved_origin_as_configuration_error():
    from cron.delivery_watchdog import classify_delivery_events

    assert classify_delivery_events(
        [{"id": "origin-job", "deliver": "origin", "origin": None}],
        [],
    ) == [
        {
            "event": "unresolved_origin",
            "execution_id": "config:origin-job",
            "job_id": "origin-job",
            "delivery_state": None,
        }
    ]


def test_watchdog_flags_latest_unknown_execution_without_replay():
    from cron.delivery_watchdog import classify_delivery_events

    assert classify_delivery_events(
        [{"id": "unknown-job", "deliver": "local"}],
        [{
            "id": "unknown-execution",
            "job_id": "unknown-job",
            "status": "unknown",
            "delivery_state": None,
            "claimed_at": "2026-07-19T08:00:00+00:00",
        }],
    ) == [
        {
            "event": "execution_unknown",
            "execution_id": "unknown-execution",
            "job_id": "unknown-job",
            "delivery_state": None,
        }
    ]


def test_watchdog_flags_enabled_registry_error_but_ignores_disabled_job():
    from cron.delivery_watchdog import classify_delivery_events

    assert classify_delivery_events(
        [
            {"id": "error-job", "enabled": True, "last_status": "error", "last_error": "provider 429"},
            {"id": "paused-job", "enabled": False, "last_status": "error", "last_error": "old failure"},
        ],
        [],
    ) == [
        {
            "event": "job_runtime_error",
            "execution_id": "runtime:error-job:provider 429",
            "job_id": "error-job",
            "delivery_state": None,
            "detail": "provider 429",
        }
    ]


def test_watchdog_records_each_execution_anomaly_once(tmp_path):
    from cron.delivery_watchdog import append_new_watchdog_events

    events = [
        {
            "event": "delivery_uncertain",
            "execution_id": "execution-1",
            "job_id": "job-1",
            "delivery_state": "uncertain_in_flight",
        }
    ]
    event_log = tmp_path / "delivery-watchdog-events.jsonl"

    assert append_new_watchdog_events(event_log, events) == events
    assert append_new_watchdog_events(event_log, events) == []
    persisted = [
        __import__("json").loads(line)
        for line in event_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(persisted) == 1
    assert persisted[0]["execution_id"] == "execution-1"


def test_watchdog_scan_reads_jobs_file_and_returns_only_new_events(tmp_path):
    import json

    from cron.delivery_watchdog import run_delivery_watchdog

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps({"jobs": [{"id": "job-1", "deliver": "telegram:123"}]}),
        encoding="utf-8",
    )
    event_log = tmp_path / "delivery-watchdog-events.jsonl"
    executions = [
        {
            "id": "execution-1",
            "job_id": "job-1",
            "status": "completed",
            "delivery_state": None,
            "claimed_at": "2026-07-19T08:00:00+00:00",
        }
    ]

    first = run_delivery_watchdog(jobs_path, event_log, executions)
    assert [event["event"] for event in first] == ["missing_delivery_receipt"]
    assert run_delivery_watchdog(jobs_path, event_log, executions) == []


def test_watchdog_main_bootstraps_silently_then_prints_new_anomaly(monkeypatch, tmp_path, capsys):
    import json

    from cron import delivery_watchdog as watchdog

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps({"jobs": [{"id": "job-1", "deliver": "telegram:123"}]}),
        encoding="utf-8",
    )
    event_log = tmp_path / "delivery-watchdog-events.jsonl"
    marker_path = tmp_path / "delivery-watchdog-baseline.json"
    old_execution = [{
        "id": "old-execution",
        "job_id": "job-1",
        "status": "completed",
        "delivery_state": None,
        "claimed_at": "2026-07-19T08:00:00+00:00",
    }]
    new_execution = [{
        "id": "new-execution",
        "job_id": "job-1",
        "status": "completed",
        "delivery_state": "uncertain_in_flight",
        "claimed_at": "2026-07-19T08:01:00+00:00",
    }]

    monkeypatch.setattr(watchdog, "list_executions", lambda **_kwargs: old_execution, raising=False)
    args = ["--jobs", str(jobs_path), "--event-log", str(event_log), "--baseline", str(marker_path)]
    assert watchdog.main(args) == 0
    assert capsys.readouterr().out == ""
    assert marker_path.exists()

    monkeypatch.setattr(watchdog, "list_executions", lambda **_kwargs: new_execution, raising=False)
    assert watchdog.main(args) == 0
    assert "delivery_uncertain" in capsys.readouterr().out
