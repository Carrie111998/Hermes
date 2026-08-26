"""Durable cron failure incidents: signature dedup, lifecycle, ack, CLI."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.incidents as incidents
import cron.jobs as cron_jobs
import cron.scheduler as sched


def _point_db(monkeypatch, tmp_path):
    """Point the incident store at a throwaway executions.db (same file shape
    the scheduler uses). ``cron.executions.EXECUTIONS_FILE`` stays None so the
    incident store falls back to its own override."""
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return incidents


def _job(**overrides):
    job = {
        "id": "incident-gating-test",
        "name": "incident gating test",
        "prompt": "hello",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "local",
        "model": None,
        "provider": None,
        "provider_snapshot": "openrouter",
        "base_url": None,
    }
    job.update(overrides)
    return job


def _tick_failing(job, tmp_path, deliveries, error="boom unrelated"):
    """Run one run_one_job tick whose agent raises ``error`` (the failure
    path that composes the per-run failure ping). Mirrors the drift-alert-once
    harness so the incident gating is exercised through the real scheduler."""
    fake_db = MagicMock()

    def fake_deliver(jb, content, adapters=None, loop=None):
        deliveries.append(content)
        return None

    with cron_jobs.use_cron_store(tmp_path), \
         patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": "openrouter",
                   "api_mode": "chat_completions",
               }), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.side_effect = RuntimeError(error)
        mock_agent_cls.return_value = mock_agent
        sched.run_one_job(dict(job))
    return mock_agent_cls.called


# ── Store + dedup ──────────────────────────────────────────────────────────


def test_new_failure_creates_incident_and_is_new(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)

    inc_id, is_new = inc.upsert_incident("job-1", "Provider timeout: read timed out")

    assert is_new is True
    assert inc_id.startswith("job-1_")
    row = inc.get_incident(inc_id)
    assert row is not None
    assert row["job_id"] == "job-1"
    assert row["state"] == "detected"
    assert row["failure_type"] == "timeout"
    assert row["first_seen_at"] == row["last_seen_at"]
    assert inc.count_incidents() == 1


def test_same_signature_dedups_same_incident(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)

    id1, new1 = inc.upsert_incident("job-1", "Provider timeout: read timed out")
    id2, new2 = inc.upsert_incident("job-1", "PROVIDER TIMEOUT: read timed out   ")

    assert id1 == id2, "normalized (case/whitespace) signature must dedup"
    assert new1 is True
    assert new2 is False
    assert inc.count_incidents() == 1
    # Refresh updates last_seen but never resets an open state.
    assert inc.get_incident(id1)["state"] == "detected"


def test_signature_normalizes_long_dynamic_numbers():
    assert incidents._error_signature(
        "job-1", "request 123456 failed at 1720000000"
    ) == incidents._error_signature(
        "job-1", "request 987654 failed at 1730000000"
    )


def test_signature_preserves_status_and_exit_codes():
    assert incidents._error_signature(
        "job-1", "request failed with status 404"
    ) != incidents._error_signature(
        "job-1", "request failed with status 500"
    )
    assert incidents._error_signature(
        "job-1", "script exited with code 1"
    ) != incidents._error_signature(
        "job-1", "script exited with code 137"
    )


def test_unacked_incident_alerts_immediately_then_after_four_hours_and_daily(
    monkeypatch, tmp_path
):
    inc = _point_db(monkeypatch, tmp_path)
    started = datetime(2026, 8, 26, tzinfo=timezone.utc)

    monkeypatch.setattr(inc, "_hermes_now", lambda: started)
    inc_id, should_alert = inc.upsert_incident_for_alert("job-1", "boom 12345")
    assert should_alert is True
    assert inc.mark_incident_alerted(inc_id) is True

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(hours=1))
    same_id, should_alert = inc.upsert_incident_for_alert("job-1", "boom 45678")
    assert same_id == inc_id
    assert should_alert is False

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(hours=4))
    _, should_alert = inc.upsert_incident_for_alert("job-1", "boom 78901")
    assert should_alert is True
    assert inc.mark_incident_alerted(inc_id) is True

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(hours=5))
    _, should_alert = inc.upsert_incident_for_alert("job-1", "boom 99999")
    assert should_alert is False

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(hours=28))
    _, should_alert = inc.upsert_incident_for_alert("job-1", "boom 11111")
    assert should_alert is True


def test_due_reminder_retries_until_delivery_is_confirmed(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    started = datetime(2026, 8, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(inc, "_hermes_now", lambda: started)
    inc_id, _ = inc.upsert_incident_for_alert("job-1", "boom")
    inc.mark_incident_alerted(inc_id)

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(hours=4))
    assert inc.upsert_incident_for_alert("job-1", "boom")[1] is True

    monkeypatch.setattr(
        inc, "_hermes_now", lambda: started + timedelta(hours=4, minutes=5)
    )
    assert inc.upsert_incident_for_alert("job-1", "boom")[1] is True

    inc.mark_incident_alerted(inc_id)
    assert inc.upsert_incident_for_alert("job-1", "boom")[1] is False


def test_closed_incident_never_escalates(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    started = datetime(2026, 8, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(inc, "_hermes_now", lambda: started)
    inc_id, _ = inc.upsert_incident_for_alert("job-1", "boom")
    inc.mark_incident_alerted(inc_id)
    inc.ack_incident(inc_id)

    monkeypatch.setattr(inc, "_hermes_now", lambda: started + timedelta(days=3))
    assert inc.upsert_incident_for_alert("job-1", "boom")[1] is False


def test_error_change_mints_new_incident(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)

    id1, _ = inc.upsert_incident("job-1", "provider timeout")
    id2, new2 = inc.upsert_incident("job-1", "provider rate limit 429")

    assert id1 != id2
    assert new2 is True
    assert inc.count_incidents() == 2


# ── Redaction / classification ─────────────────────────────────────────────


def test_redaction_applied_to_incident_error(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"

    inc_id, _ = inc.upsert_incident("job-1", f"failed: {secret} boom")

    row = inc.get_incident(inc_id)
    assert secret not in row["error"]
    assert "boom" in row["error"]


def test_error_truncated_to_bounded_length(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    long_error = "x" * 2000

    inc_id, _ = inc.upsert_incident("job-1", long_error)

    assert len(inc.get_incident(inc_id)["error"]) <= 500


def test_failure_type_classification(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    cases = [
        ("delivery failed for telegram chat", "delivery"),
        ("Provider read timed out after 60s", "timeout"),
        ("authentication failed: invalid API key", "auth"),
        ("HTTP 429: rate limit exceeded", "rate_limit"),
        ("configuration validation blocked the run", "config"),
        ("script exited with code 1", "script"),
        ("agent crashed mid-conversation", "agent"),
        ("something completely unexpected happened", "unknown"),
    ]
    for error, expected in cases:
        assert inc._classify_failure_type(error) == expected, (error, expected)


# ── Lifecycle / ack ────────────────────────────────────────────────────────


def test_lifecycle_transitions(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    inc_id, _ = inc.upsert_incident("job-1", "boom")

    assert inc.get_incident(inc_id)["state"] == "detected"
    assert inc.set_incident_state(inc_id, "alerted") is True
    assert inc.set_incident_state(inc_id, "closed") is True
    row = inc.get_incident(inc_id)
    assert row["state"] == "closed"
    assert row["acked_at"] and row["closed_at"]

    # Closed is terminal for that signature: no re-open, no re-transition.
    assert inc.set_incident_state(inc_id, "alerted") is False
    assert inc.set_incident_state(inc_id, "closed") is False
    assert inc.ack_incident(inc_id) is False
    assert inc.get_incident(inc_id)["state"] == "closed"

    # Invalid states are rejected, not raised.
    assert inc.set_incident_state(inc_id, "bogus") is False
    assert inc.list_incidents(state="bogus") == []
    assert inc.count_incidents(state="bogus") == 0


def test_acked_signature_stays_closed_on_refresh(monkeypatch, tmp_path):
    """Ack is per-signature: upserting the same error after ack must NOT
    resurrect the incident — a changed error is what mints a new one."""
    inc = _point_db(monkeypatch, tmp_path)
    inc_id, _ = inc.upsert_incident("job-1", "same failure text")
    inc.ack_incident(inc_id)

    same_id, is_new = inc.upsert_incident("job-1", "SAME FAILURE TEXT")

    assert same_id == inc_id
    assert is_new is False
    assert inc.get_incident(inc_id)["state"] == "closed"


# ── Missing DB / lazy schema ───────────────────────────────────────────────


def test_missing_db_no_crash(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)

    inc_id, is_new = inc.upsert_incident("job-1", "boom")

    assert is_new is True
    assert (tmp_path / "cron" / "executions.db").is_file()
    assert inc.list_incidents() == [inc.get_incident(inc_id)]
    assert inc.count_incidents() == 1
    assert inc.get_incident("nope") is None


def test_existing_incident_schema_gains_alert_confirmation_column(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE cron_incidents (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 error_sig TEXT NOT NULL,
                 state TEXT NOT NULL,
                 failure_type TEXT NOT NULL DEFAULT 'unknown',
                 first_seen_at TEXT NOT NULL,
                 last_seen_at TEXT NOT NULL,
                 acked_at TEXT,
                 closed_at TEXT,
                 error TEXT NOT NULL,
                 output_file TEXT
               )"""
        )
    inc = _point_db(monkeypatch, tmp_path)

    inc_id, should_alert = inc.upsert_incident_for_alert("job-1", "boom")

    assert should_alert is True
    assert inc.mark_incident_alerted(inc_id) is True
    row = inc.get_incident(inc_id)
    assert row is not None
    assert row["last_alerted_at"] is not None


