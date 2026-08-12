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

Note: each test releases its ``_WedgedSessionDb.release`` event in a ``finally`` before
returning. concurrent.futures.thread registers an atexit hook that joins
EVERY worker thread ever created by ANY ThreadPoolExecutor in the process
regardless of ``shutdown(wait=False)`` — an event left permanently unset
would hang the whole test process at interpreter exit, not just this test.
"""

import os
import shutil
import tempfile
import threading
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

    ``run_job`` resolves a pile of things lazily on its first call
    in a process — profile context, cron activity policy, config load, and the
    agent/jsonschema import chain the ``patch`` headers have not already pulled
    in. That cost used to land inside a ``time.monotonic()`` bracket the tests
    asserted on; it no longer decides any verdict (see ``_WedgedSessionDb``),
    but it can still blow the per-test cap outright and kill the file.

    Measured 2026-08-11 under the nightly gate's argv: cold, the first test's
    ``run_job`` call was 27.8s; with a second suite competing for the box (the
    ordinary state of a 24-worker gate lane) that cost blew the 60s per-test
    cap outright and pytest-timeout's thread method killed the process, so the
    whole file reported as "no tests ran". Collection is not covered by the
    per-test timeout, so paying it here puts it somewhere it cannot kill the
    file. Warms the TIMED-OUT branch specifically, since that is the path the
    tests exercise. Best-effort: a failure must not change what any test
    asserts.
    """
    home = Path(tempfile.mkdtemp(prefix="warm-run-job-"))
    wedge = _WedgedSessionDb()
    try:
        # Warm the TIMED-OUT branch specifically: a run that gets a SessionDB
        # back exercises different code than one whose init is cut short, and
        # only the latter is what the tests below measure.
        with patch.dict(os.environ, {"HERMES_CRON_SESSION_DB_TIMEOUT": "0.2"}), \
             patch("cron.scheduler._hermes_home", home), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", side_effect=wedge), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_PROVIDER), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
            run_job({"id": "warm-run-job", "name": "warm", "prompt": "hello"})
    except Exception:
        pass
    finally:
        wedge.release.set()
        shutil.rmtree(home, ignore_errors=True)


# Upper bound on the stand-in's block, so an unreleased event can never hang the
# test process at interpreter exit (see the module docstring). Nothing asserts
# on it: a passing run releases the wedge in a ``finally`` long before this.
_HANG_SECONDS = 30.0


class _WedgedSessionDb:
    """Stand-in for ``hermes_state.SessionDB()`` that blocks until released.

    Like the real incident's wedged ``sqlite3.connect``, but bounded so the
    test process can still exit cleanly once the assertions are done.

    ``returned`` is the load-independent evidence these tests run on. It is set
    ONLY if this call ran to completion — i.e. if ``run_job`` sat and waited the
    wedge out. If ``run_job``'s own timeout did its job, ``run_job`` returns
    while this call is still parked inside ``wait()`` and ``returned`` is still
    clear.

    This replaces a wall-clock assertion (``elapsed < N``) that repeatedly
    failed on correct code. ``run_job`` does a lot besides construct a
    SessionDB, all of it inside the measured region, and that cost scales with
    machine load: 6.83s standalone on 2026-08-11 against a 5.0s budget, and
    18.39s in the shared checkout with a sibling suite running — while the
    third test in this file, identical shape and identical 0.2s bound but
    running warm on an idle box, measured 1.8s. Every one of those runs had a
    working timeout. A stopwatch cannot separate "the wedge was abandoned" from
    "the machinery was slow"; this flag can, exactly, at any load.
    """

    def __init__(self):
        self.release = threading.Event()
        self.returned = threading.Event()

    def __call__(self):
        self.release.wait(timeout=_HANG_SECONDS)
        self.returned.set()
        return MagicMock()


_warm_run_job()


class TestSessionDbInitTimeout:
    def test_run_job_does_not_hang_when_sessiondb_init_wedges(self, tmp_path, monkeypatch):
        """run_job returns promptly even if SessionDB() never returns."""
        monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0.2")
        wedge = _WedgedSessionDb()
        job = {"id": "wedged-sessiondb", "name": "test", "prompt": "hello"}

        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state.SessionDB", side_effect=wedge), \
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

                success, output, final_response, error = run_job(job)
                # Sample BEFORE the finally releases the wedge.
                abandoned = not wedge.returned.is_set()
        finally:
            wedge.release.set()

        # run_job gave up on the init instead of waiting it out: the stand-in
        # was still parked inside its wait() when run_job returned.
        assert abandoned, (
            "run_job waited out the wedged SessionDB init instead of "
            "abandoning it at the 0.2s HERMES_CRON_SESSION_DB_TIMEOUT"
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
        wedge = _WedgedSessionDb()
        job = {"id": "config-timeout", "name": "test", "prompt": "hello"}

        try:
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state.SessionDB", side_effect=wedge), \
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

                success, output, final_response, error = run_job(job)
                # Sample BEFORE the finally releases the wedge.
                abandoned = not wedge.returned.is_set()
        finally:
            wedge.release.set()

        # Config value 0.2s bounded the init, not the 10s default: run_job
        # returned while the stand-in was still parked in its wait().
        assert abandoned, (
            "run_job waited out the wedged SessionDB init instead of "
            "abandoning it at cron.session_db_timeout_seconds=0.2"
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

        wedge = _WedgedSessionDb()
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
                 patch("hermes_state.SessionDB", side_effect=wedge), \
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
            wedge.release.set()
            sched._running_job_ids.discard("guard-sessiondb-hang")
            sched._shutdown_parallel_pool()
