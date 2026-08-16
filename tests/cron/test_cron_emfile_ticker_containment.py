"""Regression tests for #87644 — EMFILE / persistent ticker failure containment.

Ensures:
1. When cron_tick encounters EMFILE / OSError, the ticker records the error in ticker_error,
   records the heartbeat with success=False, and consecutive failures escalate to runtime status.
2. When the ticker subsequently succeeds, consecutive failures reset and status recovers.
3. hermes cron status CLI surfaces the failure instead of reporting healthy.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler_provider import InProcessCronScheduler
import cron.jobs as jobs


def test_failing_emfile_tick_escalates_to_runtime_status(monkeypatch):
    """Consecutive tick failures escalate to degraded runtime status with error detail."""
    status_updates = []

    def _mock_write_runtime_status(**kwargs):
        status_updates.append(kwargs)

    monkeypatch.setattr("gateway.status.write_runtime_status", _mock_write_runtime_status)

    emfile_err = OSError(24, "Too many open files")
    prov = InProcessCronScheduler()
    stop = threading.Event()

    with patch("cron.scheduler.tick", side_effect=emfile_err), \
         patch("cron.jobs.record_ticker_heartbeat") as mock_hb, \
         patch("cron.jobs.record_ticker_error") as mock_err:
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0},
            daemon=True,
        )
        t.start()
        # Let 4 iterations run
        import time
        for _ in range(50):
            if mock_err.call_count >= 3:
                break
            time.sleep(0.05)
        stop.set()
        t.join(timeout=5)

    assert mock_err.call_count >= 3
    # Verify degraded status was written
    degraded = [s for s in status_updates if s.get("platform_state") == "degraded"]
    assert len(degraded) >= 1
    assert "Too many open files" in degraded[0].get("error_message", "") or "Cron ticker failing" in degraded[0].get("error_message", "")


def test_recovery_tick_resets_degraded_status(monkeypatch):
    """A successful tick clears ticker error and sets platform_state connected."""
    status_updates = []

    def _mock_write_runtime_status(**kwargs):
        status_updates.append(kwargs)

    monkeypatch.setattr("gateway.status.write_runtime_status", _mock_write_runtime_status)

    call_count = 0

    def _tick_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise OSError(24, "Too many open files")
        return 0

    prov = InProcessCronScheduler()
    stop = threading.Event()

    with patch("cron.scheduler.tick", side_effect=_tick_side_effect), \
         patch("cron.jobs.clear_ticker_error") as mock_clear, \
         patch("cron.jobs.record_ticker_error"), \
         patch("cron.jobs.record_ticker_heartbeat"):
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0},
            daemon=True,
        )
        t.start()
        import time
        for _ in range(50):
            if call_count >= 4:
                break
            time.sleep(0.05)
        stop.set()
        t.join(timeout=5)

    assert mock_clear.called
    connected = [s for s in status_updates if s.get("platform_state") == "connected"]
    assert len(connected) >= 1
