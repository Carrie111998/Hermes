"""Each per-file pytest subprocess must get a private --basetemp.

Without one, every concurrent pytest process shares
``/tmp/pytest-of-<user>/`` and runs keep-last-3 numbered-root retention
pruning against its siblings' live roots. Under parallel CI load a
sibling's pruning deletes a root between another process creating it and
its first ``tmp_path`` use, failing fixture setup with
``FileNotFoundError: /tmp/pytest-of-runner/pytest-NNN`` (CI runs
32498702748 slice 9, 32499632201 slice 7).

These tests capture the constructed command via a fake Popen — no real
pytest subprocess is launched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_tests_parallel_under_test",
        REPO_ROOT / "scripts" / "run_tests_parallel.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakePopen:
    captured_cmds: list[list[str]] = []

    def __init__(self, cmd, **kwargs):
        type(self).captured_cmds.append(list(cmd))
        self.pid = 999999
        self.returncode = 0

    def communicate(self, timeout=None):
        return ("1 passed in 0.01s\n", None)

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _captured_cmd(mod, pytest_args):
    _FakePopen.captured_cmds.clear()
    mod.subprocess.Popen, real = _FakePopen, mod.subprocess.Popen
    try:
        mod._run_one_file_once(
            Path("tests/test_example.py"), pytest_args, REPO_ROOT, 60.0
        )
    finally:
        mod.subprocess.Popen = real
    assert len(_FakePopen.captured_cmds) == 1
    return _FakePopen.captured_cmds[0]


def test_private_basetemp_injected():
    mod = _load_runner()
    cmd = _captured_cmd(mod, ["-q"])
    basetemps = [a for a in cmd if a.startswith("--basetemp=")]
    assert len(basetemps) == 1, cmd
    assert "hermes-pytest-" in basetemps[0]
    # Cleaned up after the run (rmtree in the finally).
    leftover = basetemps[0].split("=", 1)[1]
    assert not Path(leftover).exists()


def test_user_supplied_basetemp_comes_later_and_wins():
    mod = _load_runner()
    cmd = _captured_cmd(mod, ["--basetemp=/tmp/user-choice"])
    basetemps = [a for a in cmd if a.startswith("--basetemp=")]
    assert len(basetemps) == 2, cmd
    # pytest uses the LAST occurrence; the user's must come after ours.
    assert basetemps[-1] == "--basetemp=/tmp/user-choice"


def test_no_shared_pytest_of_root_used():
    """The injected basetemp never lives under the shared pytest-of parent."""
    mod = _load_runner()
    cmd = _captured_cmd(mod, [])
    ours = next(a for a in cmd if a.startswith("--basetemp="))
    assert "pytest-of" not in ours


if __name__ == "__main__":
    sys.exit(0)
