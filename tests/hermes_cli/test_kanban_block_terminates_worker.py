"""Blocking a running card must terminate its exact worker, never orphan it.

``block_task`` used to be SQL-only: it nulled ``worker_pid`` while the worker
process kept running, so the live process became untraceable (every reclaim
path filters on ``worker_pid IS NOT NULL``) and kept consuming/writing until
it exited on its own. A controller block must reach the worker process —
through the same host-guarded termination helper the reclaim paths use — and
must never touch the worktree contents (WIP stays intact).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, *, pid: int) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, pid)
    return tid


def _wait_exit(proc: subprocess.Popen, timeout: float = 8.0) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_block_running_task_terminates_host_local_worker(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="controller stop", kind="needs_input")

            assert _wait_exit(proc), (
                "blocking a running card must terminate the exact live worker; "
                "leaving it running is a guaranteed orphan"
            )

            task = kb.get_task(conn, tid)
            assert task.status in {"blocked", "triage"}
            assert task.worker_pid is None

            events = kb.list_events(conn, tid)
            blocked = next(e for e in events if e.kind == "blocked")
            termination = blocked.payload.get("termination")
            assert termination, "blocked event must trace the termination outcome"
            assert termination["prev_pid"] == proc.pid
            assert termination["terminated"] is True
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_block_does_not_signal_foreign_host_claims(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock = ? WHERE id = ?",
                    ("otherhost:99999", tid),
                )
            assert kb.block_task(conn, tid, reason="controller stop", kind="needs_input")

            # The claim was recorded by another host: never signal a local PID
            # that merely coincides. The worker must still be alive.
            time.sleep(0.5)
            assert proc.poll() is None, (
                "a foreign-host claim must never trigger a local kill "
                "(PID collision would murder an unrelated process)"
            )
            events = kb.list_events(conn, tid)
            blocked = next(e for e in events if e.kind == "blocked")
            termination = blocked.payload.get("termination")
            assert termination is not None
            assert termination["host_local"] is False
            assert termination["termination_attempted"] is False
        finally:
            conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_block_dependency_kind_also_terminates_worker(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="waiting on HER-41", kind="dependency")
            assert _wait_exit(proc), (
                "dependency blocks route differently but must equally "
                "terminate the live worker"
            )
            assert kb.get_task(conn, tid).worker_pid is None
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_self_block_never_signals_the_calling_worker(kanban_home):
    """A worker blocking its own card (kanban_block) must not be killed mid-call.

    It still has its final report to write; it exits on its own right after.
    """
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())
        assert kb.block_task(conn, tid, reason="self", kind="needs_input")
        events = kb.list_events(conn, tid)
        blocked = next(e for e in events if e.kind == "blocked")
        assert "termination" not in blocked.payload
    finally:
        conn.close()


def test_block_stale_run_id_does_not_kill_newer_run_worker(kanban_home):
    """A block aimed at a stale run must not signal the newer run's worker."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            task = kb.get_task(conn, tid)
            stale_run = int(task.current_run_id) - 1
            assert not kb.block_task(
                conn, tid, reason="stale", kind="needs_input",
                expected_run_id=stale_run,
            )
            time.sleep(0.3)
            assert proc.poll() is None, (
                "a stale-run block must leave the live newer-run worker alone"
            )
        finally:
            conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_block_preserves_worktree_wip(kanban_home, tmp_path):
    """Termination must not touch files the worker wrote (WIP preserved)."""
    wip = tmp_path / "workspace" / "wip.txt"
    wip.parent.mkdir(parents=True)
    wip.write_text("half-finished work\n")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="stop", kind="needs_input")
            assert wip.read_text() == "half-finished work\n"
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# S-M4 — a recycled PID must never be signalled
# ---------------------------------------------------------------------------

