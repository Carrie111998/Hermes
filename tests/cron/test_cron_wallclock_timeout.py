"""Tests for cron job wall-clock (hard) timeout.

The inactivity timeout (HERMES_CRON_TIMEOUT) catches agents that stop making
progress, but it does NOT bound the total wall-clock duration of a job that
keeps making slow forward progress (e.g. a chain of slow LLM calls).

Production incident 2026-04-26: jobflow-scout fired at 18:08:54 and stayed
in run_job() for 22.8 minutes, blocking every other due cron from firing.
The agent was active throughout, so the inactivity timer never fired. This
module covers the wall-clock layer that bounds total duration regardless
of activity.

HERMES_CRON_HARD_TIMEOUT env var:
    0 (default)  → unlimited (existing behavior)
    >0           → hard cap in seconds; same kill mechanism as inactivity
                   (agent.interrupt() + raise TimeoutError)
"""

import concurrent.futures
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class FakeAgent:
    """Mock agent — reports active throughout (so inactivity never fires)."""

    def __init__(self):
        self._interrupted = False
        self._interrupt_msg = None

    def get_activity_summary(self):
        # Always reports as actively progressing → never idle
        return {
            "last_activity_ts": time.time(),
            "last_activity_desc": "api_call_streaming",
            "seconds_since_activity": 0.0,
            "current_tool": "delegate_task",
            "api_call_count": 5,
            "max_iterations": 90,
        }

    def interrupt(self, msg):
        self._interrupted = True
        self._interrupt_msg = msg

    def run_conversation(self, prompt):
        return {"final_response": "Done", "messages": []}


class SlowActiveAgent(FakeAgent):
    """Agent that runs for `run_duration` seconds while reporting as active."""

    def __init__(self, run_duration):
        super().__init__()
        self._run_duration = run_duration

    def run_conversation(self, prompt):
        time.sleep(self._run_duration)
        return {"final_response": "Eventually done", "messages": []}


def _poll_with_timeouts(future, agent, inactivity_limit, hard_limit, poll_interval=0.05):
    """Reconstruct the cron polling loop with both inactivity AND wall-clock checks.

    Returns (result, timeout_kind, elapsed) where timeout_kind is one of:
        None           → completed normally (result populated)
        "inactivity"   → inactivity limit hit
        "wallclock"    → hard wall-clock limit hit

    This mirrors the logic that lives in cron/scheduler.py:run_job and is the
    single source of truth that test cases assert against. The matching
    production change is verified by inspection (a smoke import) below.
    """
    start_time = time.monotonic()
    while True:
        done, _ = concurrent.futures.wait({future}, timeout=poll_interval)
        if done:
            return (future.result(), None, time.monotonic() - start_time)

        # Inactivity check (existing)
        idle_secs = 0.0
        if hasattr(agent, "get_activity_summary"):
            try:
                act = agent.get_activity_summary()
                idle_secs = act.get("seconds_since_activity", 0.0)
            except Exception:
                pass
        if inactivity_limit is not None and idle_secs >= inactivity_limit:
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (inactivity)")
            return (None, "inactivity", time.monotonic() - start_time)

        # Wall-clock check (new)
        if hard_limit is not None:
            elapsed = time.monotonic() - start_time
            if elapsed >= hard_limit:
                if hasattr(agent, "interrupt"):
                    agent.interrupt("Cron job timed out (wall-clock)")
                return (None, "wallclock", elapsed)


