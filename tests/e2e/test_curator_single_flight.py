"""Cross-process proof for curator run single-flight."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


_CHILD = r"""
import json
import sys
import threading
import time
from pathlib import Path

import agent.curator as curator

ready = Path(sys.argv[1])
release = Path(sys.argv[2])
entered = Path(sys.argv[3])
mode = sys.argv[4]

curator._load_config = lambda: {}

def render_candidates():
    entered.write_text("entered", encoding="utf-8")
    return "candidate: cross-process-lock-proof"

def review(_prompt):
    ready.write_text("ready", encoding="utf-8")
    if mode == "hold":
        deadline = time.monotonic() + 20
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release holder")
            time.sleep(0.01)
    return {
        "final": "ok",
        "summary": "ok",
        "model": "stub",
        "provider": "stub",
        "tool_calls": [],
        "error": None,
    }

curator._render_candidate_list = render_candidates
curator._run_llm_review = review

result = curator.run_curator_review(
    synchronous=mode != "hold",
    dry_run=True,
    consolidate=True,
)
print(json.dumps(result), flush=True)

if mode == "hold":
    deadline = time.monotonic() + 20
    while any(
        thread.name == "curator-review" and thread.is_alive()
        for thread in threading.enumerate()
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError("curator background thread did not finish")
        time.sleep(0.01)
"""


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"holder exited before {path.name}: rc={process.returncode}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"timed out waiting for {path.name}\nstdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.02)


def _result(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "child produced no result"
    return json.loads(lines[0])


def test_curator_pass_is_single_flight_across_processes(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_TESTING"] = "1"
    env["PYTHONUTF8"] = "1"

    ready = tmp_path / "holder-ready"
    release = tmp_path / "release-holder"
    holder_entered = tmp_path / "holder-entered"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(ready),
            str(release),
            str(holder_entered),
            "hold",
        ],
        cwd=repo_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for(ready, holder)
        assert holder_entered.exists()

        contender_entered = tmp_path / "contender-entered"
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD,
                str(tmp_path / "contender-ready"),
                str(release),
                str(contender_entered),
                "quick",
            ],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        assert _result(contender.stdout) == {
            "started": False,
            "reason": "already_running",
        }
        assert not contender_entered.exists()

        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=15)
        assert holder.returncode == 0, holder_stderr
        assert _result(holder_stdout)["started"] is True

        successor_entered = tmp_path / "successor-entered"
        successor = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD,
                str(tmp_path / "successor-ready"),
                str(release),
                str(successor_entered),
                "quick",
            ],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        assert _result(successor.stdout)["started"] is True
        assert successor_entered.exists()
    finally:
        if holder.poll() is None:
            release.write_text("release", encoding="utf-8")
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)
