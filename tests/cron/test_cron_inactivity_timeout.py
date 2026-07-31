"""Tests for cron job inactivity-based timeout.

Tests cover:
- Active agent runs indefinitely (no inactivity timeout)
- Idle agent triggers inactivity timeout with diagnostic info
- Unlimited timeout (HERMES_CRON_TIMEOUT=0)
- Backward compat: HERMES_CRON_TIMEOUT env var still works
- Error message includes activity summary
"""

import concurrent.futures
import contextvars
import itertools
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class FakeAgent:
    """Mock agent with controllable activity summary for timeout tests."""

    def __init__(self, idle_seconds=0.0, activity_desc="tool_call",
                 current_tool=None, api_call_count=5, max_iterations=90):
        self._idle_seconds = idle_seconds
        self._activity_desc = activity_desc
        self._current_tool = current_tool
        self._api_call_count = api_call_count
        self._max_iterations = max_iterations
        self._interrupted = False
        self._interrupt_msg = None

    def get_activity_summary(self):
        return {
            "last_activity_ts": time.time() - self._idle_seconds,
            "last_activity_desc": self._activity_desc,
            "seconds_since_activity": self._idle_seconds,
            "current_tool": self._current_tool,
            "api_call_count": self._api_call_count,
            "max_iterations": self._max_iterations,
        }

    def interrupt(self, msg):
        self._interrupted = True
        self._interrupt_msg = msg

    def run_conversation(self, prompt):
        """Simulate a quick agent run that finishes immediately."""
        return {"final_response": "Done", "messages": []}


class SlowFakeAgent(FakeAgent):
    """Agent that runs for a while, simulating active work then going idle."""

    def __init__(self, run_duration=0.5, idle_after=None, **kwargs):
        super().__init__(**kwargs)
        self._run_duration = run_duration
        self._idle_after = idle_after  # seconds before becoming idle
        self._start_time = None

    def get_activity_summary(self):
        summary = super().get_activity_summary()
        if self._idle_after is not None and self._start_time:
            elapsed = time.time() - self._start_time
            if elapsed > self._idle_after:
                # Agent has gone idle
                idle_time = elapsed - self._idle_after
                summary["seconds_since_activity"] = idle_time
                summary["last_activity_desc"] = "api_call_streaming"
            else:
                summary["seconds_since_activity"] = 0.0
        return summary

    def run_conversation(self, prompt):
        self._start_time = time.time()
        time.sleep(self._run_duration)
        return {"final_response": "Completed after work", "messages": []}


