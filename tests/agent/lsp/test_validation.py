"""Tests for the explicit bounded full-project validation path."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
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


DETACHED_FAKE_PYRIGHT = """#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

project = Path(sys.argv[sys.argv.index('--project') + 1])
Path(os.environ['ROOT_PID']).write_text(str(os.getpid()), encoding='ascii')
if os.environ.get('MODE') == 'detached_hang':
    child_code = '''
import signal
import subprocess
import sys
import time
from pathlib import Path

spawned = False
def spawn(_signum, _frame):
    global spawned
    if spawned:
        return
    spawned = True
    grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    Path(sys.argv[1]).write_text(str(grandchild.pid), encoding='ascii')

signal.signal(signal.SIGTERM, spawn)
time.sleep(60)
'''
    child = subprocess.Popen(
        [sys.executable, '-c', child_code, os.environ['GRANDCHILD_PID']],
        start_new_session=True,
    )
    Path(os.environ['CHILD_PID']).write_text(str(child.pid), encoding='ascii')
    time.sleep(60)
print('detached fake pyright ok')
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


def _force_stop(pid: int) -> None:
    if not _live(pid):
        return
    try:
        process = psutil.Process(pid)
        process.kill()
        process.wait(timeout=2.0)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


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


@pytest.mark.live_system_guard_bypass
@pytest.mark.linux_only
def test_full_check_timeout_is_bounded_with_detached_pipe_holder(tmp_path: Path):
    """A detached grandchild must not make post-timeout drain unbounded."""
    root = tmp_path / "repo"
    root.mkdir()
    fake = tmp_path / "detached-fake-pyright"
    fake.write_text(DETACHED_FAKE_PYRIGHT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    root_pid_file = tmp_path / "root.pid"
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    result_file = tmp_path / "result.json"
    wrapper = """
import json
import sys
from agent.lsp.validation import run_full_project_check

result = run_full_project_check(
    sys.argv[1],
    executable=sys.argv[2],
    timeout=0.2,
    term_grace=0.1,
)
with open(sys.argv[3], 'w', encoding='utf-8') as f:
    json.dump({'timed_out': result.timed_out, 'returncode': result.returncode}, f)
"""
    env = os.environ.copy()
    env.update(
        {
            "MODE": "detached_hang",
            "ROOT_PID": str(root_pid_file),
            "CHILD_PID": str(child_pid_file),
            "GRANDCHILD_PID": str(grandchild_pid_file),
        }
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", wrapper, str(root), str(fake), str(result_file)],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        worker.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=2.0)
        pytest.fail("full-project validation did not return after its timeout")
    finally:
        for pid_file in (root_pid_file, child_pid_file, grandchild_pid_file):
            if pid_file.exists():
                _force_stop(int(pid_file.read_text(encoding="ascii")))

    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["timed_out"] is True
    assert payload["returncode"] is not None
    assert _wait_stopped(int(child_pid_file.read_text(encoding="ascii"))) is True
    assert _wait_stopped(int(grandchild_pid_file.read_text(encoding="ascii"))) is True


def test_full_check_rejects_include_outside_workspace(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside workspace"):
        run_full_project_check(
            str(root),
            executable=sys.executable,
            include=[str(outside)],
        )


def test_full_check_rejects_project_config_outside_workspace(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "pyrightconfig.json"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside workspace"):
        run_full_project_check(
            str(root),
            executable=sys.executable,
            project_config=str(outside),
        )


def test_full_check_rejects_json_config_paths_outside_workspace(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyrightconfig.json").write_text(
        json.dumps({"extends": "../outside-pyrightconfig.json"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside workspace"):
        run_full_project_check(str(root), executable=sys.executable)


def test_full_check_preserves_mandatory_excludes_with_custom_overlay(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    capture = tmp_path / "capture.json"
    project_path = tmp_path / "project-path.txt"
    fake = _write_fake_pyright(tmp_path)

    run_full_project_check(
        str(root),
        executable=str(fake),
        env={"CAPTURE": str(capture), "PROJECT_PATH": str(project_path)},
        exclude=["user-only-exclude"],
    )

    options = json.loads(capture.read_text(encoding="utf-8"))
    overlay_dir = Path(project_path.read_text(encoding="utf-8")).parent
    resolved_excludes = {
        (overlay_dir / value).resolve()
        for value in options["exclude"]
    }
    assert {root / item for item in DEFAULT_EXCLUDES}.issubset(resolved_excludes)
    assert (overlay_dir / "../user-only-exclude").resolve() in resolved_excludes


def test_full_check_rebases_pyproject_relative_extra_paths(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pyright]\nextraPaths = ['src']\n",
        encoding="utf-8",
    )
    capture = tmp_path / "capture.json"
    project_path = tmp_path / "project-path.txt"
    fake = _write_fake_pyright(tmp_path)

    run_full_project_check(
        str(root),
        executable=str(fake),
        env={"CAPTURE": str(capture), "PROJECT_PATH": str(project_path)},
    )

    options = json.loads(capture.read_text(encoding="utf-8"))
    overlay_dir = Path(project_path.read_text(encoding="utf-8")).parent
    assert options["extraPaths"] == [os.path.relpath(root / "src", overlay_dir)]


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright is not installed")
def test_full_check_runs_installed_pyright_with_relative_project_options(tmp_path: Path):
    root = tmp_path / "repo"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "helper.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from helper import VALUE\nresult: int = VALUE\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.pyright]\nextraPaths = ['src']\ntypeCheckingMode = 'strict'\n",
        encoding="utf-8",
    )

    result = run_full_project_check(str(root), timeout=15)

    assert result.timed_out is False
    assert result.returncode == 0, result.stderr
