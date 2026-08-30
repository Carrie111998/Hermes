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


@pytest.mark.parametrize("desktop_build_ok", [True, False])
def test_current_checkout_handoff_reports_desktop_rebuild_result(
    monkeypatch, tmp_path, capsys, desktop_build_ok
):
    """The real no-commit hand-off branch propagates its Desktop result."""
    from hermes_cli import update_cmd
    from hermes_cli import update_inventory, update_receipt
    from hermes_cli import gitlock, managed_uv

    (tmp_path / ".git").mkdir()
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    def fake_git_run(command, **_kwargs):
        if "--abbrev-ref" in command:
            stdout = "main\n"
        elif "rev-list" in command:
            stdout = "0\n"
        elif "--is-shallow-repository" in command:
            stdout = "false\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    events = []
    rebuilds = []

    def rebuild(path, **kwargs):
        events.append("rebuild")
        rebuilds.append((path, kwargs))
        return desktop_build_ok

    monkeypatch.setenv("HERMES_UPDATE_REEXEC", "1")
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_git_run)
    monkeypatch.setattr(update_cmd, "_read_project_version", lambda: None)
    monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda _path: True)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", lambda: (True, ""))
    monkeypatch.setattr(update_cmd, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(update_cmd, "venv_python_path", lambda *_args, **_kwargs: venv_python)
    monkeypatch.setattr(
        update_cmd,
        "_check_and_apply_config_migration",
        lambda **_kwargs: events.append("migration"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_print_update_completion",
        lambda message: events.append(("completion", message)),
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(("gateway_exit", ok)),
    )
    monkeypatch.setattr(update_cmd, "_apply_pending_fleet_restart_catchup", lambda: None)
    monkeypatch.setattr(update_cmd, "_rebuild_desktop_after_update", rebuild)

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli_main, "_capture_active_lazy_features", lambda: [])
    monkeypatch.setattr(cli_main, "_capture_active_tool_dependencies", lambda: {})
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(cli_main, "_resume_windows_gateways_after_update", lambda _token: None)
    monkeypatch.setattr(cli_main, "_get_origin_url", lambda *_args: "https://github.com/NousResearch/hermes-agent.git")
    monkeypatch.setattr(cli_main, "_resolve_update_branch", lambda _args: "main")
    monkeypatch.setattr(cli_main, "_stash_local_changes_if_needed", lambda *_args: None)
    monkeypatch.setattr(cli_main, "_abort_dependency_sync_if_self_locked", lambda _token: None)
    monkeypatch.setattr(cli_main, "_install_python_dependencies_with_optional_fallback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "_refresh_active_lazy_features", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "_restore_active_tool_dependencies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(update_receipt, "begin_update_receipt", lambda: None)
    monkeypatch.setattr(update_inventory, "collect_runtime_inventory", lambda: SimpleNamespace(runtimes=[]))
    monkeypatch.setattr(update_inventory, "record_plan_in_receipt", lambda _plan: None)
    monkeypatch.setattr(gitlock, "clear_stale_git_locks", lambda _root: [])
    monkeypatch.setattr(gitlock, "clear_stale_tmp_packs", lambda _root: [])
    monkeypatch.setattr(managed_uv, "update_managed_uv", lambda **_kwargs: None)
    monkeypatch.setattr(managed_uv, "ensure_uv", lambda **_kwargs: "uv")

    update_cmd._cmd_update_impl(_update_args(force=True), gateway_mode=True)

    assert rebuilds == [
        (
            tmp_path / "apps" / "desktop",
            {"had_desktop_app_before_update": True},
        )
    ]
    if desktop_build_ok:
        assert events == [
            "rebuild",
            "migration",
            ("completion", "✓ Update complete!"),
            ("gateway_exit", True),
        ]
    else:
        assert events == ["rebuild", "migration", ("gateway_exit", False)]
        output = capsys.readouterr().out
        assert "Update partially complete" in output
        assert "Update complete!" not in output


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
