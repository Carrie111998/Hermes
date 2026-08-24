"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5


def test_banner_fetch_timeout_stops_spawned_child_before_sweep(tmp_path):
    """The passive banner path must quiesce transports before zero-age cleanup."""
    import hermes_cli.banner as banner

    script = tmp_path / "fetch_with_child.py"
    child_pid_path = tmp_path / "child.pid"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    def assert_child_stopped(*_args, **_kwargs):
        import psutil

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        try:
            child = psutil.Process(child_pid)
        except psutil.NoSuchProcess:
            return []
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        return []

    with (
        patch.object(banner, "BANNER_UPDATE_FETCH_TIMEOUT_SECONDS", 0.5),
        patch(
            "hermes_cli.gitlock.clear_stale_git_artifacts",
            side_effect=assert_child_stopped,
        ),
    ):
        result = banner._run_banner_update_fetch(
            tmp_path,
            is_shallow=False,
            git_cmd=[sys.executable, str(script)],
        )

    assert result.returncode == 124
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            import psutil

            child = psutil.Process(child_pid)
            if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"banner fetch transport child {child_pid} survived timeout")