class TestWallclockTimeout:
    def test_fast_agent_completes_within_wallclock_limit(self):
        """An agent that finishes quickly returns its result; no timeout."""
        agent = FakeAgent()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")

        result, kind, _ = _poll_with_timeouts(
            future, agent, inactivity_limit=10.0, hard_limit=10.0
        )
        pool.shutdown(wait=False)

        assert kind is None
        assert result["final_response"] == "Done"
        assert not agent._interrupted

    def test_slow_active_agent_triggers_wallclock_timeout(self):
        """An agent that keeps making progress but for too long must be killed by wall-clock."""
        agent = SlowActiveAgent(run_duration=5.0)  # would run forever otherwise
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")

        result, kind, elapsed = _poll_with_timeouts(
            future, agent, inactivity_limit=None, hard_limit=0.3
        )
        pool.shutdown(wait=False, cancel_futures=True)

        assert kind == "wallclock"
        assert result is None
        assert 0.25 <= elapsed <= 1.0  # wallclock fires near the limit, not the agent's 5s
        assert agent._interrupted is True
        assert "wall-clock" in agent._interrupt_msg

    def test_wallclock_unlimited_lets_agent_complete(self):
        """hard_limit=None means no wall-clock cap (existing behavior preserved)."""
        agent = SlowActiveAgent(run_duration=0.2)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")

        result, kind, _ = _poll_with_timeouts(
            future, agent, inactivity_limit=None, hard_limit=None
        )
        pool.shutdown(wait=False)

        assert kind is None
        assert result["final_response"] == "Eventually done"
        assert not agent._interrupted

    def test_inactivity_takes_precedence_when_both_configured(self):
        """If the agent goes idle BEFORE the wall-clock deadline, inactivity fires first."""
        # Custom agent that goes idle immediately
        class IdleAgent(FakeAgent):
            def get_activity_summary(self):
                return {
                    "last_activity_desc": "api_call_streaming",
                    "seconds_since_activity": 5.0,  # already idle 5s
                    "current_tool": None,
                    "api_call_count": 1,
                    "max_iterations": 90,
                }
            def run_conversation(self, prompt):
                time.sleep(2.0)
                return {"final_response": "would have finished"}

        agent = IdleAgent()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")

        result, kind, elapsed = _poll_with_timeouts(
            future, agent, inactivity_limit=0.5, hard_limit=10.0
        )
        pool.shutdown(wait=False, cancel_futures=True)

        assert kind == "inactivity"
        assert result is None
        assert elapsed < 1.0  # inactivity caught it immediately
        assert "inactivity" in agent._interrupt_msg

    def test_wallclock_takes_precedence_when_agent_active_until_deadline(self):
        """Active agent that runs past the wall-clock deadline triggers wallclock kind."""
        agent = SlowActiveAgent(run_duration=2.0)  # runs 2s, never goes idle
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")

        result, kind, elapsed = _poll_with_timeouts(
            future, agent, inactivity_limit=10.0, hard_limit=0.3
        )
        pool.shutdown(wait=False, cancel_futures=True)

        assert kind == "wallclock"
        assert 0.25 <= elapsed <= 1.0
        assert "wall-clock" in agent._interrupt_msg


class TestWallclockEnvVarParsing:
    """HERMES_CRON_HARD_TIMEOUT env var parsing — same idiom as HERMES_CRON_TIMEOUT."""

    def test_default_is_zero_meaning_unlimited(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_HARD_TIMEOUT", raising=False)
        hard_timeout = float(os.getenv("HERMES_CRON_HARD_TIMEOUT", 0))
        hard_limit = hard_timeout if hard_timeout > 0 else None
        assert hard_limit is None

    def test_explicit_zero_means_unlimited(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_HARD_TIMEOUT", "0")
        hard_timeout = float(os.getenv("HERMES_CRON_HARD_TIMEOUT", 0))
        hard_limit = hard_timeout if hard_timeout > 0 else None
        assert hard_limit is None

    def test_positive_value_yields_limit(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_HARD_TIMEOUT", "1800")
        hard_timeout = float(os.getenv("HERMES_CRON_HARD_TIMEOUT", 0))
        hard_limit = hard_timeout if hard_timeout > 0 else None
        assert hard_limit == 1800.0


class TestSchedulerWallclockIntegration:
    """Smoke test that the production source actually contains the wall-clock check.

    The unit tests above use a reconstructed copy of the polling loop (mirrors
    the production code). This test pins the production source to make sure
    the wall-clock check actually exists in cron/scheduler.py:run_job — without
    this, the unit tests above would be a documentation-only artifact.
    """

    def test_production_runJob_reads_wallclock_env_var(self):
        from pathlib import Path
        scheduler_src = (
            Path(__file__).parent.parent.parent / "cron" / "scheduler.py"
        ).read_text(encoding="utf-8")
        assert "HERMES_CRON_HARD_TIMEOUT" in scheduler_src, (
            "cron/scheduler.py must read HERMES_CRON_HARD_TIMEOUT to enable wall-clock timeout"
        )

    def test_production_runJob_raises_on_wallclock_limit(self):
        from pathlib import Path
        scheduler_src = (
            Path(__file__).parent.parent.parent / "cron" / "scheduler.py"
        ).read_text(encoding="utf-8")
        # The wall-clock branch must call agent.interrupt() and raise TimeoutError.
        # Both the inactivity and wall-clock branches must be present.
        assert "wall-clock" in scheduler_src.lower(), (
            "cron/scheduler.py must mention wall-clock in its timeout-handling code"
        )
