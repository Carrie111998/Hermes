"""Tests for the explicit bounded full-project validation path."""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import psutil
import pytest

from agent.lsp.validation import DEFAULT_EXCLUDES, run_full_project_check


FAKE_PYRIGHT = """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

project = Path(sys.argv[sys.argv.index('--project') + 1])
Path(os.environ['PROJECT_PATH']).write_text(str(project), encoding='utf-8')
Path(os.environ['CAPTURE']).write_text(
    json.dumps(json.loads(project.read_text(encoding='utf-8'))),
    encoding='utf-8',
)
if os.environ.get('MODE') == 'hang':
    child = subprocess.Popen([
        sys.executable, '-c', 'import time; time.sleep(60)'
    ])
    Path(os.environ['CHILD_PID']).write_text(str(child.pid), encoding='ascii')
    time.sleep(60)
print('fake pyright ok')
"""


def _write_fake_pyright(tmp_path: Path) -> Path:
    path = tmp_path / "fake-pyright"
    path.write_text(FAKE_PYRIGHT, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_stopped(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _live(pid):
            return True
        time.sleep(0.01)
    return not _live(pid)


def test_full_check_uses_repository_config_and_cleans_overlay(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    config = root / "pyrightconfig.json"
    config.write_text('{"typeCheckingMode": "strict"}\n', encoding="utf-8")
    capture = tmp_path / "capture.json"
    project_path = tmp_path / "project-path.txt"
    fake = _write_fake_pyright(tmp_path)

    result = run_full_project_check(
        str(root),
        executable=str(fake),
        env={"CAPTURE": str(capture), "PROJECT_PATH": str(project_path)},
        timeout=3.0,
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert "fake pyright ok" in result.stdout
    overlay_path = json.loads(capture.read_text(encoding="utf-8"))
    assert overlay_path["extends"] == str(config)
    assert overlay_path["include"] == [".."]
    assert overlay_path["exclude"] == [f"../{item}" for item in DEFAULT_EXCLUDES]
    assert not Path(project_path.read_text(encoding="utf-8")).exists()


@pytest.mark.linux_only
def test_full_check_terminates_process_group_after_timeout(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    capture = tmp_path / "capture.json"
    project_path = tmp_path / "project-path.txt"
    child_pid_file = tmp_path / "child.pid"
    fake = _write_fake_pyright(tmp_path)

    result = run_full_project_check(
        str(root),
        executable=str(fake),
        env={
            "CAPTURE": str(capture),
            "PROJECT_PATH": str(project_path),
            "MODE": "hang",
            "CHILD_PID": str(child_pid_file),
        },
        timeout=0.2,
        term_grace=0.1,
    )

    assert result.timed_out is True
    assert result.returncode is not None
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    assert _wait_stopped(child_pid) is True