class TestInactivityTimeout:
    """Test the inactivity-based timeout polling loop in cron scheduler."""

    def test_active_agent_completes_normally(self):
        """An agent that finishes quickly should return its result."""
        agent = FakeAgent(idle_seconds=0.0)
        _cron_inactivity_limit = 10.0
        _POLL_INTERVAL = 0.1

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test prompt")
        _inactivity_timeout = False

        result = None
        while True:
            done, _ = concurrent.futures.wait({future}, timeout=_POLL_INTERVAL)
            if done:
                result = future.result()
                break
            _idle_secs = 0.0
            if hasattr(agent, "get_activity_summary"):
                _act = agent.get_activity_summary()
                _idle_secs = _act.get("seconds_since_activity", 0.0)
            if _idle_secs >= _cron_inactivity_limit:
                _inactivity_timeout = True
                break

        pool.shutdown(wait=False)
        assert result is not None
        assert result["final_response"] == "Done"
        assert not _inactivity_timeout
        assert not agent._interrupted

    def test_idle_agent_triggers_timeout(self):
        """An agent that goes idle should be detected and interrupted."""
        # Agent will run for 0.3s, then become idle after 0.1s of that
        agent = SlowFakeAgent(
            run_duration=5.0,  # would run forever without timeout
            idle_after=0.1,    # goes idle almost immediately
            activity_desc="api_call_streaming",
            current_tool="web_search",
            api_call_count=3,
            max_iterations=50,
        )

        _cron_inactivity_limit = 0.5  # 0.5s inactivity triggers timeout
        _POLL_INTERVAL = 0.1

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test prompt")
        _inactivity_timeout = False

        result = None
        while True:
            done, _ = concurrent.futures.wait({future}, timeout=_POLL_INTERVAL)
            if done:
                result = future.result()
                break
            _idle_secs = 0.0
            if hasattr(agent, "get_activity_summary"):
                try:
                    _act = agent.get_activity_summary()
                    _idle_secs = _act.get("seconds_since_activity", 0.0)
                except Exception:
                    pass
            if _idle_secs >= _cron_inactivity_limit:
                _inactivity_timeout = True
                break

        pool.shutdown(wait=False, cancel_futures=True)
        assert _inactivity_timeout is True
        assert result is None  # Never got a result — interrupted

    def test_unlimited_timeout(self):
        """HERMES_CRON_TIMEOUT=0 means no timeout at all."""
        agent = FakeAgent(idle_seconds=0.0)
        _cron_inactivity_limit = None  # unlimited

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test prompt")

        # With unlimited, we just await the result directly.
        result = future.result()
        pool.shutdown(wait=False)

        assert result["final_response"] == "Done"

    def _parse_cron_timeout(self, raw_value):
        """Mirror the defensive parsing logic from cron/scheduler.py run_job()."""
        if raw_value:
            try:
                return float(raw_value)
            except (ValueError, TypeError):
                return 600.0
        return 600.0

    def test_timeout_env_var_parsing(self, monkeypatch):
        """HERMES_CRON_TIMEOUT env var is respected."""
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "1200")
        raw = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
        _cron_timeout = self._parse_cron_timeout(raw)
        assert _cron_timeout == 1200.0

        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        assert _cron_inactivity_limit == 1200.0


    def test_agent_without_activity_summary_uses_wallclock_fallback(self):
        """If agent lacks get_activity_summary, idle_secs stays 0 (never times out).
        
        This ensures backward compat if somehow an old agent is used.
        The polling loop will eventually complete when the task finishes.
        """
        class BareAgent:
            def run_conversation(self, prompt):
                return {"final_response": "no activity tracker", "messages": []}

        agent = BareAgent()
        _cron_inactivity_limit = 0.1  # tiny limit
        _POLL_INTERVAL = 0.1

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.run_conversation, "test")
        _inactivity_timeout = False

        while True:
            done, _ = concurrent.futures.wait({future}, timeout=_POLL_INTERVAL)
            if done:
                result = future.result()
                break
            _idle_secs = 0.0
            if hasattr(agent, "get_activity_summary"):
                try:
                    _act = agent.get_activity_summary()
                    _idle_secs = _act.get("seconds_since_activity", 0.0)
                except Exception:
                    pass
            if _idle_secs >= _cron_inactivity_limit:
                _inactivity_timeout = True
                break

        pool.shutdown(wait=False)
        # Should NOT have timed out — bare agent has no get_activity_summary
        assert not _inactivity_timeout
        assert result["final_response"] == "no activity tracker"

    def test_timeout_keeps_terminal_lifecycle_open_until_nested_worker_exits(self, tmp_path, monkeypatch):
        """A timed-out agent stays admitted until its submitted worker exits."""
        import cron.scheduler as scheduler

        started = threading.Event()
        interrupted = threading.Event()
        release = threading.Event()
        terminalized = threading.Event()
        terminal = []
        mark_calls = []
        fire_heartbeats = []
        profile_seen = []
        from hermes_constants import (
            get_hermes_home_override,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        profile_home = tmp_path / "profile"
        profile_token = set_hermes_home_override(profile_home)

        class EventControlledAgent:
            def get_activity_summary(self):
                return {
                    "last_activity_desc": "tool_call",
                    "seconds_since_activity": 1,
                    "api_call_count": 1,
                    "max_iterations": 90,
                }

            def interrupt(self, _message):
                interrupted.set()

            def run_conversation(self, _prompt):
                profile_seen.append(get_hermes_home_override())
                started.set()
                assert release.wait(timeout=5)
                return {"final_response": "late success", "messages": []}

            def close(self):
                terminal.append("teardown")

        def immediate_poll(futures, timeout):
            done = {future for future in futures if future.done()}
            return done, set(futures) - done

        agent = EventControlledAgent()
        fake_db = MagicMock()
        fake_db.close.side_effect = lambda: terminal.append("db-close")
        monkeypatch.setattr(scheduler.concurrent.futures, "wait", immediate_poll)
        clock = itertools.count(start=0, step=61)
        monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0.1")
        monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
        monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
        monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: terminal.append("save"))
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: terminal.append("deliver"))
        def mark(*args, **kwargs):
            mark_calls.append((args, kwargs))
            terminal.append("mark")
            terminalized.set()

        monkeypatch.setattr(scheduler, "mark_job_run", mark)
        monkeypatch.setattr(scheduler, "finish_execution", lambda *_args, **_kwargs: terminal.append("finish"))
        monkeypatch.setattr(
            scheduler,
            "heartbeat_fire_claim",
            lambda job_id, *, expected_incarnation: fire_heartbeats.append((job_id, expected_incarnation)) or True,
        )

        result = []
        run_context = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: run_context.run(
                lambda: result.append(
                    scheduler.run_one_job({
                        "id": "timeout-job",
                        "name": "timeout",
                        "prompt": "go",
                        "execution_id": "e1",
                        "fire_claim": {"incarnation": "fire-incarnation"},
                    })
                )
            ),
        )
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
                 "api_key": "test-key",
                 "base_url": "https://example.invalid/v1",
                 "provider": "openrouter",
                 "api_mode": "chat_completions",
             }), \
             patch("run_agent.AIAgent", return_value=agent):
            thread.start()
            assert started.wait(timeout=5)
            assert interrupted.wait(timeout=5)
            try:
                assert not terminalized.wait(timeout=0.2)
                assert thread.is_alive()
                assert fire_heartbeats
            finally:
                release.set()
            thread.join(timeout=5)
        reset_hermes_home_override(profile_token)

        assert not thread.is_alive()
        assert result == [True]
        assert terminal == ["db-close", "save", "deliver", "teardown", "mark", "finish"]
        assert len(mark_calls) == 1
        assert mark_calls[0][0][1] is False
        assert "idle for" in mark_calls[0][0][2]
        assert profile_seen == [str(profile_home)]
        assert all(heartbeat == ("timeout-job", "fire-incarnation") for heartbeat in fire_heartbeats)


class TestSysPathOrdering:
    """Test that sys.path is set before repo-level imports."""

    def test_hermes_time_importable(self):
        """hermes_time should be importable when cron.scheduler loads."""
        # This import would fail if sys.path.insert comes after the import
        from cron.scheduler import _hermes_now
        assert callable(_hermes_now)

