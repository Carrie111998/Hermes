"""Regression test for a hung SessionDB() init permanently wedging a cron job.

Real-world incident: a cron job's ``SessionDB()`` construction inside
``run_job`` blocked forever (a wedged sqlite3.connect against state.db, no
other process holding a competing lock by the time it was diagnosed). Because
that call had no timeout of its own — unlike the agent's run_conversation,
which is already bounded by HERMES_CRON_TIMEOUT — the worker thread submitted
by ``_submit_with_guard`` never returned. Its ``finally`` block, which is the
only thing that discards the job ID from ``_running_job_ids``, never ran.
Every later tick logged "already running — skipping" and the job never fired
again until the whole gateway process was restarted days later.

These tests prove ``run_job`` now bounds the SessionDB init with its own
timeout (HERMES_CRON_SESSION_DB_TIMEOUT, default 10s) so a hang there can
never again wedge the job past that bound, and — end to end — that the
dispatch guard is released and the job becomes dispatchable again afterward.

Note: each test releases its ``never_set`` event in a ``finally`` before
returning. concurrent.futures.thread registers an atexit hook that joins
EVERY worker thread ever created by ANY ThreadPoolExecutor in the process
regardless of ``shutdown(wait=False)`` — an event left permanently unset
would hang the whole test process at interpreter exit, not just this test.
"""

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import run_job


_PROVIDER = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}

def _warm_run_job() -> None:
    """Pay ``run_job``'s one-time cost HERE, at collection.

    The elapsed-time assertions below bound the *hang*, not the *machinery*:
    they exist to prove HERMES_CRON_SESSION_DB_TIMEOUT cut a SessionDB init
    short. But ``run_job`` resolves a pile of things lazily on its first call
    in a process — profile context, cron activity policy, config load, and the
    agent/jsonschema import chain the ``patch`` headers have not already pulled
    in — and that one-time cost lands inside the ``time.monotonic()`` bracket
    of whichever test runs first.

    Measured 2026-08-11 running this file standalone under the nightly gate's
    argv: the first timed test saw ``elapsed`` = 6.83s against its 5.0s budget
    and FAILED, while ``test_timeout_resolved_from_config_yaml`` — same shape,
    same 0.2s bound, but third in the file — measured 1.8s and passed. The
    0.2s timeout was working correctly in both; only the first caller was
    charged for the warm-up. Re-run with a second suite competing for the box
    (the ordinary state of a 24-worker gate lane) and the same cost blew the
    60s per-test cap outright, killing the process mid-import.

    So warm at collection rather than loosening the budget: a budget wide
    enough to absorb a cold ``run_job`` would also absorb a real regression,
    and collection is not covered by the per-test timeout, so the cost sits
    somewhere it cannot kill the file. The SessionDB stand-in returns
    immediately, so nothing here is itself timed. Best-effort: a failure must
    not change what any test asserts.
    """
    home = Path(tempfile.mkdtemp(prefix="warm-run-job-"))
    released = threading.Event()
    try:
        # Warm the TIMED-OUT branch specifically: a run that gets a SessionDB
        # back exercises different code than one whose init is cut short, and
        # only the latter is what the tests below measure.
        with patch.dict(os.environ, {"HERMES_CRON_SESSION_DB_TIMEOUT": "0.2"}), \
             patch("cron.scheduler._hermes_home", home), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", side_effect=lambda: _hanging_session_db(released)), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_PROVIDER), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
            run_job({"id": "warm-run-job", "name": "warm", "prompt": "hello"})
    except Exception:
        pass
    finally:
        released.set()
        shutil.rmtree(home, ignore_errors=True)


# How long the stand-in SessionDB wedges for. This is the number the elapsed
# assertions are really about: with the fix, run_job returns after its own
# (0.2s) timeout; without it, run_job cannot return in less than _HANG_SECONDS.
_HANG_SECONDS = 30.0

# Budget for the timed runs. Deliberately expressed as a fraction of the hang
# rather than as a tight absolute: what the tests falsify is "run_job waited
# out the wedge", and anything comfortably below _HANG_SECONDS does that.
#
# The original 5.0s was tight enough to be measuring the wrong thing. run_job's
# first call in a process carries one-time cost that the assertion charges to
# the hang — 6.83s standalone on 2026-08-11 (a FAIL), while the identically
# shaped third test in this file, running warm, measured 1.8s and passed. The
# collection-time warm above removes most of that (27.8s call -> 5.4s), but the
# residue still scales with machine load, and the nightly gate runs 24 workers.
# Widening a budget that a REAL regression would blow past by 6x costs nothing;
# keeping it tight costs a red gate every time the box is busy.
_ELAPSED_BUDGET = _HANG_SECONDS / 2


def _hanging_session_db(never_set: threading.Event):
    """Stand-in for hermes_state.SessionDB() that blocks until released —
    like the real incident's wedged sqlite3.connect, but bounded so the test
    process can still exit cleanly once the assertions are done."""
    never_set.wait(timeout=_HANG_SECONDS)
    return MagicMock()


_warm_run_job()


