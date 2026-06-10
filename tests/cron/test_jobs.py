"""Tests for cron/jobs.py — schedule parsing, job CRUD, and due-job detection."""

import json
import threading
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cron.jobs import (
    parse_duration,
    parse_schedule,
    compute_next_run,
    create_job,
    load_jobs,
    save_jobs,
    get_job,
    list_jobs,
    update_job,
    pause_job,
    resume_job,
    remove_job,
    mark_job_run,
    advance_next_run,
    get_due_jobs,
    get_due_and_skipped_jobs,
    save_job_output,
)


# =========================================================================
# parse_duration
# =========================================================================

class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("30m") == 30
        assert parse_duration("1min") == 1
        assert parse_duration("5mins") == 5
        assert parse_duration("10minute") == 10
        assert parse_duration("120minutes") == 120

    def test_hours(self):
        assert parse_duration("2h") == 120
        assert parse_duration("1hr") == 60
        assert parse_duration("3hrs") == 180
        assert parse_duration("1hour") == 60
        assert parse_duration("24hours") == 1440

    def test_days(self):
        assert parse_duration("1d") == 1440
        assert parse_duration("7day") == 7 * 1440
        assert parse_duration("2days") == 2 * 1440

    def test_whitespace_tolerance(self):
        assert parse_duration("  30m  ") == 30
        assert parse_duration("2 h") == 120

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_duration("abc")
        with pytest.raises(ValueError):
            parse_duration("30x")
        with pytest.raises(ValueError):
            parse_duration("")
        with pytest.raises(ValueError):
            parse_duration("m30")


# =========================================================================
# parse_schedule
# =========================================================================

class TestParseSchedule:
    def test_duration_becomes_once(self):
        result = parse_schedule("30m")
        assert result["kind"] == "once"
        assert "run_at" in result
        # run_at should be a valid ISO timestamp string ~30 minutes from now
        run_at_str = result["run_at"]
        assert isinstance(run_at_str, str)
        run_at = datetime.fromisoformat(run_at_str)
        now = datetime.now().astimezone()
        assert run_at > now
        assert run_at < now + timedelta(minutes=31)

    def test_every_becomes_interval(self):
        result = parse_schedule("every 2h")
        assert result["kind"] == "interval"
        assert result["minutes"] == 120

    def test_every_case_insensitive(self):
        result = parse_schedule("Every 30m")
        assert result["kind"] == "interval"
        assert result["minutes"] == 30

    def test_cron_expression(self):
        pytest.importorskip("croniter")
        result = parse_schedule("0 9 * * *")
        assert result["kind"] == "cron"
        assert result["expr"] == "0 9 * * *"

    def test_iso_timestamp(self):
        result = parse_schedule("2030-01-15T14:00:00")
        assert result["kind"] == "once"
        assert "2030-01-15" in result["run_at"]

    def test_invalid_schedule_raises(self):
        with pytest.raises(ValueError):
            parse_schedule("not_a_schedule")

    def test_invalid_cron_raises(self):
        pytest.importorskip("croniter")
        with pytest.raises(ValueError):
            parse_schedule("99 99 99 99 99")


# =========================================================================
# compute_next_run
# =========================================================================

class TestComputeNextRun:
    def test_once_future_returns_time(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        schedule = {"kind": "once", "run_at": future}
        assert compute_next_run(schedule) == future

    def test_once_recent_past_within_grace_returns_time(self, monkeypatch):
        now = datetime(2026, 3, 18, 4, 22, 3, tzinfo=timezone.utc)
        run_at = "2026-03-18T04:22:00+00:00"
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "once", "run_at": run_at}

        assert compute_next_run(schedule) == run_at

    def test_once_past_returns_none(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        schedule = {"kind": "once", "run_at": past}
        assert compute_next_run(schedule) is None

    def test_once_with_last_run_returns_none_even_within_grace(self, monkeypatch):
        now = datetime(2026, 3, 18, 4, 22, 3, tzinfo=timezone.utc)
        run_at = "2026-03-18T04:22:00+00:00"
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "once", "run_at": run_at}

        assert compute_next_run(schedule, last_run_at=now.isoformat()) is None

    def test_interval_first_run(self):
        schedule = {"kind": "interval", "minutes": 60}
        result = compute_next_run(schedule)
        next_dt = datetime.fromisoformat(result)
        # Should be ~60 minutes from now
        assert next_dt > datetime.now().astimezone() + timedelta(minutes=59)

    def test_interval_subsequent_run(self):
        schedule = {"kind": "interval", "minutes": 30}
        last = datetime.now().astimezone().isoformat()
        result = compute_next_run(schedule, last_run_at=last)
        next_dt = datetime.fromisoformat(result)
        # Should be ~30 minutes from last run
        assert next_dt > datetime.now().astimezone() + timedelta(minutes=29)

    def test_cron_returns_future(self):
        pytest.importorskip("croniter")
        schedule = {"kind": "cron", "expr": "* * * * *"}  # every minute
        result = compute_next_run(schedule)
        assert isinstance(result, str), f"Expected ISO timestamp string, got {type(result)}"
        assert len(result) > 0
        next_dt = datetime.fromisoformat(result)
        assert isinstance(next_dt, datetime)
        assert next_dt > datetime.now().astimezone()

    def test_unknown_kind_returns_none(self):
        assert compute_next_run({"kind": "unknown"}) is None


# =========================================================================
# Job CRUD (with tmp file storage)
# =========================================================================

