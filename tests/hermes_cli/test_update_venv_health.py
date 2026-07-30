"""Tests for the Windows half-updated-venv hardening (July 2026 incident).

Covers three additions to ``hermes update``:

1. ``_venv_core_imports_healthy`` — the venv health probe that lets an
   "Already up to date" checkout still repair a broken dependency install.
2. ``_detect_venv_python_processes`` — the venv-interpreter process guard
   that refuses to mutate the venv while a desktop backend / stray python
   holds .pyd files mapped.
3. The commit_count == 0 repair branch wiring in ``_cmd_update_impl``.

All Windows-specific paths are exercised via ``_is_windows`` patching so
they run on any host (same approach as test_update_concurrent_quarantine).
"""

from __future__ import annotations

import subprocess
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import main as cli_main


# ---------------------------------------------------------------------------
# _venv_core_imports_healthy
# ---------------------------------------------------------------------------




def _fake_venv_python(tmp_path, *, windows: bool = False):
    bin_dir = tmp_path / "venv" / ("Scripts" if windows else "bin")
    bin_dir.mkdir(parents=True)
    py = bin_dir / ("python.exe" if windows else "python")
    py.write_bytes(b"")
    return py




# ---------------------------------------------------------------------------
# _detect_venv_python_processes
# ---------------------------------------------------------------------------


def _proc(pid: int, exe: str, name: str, cmdline: list[str] | None = None, cwd: str = ""):
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "exe": exe,
        "name": name,
        "cmdline": cmdline or [],
        "cwd": cwd,
    }
    return proc


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_only_loads_expensive_details_for_python_candidates(_winp, tmp_path):
    """Windows process snapshots must stay bounded on process-heavy desktops."""

    class FakeProcess:
        def __init__(self, *, pid, name, exe, cmdline, cwd=""):
            self.all_info = {
                "pid": pid,
                "name": name,
                "exe": exe,
                "cmdline": cmdline,
                "cwd": cwd,
            }
            self.info = {}
            self.detail_calls = 0

        def as_dict(self, attrs):
            self.detail_calls += 1
            return {attr: self.all_info.get(attr) for attr in attrs}

    venv_py = str(tmp_path / "venv" / "Scripts" / "python.exe")
    unrelated = FakeProcess(
        pid=101,
        name="chrome.exe",
        exe="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        cmdline=["chrome.exe"],
    )
    candidate = FakeProcess(
        pid=102,
        name="python.exe",
        exe=venv_py,
        cmdline=[venv_py, "-m", "hermes_cli.main", "serve"],
    )
    venv_tool = FakeProcess(
        pid=103,
        name="custom-tool.exe",
        exe=str(tmp_path / "venv" / "Scripts" / "custom-tool.exe"),
        cmdline=["custom-tool.exe"],
    )
    requested_attrs = []

    def process_iter(attrs):
        requested_attrs.append(attrs)
        for proc in (unrelated, candidate, venv_tool):
            proc.info = {attr: proc.all_info.get(attr) for attr in attrs}
        return iter((unrelated, candidate, venv_tool))

    me = MagicMock()
    me.parents.return_value = []
    fake_psutil = types.SimpleNamespace(
        process_iter=process_iter,
        Process=lambda *a, **k: me,
    )

    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        matches = cli_main._detect_venv_python_processes()

    assert requested_attrs == [["pid", "name", "exe"]]
    assert unrelated.detail_calls == 0
    assert candidate.detail_calls == 1
    assert venv_tool.detail_calls == 1
    assert [match[0] for match in matches] == [102, 103]


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_venv_python_excludes_self_and_ancestors(_winp, tmp_path):
    import os as _os

    venv_py = str(tmp_path / "venv" / "Scripts" / "python.exe")
    parent = MagicMock()
    parent.pid = 555
    me = MagicMock()
    me.parents.return_value = [parent]
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                _proc(_os.getpid(), venv_py, "python.exe"),
                _proc(555, venv_py, "hermes.exe"),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.dict(
        sys.modules, {"psutil": fake_psutil}
    ):
        assert cli_main._detect_venv_python_processes() == []




# ---------------------------------------------------------------------------
# --force vs --force-venv gating of the venv-holder guard
# ---------------------------------------------------------------------------


def _update_args(**overrides):
    defaults = dict(
        gateway=False,
        check=False,
        no_backup=True,
        backup=False,
        yes=True,
        branch=None,
        force=False,
        force_venv=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _run_update_until_guard(args):
    """Drive _cmd_update_impl just far enough to hit the venv-holder guard.

    Everything before the guard is stubbed; the guard firing is observed via
    SystemExit(2). The first statement AFTER the guard is
    ``git_dir = PROJECT_ROOT / ".git"`` — a PROJECT_ROOT sentinel whose
    ``__truediv__`` raises marks 'guard passed'."""

    class _PastGuard(Exception):
        pass

    class _RootSentinel:
        def __truediv__(self, _other):
            raise _PastGuard

    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "_venv_scripts_dir", return_value=None
    ), patch.object(cli_main, "_run_pre_update_backup"), patch.object(
        cli_main, "_pause_windows_gateways_for_update", return_value=None
    ), patch.object(
        cli_main, "_resume_windows_gateways_after_update"
    ), patch.object(
        cli_main,
        "_detect_venv_python_processes",
        return_value=[(101, "python.exe", "python.exe -m hermes_cli.main serve")],
    ), patch.object(
        cli_main, "PROJECT_ROOT", _RootSentinel()
    ):
        try:
            cli_main._cmd_update_impl(args, gateway_mode=False)
        except _PastGuard:
            return "past_guard"
        except SystemExit as exc:
            return f"exit_{exc.code}"
    return "returned"


@pytest.mark.parametrize(
    "force,force_venv,expected",
    [
        (False, False, "exit_2"),   # guard fires
        (True, False, "exit_2"),    # plain --force does NOT bypass the venv guard
        (False, True, "past_guard"),  # --force-venv is the explicit escape hatch
        (True, True, "past_guard"),
    ],
)
def test_venv_holder_guard_force_semantics(force, force_venv, expected, capsys):
    result = _run_update_until_guard(_update_args(force=force, force_venv=force_venv))
    assert result == expected, capsys.readouterr().out