class TestSessionDbInitTimeout:
    def test_run_job_does_not_hang_when_sessiondb_init_wedges(self, tmp_path, monkeypatch):
        """run_job returns promptly even if SessionDB() never returns."""
        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
        never_set = threading.Event()
        job = {"id": "wedged-sessiondb", "name": "test", "prompt": "hello"}

        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state.SessionDB", side_effect=lambda: _hanging_session_db(never_set)), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value={
                         "api_key": "test-key",
                         "base_url": "https://example.invalid/v1",
                         "provider": "openrouter",
                         "api_mode": "chat_completions",
                     },
                 ), \
                 patch("run_agent.AIAgent") as mock_agent_cls:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent_cls.return_value = mock_agent

                start = time.monotonic()
                success, output, final_response, error = run_job(job)
                elapsed = time.monotonic() - start
        finally:
            never_set.set()

        # Bounded by the 0.2s timeout, not by the hang (which never resolves
        # on its own within the test).
        assert elapsed < _ELAPSED_BUDGET, (
            f"run_job took {elapsed:.2f}s; the 0.2s SessionDB timeout should "
            f"have cut the {_HANG_SECONDS:.0f}s wedge short"
        )
        # The run still completes successfully without a session store.
        assert success is True
        assert final_response == "ok"
        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["session_db"] is None

    def test_invalid_timeout_env_falls_back_to_default(self, tmp_path, monkeypatch, caplog):
        """A malformed HERMES_CRON_SESSION_DB_TIMEOUT logs a warning and still
        bounds the call (mirrors HERMES_CRON_TIMEOUT's own fallback)."""
        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "not-a-number")
        fake_db = MagicMock()
        job = {"id": "bad-timeout-env", "name": "test", "prompt": "hello"}

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            with caplog.at_level("WARNING"):
                success, output, final_response, error = run_job(job)

        assert success is True
        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["session_db"] is fake_db  # default 10s was plenty for a MagicMock
        # The malformed env var must produce a warning so the misconfiguration
        # is observable — otherwise it silently falls back and operators can't
        # diagnose why their custom timeout isn't taking effect.
        assert any(
            "HERMES_CRON_SESSION_DB_TIMEOUT" in rec.message
            for rec in caplog.records
        ), f"Expected warning about invalid timeout env var; got: {[r.message for r in caplog.records]}"

    def test_timeout_resolved_from_config_yaml(self, tmp_path, monkeypatch):
        """cron.session_db_timeout_seconds in config.yaml is respected when
        the env var is not set — the canonical config-first resolution path."""
        import yaml

        monkeypatch.delenv("HERMES_CRON_SESSION_DB_TIMEOUT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"cron": {"session_db_timeout_seconds": 0.2}})
        )
        never_set = threading.Event()
        job = {"id": "config-timeout", "name": "test", "prompt": "hello"}

        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state.SessionDB", side_effect=lambda: _hanging_session_db(never_set)), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value={
                         "api_key": "test-key",
                         "base_url": "https://example.invalid/v1",
                         "provider": "openrouter",
                         "api_mode": "chat_completions",
                     },
                 ), \
                 patch("run_agent.AIAgent") as mock_agent_cls:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent_cls.return_value = mock_agent

                start = time.monotonic()
                success, output, final_response, error = run_job(job)
                elapsed = time.monotonic() - start
        finally:
            never_set.set()

        # Config value 0.2s bounds the hang, not the 10s default.
        assert elapsed < _ELAPSED_BUDGET, (
            f"run_job took {elapsed:.2f}s; cron.session_db_timeout_seconds=0.2 "
            f"should have cut the {_HANG_SECONDS:.0f}s wedge short"
        )
        assert success is True
        assert mock_agent_cls.call_args.kwargs["session_db"] is None


class TestDispatchGuardReleasedAfterHang:
    """End-to-end: the real bug symptom was every later tick silently
    skipping the job forever. Confirm the fix actually clears that path."""

    def test_guard_is_released_and_job_refires_after_sessiondb_hang(self, tmp_path, monkeypatch):
        import cron.scheduler as sched

        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        never_set = threading.Event()
        job = {
            "id": "guard-sessiondb-hang",
            "name": "guard-sessiondb-hang",
            "prompt": "hello",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state.SessionDB", side_effect=lambda: _hanging_session_db(never_set)), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value={
                         "api_key": "test-key",
                         "base_url": "https://example.invalid/v1",
                         "provider": "openrouter",
                         "api_mode": "chat_completions",
                     },
                 ), \
                 patch("run_agent.AIAgent") as mock_agent_cls, \
                 patch.object(sched, "get_due_and_skipped_jobs", return_value=([job], [])), \
                 patch.object(sched, "advance_next_run"), \
                 patch.object(sched, "save_job_output", return_value="/tmp/out"), \
                 patch.object(sched, "mark_job_run"), \
                 patch.object(sched, "_deliver_result", return_value=None):
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "ok"}
                mock_agent_cls.return_value = mock_agent

                n = sched.tick(verbose=False)  # sync=True by default: waits for the job
                assert n == 1

                # Without the fix this would still contain the job ID forever.
                assert "guard-sessiondb-hang" not in sched.get_running_job_ids()

                # A second tick can dispatch the same job again — before the
                # fix this would log "already running — skipping" and
                # return 0.
                n2 = sched.tick(verbose=False)
                assert n2 == 1
        finally:
            never_set.set()
            sched._running_job_ids.discard("guard-sessiondb-hang")
            sched._shutdown_parallel_pool()
