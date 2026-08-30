"""Drift-guard skips must alert once per job, not once per tick (#44585 + #73506).

Field report: a fleet-wide config change moved the global
default provider and every unpinned cron started alerting on every tick —
40 jobs x N ticks of identical "Skipped to prevent unintended spend" spam.
The #44585 drift guard correctly fails closed; this wires the existing
#73506 alert-once shape (persisted per-job bit, cleared when the condition
heals) to the drift branch, exactly as pre-dispatch preflight already does
for blocked_config.

Contract:
- First drifted tick delivers ONE loud, actionable alert.
- Subsequent drifted ticks deliver nothing.
- When drift heals (guard passes again), the bit clears, so a FUTURE drift
  re-alerts instead of being silently swallowed.
- Only the drift branch gets the bit — other failures keep alerting per tick.
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
import cron.scheduler as sched


def _job(**overrides):
    job = {
        "id": "drift-once-test",
        "name": "drift once test",
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


def _tick(job, tmp_path, current_provider, deliveries):
    """Run one run_one_job tick with the provider resolution pinned."""
    fake_db = MagicMock()

    def fake_deliver(job, content, adapters=None, loop=None):
        deliveries.append(content)
        return None

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": current_provider,
                   "api_mode": "chat_completions",
               }), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        ok = sched.run_one_job(job)
    return ok, mock_agent_cls.called


class TestDriftAlertOnce:
    def test_two_drifted_ticks_alert_exactly_once(self, tmp_path):
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            for _ in range(2):
                fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
                ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
                assert agent_called is False, "drifted tick must not spend"

            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            assert stored.get("drift_alerted") is True

        assert len(deliveries) == 1, f"expected 1 alert, got {len(deliveries)}: {deliveries}"
        blob = deliveries[0].lower()
        assert "drift" in blob
        assert "pin" in blob
        assert "host running hermes" in blob
        # The single alert must carry the complete supported remediation
        # command — the generic summarizer's 180-char truncation must not eat it.
        assert "hermes cron edit drift-once-test" in deliveries[0]
        assert "cronjob action=update" not in deliveries[0]
        assert "[drift_skip" not in deliveries[0]

    def test_healed_drift_clears_bit_and_redrift_realerts(self, tmp_path):
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            # Tick 1: drifted -> one alert, bit set.
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            _tick(fresh, tmp_path, "nous", deliveries)
            assert len(deliveries) == 1

            # Tick 2: drift healed (resolution matches snapshot) -> runs, bit cleared.
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            ok, agent_called = _tick(fresh, tmp_path, "openrouter", deliveries)
            assert agent_called is True
            stored = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            assert not stored.get("drift_alerted")

            # Tick 3: drifts again -> re-alerts (not swallowed).
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            _tick(fresh, tmp_path, "nous", deliveries)

        drift_alerts = [d for d in deliveries if "drift" in d.lower()]
        assert len(drift_alerts) == 2, f"expected re-alert after heal: {deliveries}"

    def test_non_drift_failures_untouched_by_the_bit(self, tmp_path):
        """A job with the drift bit set whose run fails for another reason
        still alerts — only the drift branch consults the bit."""
        job = _job(provider_snapshot=None, drift_alerted=True)
        deliveries = []

        def fake_deliver(jb, content, adapters=None, loop=None):
            deliveries.append(content)
            return None

        fake_db = MagicMock()
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = [j for j in cron_jobs.load_jobs() if j["id"] == job["id"]][0]
            with patch("cron.scheduler._hermes_home", tmp_path), \
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
                mock_agent.run_conversation.side_effect = RuntimeError("boom unrelated")
                mock_agent_cls.return_value = mock_agent
                sched.run_one_job(fresh)

        assert len(deliveries) == 1, "non-drift failure must still deliver"
        assert "boom unrelated" in deliveries[0]


class TestDriftSkipLogging:
    """Regression: a drift-skip tick must NOT be logged at ERROR with a
    full traceback. The existing ``TestDriftAlertOnce`` only asserts delivery
    dedup (``len(deliveries) == 1``) and never inspects the log record, so it
    stayed green even though ``run_one_job``'s run-wide ``except`` logged the
    benign sentinel at ERROR + exc_info on *every* tick (#44585 drift guard).

    Contract under test:
    - First (loud ``[drift_skip]``) tick: one WARNING, no traceback.
    - Subsequent (``[drift_skip:silent]``) ticks: no ERROR record at all
      (DEBUG), so errors.log / ERROR-level alerting stays clean.
    - A *genuine* failure (no sentinel) must still ERROR + full traceback,
      so the fix does not hide real breakage.
    """

    def test_drift_ticks_dont_emit_error_with_traceback(self, tmp_path):
        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        cap = _Capture()
        old_level = sched.logger.level
        old_prop = sched.logger.propagate
        sched.logger.setLevel(logging.DEBUG)
        sched.logger.addHandler(cap)
        sched.logger.propagate = False
        all_records = []  # not cleared between ticks — keep tick-1 WARNING
        try:
            with cron_jobs.use_cron_store(tmp_path):
                job = _job()
                cron_jobs.save_jobs([job])
                # Two drifted ticks: 1st raises [drift_skip] (loud), 2nd
                # raises [drift_skip:silent] (already alerted on tick 1).
                deliveries = []
                for _ in range(2):
                    fresh = [
                        j for j in cron_jobs.load_jobs() if j["id"] == job["id"]
                    ][0]
                    cap.records.clear()
                    _tick(fresh, tmp_path, "nous", deliveries)
                    all_records.extend(cap.records)
                error_records = [
                    r for r in all_records
                    if r.levelno >= logging.ERROR and r.exc_info
                ]
        finally:
            sched.logger.removeHandler(cap)
            sched.logger.level = old_level
            sched.logger.propagate = old_prop

        assert not error_records, (
            f"drift-skip tick logged {len(error_records)} ERROR record(s) with "
            f"traceback — expected 0 (already-alerted silent ticks must stay "
            f"quiet). Got: {[r.getMessage() for r in error_records]}"
        )
        # Sanity: a WARNING *was* emitted (the loud, first-tick alert) and
        # carries no traceback — proving the sentinel is handled, not dropped.
        warning_records = [
            r for r in all_records
            if r.levelname == "WARNING" and "sentinel" in r.getMessage()
        ]
        assert warning_records, (
            "expected a WARNING sentinel record; "
            f"got levels {[r.levelname for r in all_records]}"
        )

    def test_real_failure_still_errors_with_traceback(self, tmp_path):
        """Guard against the fix hiding genuine breakage: a non-sentinel
        RuntimeError must still produce an ERROR record with exc_info."""

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        cap = _Capture()
        old_level = sched.logger.level
        old_prop = sched.logger.propagate
        sched.logger.setLevel(logging.DEBUG)
        sched.logger.addHandler(cap)
        sched.logger.propagate = False
        try:
            with cron_jobs.use_cron_store(tmp_path):
                job = _job()  # provider_snapshot matches current -> drift guard
                # passes; the failure comes from the agent raising.
                job["provider_snapshot"] = "openrouter"
                cron_jobs.save_jobs([job])
                fresh = [
                     j for j in cron_jobs.load_jobs() if j["id"] == job["id"]
                ][0]
                cap.records.clear()

                def _fail_deliver(jb, content, adapters=None, loop=None):
                    return None

                with patch("cron.scheduler._hermes_home", tmp_path), \
                     patch("cron.scheduler._resolve_origin", return_value=None), \
                     patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                     patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                     patch("hermes_state.SessionDB", return_value=MagicMock()), \
                     patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
                     patch(
                        "hermes_cli.runtime_provider.resolve_runtime_provider",
                        return_value={
                            "api_key": "test-key",
                            "base_url": "https://example.invalid/v1",
                            "provider": "openrouter",
                            "api_mode": "chat_completions",
                        },
                     ), \
                     patch.object(
                        sched, "_deliver_result", side_effect=_fail_deliver
                     ), \
                     patch("run_agent.AIAgent") as mock_agent_cls:
                    mock_agent = MagicMock()
                    mock_agent.run_conversation.side_effect = RuntimeError(
                        "genuine boom — not a sentinel"
                    )
                    mock_agent_cls.return_value = mock_agent
                    sched.run_one_job(fresh)
                error_records = [
                    r for r in cap.records
                    if r.levelno >= logging.ERROR and r.exc_info
                ]
        finally:
            sched.logger.removeHandler(cap)
            sched.logger.level = old_level
            sched.logger.propagate = old_prop

        assert len(error_records) == 1, (
            "genuine failure must still log exactly one ERROR record with a "
            f"traceback; got {len(error_records)}: "
            f"{[r.getMessage() for r in error_records]}"
        )
        assert "genuine boom" in error_records[0].getMessage()
        assert error_records[0].exc_info is not None
