"""E2E: dispatcher spawns real Python subprocess workers.

This validates the IPC + lifecycle story that mocks can't:
  - spawn_fn returns a real PID
  - the child process resolves hermes_cli.kanban_db on its own
  - the child writes heartbeats via the CLI (real argparse, real init_db)
  - the child completes via the CLI with --summary + --metadata
  - the dispatcher observes all of this through the DB only
  - worker logs are captured to HERMES_HOME/kanban/logs/<task>.log
  - crash detection works against a real dead PID
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

WT = str(Path(__file__).resolve().parents[2])
FAKE_WORKER = str(Path(__file__).parent / "_fake_worker.py")
PY = sys.executable
IS_WINDOWS = os.name == "nt"

if WT not in sys.path:
    sys.path.insert(0, WT)
from tests.timeout_budget import scaled  # noqa: E402

# The CLI the spawned workers must use. Passing this explicitly is what
# makes the test hermetic: the worker runs *this* tree's hermes_cli.main
# rather than whatever `hermes` sits on the host PATH.
CLI_CMD = [PY, "-m", "hermes_cli.main"]


def make_spawn_fn(home: str):
    """Return a spawn_fn the dispatcher can call. Launches the fake
    worker as a detached subprocess."""

    def _spawn(task, workspace):
        log_path = os.path.join(home, f"worker_{task.id}.log")
        env = {
            **os.environ,
            "HERMES_HOME": home,
            "HOME": home,
            "PYTHONPATH": WT,
            "HERMES_KANBAN_TASK": task.id,
            "HERMES_KANBAN_WORKSPACE": workspace,
            "HERMES_CLI_CMD": json.dumps(CLI_CMD),
            "PATH": os.pathsep.join(
                [os.path.dirname(PY), os.environ.get("PATH", "")]
            ),
        }
        log_f = open(log_path, "ab")
        # cwd pinned to the temp home: run_tests_parallel.py gives every pytest
        # worker cwd=repo_root, so anything this worker writes relative to its
        # CWD lands in the shared checkout root. Safe to repoint because
        # PYTHONPATH=WT above already pins the import root and FAKE_WORKER is
        # an absolute path.
        proc = subprocess.Popen(
            [PY, FAKE_WORKER],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=home,
            start_new_session=True,
        )
        return proc.pid

    return _spawn


def main():
    home = tempfile.mkdtemp(prefix="hermes_e2e_")
    os.environ["HERMES_HOME"] = home
    os.environ["HOME"] = home
    sys.path.insert(0, WT)
    from hermes_cli import kanban_db as kb

    # Child processes are pointed at this tree's hermes_cli.main through
    # HERMES_CLI_CMD (see make_spawn_fn). This used to be a `#!/bin/sh`
    # shim named `hermes` prepended to PATH, which silently did nothing on
    # Windows — CreateProcess will not run an extensionless shell script,
    # so the workers fell through to whatever `hermes` was installed on
    # the host. An explicit argv is portable and provably hermetic.

    kb.init_db()
    conn = kb.connect()

    # ============ SCENARIO A: happy path, 3 tasks ============
    print("=" * 60)
    print("A. Real-subprocess happy path (3 tasks)")
    print("=" * 60)

    tids = []
    for i in range(3):
        tid = kb.create_task(
            conn, title=f"real-e2e-{i}", assignee="default",
        )
        tids.append(tid)

    spawn_fn = make_spawn_fn(home)
    result = kb.dispatch_once(conn, spawn_fn=spawn_fn)
    print(f"  dispatched: {len(result.spawned)} spawned")
    spawned_pids = []
    # The dispatcher sets worker_pid on each claimed task via _set_worker_pid.
    for tid in tids:
        task = kb.get_task(conn, tid)
        spawned_pids.append(task.worker_pid)
        print(f"  task {tid}: pid={task.worker_pid} status={task.status}")

    # Wait for all workers to complete. Incidental safety net, not the
    # assertion (tests/timeout_budget.py rule 2): each worker makes five
    # real CLI invocations and every one pays full interpreter + hermes
    # import startup (~2.5s on Windows), so three concurrent workers need
    # far more than the 10s this used to allow. Under-budgeting here does
    # not report "too slow" — it reports status=running, a dangling
    # current_run_id and missing heartbeats, i.e. it looks exactly like a
    # kernel bug.
    deadline = time.monotonic() + scaled(180)
    while time.monotonic() < deadline:
        statuses = [kb.get_task(conn, tid).status for tid in tids]
        if all(s == "done" for s in statuses):
            break
        time.sleep(0.2)

    print()
    failures = []
    for tid in tids:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        print(f"  task {tid}: status={task.status}, current_run_id={task.current_run_id}, "
              f"runs={[(r.id, r.outcome) for r in runs]}")
        if task.status != "done":
            failures.append(f"task {tid} not done: status={task.status}")
        if task.current_run_id is not None:
            failures.append(f"task {tid} has dangling current_run_id={task.current_run_id}")
        if len(runs) != 1:
            failures.append(f"task {tid} has {len(runs)} runs, expected 1")
        else:
            r = runs[0]
            if r.outcome != "completed":
                failures.append(f"task {tid} run outcome={r.outcome}, expected completed")
            if not r.summary or "real-subprocess worker finished" not in r.summary:
                failures.append(f"task {tid} summary missing: {r.summary!r}")
            if not r.metadata or r.metadata.get("iterations") != 3:
                failures.append(f"task {tid} metadata missing iterations: {r.metadata}")
            # Heartbeat events should be present
            events = kb.list_events(conn, tid)
            heartbeats = [e for e in events if e.kind == "heartbeat"]
            if len(heartbeats) < 3:  # start + 3 progress
                failures.append(f"task {tid} heartbeats={len(heartbeats)} expected >=3")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)

    print("\n  ✔ Scenario A: all 3 real-subprocess workers completed cleanly")

    # ============ SCENARIO B: crashed worker ============
    print()
    print("=" * 60)
    print("B. Crashed worker (kill -9 mid-heartbeat)")
    print("=" * 60)

    if IS_WINDOWS:
        # This scenario is irreducibly POSIX: subprocess pass_fds is
        # rejected on Windows, there is no `sleep` binary, and the
        # double-fork orphaning it depends on has no Windows equivalent
        # (no init to reparent to, so a killed child would be observed
        # differently). Skipping keeps A and C meaningful here; the crash
        # path stays covered on Linux.
        print("  ↷ SKIP on Windows: pass_fds/double-fork orphaning is POSIX-only")
    else:
        _scenario_b(kb, conn)

    # ============ SCENARIO C: worker log was captured ============
    print()
    print("=" * 60)
    print("C. Worker log captured to disk")
    print("=" * 60)
    # Scenario A workers wrote to <home>/worker_*.log
    import glob
    logs = glob.glob(os.path.join(home, "worker_*.log"))
    print(f"  {len(logs)} worker log files")
    for lp in logs[:3]:
        size = os.path.getsize(lp)
        print(f"    {os.path.basename(lp)}: {size} bytes")
        # Our fake worker is quiet (no prints); size=0 is fine

    conn.close()
    print("\n✔ ALL E2E SCENARIOS PASS")


def _scenario_b(kb, conn):
    """Crashed-worker detection. POSIX-only; see the guard in main()."""
    crash_tid = kb.create_task(
        conn, title="crash-e2e", assignee="default",
    )

    # Spawn a worker that sleeps long enough for us to kill it.
    # CRITICAL: spawn through a double-fork so when we kill the child it
    # doesn't zombify under our pid (which would fool kill -0 liveness
    # checks into thinking it's still alive). In production the
    # dispatcher daemon is long-lived but its workers are reaped by init
    # after exit; the test needs to match that orphaning behavior.
    def spawn_sleeper(task, workspace):
        r, w = os.pipe()
        middleman = subprocess.Popen(
            [
                PY, "-c",
                "import os,sys,subprocess;"
                "p=subprocess.Popen(['sleep','30'],"
                "stdin=subprocess.DEVNULL,"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                "start_new_session=True);"
                "os.write(int(sys.argv[1]), str(p.pid).encode());"
                "sys.exit(0)",
                str(w),
            ],
            pass_fds=(w,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(w)
        middleman.wait()  # middleman exits immediately, orphaning the sleep
        grandchild_pid = int(os.read(r, 16))
        os.close(r)
        return grandchild_pid

    kb.dispatch_once(conn, spawn_fn=spawn_sleeper)
    task = kb.get_task(conn, crash_tid)
    print(f"  spawned sleeper pid={task.worker_pid} for {crash_tid}")
    # Kill the sleeper forcibly
    os.kill(task.worker_pid, 9)
    # Give the OS a moment to reap
    time.sleep(0.5)

    # Simulate next dispatcher tick — should detect the crashed PID
    crashed = kb.detect_crashed_workers(conn)
    print(f"  detect_crashed_workers returned {len(crashed)} crashed (expected 1)")

    task = kb.get_task(conn, crash_tid)
    runs = kb.list_runs(conn, crash_tid)
    print(f"  task status={task.status}, runs={[(r.id, r.outcome) for r in runs]}")

    if len(crashed) < 1:
        print("  ✗ crash NOT detected")
        sys.exit(1)
    if task.status != "ready":
        print(f"  ✗ task should be back to ready, got {task.status}")
        sys.exit(1)
    if runs[0].outcome != "crashed":
        print(f"  ✗ run outcome should be 'crashed', got {runs[0].outcome!r}")
        sys.exit(1)
    print("\n  ✔ Scenario B: crash detected, task re-queued, run outcome=crashed")


if __name__ == "__main__":
    main()
