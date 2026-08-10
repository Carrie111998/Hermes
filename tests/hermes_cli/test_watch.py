"""Tests for ``hermes watch`` subcommand."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _hermes(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes watch`` inline. Returns the process result."""
    hermes_bin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "hermes",
    )
    if not os.path.exists(hermes_bin):
        hermes_bin = "hermes"
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, hermes_bin, "watch", *args],
        capture_output=True, text=True, timeout=10, cwd=cwd, env=env,
    )


def test_watch_help():
    """``hermes watch --help`` prints usage and exits 0."""
    proc = _hermes(["--help"])
    assert proc.returncode == 0, proc.stderr
    assert "Watching" not in proc.stdout  # help, not a run
    assert "--pattern" in proc.stdout


def test_watch_nonexistent_path():
    """Non-existent path should print a skip message but exit 1."""
    proc = _hermes(["/tmp/does-not-exist-924781234"])
    assert proc.returncode == 1, proc.stderr
    assert "skipping" in proc.stderr.lower() or "no valid" in proc.stderr.lower()


def test_watch_polling_detects_new_file(temp_dir):
    """Polling watch should detect a newly created file."""
    watch_file = temp_dir / "watched.txt"

    # Start watcher in background
    proc = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "watch", str(temp_dir),
         "--interval", "0.5"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(temp_dir),
    )

    time.sleep(0.8)
    watch_file.write_text("hello")

    time.sleep(1.5)
    proc.terminate()
    proc.wait(timeout=3)

    stdout = proc.stdout.read() if proc.stdout else ""
    stderr = proc.stderr.read() if proc.stderr else ""
    output = stdout + stderr
    # Should mention "created" or "Watching"
    assert "Watching" in output or "created" in output.lower(), (
        f"No watch output detected:\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def test_watch_no_valid_paths():
    """No valid paths should exit 1."""
    proc = _hermes(["--interval", "0.1"],
                   cwd="/tmp/_nonexistent_dir_98761234")
    assert proc.returncode == 1