@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestJobCRUD:
    def test_create_and_get(self, tmp_cron_dir):
        job = create_job(prompt="Check server status", schedule="30m")
        assert job["id"]
        assert job["prompt"] == "Check server status"
        assert job["enabled"] is True
        assert job["schedule"]["kind"] == "once"

        fetched = get_job(job["id"])
        assert fetched is not None
        assert fetched["prompt"] == "Check server status"

    def test_list_jobs(self, tmp_cron_dir):
        create_job(prompt="Job 1", schedule="every 1h")
        create_job(prompt="Job 2", schedule="every 2h")
        jobs = list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_normalizes_partial_legacy_records(self, tmp_cron_dir):
        save_jobs([
            {
                "id": "abc123deadbe",
                "name": None,
                "prompt": None,
                "schedule_display": None,
                "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
                "enabled": True,
            }
        ])

        jobs = list_jobs()

        assert jobs[0]["id"] == "abc123deadbe"
        assert jobs[0]["name"] == "abc123deadbe"
        assert jobs[0]["prompt"] == ""
        assert jobs[0]["schedule_display"] == "every 60m"
        assert jobs[0]["state"] == "scheduled"

    def test_remove_job(self, tmp_cron_dir):
        job = create_job(prompt="Temp job", schedule="30m")
        assert remove_job(job["id"]) is True
        assert get_job(job["id"]) is None

    def test_remove_job_rejects_unsafe_legacy_id_before_output_cleanup(self, tmp_cron_dir):
        """Legacy unsafe IDs left over from before the create-time guard
        must fail closed without half-applying the removal."""
        job = create_job(prompt="Legacy unsafe", schedule="every 1h")
        job["id"] = "../escape"
        save_jobs([job])
        outside = tmp_cron_dir / "escape"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")

        with pytest.raises(ValueError, match="output path"):
            remove_job("../escape")

        # Job should still be in the store and the escape dir untouched.
        assert load_jobs()[0]["id"] == "../escape"
        assert (outside / "keep.txt").exists()

    def test_remove_nonexistent_returns_false(self, tmp_cron_dir):
        assert remove_job("nonexistent") is False

    def test_auto_repeat_for_once(self, tmp_cron_dir):
        job = create_job(prompt="One-shot", schedule="1h")
        assert job["repeat"]["times"] == 1

    def test_interval_no_auto_repeat(self, tmp_cron_dir):
        job = create_job(prompt="Recurring", schedule="every 1h")
        assert job["repeat"]["times"] is None

    def test_default_delivery_origin(self, tmp_cron_dir):
        job = create_job(
            prompt="Test", schedule="30m",
            origin={"platform": "telegram", "chat_id": "123"},
        )
        assert job["deliver"] == "origin"

    def test_default_delivery_local_no_origin(self, tmp_cron_dir):
        job = create_job(prompt="Test", schedule="30m")
        assert job["deliver"] == "local"


class TestUpdateJob:
    def test_update_name(self, tmp_cron_dir):
        job = create_job(prompt="Check server status", schedule="every 1h", name="Old Name")
        assert job["name"] == "Old Name"
        updated = update_job(job["id"], {"name": "New Name"})
        assert updated is not None
        assert isinstance(updated, dict)
        assert updated["name"] == "New Name"
        # Verify other fields are preserved
        assert updated["prompt"] == "Check server status"
        assert updated["id"] == job["id"]
        assert updated["schedule"] == job["schedule"]
        # Verify persisted to disk
        fetched = get_job(job["id"])
        assert fetched["name"] == "New Name"

    def test_update_schedule(self, tmp_cron_dir):
        job = create_job(prompt="Daily report", schedule="every 1h")
        assert job["schedule"]["kind"] == "interval"
        assert job["schedule"]["minutes"] == 60
        old_next_run = job["next_run_at"]
        new_schedule = parse_schedule("every 2h")
        updated = update_job(job["id"], {"schedule": new_schedule, "schedule_display": new_schedule["display"]})
        assert updated is not None
        assert updated["schedule"]["kind"] == "interval"
        assert updated["schedule"]["minutes"] == 120
        assert updated["schedule_display"] == "every 120m"
        assert updated["next_run_at"] != old_next_run
        # Verify persisted to disk
        fetched = get_job(job["id"])
        assert fetched["schedule"]["minutes"] == 120
        assert fetched["schedule_display"] == "every 120m"

    def test_update_enable_disable(self, tmp_cron_dir):
        job = create_job(prompt="Toggle me", schedule="every 1h")
        assert job["enabled"] is True
        updated = update_job(job["id"], {"enabled": False})
        assert updated["enabled"] is False
        fetched = get_job(job["id"])
        assert fetched["enabled"] is False

    def test_update_nonexistent_returns_none(self, tmp_cron_dir):
        result = update_job("nonexistent_id", {"name": "X"})
        assert result is None

    def test_update_rejects_id_change(self, tmp_cron_dir):
        """Job IDs are filesystem path components — must be immutable."""
        job = create_job(prompt="Original", schedule="every 1h")

        with pytest.raises(ValueError, match="id"):
            update_job(job["id"], {"id": "../escape"})

        # Original job still resolvable, no rename happened.
        assert get_job(job["id"]) is not None
        assert get_job("../escape") is None


class TestPauseResumeJob:
    def test_pause_sets_state(self, tmp_cron_dir):
        job = create_job(prompt="Pause me", schedule="every 1h")
        paused = pause_job(job["id"], reason="user paused")
        assert paused is not None
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"] == "user paused"

    def test_resume_reenables_job(self, tmp_cron_dir):
        job = create_job(prompt="Resume me", schedule="every 1h")
        pause_job(job["id"], reason="user paused")
        resumed = resume_job(job["id"])
        assert resumed is not None
        assert resumed["enabled"] is True
        assert resumed["state"] == "scheduled"
        assert resumed["paused_at"] is None
        assert resumed["paused_reason"] is None


