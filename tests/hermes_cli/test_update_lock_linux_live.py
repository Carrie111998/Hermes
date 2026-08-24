"""Native Linux process evidence for the install-wide update lock.

The unit coverage deliberately exercises marker races hermetically.  This
module complements it with a real second interpreter and the kernel PID probe:
no process-liveness or marker operation is mocked.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.update_lock import UpdateLock


def _close_process_pipes(process: subprocess.Popen[str]) -> str:
    if process.stdin is not None:
        process.stdin.close()
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is None:
        return ""
    stderr = process.stderr.read()
    process.stderr.close()
    return stderr


def _readline_with_timeout(
    process: subprocess.Popen[str], *, timeout_seconds: float = 10
) -> str:
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], timeout_seconds)
    if readable:
        return process.stdout.readline().strip()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    stderr = process.stderr.read() if process.stderr is not None else ""
    raise AssertionError(
        f"lock claimant {process.pid} produced no output within "
        f"{timeout_seconds:g}s; stderr={stderr!r}"
    )


@pytest.mark.linux_only
def test_live_process_owner_blocks_other_profile_then_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named profiles contend on a live child PID and reacquire after release."""

    root = tmp_path / "hermes-root"
    owner_home = root / "profiles" / "alpha"
    contender_home = root / "profiles" / "beta"
    owner_env = os.environ.copy()
    owner_env["HERMES_HOME"] = str(owner_home)
    owner_script = """
from hermes_cli.update_lock import UpdateLock

lock = UpdateLock(heartbeat_seconds=0.05)
print(f"acquired={int(lock.acquire())};pid={lock._owner_pid};path={lock.path}", flush=True)
input()
lock.release()
print(f"released={int(not lock.path.exists())}", flush=True)
"""

    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script],
        cwd=Path(__file__).resolve().parents[2],
        env=owner_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _readline_with_timeout(owner)
        assert ready.startswith(f"acquired=1;pid={owner.pid};path=")

        monkeypatch.setenv("HERMES_HOME", str(contender_home))
        contender = UpdateLock(heartbeat_seconds=0.05)
        assert contender.acquire() is False
        assert contender.holder is not None
        assert contender.holder.pid == owner.pid
        assert contender.path == root / ".hermes-update-in-progress"

        assert owner.stdin is not None
        owner.stdin.write("release\n")
        owner.stdin.flush()
        assert _readline_with_timeout(owner) == "released=1"
        assert owner.wait(timeout=10) == 0

        successor = UpdateLock(heartbeat_seconds=0.05)
        assert successor.acquire() is True
        successor.release()
        assert not successor.path.exists()
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=10)
        assert not _close_process_pipes(owner)


@pytest.mark.linux_only
def test_simultaneous_independent_claims_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    """A shared start barrier proves the no-clobber claim across processes."""

    root = tmp_path / "hermes-root"
    barrier = tmp_path / "start"
    marker = root / ".hermes-update-in-progress"
    claimant_script = """
import sys
import time
from pathlib import Path

from hermes_cli.update_lock import UpdateLock

barrier = Path(sys.argv[1])
while not barrier.exists():
    time.sleep(0.001)
lock = UpdateLock(heartbeat_seconds=0.05)
acquired = lock.acquire()
payload = lock.path.read_text(encoding="utf-8").splitlines()
print(
    f"acquired={int(acquired)};pid={lock._owner_pid or 0};"
    f"payload={'|'.join(payload)}",
    flush=True,
)
if acquired:
    input()
    lock.release()
"""

    claimants: list[subprocess.Popen[str]] = []
    for profile in ("alpha", "beta"):
        env = os.environ.copy()
        env["HERMES_HOME"] = str(root / "profiles" / profile)
        claimants.append(
            subprocess.Popen(
                [sys.executable, "-c", claimant_script, str(barrier)],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    winner: subprocess.Popen[str] | None = None
    try:
        barrier.touch()
        observations: list[tuple[subprocess.Popen[str], str]] = []
        for claimant in claimants:
            line = _readline_with_timeout(claimant)
            observations.append((claimant, line))

        winners = [item for item in observations if item[1].startswith("acquired=1;")]
        losers = [item for item in observations if item[1].startswith("acquired=0;")]
        assert len(winners) == len(losers) == 1
        winner, winner_line = winners[0]
        loser, loser_line = losers[0]
        assert f"pid={winner.pid};" in winner_line
        assert f"payload={winner.pid}|" in winner_line
        assert f"payload={winner.pid}|" in loser_line
        marker_lines = marker.read_text(encoding="utf-8").splitlines()
        assert len(marker_lines) == 2
        assert marker_lines[0] == str(winner.pid)

        assert loser.wait(timeout=10) == 0
        assert winner.stdin is not None
        winner.stdin.write("release\n")
        winner.stdin.flush()
        assert winner.wait(timeout=10) == 0
        assert not marker.exists()
    finally:
        for claimant in claimants:
            if claimant.poll() is None:
                if claimant is winner and claimant.stdin is not None:
                    claimant.stdin.write("release\n")
                    claimant.stdin.flush()
                else:
                    claimant.terminate()
                claimant.wait(timeout=10)
            assert not _close_process_pipes(claimant)
