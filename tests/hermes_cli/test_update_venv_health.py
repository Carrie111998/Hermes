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


def test_rollout_absent_venv_is_created_inside_transaction(
    monkeypatch, tmp_path
):
    import hermes_cli.update_cmd as update_cmd

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        interpreter = tmp_path / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    target, interpreter = update_cmd._prepare_rollout_target_venv(
        tmp_path, "venv"
    )

    assert target == tmp_path / "venv"
    assert interpreter == tmp_path / "venv" / "bin" / "python"
    assert commands == [
        [sys.executable, "-I", "-m", "venv", str(tmp_path / "venv")]
    ]


def test_rollout_target_venv_name_is_fail_closed(tmp_path):
    import hermes_cli.update_cmd as update_cmd

    with pytest.raises(RuntimeError, match="invalid venv name"):
        update_cmd._prepare_rollout_target_venv(tmp_path, "../external")


def test_absent_checkpoint_venv_forces_full_dependency_install(
    monkeypatch, tmp_path
):
    import hermes_cli.update_cmd as update_cmd

    assert not update_cmd._rollout_dependency_install_is_current(
        True,
        {"dependency_state": {"venv_present": False}},
        project_root=tmp_path,
    )
    monkeypatch.setattr(
        update_cmd,
        "_venv_core_imports_healthy",
        lambda *args, **kwargs: (True, ""),
    )
    assert update_cmd._rollout_dependency_install_is_current(
        True,
        {"dependency_state": {"venv_present": True}},
        project_root=tmp_path,
    )


def test_existing_unhealthy_checkpoint_venv_forces_rollout_reinstall(
    monkeypatch, tmp_path
):
    import hermes_cli.update_cmd as update_cmd

    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    calls: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setenv("PYTHONHOME", "/coordinator/python")
    monkeypatch.setenv("PYTHONPATH", "/coordinator/modules")

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="fastapi: No module named 'fastapi'\n",
            stderr="",
        )

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    assert not update_cmd._rollout_dependency_install_is_current(
        True,
        {
            "venv_name": ".venv",
            "dependency_state": {"venv_present": True},
        },
        project_root=tmp_path,
    )
    command, kwargs = calls[0]
    assert command[:3] == [str(interpreter), "-I", "-c"]
    assert kwargs["cwd"] == tmp_path
    assert "PYTHONHOME" not in kwargs["env"]
    assert "PYTHONPATH" not in kwargs["env"]


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
    ), patch(
        "hermes_cli.update_cmd._new_update_context",
        return_value=("guard-test", {}),
    ), patch.object(cli_main, "_run_pre_update_backup"), patch.object(
        cli_main, "_pause_windows_gateways_for_update", return_value=None
    ), patch.object(
        cli_main, "_resume_windows_gateways_after_update"
    ), patch.object(
        cli_main,
        "_detect_venv_python_processes",
        return_value=[(101, "python.exe", "python.exe -m hermes_cli.main serve")],
    ), patch.object(
        # Pin the orphan classifier: this test exercises --force/--force-venv
        # gating, not orphan detection (covered in
        # test_update_orphan_backend_reap.py). None = "not provably orphaned"
        # → the guard refuses exactly as before the orphan-reap addition.
        cli_main, "_orphaned_desktop_backend_pids", return_value=None
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