class TestResolveJobRef:
    """Name-based job lookup for CLI/tool callers (PR #2627, @buntingszn)."""

    def test_resolve_by_exact_id(self, tmp_cron_dir):
        from cron.jobs import resolve_job_ref

        job = create_job(prompt="A", schedule="1h", name="alpha")
        assert resolve_job_ref(job["id"])["id"] == job["id"]

    def test_resolve_by_name(self, tmp_cron_dir):
        from cron.jobs import resolve_job_ref

        job = create_job(prompt="A", schedule="1h", name="alpha")
        assert resolve_job_ref("alpha")["id"] == job["id"]

    def test_resolve_by_name_case_insensitive(self, tmp_cron_dir):
        from cron.jobs import resolve_job_ref

        job = create_job(prompt="A", schedule="1h", name="MyJob")
        assert resolve_job_ref("myjob")["id"] == job["id"]
        assert resolve_job_ref("MYJOB")["id"] == job["id"]

    def test_resolve_returns_none_when_not_found(self, tmp_cron_dir):
        from cron.jobs import resolve_job_ref

        create_job(prompt="A", schedule="1h", name="alpha")
        assert resolve_job_ref("does-not-exist") is None
        assert resolve_job_ref("") is None

    def test_resolve_id_wins_over_name(self, tmp_cron_dir):
        """If a job's name happens to equal another job's ID, ID match wins."""
        from cron.jobs import resolve_job_ref

        j1 = create_job(prompt="A", schedule="1h")
        # Create a second job whose name is j1's ID
        j2 = create_job(prompt="B", schedule="1h", name=j1["id"])
        # Looking up j1["id"] must return j1, not the colliding-name job j2
        assert resolve_job_ref(j1["id"])["id"] == j1["id"]
        assert resolve_job_ref(j1["id"])["id"] != j2["id"]

    def test_resolve_ambiguous_name_raises(self, tmp_cron_dir):
        """Two jobs sharing a name → refuse to pick, surface both IDs."""
        from cron.jobs import AmbiguousJobReference, resolve_job_ref

        j1 = create_job(prompt="A", schedule="1h", name="dup")
        j2 = create_job(prompt="B", schedule="1h", name="dup")
        with pytest.raises(AmbiguousJobReference) as exc_info:
            resolve_job_ref("dup")
        ids = {m["id"] for m in exc_info.value.matches}
        assert ids == {j1["id"], j2["id"]}
        # Error message mentions both IDs so the user can pick one
        assert j1["id"] in str(exc_info.value)
        assert j2["id"] in str(exc_info.value)

    def test_trigger_by_name(self, tmp_cron_dir):
        from cron.jobs import trigger_job

        job = create_job(prompt="A", schedule="1h", name="alpha")
        result = trigger_job("alpha")
        assert result is not None
        assert result["id"] == job["id"]

    def test_pause_by_name(self, tmp_cron_dir):
        job = create_job(prompt="A", schedule="1h", name="alpha")
        result = pause_job("alpha", reason="manual")
        assert result is not None
        assert result["id"] == job["id"]
        assert result["state"] == "paused"

    def test_remove_by_name(self, tmp_cron_dir):
        job = create_job(prompt="A", schedule="1h", name="alpha")
        assert remove_job("alpha") is True
        assert get_job(job["id"]) is None

    def test_mutations_refuse_ambiguous_name(self, tmp_cron_dir):
        """pause/resume/trigger/remove must refuse to act on an ambiguous name."""
        from cron.jobs import AmbiguousJobReference, trigger_job

        create_job(prompt="A", schedule="1h", name="dup")
        create_job(prompt="B", schedule="1h", name="dup")
        for fn in (pause_job, resume_job, trigger_job):
            with pytest.raises(AmbiguousJobReference):
                fn("dup")
        with pytest.raises(AmbiguousJobReference):
            remove_job("dup")


