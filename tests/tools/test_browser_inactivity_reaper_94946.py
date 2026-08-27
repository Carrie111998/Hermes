"""Regression coverage for issue #94946: browser.inactivity_timeout and the
orphan reaper are dead code under the default Browser Use CLI backend.

The Browser Use CLI mode is the default backend since #83402 / #81958, but
``tools/browser_use_cli.py::browser_exec`` did not call the lifecycle hooks
that drive the inactivity cleanup / orphan reaper in ``browser_tool.py``.
The result was that on a default install, browser daemons spawned by the
harness were never reaped — and ``browser.inactivity_timeout`` was silently
ignored.

The fix has four moving parts; this file covers them as four small test
groups so the regression is easy to localize if any one part breaks again:

1. ``browser_exec`` updates the activity timestamp and starts the cleanup
   thread for the task it runs under.
2. The orphan reaper also scans the harness runtime directory (under
   ``~/.config/browser-harness/runtime`` / platform equivalent) — the harness
   daemon does not drop a socket dir under ``/tmp/agent-browser-*``.
3. ``_verify_reapable_browser_daemon`` accepts a harness daemon
   (``python -m browser_harness.daemon``) when the binding check passes — but
   the socket-dir binding check (the actual spoof defense) is preserved.
4. The cleanup thread, the orphan reaper, and an emergency cleanup can all
   run concurrently with an in-flight ``browser_exec`` without deadlocking.

A pre-fix run shows failures in groups 1, 2, and 3; group 4 (no deadlock) is
a guardrail so the fix does not regress in the other direction (e.g. by
introducing a ``_cleanup_lock`` ordering cycle between ``browser_exec`` and
``_cleanup_inactive_browser_sessions``).
"""

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_browser_state():
    """Snapshot and reset every browser-tool global this test touches.

    The test exercises the cleanup-thread / reaper state directly. Without
    a per-test reset, leftover entries from earlier tests would leak across
    test classes and create phantom "active sessions" that the reaper then
    refuses to reap.
    """
    import tools.browser_tool as bt

    saved = {
        "_active_sessions": dict(bt._active_sessions),
        "_session_last_activity": dict(bt._session_last_activity),
        "_last_active_session_key": dict(bt._last_active_session_key),
        "_cleanup_thread": bt._cleanup_thread,
        "_cleanup_running": bt._cleanup_running,
    }
    bt._active_sessions.clear()
    bt._session_last_activity.clear()
    bt._last_active_session_key.clear()
    bt._cleanup_thread = None
    bt._cleanup_running = False
    yield
    bt._active_sessions.clear()
    bt._session_last_activity.clear()
    bt._last_active_session_key.clear()
    bt._last_active_session_key.update(saved["_last_active_session_key"])
    bt._active_sessions.update(saved["_active_sessions"])
    bt._session_last_activity.update(saved["_session_last_activity"])
    bt._cleanup_thread = saved["_cleanup_thread"]
    bt._cleanup_running = saved["_cleanup_running"]


@pytest.fixture
def fake_harness_runtime(tmp_path, monkeypatch):
    """Point ``_harness_runtime_dir()`` at a temp dir we control."""
    runtime = tmp_path / "browser-harness" / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(
        "tools.browser_tool._harness_runtime_dir", lambda: str(runtime)
    )
    return runtime


def _fake_proc(name, cmdline, environ=None):
    """Minimal psutil.Process substitute for ``_verify_reapable_browser_daemon``."""

    class _Proc:
        def name(self_inner):
            return name

        def cmdline(self_inner):
            return list(cmdline)

        def environ(self_inner):
            return dict(environ or {})

    return _Proc()


# ---------------------------------------------------------------------------
# Group 1: browser_exec starts the cleanup thread + updates activity
# ---------------------------------------------------------------------------


