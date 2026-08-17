"""Regression test for #88220: cron next_run_at must be evaluated in UTC.

The ticker's due-check compares next_run_at against the wall clock in UTC
terms, but compute_next_run() used _hermes_now() (the *configured* zone) as
the croniter base. With timezone: Asia/Shanghai and a system UTC clock, a
"30 14 * * *" job persisted "2026-08-17T14:30:00+08:00" — an absolute
instant 8h earlier than the intended 14:30 UTC fire — so a gateway restart
made the job fire 8h early.
"""
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

pytest.importorskip("croniter")

from cron.jobs import compute_next_run


class TestCronNextRunUTCBaseline:
    """compute_next_run MUST evaluate cron expressions in UTC so the
    persisted next_run_at is the correct absolute instant regardless of the
    configured timezone."""

    def test_shanghai_config_system_utc(self, monkeypatch):
        shanghai = ZoneInfo("Asia/Shanghai")
        # 2026-08-17 15:51 Shanghai == 07:51 UTC (system clock).
        now = datetime(2026, 8, 17, 15, 51, 3, tzinfo=shanghai)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "cron", "expr": "30 14 * * *"}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)

        # Intended fire: next 14:30 UTC (2026-08-17, since now is 07:51 UTC).
        assert next_dt.utcoffset().total_seconds() == 0, (
            f"next_run_at must be UTC, got {result}"
        )
        assert next_dt == datetime(2026, 8, 17, 14, 30, 0, tzinfo=timezone.utc), (
            f"Expected 2026-08-17T14:30:00+00:00, got {result}"
        )

    def test_shanghai_restart_with_last_run_at(self, monkeypatch):
        shanghai = ZoneInfo("Asia/Shanghai")
        # Gateway restarted at 06:25 UTC == 14:25 Shanghai; last run was
        # 2026-08-16 14:30 UTC (== 22:30 Shanghai).
        now = datetime(2026, 8, 17, 14, 25, 0, tzinfo=shanghai)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
        last_run = datetime(2026, 8, 16, 22, 30, 0, tzinfo=shanghai)

        schedule = {"kind": "cron", "expr": "30 14 * * *"}
        result = compute_next_run(schedule, last_run_at=last_run.isoformat())
        assert result is not None
        next_dt = datetime.fromisoformat(result)

        # Next fire is 2026-08-17 14:30 UTC — NOT 8h early at 06:30 UTC.
        assert next_dt == datetime(2026, 8, 17, 14, 30, 0, tzinfo=timezone.utc), (
            f"Expected 2026-08-17T14:30:00+00:00, got {result}"
        )

    def test_utc_config_unchanged(self, monkeypatch):
        """A UTC-configured environment must be unaffected (no behavior
        change when configured zone already is UTC)."""
        utc = timezone.utc
        now = datetime(2026, 8, 17, 7, 51, 3, tzinfo=utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "cron", "expr": "30 14 * * *"}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == datetime(2026, 8, 17, 14, 30, 0, tzinfo=timezone.utc)