def test_recorded_start_time_is_persisted_with_the_worker_pid(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            row = conn.execute(
                "SELECT worker_start_time FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            assert row["worker_start_time"] == kb._worker_process_start_time(proc.pid)
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_block_never_signals_a_recycled_pid(kanban_home):
    """A PID whose start-time no longer matches is a different process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    signalled: list = []
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            # Simulate PID recycling: the recorded identity belongs to a
            # process that already exited; this PID is now somebody else.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_start_time = ? WHERE id = ?",
                    ("Mon Jan  1 00:00:00 1990", tid),
                )
            termination = kb._terminate_worker_for_block(
                conn, tid, None, signal_fn=lambda p, s: signalled.append((p, s)),
            )
            assert signalled == [], (
                "a recycled PID must never receive a signal; the recorded "
                "start-time identity no longer matches this process"
            )
            assert not (termination or {}).get("termination_attempted")
            assert proc.poll() is None
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_block_still_terminates_when_start_time_identity_matches(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="stop", kind="needs_input")
            assert _wait_exit(proc)
            events = kb.list_events(conn, tid)
            blocked = next(e for e in events if e.kind == "blocked")
            assert blocked.payload["termination"]["terminated"] is True
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# R2-M4 — identity must fail closed at EVERY signal, not just the first
# ---------------------------------------------------------------------------

def test_recycled_pid_during_grace_is_never_sigkilled(kanban_home, monkeypatch):
    """Identity must be re-proven immediately before SIGKILL too.

    A worker can exit during the SIGTERM grace window and the OS can hand its
    PID to another process. Signalling again on the strength of the pre-SIGTERM
    check would kill that innocent process.
    """
    import signal as signal_module

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    sent: list = []
    identity = kb._worker_process_start_time(proc.pid)
    state = {"alive_calls": 0}

    def flaky_identity(pid, start_time):
        # Alive for the pre-SIGTERM check, recycled (a *different* process now
        # owns the PID) by the time the grace window expires.
        state["alive_calls"] += 1
        return kb.IDENTITY_ALIVE if state["alive_calls"] == 1 else kb.IDENTITY_DEAD

    monkeypatch.setattr(kb, "_worker_identity_state", flaky_identity)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb.time, "sleep", lambda seconds: None)

    try:
        info = kb._terminate_reclaimed_worker(
            proc.pid, f"{kb._claimer_id().split(':', 1)[0]}:x",
            signal_fn=lambda p, s: sent.append(s),
            process_start_time=identity,
        )
        assert signal_module.SIGTERM in sent, "the live worker must get SIGTERM"
        assert not any(
            s == getattr(signal_module, "SIGKILL", signal_module.SIGTERM)
            for s in sent[1:]
        ), f"a recycled PID must never be SIGKILLed: {sent}"
        assert info.get("sigkill") is False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_missing_start_time_capture_refuses_to_signal(kanban_home, monkeypatch):
    """If identity cannot be captured, never fall back to PID-only signalling."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    sent: list = []
    try:
        conn = kb.connect()
        try:
            def refuse_start_time(pid):
                raise RuntimeError("ps unavailable")

            monkeypatch.setattr(kb, "_worker_process_start_time", refuse_start_time)
            tid = _running_task(conn, pid=proc.pid)
            row = conn.execute(
                "SELECT worker_start_time FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            assert row["worker_start_time"] is None

            termination = kb._terminate_worker_for_block(
                conn, tid, None, signal_fn=lambda p, s: sent.append((p, s)),
            )
            assert sent == [], (
                "no captured identity means no proof of what this PID is; "
                "signalling it would be a blind kill"
            )
            assert (termination or {}).get("identity_unproven") is True
            assert proc.poll() is None
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_legacy_null_identity_is_refused_on_the_controller_block_path(kanban_home):
    """The block path (new) refuses; the historical reclaim path is unchanged.

    Rows predating the identity migration carry no fingerprint. Blind-killing
    is unacceptable for the controller-block path added by HER-118, while the
    pre-existing reclaim contract is preserved so live legacy workers are not
    stranded — both record the ``identity_unproven`` diagnostic.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    host_lock = f"{kb._claimer_id().split(':', 1)[0]}:x"
    try:
        refused: list = []
        info = kb._terminate_reclaimed_worker(
            proc.pid, host_lock,
            signal_fn=lambda p, s: refused.append(s),
            process_start_time=None,
            require_identity=True,
        )
        assert refused == [], f"block path must not blind-signal: {refused}"
        assert info["termination_attempted"] is False
        assert info["identity_unproven"] is True
        assert proc.poll() is None

        legacy: list = []
        info = kb._terminate_reclaimed_worker(
            proc.pid, host_lock,
            signal_fn=lambda p, s: legacy.append(s),
            process_start_time=None,
        )
        assert legacy, "reclaim keeps its historical PID-only contract"
        assert info["termination_attempted"] is True
        assert info["identity_unproven"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# R3-M4 — liveness is tri-state: UNKNOWN is never DEAD
# ---------------------------------------------------------------------------

def test_identity_state_distinguishes_alive_dead_and_unknown(kanban_home, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        identity = kb._worker_process_start_time(proc.pid)
        assert kb._worker_identity_state(proc.pid, identity) == kb.IDENTITY_ALIVE
        # A start-time that cannot belong to this process: proven different.
        assert kb._worker_identity_state(
            proc.pid, "Mon Jan  1 00:00:00 1990",
        ) == kb.IDENTITY_DEAD

        # ``ps`` unavailable: we know nothing, and nothing is not death.
        def broken_ps(*args, **kwargs):
            raise OSError("ps unavailable")

        monkeypatch.setattr(kb.subprocess, "run", broken_ps)
        assert kb._worker_identity_state(proc.pid, identity) == kb.IDENTITY_UNKNOWN
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_unknown_identity_never_signals_and_never_claims_termination(
    kanban_home, monkeypatch,
):
    """UNKNOWN must not authorize a signal nor let a duplicate be spawned."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    sent: list = []
    try:
        identity = kb._worker_process_start_time(proc.pid)
        monkeypatch.setattr(
            kb, "_worker_identity_state", lambda pid, start: kb.IDENTITY_UNKNOWN,
        )
        info = kb._terminate_reclaimed_worker(
            proc.pid, f"{kb._claimer_id().split(':', 1)[0]}:x",
            signal_fn=lambda p, s: sent.append(s),
            process_start_time=identity,
        )
        assert sent == [], f"UNKNOWN identity must not be signalled: {sent}"
        assert info["terminated"] is False, "UNKNOWN is never a proven termination"
        assert info.get("identity_unknown") is True
        # And the reclaim guard must treat it as "worker may still live", so no
        # duplicate is spawned beside it.
        assert kb._worker_survived_termination(info) is True
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_transient_ps_failure_before_sigkill_does_not_claim_termination(
    kanban_home, monkeypatch,
):
    """A ``ps`` blip during grace must not suppress escalation AND report success."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    sent: list = []
    states = [kb.IDENTITY_ALIVE] + [kb.IDENTITY_UNKNOWN] * 40
    try:
        identity = kb._worker_process_start_time(proc.pid)
        monkeypatch.setattr(
            kb, "_worker_identity_state",
            lambda pid, start: states.pop(0) if states else kb.IDENTITY_UNKNOWN,
        )
        monkeypatch.setattr(kb.time, "sleep", lambda seconds: None)
        info = kb._terminate_reclaimed_worker(
            proc.pid, f"{kb._claimer_id().split(':', 1)[0]}:x",
            signal_fn=lambda p, s: sent.append(s),
            process_start_time=identity,
        )
        # SIGTERM went out while identity was proven ALIVE...
        assert sent and sent[0] == signal.SIGTERM
        # ...but an UNKNOWN outcome must never be reported as terminated.
        assert info["terminated"] is False
        assert info.get("identity_unknown") is True
        assert kb._worker_survived_termination(info) is True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