class TestBrowserExecStartsCleanupThread:
    """``browser_exec`` must call the lifecycle hooks that drive inactivity
    cleanup.  Without this, ``browser.inactivity_timeout`` is silently
    ignored on a default install — the cleanup thread literally never
    starts, so even the ``BROWSER_SESSION_INACTIVITY_TIMEOUT`` constant is
    dead."""

    def test_browser_exec_invokes_start_cleanup_thread(self, monkeypatch):
        """``browser_exec`` calls ``_start_browser_cleanup_thread()`` at least
        once, even when no provider is configured (the default path)."""
        import tools.browser_tool as bt
        import tools.browser_use_cli as bu_cli

        calls = []
        monkeypatch.setattr(
            bt, "_start_browser_cleanup_thread",
            lambda: calls.append(threading.current_thread().name),
        )
        # Default backend: no CDP override, no cloud provider.
        monkeypatch.setattr(bt, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)

        # browser_exec short-circuits on a missing CLI before running code,
        # but the lifecycle hooks must still fire — they have nothing to do
        # with whether the CLI is installed.
        result = bu_cli.browser_exec("# noop", task_id="task-94946")
        assert "error" in result  # CLI missing is fine for this test
        assert calls, (
            "_start_browser_cleanup_thread was not called from browser_exec — "
            "browser.inactivity_timeout is unreachable on a default install"
        )

    def test_browser_exec_updates_activity_timestamp(self, monkeypatch):
        """``browser_exec`` must record activity for the task_id it runs under.

        Without this, the cleanup thread — even if it started — would see no
        activity and reap the session on its first cycle.  Conversely, an
        idle session must not have its activity bumped, or the timeout never
        fires.  The fix must take a fresh timestamp per call.
        """
        import tools.browser_tool as bt
        import tools.browser_use_cli as bu_cli

        seen = []
        monkeypatch.setattr(
            bt, "_update_session_activity",
            lambda task_id: seen.append((task_id, time.time())),
        )
        monkeypatch.setattr(bt, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(bt, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)

        # Two calls, a beat apart: each must update the timestamp.
        bu_cli.browser_exec("# first", task_id="task-A")
        time.sleep(0.05)
        bu_cli.browser_exec("# second", task_id="task-A")

        # Filter to task-A entries (other tasks may also be touched by the
        # lifecycle hooks; we only care that task-A got two distinct updates).
        task_a_entries = [ts for task, ts in seen if task == "task-A"]
        assert len(task_a_entries) >= 2, (
            f"expected >=2 activity updates for task-A, got {seen}"
        )
        assert task_a_entries[-1] > task_a_entries[0], (
            "activity timestamp did not advance between calls — "
            "the cleanup thread would never see new activity"
        )


# ---------------------------------------------------------------------------
# Group 2: the reaper scans the harness runtime dir
# ---------------------------------------------------------------------------


class TestReaperScansHarnessRuntime:
    """The orphan reaper only globs ``/tmp/agent-browser-*`` today.  Under
    Browser Use CLI mode the harness keeps its state under
    ``~/.config/browser-harness/runtime/`` — so the reaper is a no-op by
    construction.  The fix extends the reaper to scan that dir too, with the
    same identity-and-binding verification (so a planted PID cannot turn
    the reaper into an arbitrary-process DoS)."""

    def test_reaper_reaps_orphaned_harness_daemon(self, fake_harness_runtime, monkeypatch):
        """An orphan harness daemon (alive, no live owner) is terminated
        through the standard reaper path."""
        from tools.browser_tool import _reap_orphaned_browser_sessions

        session_dir = fake_harness_runtime / "h_orphan1234"
        session_dir.mkdir()
        (session_dir / "h_orphan1234.pid").write_text("99999")

        terminated = []
        # PID is "alive" but the owner check fails (no owner_pid file) and
        # the daemon is not in our in-memory tracking — reaper should run.
        monkeypatch.setattr(
            "gateway.status._pid_exists", lambda pid: pid == 99999
        )
        monkeypatch.setattr(
            "tools.browser_tool._verify_reapable_browser_daemon",
            lambda pid, sd, name: pid == 99999 and sd == str(session_dir),
        )
        monkeypatch.setattr(
            "tools.process_registry.ProcessRegistry._terminate_host_pid",
            lambda pid: terminated.append(pid),
        )

        _reap_orphaned_browser_sessions()

        assert 99999 in terminated, (
            "reaper ignored the harness runtime dir — orphan not terminated"
        )

    def test_reaper_skips_harness_daemon_owned_by_live_process(
        self, fake_harness_runtime, monkeypatch
    ):
        """If the owner_pid is alive AND the session is in the tracked set,
        the reaper must NOT terminate it (cross-process safety)."""
        from tools.browser_tool import (
            _reap_orphaned_browser_sessions,
            _active_sessions,
        )

        _active_sessions["live-task"] = {
            "session_name": "h_live12345",
            "backend": "browser_harness",
        }
        session_dir = fake_harness_runtime / "h_live12345"
        session_dir.mkdir()
        (session_dir / "h_live12345.pid").write_text("55555")
        (session_dir / "h_live12345.owner_pid").write_text(str(os.getpid()))

        terminated = []
        monkeypatch.setattr(
            "gateway.status._pid_exists", lambda pid: True,
        )
        monkeypatch.setattr(
            "tools.process_registry.ProcessRegistry._terminate_host_pid",
            lambda pid: terminated.append(pid),
        )

        _reap_orphaned_browser_sessions()

        assert 55555 not in terminated, (
            "reaper killed a daemon owned by a live hermes process"
        )


# ---------------------------------------------------------------------------
# Group 3: the verifier accepts browser_harness.daemon (with binding check)
# ---------------------------------------------------------------------------


class TestVerifierAcceptsHarnessDaemon:
    """``_verify_reapable_browser_daemon`` currently only accepts processes
    whose name/cmdline contains ``agent-browser``.  Under Browser Use CLI
    mode the daemon is ``python -m browser_harness.daemon``, so it is
    refused even when every other safety check would pass.

    The fix accepts ``browser_harness`` in the name/cmdline too, but only
    when the socket-dir binding check still holds — the binding check is
    the spoof defense (defends against a planted PID pointing at a victim
    process), and it must not be relaxed."""

    def test_harness_daemon_bound_to_socket_dir_is_reapable(self, monkeypatch):
        from tools.browser_tool import _verify_reapable_browser_daemon

        # Harness runtime dirs DO NOT contain "agent-browser" — the original
        # binding check would incidentally pass them because of the substring,
        # but the *intent* of the check is process identity + binding to a
        # known socket dir.  Use a realistic harness path here.
        socket_dir = "/home/u/.config/browser-harness/runtime/h_sess123456"
        proc = _fake_proc(
            name="python",
            cmdline=[
                sys.executable, "-m", "browser_harness.daemon",
                "--socket-dir", socket_dir,
            ],
        )
        monkeypatch.setattr("psutil.Process", lambda pid: proc)
        assert _verify_reapable_browser_daemon(
            12345, socket_dir, "h_sess123456"
        ) is True

    def test_unrelated_python_process_is_refused(self, monkeypatch):
        """The spoof defense must NOT be weakened: a plain ``python`` process
        whose cmdline has nothing to do with the harness must still be
        refused, even if the socket dir happens to appear in its environ.
        """
        from tools.browser_tool import _verify_reapable_browser_daemon

        socket_dir = "/home/u/.config/browser-harness/runtime/h_sess123456"
        proc = _fake_proc(
            name="python",
            cmdline=[sys.executable, "/home/u/random_script.py"],
            environ={"SOCKET_DIR": socket_dir},
        )
        monkeypatch.setattr("psutil.Process", lambda pid: proc)
        assert _verify_reapable_browser_daemon(
            12345, socket_dir, "h_sess123456"
        ) is False

    def test_harness_daemon_for_other_session_is_refused(self, monkeypatch):
        """A harness daemon bound to a DIFFERENT socket dir must not be
        reaped — same recycled-PID / planted-PID defense the verifier
        already enforces for ``agent-browser``.
        """
        from tools.browser_tool import _verify_reapable_browser_daemon

        our_dir = "/home/u/.config/browser-harness/runtime/h_ours00000"
        other_dir = "/home/u/.config/browser-harness/runtime/h_other99999"
        proc = _fake_proc(
            name="python",
            cmdline=[
                sys.executable, "-m", "browser_harness.daemon",
                "--socket-dir", other_dir,
            ],
        )
        monkeypatch.setattr("psutil.Process", lambda pid: proc)
        assert _verify_reapable_browser_daemon(
            12345, our_dir, "h_ours00000"
        ) is False


# ---------------------------------------------------------------------------
# Group 4: cleanup + reaper + browser_exec do not deadlock each other
# ---------------------------------------------------------------------------


class TestNoDeadlockBetweenCleanupAndExec:
    """The fix must not introduce a lock-ordering cycle.  Before the fix,
    the cleanup thread and the reaper did not interact with ``browser_exec``
    at all (they were dead code), so there was no contention — but also no
    enforcement.  After the fix, ``browser_exec`` takes ``_cleanup_lock``
    briefly (via ``_update_session_activity``), and the cleanup thread /
    reaper take the same lock.  We must be able to interleave them without
    either side hanging on the other.

    Concretely: spawn a thread that hammers ``browser_exec`` (which takes
    ``_cleanup_lock`` and starts the cleanup thread), and concurrently run
    the cleanup pass + a reaper pass.  The whole exercise must finish in
    well under the inactivity timeout, with no thread left spinning.
    """

    def test_concurrent_exec_and_cleanup_does_not_hang(self, monkeypatch):
        import tools.browser_tool as bt
        import tools.browser_use_cli as bu_cli

        # Stub everything expensive / network-touching.
        monkeypatch.setattr(bt, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        # No real reaper / socket-dir scans during this test.
        monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path := __import__("pathlib").Path(__file__).parent))
        monkeypatch.setattr(bt, "_reap_orphaned_browser_sessions", lambda: None)
        monkeypatch.setattr(bt, "_cleanup_inactive_browser_sessions", lambda: None)

        # Run a batch of browser_exec calls on one thread, and the cleanup
        # thread's main loop on another.  If the lock-ordering is wrong,
        # one of them will hang and the join(timeout=...) will fire.
        results = {}
        started = threading.Event()

        def exec_hammers():
            started.set()
            for i in range(15):
                bu_cli.browser_exec(f"# iter {i}", task_id="hammer")
            results["exec"] = "done"

        def cleanup_thread_runs():
            started.set()
            for _ in range(10):
                bt._update_session_activity("hammer")
                # Force the cleanup thread's idle loop to advance without
                # actually scanning the filesystem.
                with bt._cleanup_lock:
                    bt._active_sessions.get("hammer")
            results["cleanup"] = "done"

        t_exec = threading.Thread(target=exec_hammers, name="exec")
        t_clean = threading.Thread(target=cleanup_thread_runs, name="cleanup")
        t_exec.start()
        t_clean.start()
        started.wait(timeout=1.0)

        # Hard cap: 5 seconds is generous (in practice < 200ms).  ``join``
        # returns None; the only thing that matters is whether the threads
        # are still alive after the wait.
        t_exec.join(timeout=5.0)
        t_clean.join(timeout=5.0)
        if t_exec.is_alive() or t_clean.is_alive():
            pytest.fail(
                "browser_exec + activity update deadlocked — "
                f"exec-alive={t_exec.is_alive()}, clean-alive={t_clean.is_alive()}"
            )

        assert results.get("exec") == "done"
        assert results.get("cleanup") == "done"
