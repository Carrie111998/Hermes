"""Real-process contracts for the HERMES_HOME scheduler lease."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cron.scheduler_lease import SchedulerOwnershipLease

_ROOT = Path(__file__).resolve().parents[2]
_PROBE = """
import sys, time
from pathlib import Path
from cron.scheduler_lease import SchedulerOwnershipLease
lease = SchedulerOwnershipLease.try_acquire(
    hermes_home=Path(sys.argv[1]), owner="gateway", provider="builtin")
print("acquired" if lease else "blocked", flush=True)
if lease:
    time.sleep(float(sys.argv[2]))
    lease.release()
"""


def _spawn(home: Path, hold: float = 0) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["HOME"] = str(home.parent / "isolated-os-home")
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(_ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", _PROBE, str(home), str(hold)],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_same_home_process_contention_and_release(tmp_path):
    home = tmp_path / "hermes"
    # Leave ample margin for a heavily loaded suite to start the contender
    # before the first process releases its lease.
    first = _spawn(home, 3.0)
    assert first.stdout is not None
    assert first.stdout.readline().strip() == "acquired"
    second = _spawn(home)
    assert second.communicate(timeout=5)[0].strip() == "blocked"
    assert first.wait(timeout=5) == 0
    third = _spawn(home)
    assert third.communicate(timeout=5)[0].strip() == "acquired"


def test_different_homes_and_same_process_rules(tmp_path):
    first = SchedulerOwnershipLease.try_acquire(
        hermes_home=tmp_path / "a", owner="gateway", provider="builtin"
    )
    second = SchedulerOwnershipLease.try_acquire(
        hermes_home=tmp_path / "b", owner="desktop", provider="builtin"
    )
    assert first is not None and second is not None
    try:
        assert (
            SchedulerOwnershipLease.try_acquire(
                hermes_home=tmp_path / "a",
                owner="desktop",
                provider="builtin",
            )
            is None
        )
    finally:
        first.release()
        second.release()