class TestMarkJobRun:
    def test_increments_completed(self, tmp_cron_dir):
        job = create_job(prompt="Test", schedule="every 1h")
        mark_job_run(job["id"], success=True)
        updated = get_job(job["id"])
        assert updated["repeat"]["completed"] == 1
        assert updated["last_status"] == "ok"

    def test_repeat_limit_removes_job(self, tmp_cron_dir):
        job = create_job(prompt="Once", schedule="30m", repeat=1)
        mark_job_run(job["id"], success=True)
        # Job should be removed after hitting repeat limit
        assert get_job(job["id"]) is None

    def test_repeat_negative_one_is_infinite(self, tmp_cron_dir):
        # LLMs often pass repeat=-1 to mean "infinite/forever".
        # The job must NOT be deleted after runs when repeat <= 0.
        job = create_job(prompt="Forever", schedule="every 1h", repeat=-1)
        # -1 should be normalised to None (infinite) at create time
        assert job["repeat"]["times"] is None
        # Running it multiple times should never delete it
        for _ in range(3):
            mark_job_run(job["id"], success=True)
            assert get_job(job["id"]) is not None, "job was deleted after run despite infinite repeat"

    def test_repeat_zero_is_infinite(self, tmp_cron_dir):
        # repeat=0 should also be treated as None (infinite), not "run zero times".
        job = create_job(prompt="ZeroRepeat", schedule="every 1h", repeat=0)
        assert job["repeat"]["times"] is None
        mark_job_run(job["id"], success=True)
        assert get_job(job["id"]) is not None

    def test_error_status(self, tmp_cron_dir):
        job = create_job(prompt="Fail", schedule="every 1h")
        mark_job_run(job["id"], success=False, error="timeout")
        updated = get_job(job["id"])
        assert updated["last_status"] == "error"
        assert updated["last_error"] == "timeout"

    def test_delivery_error_tracked_separately(self, tmp_cron_dir):
        """Agent succeeds but delivery fails — both tracked independently."""
        job = create_job(prompt="Report", schedule="every 1h")
        mark_job_run(job["id"], success=True, delivery_error="platform 'telegram' not configured")
        updated = get_job(job["id"])
        assert updated["last_status"] == "ok"
        assert updated["last_error"] is None
        assert updated["last_delivery_error"] == "platform 'telegram' not configured"

    def test_delivery_error_cleared_on_success(self, tmp_cron_dir):
        """Successful delivery clears the previous delivery error."""
        job = create_job(prompt="Report", schedule="every 1h")
        mark_job_run(job["id"], success=True, delivery_error="network timeout")
        updated = get_job(job["id"])
        assert updated["last_delivery_error"] == "network timeout"
        # Next run delivers successfully
        mark_job_run(job["id"], success=True, delivery_error=None)
        updated = get_job(job["id"])
        assert updated["last_delivery_error"] is None

    def test_both_agent_and_delivery_error(self, tmp_cron_dir):
        """Agent fails AND delivery fails — both errors recorded."""
        job = create_job(prompt="Report", schedule="every 1h")
        mark_job_run(job["id"], success=False, error="model timeout",
                     delivery_error="platform 'discord' not enabled")
        updated = get_job(job["id"])
        assert updated["last_status"] == "error"
        assert updated["last_error"] == "model timeout"
        assert updated["last_delivery_error"] == "platform 'discord' not enabled"

    def test_recurring_cron_not_disabled_when_croniter_missing(self, tmp_cron_dir, monkeypatch):
        """Regression test for issue #16265.

        If the gateway runs in an env where `croniter` went missing after a
        recurring cron job was persisted, `compute_next_run()` returns None.
        `mark_job_run()` must NOT treat that as terminal completion — the job
        has to stay enabled with state=error so the user notices, rather than
        silently flipping to enabled=false, state=completed.
        """
        pytest.importorskip("croniter")  # need it to create the job
        job = create_job(prompt="Recurring", schedule="0 7,15,23 * * *")
        assert job["schedule"]["kind"] == "cron"

        # Simulate the runtime env having lost croniter between job creation
        # and this run.
        monkeypatch.setattr("cron.jobs.HAS_CRONITER", False)

        mark_job_run(job["id"], success=True)

        updated = get_job(job["id"])
        assert updated is not None, "recurring cron job was deleted"
        assert updated["enabled"] is True, (
            "recurring cron job was disabled despite croniter-missing being "
            "a runtime dep issue, not a terminal completion"
        )
        assert updated["state"] == "error"
        assert updated["state"] != "completed"
        assert updated["next_run_at"] is None
        assert updated["last_error"]
        assert "croniter" in updated["last_error"].lower()

    def test_recurring_interval_not_disabled_when_next_run_is_none(self, tmp_cron_dir, monkeypatch):
        """Defensive sibling of the cron test — any recurring schedule that
        somehow yields next_run_at=None must stay enabled with state=error.
        """
        job = create_job(prompt="Recurring", schedule="every 1h")
        assert job["schedule"]["kind"] == "interval"

        # Force compute_next_run to return None for this call — simulates
        # any future regression where a recurring schedule loses its
        # next-run computation (missing dep, corrupt schedule, etc.).
        monkeypatch.setattr("cron.jobs.compute_next_run", lambda *a, **kw: None)

        mark_job_run(job["id"], success=True)

        updated = get_job(job["id"])
        assert updated is not None
        assert updated["enabled"] is True
        assert updated["state"] == "error"
        assert updated["state"] != "completed"

    def test_oneshot_still_completes_when_next_run_is_none(self, tmp_cron_dir):
        """One-shot jobs must still flip to enabled=false, state=completed
        when next_run_at cannot be computed — the #16265 fix must not
        regress this path. We bypass create_job and craft a minimal
        one-shot record directly so that the repeat-limit branch doesn't
        pop the job before we observe the terminal-completion branch.
        """
        jobs = [{
            "id": "oneshot-test",
            "prompt": "Once",
            "schedule": {"kind": "once", "run_at": "2020-01-01T00:00:00+00:00", "display": "once"},
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "created_at": "2020-01-01T00:00:00+00:00",
        }]
        save_jobs(jobs)

        mark_job_run("oneshot-test", success=True)

        updated = get_job("oneshot-test")
        assert updated is not None
        assert updated["next_run_at"] is None
        assert updated["enabled"] is False
        assert updated["state"] == "completed"


class TestAdvanceNextRun:
    """Tests for advance_next_run() — crash-safety for recurring jobs."""

    def test_advances_interval_job(self, tmp_cron_dir):
        """Interval jobs should have next_run_at bumped to the next future occurrence."""
        job = create_job(prompt="Recurring check", schedule="every 1h")
        # Force next_run_at to 5 minutes ago (i.e. the job is due)
        jobs = load_jobs()
        old_next = (datetime.now() - timedelta(minutes=5)).isoformat()
        jobs[0]["next_run_at"] = old_next
        save_jobs(jobs)

        result = advance_next_run(job["id"])
        assert result is True

        updated = get_job(job["id"])
        from cron.jobs import _ensure_aware, _hermes_now
        new_next_dt = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert new_next_dt > _hermes_now(), "next_run_at should be in the future after advance"

    def test_advances_cron_job(self, tmp_cron_dir):
        """Cron-expression jobs should have next_run_at bumped to the next occurrence."""
        pytest.importorskip("croniter")
        job = create_job(prompt="Daily wakeup", schedule="15 6 * * *")
        # Force next_run_at to 30 minutes ago
        jobs = load_jobs()
        old_next = (datetime.now() - timedelta(minutes=30)).isoformat()
        jobs[0]["next_run_at"] = old_next
        save_jobs(jobs)

        result = advance_next_run(job["id"])
        assert result is True

        updated = get_job(job["id"])
        from cron.jobs import _ensure_aware, _hermes_now
        new_next_dt = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert new_next_dt > _hermes_now(), "next_run_at should be in the future after advance"

    def test_skips_oneshot_job(self, tmp_cron_dir):
        """One-shot jobs should NOT be advanced — they need to retry on restart."""
        job = create_job(prompt="Run once", schedule="30m")
        original_next = get_job(job["id"])["next_run_at"]

        result = advance_next_run(job["id"])
        assert result is False

        updated = get_job(job["id"])
        assert updated["next_run_at"] == original_next, "one-shot next_run_at should be unchanged"

    def test_nonexistent_job_returns_false(self, tmp_cron_dir):
        result = advance_next_run("nonexistent-id")
        assert result is False

    def test_already_future_stays_future(self, tmp_cron_dir):
        """If next_run_at is already in the future, advance keeps it in the future (no harm)."""
        job = create_job(prompt="Future job", schedule="every 1h")
        # next_run_at is already set to ~1h from now by create_job
        advance_next_run(job["id"])
        # Regardless of return value, the job should still be in the future
        updated = get_job(job["id"])
        from cron.jobs import _ensure_aware, _hermes_now
        new_next_dt = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert new_next_dt > _hermes_now(), "next_run_at should remain in the future"

    def test_crash_safety_scenario(self, tmp_cron_dir):
        """Simulate the crash-loop scenario: after advance, the job should NOT be due."""
        job = create_job(prompt="Crash test", schedule="every 1h")
        # Force next_run_at to 5 minutes ago (job is due)
        jobs = load_jobs()
        jobs[0]["next_run_at"] = (datetime.now() - timedelta(minutes=5)).isoformat()
        save_jobs(jobs)

        # Job should be due before advance
        due_before = get_due_jobs()
        assert len(due_before) == 1

        # Advance (simulating what tick() does before run_job)
        advance_next_run(job["id"])

        # Now the job should NOT be due (simulates restart after crash)
        due_after = get_due_jobs()
        assert len(due_after) == 0, "Job should not be due after advance_next_run"