def test_existing_incidents_migrate_to_normalized_signatures(monkeypatch, tmp_path):
    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    legacy_rows = [
        ("job-1", "request abcdef123456 failed", "alerted"),
        ("job-2", "worker failed at 1720000000", "closed"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE cron_incidents (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 error_sig TEXT NOT NULL,
                 state TEXT NOT NULL,
                 failure_type TEXT NOT NULL DEFAULT 'unknown',
                 first_seen_at TEXT NOT NULL,
                 last_seen_at TEXT NOT NULL,
                 last_alerted_at TEXT,
                 acked_at TEXT,
                 closed_at TEXT,
                 error TEXT NOT NULL,
                 output_file TEXT
               )"""
        )
        for job_id, error, state in legacy_rows:
            normalized = incidents._normalize_error(error)[:200]
            legacy_sig = hashlib.sha256(
                job_id.encode() + normalized.encode()
            ).hexdigest()[:12]
            legacy_id = incidents._incident_id(job_id, legacy_sig)
            conn.execute(
                """INSERT INTO cron_incidents
                   (id, job_id, error_sig, state, failure_type,
                    first_seen_at, last_seen_at, last_alerted_at,
                    acked_at, closed_at, error, output_file)
                   VALUES (?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    legacy_id,
                    job_id,
                    legacy_sig,
                    state,
                    "2026-08-25T00:00:00+00:00",
                    "2026-08-26T00:00:00+00:00",
                    "2026-08-25T00:00:00+00:00" if state == "alerted" else None,
                    "2026-08-26T00:00:00+00:00" if state == "closed" else None,
                    "2026-08-26T00:00:00+00:00" if state == "closed" else None,
                    error,
                ),
            )

    inc = _point_db(monkeypatch, tmp_path)
    rows = {row["job_id"]: row for row in inc.list_incidents()}

    assert set(rows) == {"job-1", "job-2"}
    for job_id, error, state in legacy_rows:
        expected_sig = inc._error_signature(job_id, error)
        assert rows[job_id]["id"] == inc._incident_id(job_id, expected_sig)
        assert rows[job_id]["error_sig"] == expected_sig
        assert rows[job_id]["state"] == state


def test_signature_migration_merges_collisions_and_preserves_ack(monkeypatch, tmp_path):
    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    errors = (
        ("worker failed at 1720000000", "alerted"),
        ("worker failed at 1730000000", "closed"),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE cron_incidents (
                 id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                 error_sig TEXT NOT NULL, state TEXT NOT NULL,
                 failure_type TEXT NOT NULL DEFAULT 'unknown',
                 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                 last_alerted_at TEXT, acked_at TEXT, closed_at TEXT,
                 error TEXT NOT NULL, output_file TEXT
               )"""
        )
        for index, (error, state) in enumerate(errors):
            normalized = incidents._normalize_error(error)[:200]
            legacy_sig = hashlib.sha256(
                b"job-1" + normalized.encode()
            ).hexdigest()[:12]
            conn.execute(
                """INSERT INTO cron_incidents
                   VALUES (?, 'job-1', ?, ?, 'unknown', ?, ?, NULL, ?, ?, ?, NULL)""",
                (
                    incidents._incident_id("job-1", legacy_sig),
                    legacy_sig,
                    state,
                    f"2026-08-2{index + 4}T00:00:00+00:00",
                    f"2026-08-2{index + 5}T00:00:00+00:00",
                    "2026-08-26T00:00:00+00:00" if state == "closed" else None,
                    "2026-08-26T00:00:00+00:00" if state == "closed" else None,
                    error,
                ),
            )

    inc = _point_db(monkeypatch, tmp_path)
    rows = inc.list_incidents()

    assert len(rows) == 1
    assert rows[0]["state"] == "closed"
    expected_sig = inc._error_signature("job-1", errors[0][0])
    assert rows[0]["id"] == inc._incident_id("job-1", expected_sig)
    assert inc.upsert_incident_for_alert("job-1", errors[1][0])[1] is False


def test_signature_migration_matches_legacy_raw_error_after_redaction(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    raw_error = "provider rejected api_key=sk-secret-value"
    stored_error = "provider rejected api_key=***REDACTED***"
    legacy_sig = hashlib.sha256(
        b"job-1" + incidents._normalize_error(raw_error)[:200].encode()
    ).hexdigest()[:12]
    legacy_id = incidents._incident_id("job-1", legacy_sig)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE cron_incidents (
                 id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                 error_sig TEXT NOT NULL, state TEXT NOT NULL,
                 failure_type TEXT NOT NULL DEFAULT 'unknown',
                 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                 last_alerted_at TEXT, acked_at TEXT, closed_at TEXT,
                 error TEXT NOT NULL, output_file TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO cron_incidents
               VALUES (?, 'job-1', ?, 'closed', 'auth', ?, ?, NULL, ?, ?, ?, NULL)""",
            (
                legacy_id,
                legacy_sig,
                "2026-08-25T00:00:00+00:00",
                "2026-08-26T00:00:00+00:00",
                "2026-08-26T00:00:00+00:00",
                "2026-08-26T00:00:00+00:00",
                stored_error,
            ),
        )

    inc = _point_db(monkeypatch, tmp_path)
    inc.list_incidents()  # Run the eager migration from persisted data.
    incident_id, should_alert = inc.upsert_incident_for_alert("job-1", raw_error)

    assert should_alert is False
    assert inc.count_incidents() == 1
    row = inc.get_incident(incident_id)
    assert row is not None
    assert row["state"] == "closed"


# ── Scheduler gating ───────────────────────────────────────────────────────


def test_unacked_failure_alerts_immediately_then_escalates_with_nudge(
    monkeypatch, tmp_path
):
    inc = _point_db(monkeypatch, tmp_path)
    deliveries = []
    job = _job()
    started = datetime(2026, 8, 26, tzinfo=timezone.utc)
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        monkeypatch.setattr(inc, "_hermes_now", lambda: started)
        _tick_failing(job, tmp_path, deliveries, error="unacked boom")
        incident_id = inc.list_incidents()[0]["id"]
        assert inc.mark_incident_alerted(incident_id) is True

        monkeypatch.setattr(
            inc, "_hermes_now", lambda: started + timedelta(hours=1)
        )
        _tick_failing(job, tmp_path, deliveries, error="unacked boom")

        monkeypatch.setattr(
            inc, "_hermes_now", lambda: started + timedelta(hours=4)
        )
        _tick_failing(
            {**job, "failure_streak": 2},
            tmp_path,
            deliveries,
            error="unacked boom",
        )

    assert len(deliveries) == 2
    assert "failed 3 runs in a row" in deliveries[-1]
    rows = inc.list_incidents()
    assert len(rows) == 1
    assert rows[0]["state"] == "alerted"
    assert rows[0]["last_alerted_at"] == started.isoformat()


def test_ack_suppresses_alert_until_signature_changes(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    deliveries = []
    job = _job()
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        # First failure: alert delivered, incident minted.
        _tick_failing(job, tmp_path, deliveries, error="boom signature A")
        assert len(deliveries) == 1
        rows = inc.list_incidents()
        assert len(rows) == 1 and rows[0]["state"] == "detected"
        inc_id = rows[0]["id"]

        # Acknowledge it.
        assert inc.ack_incident(inc_id) is True

        # Same signature: alert suppressed, incident stays closed.
        _tick_failing(job, tmp_path, deliveries, error="boom signature A")
        assert len(deliveries) == 1, "acked signature must not re-ping"
        assert inc.get_incident(inc_id)["state"] == "closed"

        # Changed signature: new incident, alert again.
        _tick_failing(job, tmp_path, deliveries, error="boom signature B")
        assert len(deliveries) == 2, "changed signature must re-alert"
        assert inc.count_incidents() == 2


def test_mark_incident_alerted_sets_state_never_resurrects(monkeypatch, tmp_path):
    """The post-delivery 'alerted' transition records that a ping went out,
    and is a no-op on a closed (acked) incident — it can never resurrect one."""
    inc = _point_db(monkeypatch, tmp_path)

    inc_id, _ = inc.upsert_incident("job-1", "boom")
    sched._mark_incident_alerted(inc_id)
    assert inc.get_incident(inc_id)["state"] == "alerted"

    inc.ack_incident(inc_id)
    sched._mark_incident_alerted(inc_id)
    assert inc.get_incident(inc_id)["state"] == "closed"

    # Best-effort: bad/missing ids never raise.
    sched._mark_incident_alerted(None)
    sched._mark_incident_alerted("nonexistent")


def test_best_effort_incident_store_failure_fails_open(monkeypatch, tmp_path):
    """An incident-store error must never break the cron delivery path."""
    _point_db(monkeypatch, tmp_path)
    with patch("cron.incidents.upsert_incident_for_alert",
               side_effect=RuntimeError("db locked")):
        assert sched._upsert_incident_for_failure(_job(), "boom") == (
            True, False, None
        )


def test_ack_between_alert_decision_and_state_read_wins():
    with patch(
        "cron.incidents.upsert_incident_for_alert",
        return_value=("incident-1", True),
    ), patch(
        "cron.incidents.get_incident",
        return_value={"id": "incident-1", "state": "closed"},
    ):
        assert sched._upsert_incident_for_failure(_job(), "boom") == (
            False, True, "incident-1"
        )


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_list_and_ack(monkeypatch, tmp_path, capsys):
    from hermes_cli.cron import cron_incidents

    inc = _point_db(monkeypatch, tmp_path)
    inc_id, _ = inc.upsert_incident("job-1", "provider timeout boom")

    # List.
    list_args = argparse.Namespace(
        incident_action="list", state=None, incident_id=None
    )
    assert cron_incidents(list_args) == 0
    out = capsys.readouterr().out
    assert inc_id in out
    assert "job-1" in out

    # State filter.
    filter_args = argparse.Namespace(
        incident_action="list", state="closed", incident_id=None
    )
    assert cron_incidents(filter_args) == 0
    out = capsys.readouterr().out
    assert "No cron failure incidents recorded." in out

    # Ack.
    ack_args = argparse.Namespace(
        incident_action="ack", state=None, incident_id=inc_id
    )
    assert cron_incidents(ack_args) == 0
    assert inc.get_incident(inc_id)["state"] == "closed"
    out = capsys.readouterr().out
    assert "acknowledged" in out.lower()

    # Ack again: already closed, still a clean exit.
    assert cron_incidents(ack_args) == 0
    out = capsys.readouterr().out
    assert "already closed" in out.lower()

    # Ack with a missing id is a usage error.
    missing_args = argparse.Namespace(
        incident_action="ack", state=None, incident_id=None
    )
    assert cron_incidents(missing_args) == 1