class TestGetDueJobs:
    def test_past_due_within_window_returned(self, tmp_cron_dir):
        """Jobs within the dynamic grace window are still considered due (not stale).

        For an hourly job, grace = 30 min (half the period, clamped to [120s, 2h]).
        """
        job = create_job(prompt="Due now", schedule="every 1h")
        # Force next_run_at to 10 minutes ago (within the 30-min grace for hourly)
        jobs = load_jobs()
        jobs[0]["next_run_at"] = (datetime.now() - timedelta(minutes=10)).isoformat()
        save_jobs(jobs)

        due = get_due_jobs()
        assert len(due) == 1
        assert due[0]["id"] == job["id"]

    def test_stale_past_due_skipped(self, tmp_cron_dir):
        """Recurring jobs past grace fire-once-on-recovery (daily-or-shorter, miss<=24h).

        Per cron-restart-catchup-gap design (2026-04-30): an hourly cron missed
        by 35 min (period=3600s <= 86400s, missed <= 86400s, no skip_only opt-out)
        is fire-once-eligible. The job is returned in `due`; the scheduler's
        advance_next_run() (called before each run) advances next_run_at so the
        same miss is not re-fired on subsequent ticks.
        """
        job = create_job(prompt="Stale", schedule="every 1h")
        # Force next_run_at to 35 minutes ago (beyond the 30-min grace for hourly)
        jobs = load_jobs()
        jobs[0]["next_run_at"] = (datetime.now() - timedelta(minutes=35)).isoformat()
        save_jobs(jobs)

        due = get_due_jobs()
        # Hourly past grace is now fire-once-eligible — appears in due.
        assert len(due) == 1
        assert due[0]["id"] == job["id"]
        # next_run_at is left intact for the scheduler to advance pre-run.
        updated = get_job(job["id"])
        from cron.jobs import _ensure_aware, _hermes_now
        next_dt = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert next_dt < _hermes_now(), \
            "fire-once leaves next_run_at in the past; scheduler advances pre-run"

    def test_future_not_returned(self, tmp_cron_dir):
        create_job(prompt="Not yet", schedule="every 1h")
        due = get_due_jobs()
        assert len(due) == 0

    def test_disabled_not_returned(self, tmp_cron_dir):
        job = create_job(prompt="Disabled", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["enabled"] = False
        jobs[0]["next_run_at"] = (datetime.now() - timedelta(minutes=5)).isoformat()
        save_jobs(jobs)

        due = get_due_jobs()
        assert len(due) == 0

    def test_broken_recent_one_shot_without_next_run_is_recovered(self, tmp_cron_dir, monkeypatch):
        now = datetime(2026, 3, 18, 4, 22, 30, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        run_at = "2026-03-18T04:22:00+00:00"
        save_jobs(
            [{
                "id": "oneshot-recover",
                "name": "Recover me",
                "prompt": "Word of the day",
                "schedule": {"kind": "once", "run_at": run_at, "display": "once at 2026-03-18 04:22"},
                "schedule_display": "once at 2026-03-18 04:22",
                "repeat": {"times": 1, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": "2026-03-18T04:21:00+00:00",
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "deliver": "local",
                "origin": None,
            }]
        )

        due = get_due_jobs()

        assert [job["id"] for job in due] == ["oneshot-recover"]
        assert get_job("oneshot-recover")["next_run_at"] == run_at

    def test_broken_stale_one_shot_without_next_run_is_not_recovered(self, tmp_cron_dir, monkeypatch):
        now = datetime(2026, 3, 18, 4, 30, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        save_jobs(
            [{
                "id": "oneshot-stale",
                "name": "Too old",
                "prompt": "Word of the day",
                "schedule": {"kind": "once", "run_at": "2026-03-18T04:22:00+00:00", "display": "once at 2026-03-18 04:22"},
                "schedule_display": "once at 2026-03-18 04:22",
                "repeat": {"times": 1, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": "2026-03-18T04:21:00+00:00",
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "deliver": "local",
                "origin": None,
            }]
        )

        assert get_due_jobs() == []
        assert get_job("oneshot-stale")["next_run_at"] is None

    def test_broken_interval_without_next_run_is_recovered(self, tmp_cron_dir, monkeypatch):
        """Regression: interval jobs with valid schedule but null next_run_at
        used to be silently skipped forever (jobs.py:702-710 only recovered
        kind=once). Real incident: 2026-04-29 critic-skill-review zombie.
        """
        now = datetime(2026, 4, 29, 5, 19, 41, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        save_jobs(
            [{
                "id": "interval-zombie",
                "name": "critic-skill-review",
                "prompt": "do work",
                "schedule": {"kind": "interval", "minutes": 480, "display": "every 480m"},
                "schedule_display": "every 480m",
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": "2026-04-26T17:15:15+00:00",
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "deliver": "local",
                "origin": None,
            }]
        )

        # Interval job is not yet due (next run = now + 480m), so it
        # shouldn't appear in the due list — but next_run_at MUST be
        # populated and persisted so the next tick after its window picks
        # it up instead of skipping forever.
        assert get_due_jobs() == []
        recovered = get_job("interval-zombie")["next_run_at"]
        assert recovered is not None
        recovered_dt = datetime.fromisoformat(recovered)
        if recovered_dt.tzinfo is None:
            recovered_dt = recovered_dt.replace(tzinfo=timezone.utc)
        expected = now + timedelta(minutes=480)
        assert abs((recovered_dt - expected).total_seconds()) < 5

    def test_broken_cron_without_next_run_is_recovered(self, tmp_cron_dir, monkeypatch):
        """Regression: cron-expression jobs with valid schedule but null
        next_run_at used to be silently skipped forever. Real incident:
        2026-04-29 curator-nightly + Pipeline Drift Audit zombies.
        """
        now = datetime(2026, 4, 29, 5, 19, 41, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        save_jobs(
            [{
                "id": "cron-zombie",
                "name": "curator-nightly",
                "prompt": "do work",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"},
                "schedule_display": "0 7 * * *",
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": "2026-04-26T18:30:00+00:00",
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "deliver": "local",
                "origin": None,
            }]
        )

        # Next 07:00 UTC after 2026-04-29 05:19 is 2026-04-29 07:00.
        assert get_due_jobs() == []
        recovered = get_job("cron-zombie")["next_run_at"]
        assert recovered is not None
        recovered_dt = datetime.fromisoformat(recovered)
        if recovered_dt.tzinfo is None:
            recovered_dt = recovered_dt.replace(tzinfo=timezone.utc)
        assert recovered_dt > now
        # Must be the same day at 07:00, not silently skipped to a
        # past time or the next day.
        assert recovered_dt.hour == 7 and recovered_dt.minute == 0
        assert recovered_dt.date() == now.date()


class TestEnabledToolsets:
    def test_enabled_toolsets_stored(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h", enabled_toolsets=["web", "terminal"])
        assert job["enabled_toolsets"] == ["web", "terminal"]

    def test_enabled_toolsets_persisted(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h", enabled_toolsets=["web", "file"])
        fetched = get_job(job["id"])
        assert fetched["enabled_toolsets"] == ["web", "file"]

    def test_enabled_toolsets_none_when_omitted(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h")
        assert job["enabled_toolsets"] is None

    def test_enabled_toolsets_empty_list_normalizes_to_none(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h", enabled_toolsets=[])
        assert job["enabled_toolsets"] is None

    def test_enabled_toolsets_whitespace_entries_stripped(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h", enabled_toolsets=["web", " ", "file"])
        assert job["enabled_toolsets"] == ["web", "file"]

    def test_enabled_toolsets_updated_via_update_job(self, tmp_cron_dir):
        job = create_job(prompt="monitor", schedule="every 1h")
        update_job(job["id"], {"enabled_toolsets": ["web", "delegation"]})
        fetched = get_job(job["id"])
        assert fetched["enabled_toolsets"] == ["web", "delegation"]


class TestMarkJobRunConcurrency:
    """Regression tests for concurrent parallel job state writes.

    tick() dispatches multiple jobs to separate threads simultaneously.
    Without _jobs_file_lock protecting the load→modify→save cycle in
    mark_job_run(), concurrent writes can clobber each other's updates
    (last-writer-wins), leaving some jobs with stale last_status / last_run_at.
    """

    def test_three_concurrent_mark_job_run_no_overwrites(self, tmp_cron_dir):
        """Run mark_job_run() for 3 jobs in parallel threads; all must land correctly."""
        # Create 3 distinct recurring jobs
        job_a = create_job(prompt="Job A", schedule="every 1h")
        job_b = create_job(prompt="Job B", schedule="every 1h")
        job_c = create_job(prompt="Job C", schedule="every 1h")

        errors: list = []

        def run_mark(job_id: str, success: bool, error_msg=None):
            try:
                mark_job_run(job_id, success=success, error=error_msg)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        # Fire all three concurrently
        threads = [
            threading.Thread(target=run_mark, args=(job_a["id"], True)),
            threading.Thread(target=run_mark, args=(job_b["id"], False, "timeout")),
            threading.Thread(target=run_mark, args=(job_c["id"], True)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected exceptions in worker threads: {errors}"

        # Verify each job has the correct state — no overwrites
        a = get_job(job_a["id"])
        b = get_job(job_b["id"])
        c = get_job(job_c["id"])

        assert a is not None, "Job A was unexpectedly deleted"
        assert b is not None, "Job B was unexpectedly deleted"
        assert c is not None, "Job C was unexpectedly deleted"

        assert a["last_status"] == "ok", f"Job A last_status wrong: {a['last_status']}"
        assert a["last_run_at"] is not None, "Job A last_run_at not set"
        assert a["repeat"]["completed"] == 1, f"Job A completed count wrong: {a['repeat']['completed']}"

        assert b["last_status"] == "error", f"Job B last_status wrong: {b['last_status']}"
        assert b["last_error"] == "timeout", f"Job B last_error wrong: {b['last_error']}"
        assert b["last_run_at"] is not None, "Job B last_run_at not set"
        assert b["repeat"]["completed"] == 1, f"Job B completed count wrong: {b['repeat']['completed']}"

        assert c["last_status"] == "ok", f"Job C last_status wrong: {c['last_status']}"
        assert c["last_run_at"] is not None, "Job C last_run_at not set"
        assert c["repeat"]["completed"] == 1, f"Job C completed count wrong: {c['repeat']['completed']}"

    def test_repeated_concurrent_runs_accumulate_completed_count(self, tmp_cron_dir):
        """Stress test: 10 threads each call mark_job_run on a different job once.

        The completed count for every job must be exactly 1 after all threads finish,
        confirming no thread's write was silently dropped.
        """
        n = 10
        jobs = [create_job(prompt=f"Stress job {i}", schedule="every 1h") for i in range(n)]
        errors: list = []

        def run_mark(job_id: str):
            try:
                mark_job_run(job_id, success=True)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run_mark, args=(j["id"],)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected exceptions: {errors}"

        for job in jobs:
            updated = get_job(job["id"])
            assert updated is not None, f"Job {job['id']} was deleted"
            assert updated["last_status"] == "ok", (
                f"Job {job['id']} has wrong last_status: {updated['last_status']}"
            )
            assert updated["repeat"]["completed"] == 1, (
                f"Job {job['id']} completed count is {updated['repeat']['completed']}, expected 1"
            )


class TestSaveJobOutput:
    def test_creates_output_file(self, tmp_cron_dir):
        output_file = save_job_output("test123", "# Results\nEverything ok.")
        assert output_file.exists()
        assert output_file.read_text() == "# Results\nEverything ok."
        assert "test123" in str(output_file)


from cron.jobs import _compute_period_seconds


class TestComputePeriodSeconds:
    def test_interval_returns_minutes_x_60(self):
        schedule = {"kind": "interval", "minutes": 480}
        assert _compute_period_seconds(schedule) == 480 * 60

    def test_interval_minute_fallback(self):
        schedule = {"kind": "interval", "minutes": 1}
        assert _compute_period_seconds(schedule) == 60

    def test_cron_daily_returns_86400(self):
        schedule = {"kind": "cron", "expr": "0 19 * * *"}
        assert _compute_period_seconds(schedule) == 86400

    def test_cron_weekly_returns_604800(self):
        schedule = {"kind": "cron", "expr": "0 9 * * 1"}
        assert _compute_period_seconds(schedule) == 604800

    def test_cron_every_5h_returns_18000(self):
        schedule = {"kind": "cron", "expr": "0 8,13,18 * * *"}
        # Periods are 5h, 5h, 14h — first interval used (matches grace logic)
        assert _compute_period_seconds(schedule) == 18000

    def test_once_returns_none(self):
        assert _compute_period_seconds({"kind": "once", "at": "2026-05-01T10:00:00Z"}) is None

    def test_unknown_kind_returns_none(self):
        assert _compute_period_seconds({"kind": "weird"}) is None

    def test_invalid_cron_expr_returns_none(self):
        assert _compute_period_seconds({"kind": "cron", "expr": "not a cron"}) is None


def _set_next_run(job_id: str, iso: str) -> None:
    """Test helper — directly mutate next_run_at for a job."""
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["next_run_at"] = iso
    save_jobs(jobs)


def _set_recovery_policy(job_id: str, policy: str) -> None:
    """Test helper — set top-level recovery_policy field."""
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["recovery_policy"] = policy
    save_jobs(jobs)


class TestRecoveryPolicy:
    """Fire-once-on-recovery + skip-only-emit behavior for missed crons."""

    def test_daily_cron_missed_within_24h_fires_once(self, tmp_cron_dir, monkeypatch):
        """Daily cron missed by 4h, no recovery_policy → fire once."""
        # Freeze time
        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        # Set next_run_at 4h ago (yesterday's 23:00 UTC fire)
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        due, skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), "fire-once-eligible cron should be in due list"
        assert not any(s["job_id"] == job["id"] for s in skipped), "fire-once-eligible should NOT be in skipped"

    def test_daily_cron_missed_over_24h_skip_only(self, tmp_cron_dir, monkeypatch):
        """Daily cron missed by 30h → exceeds 24h cap → skip + emit."""
        now = datetime(2026, 4, 30, 5, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-28T23:00:00+00:00")

        due, skipped = get_due_and_skipped_jobs()

        assert not any(j["id"] == job["id"] for j in due), "missed >24h should NOT fire"
        skip_entries = [s for s in skipped if s["job_id"] == job["id"]]
        assert len(skip_entries) == 1
        entry = skip_entries[0]
        assert entry["reason"] == "miss_exceeded_24h_cap"
        assert entry["missed_seconds"] >= 24 * 3600
        assert entry["schedule_kind"] == "cron"

    def test_skip_only_recovery_policy_blocks_fire_once(self, tmp_cron_dir, monkeypatch):
        """Daily cron missed by 3h (past 2h grace) with skip_only → skip + emit."""
        now = datetime(2026, 4, 30, 2, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="anchored daily", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")
        _set_recovery_policy(job["id"], "skip_only")

        due, skipped = get_due_and_skipped_jobs()

        assert not any(j["id"] == job["id"] for j in due), "skip_only must block fire-once"
        skip_entries = [s for s in skipped if s["job_id"] == job["id"]]
        assert len(skip_entries) == 1
        assert skip_entries[0]["reason"] == "skip_only"

    def test_weekly_cron_default_skip(self, tmp_cron_dir, monkeypatch):
        """Weekly cron missed by 3h (past 2h grace) → never fire-once → skip + emit."""
        now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="weekly retro", schedule="0 9 * * 1")
        _set_next_run(job["id"], "2026-04-27T09:00:00+00:00")

        due, skipped = get_due_and_skipped_jobs()

        assert not any(j["id"] == job["id"] for j in due), "weekly cron must not fire stale"
        skip_entries = [s for s in skipped if s["job_id"] == job["id"]]
        assert len(skip_entries) == 1
        assert skip_entries[0]["reason"] == "default_period_cap"

    def test_weekly_cron_within_grace_fires_on_time(self, tmp_cron_dir, monkeypatch):
        """Weekly cron observed 12s after its instant is an ON-TIME fire, not a miss.

        Regression for security-audit-weekly (9225c1940fdd): the sequential
        scheduler tick always observes a due job some seconds late, so a
        period-cap that treats ANY positive lateness as a missed window makes
        every weekly cron permanently unable to fire (2026-06-01 and
        2026-06-08 skips, missed_seconds 12/27, reason=default_period_cap).
        Within grace (period/2 clamped to [120s, 7200s]) the job must fire.
        """
        now = datetime(2026, 4, 27, 9, 0, 12, tzinfo=timezone.utc)  # Monday 09:00:12
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="weekly audit", schedule="0 9 * * 1")
        _set_next_run(job["id"], "2026-04-27T09:00:00+00:00")

        due, skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), \
            "weekly cron within grace must fire on time"
        assert not any(s["job_id"] == job["id"] for s in skipped), \
            "on-time fire must not emit cron_skipped"

    def test_skip_only_within_grace_fires_on_time(self, tmp_cron_dir, monkeypatch):
        """skip_only governs miss RECOVERY, not normal operation.

        A skip_only daily observed 30s after its instant (tick jitter) is an
        on-time fire — without this, skip_only crons (scribe-am/pm,
        learning-loop, ...) never fire at all.
        """
        now = datetime(2026, 4, 29, 23, 0, 30, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="anchored daily", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")
        _set_recovery_policy(job["id"], "skip_only")

        due, skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), \
            "skip_only cron within grace must fire on time"
        assert not any(s["job_id"] == job["id"] for s in skipped)

    def test_short_period_within_grace_unchanged(self, tmp_cron_dir, monkeypatch):
        """10-min interval missed by 4 min stays in due (existing path), no skip emit."""
        now = datetime(2026, 4, 30, 12, 4, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="frequent poll", schedule="every 10m")
        _set_next_run(job["id"], "2026-04-30T12:00:00+00:00")

        due, skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), "within-grace miss should fire normally"
        assert not any(s["job_id"] == job["id"] for s in skipped)

    def test_fire_once_advances_next_run_at_no_redundant_fire(self, tmp_cron_dir, monkeypatch):
        """After a fire-once tick, advance_next_run keeps the job out of due on second tick."""
        from cron.jobs import advance_next_run

        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        # First tick — job is fire-once eligible
        due_1, _ = get_due_and_skipped_jobs()
        assert any(j["id"] == job["id"] for j in due_1)

        # Scheduler.tick() advances next_run_at before running — simulate that
        for j in due_1:
            advance_next_run(j["id"])

        # Second tick at the same `now` — job must NOT reappear
        due_2, _ = get_due_and_skipped_jobs()
        assert not any(j["id"] == job["id"] for j in due_2), \
            "advance_next_run must move past the missed time so we don't re-fire"


# =========================================================================
# trigger_job — caller traceability
#
# Spec: docs/superpowers/plans/2026-04-30-cron-trigger-traceability.md
# Origin: 2026-04-30 sentinel-vip-morning triple-fire silence-investigation
# =========================================================================

class TestTriggerJob:
    def test_basic_signature_with_caller_and_reason(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import create_job, trigger_job
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = trigger_job(
            job["id"],
            caller="hermes_cli:cron_run",
            reason="investigation 2026-04-30",
        )

        assert result is not None
        assert result["state"] == "scheduled"

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        e = events[0]
        assert e.payload["caller"] == "hermes_cli:cron_run"
        assert e.payload["reason"] == "investigation 2026-04-30"
        assert e.payload["job_id"] == job["id"]
        assert e.payload["job_name"] == job["name"]
        assert e.payload["previous_next_run_at"] == job["next_run_at"]
        assert e.payload["new_next_run_at"] == result["next_run_at"]

    def test_anonymous_caller_logs_warning(self, tmp_cron_dir, monkeypatch, caplog):
        import logging
        from cron.jobs import create_job, trigger_job
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        with caplog.at_level(logging.WARNING, logger="cron.jobs"):
            trigger_job(job["id"])  # no caller

        assert any(
            "anonymous" in rec.message.lower() or "caller=None" in rec.message
            for rec in caplog.records
        ), f"Expected anonymous-caller warning; got: {[r.message for r in caplog.records]}"

    def test_returns_none_for_unknown_job(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import trigger_job
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        assert trigger_job("nonexistent", caller="test") is None
        assert bus.query(event_type=EventType.CRON_TRIGGERED) == []

    def test_emit_failure_does_not_break_trigger(self, tmp_cron_dir, monkeypatch):
        """Bus failure must not propagate — trigger_job must still update state."""
        from cron.jobs import create_job, trigger_job, get_job

        def broken_bus():
            raise RuntimeError("bus broken")

        monkeypatch.setattr("cron.jobs._get_event_bus", broken_bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = trigger_job(job["id"], caller="test")

        assert result is not None
        assert result["state"] == "scheduled"
        assert get_job(job["id"])["state"] == "scheduled"

    @pytest.mark.parametrize("bad_job_id", ["../escape", "nested/escape", ".", "..", ""])
    def test_rejects_unsafe_job_id(self, tmp_cron_dir, bad_job_id):
        """Path-escape attempts must fail closed and never create dirs."""
        with pytest.raises(ValueError, match="output path"):
            save_job_output(bad_job_id, "# Results")
        assert not (tmp_cron_dir / "escape").exists()

    def test_rejects_absolute_job_id(self, tmp_cron_dir):
        """Absolute paths as job IDs must fail closed."""
        with pytest.raises(ValueError, match="output path"):
            save_job_output(str(tmp_cron_dir / "outside"), "# Results")
        assert not (tmp_cron_dir / "outside").exists()
