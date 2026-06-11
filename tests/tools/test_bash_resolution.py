"""Git-Bash-preferring bash resolution — terminal tool + shared helper.

On any Windows box with WSL installed, PATH order puts the WSL launcher
(``C:\\WINDOWS\\system32\\bash.exe``) and/or the Microsoft Store stub
(``...\\WindowsApps\\bash.exe``) ahead of Git Bash — when Git Bash is on
PATH at all (a default Git for Windows install only adds ``Git\\cmd``,
which has no bash).  WSL bash runs commands inside the Linux VM, where
the ``C:/...``-style snapshot / cwd-marker / temp paths that
``LocalEnvironment`` builds do not resolve: every terminal command exits
127 with a mangled-path "No such file or directory".

The same bug was fixed for cron ``.sh`` scripts in ``cron/scheduler.py``
(commit 8f44b8209).  These tests cover the shared resolver in
``hermes_cli._subprocess_compat`` and both consumers, mirroring
``tests/cron/test_cron_no_agent.py``'s bash tests at the terminal-tool
level.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ON_WINDOWS = platform.system() == "Windows"

# What shutil.which("bash") really returns on a WSL-enabled box (note the
# uppercase .EXE — the refusal check must be case-insensitive).
_WSL_LAUNCHER = r"C:\WINDOWS\system32\bash.EXE"
_STORE_STUB = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe"


def _mk_bash(path: Path) -> str:
    """Create an empty stand-in bash.exe and return its str path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return str(path)


@pytest.fixture
def win_bash_env(monkeypatch, tmp_path):
    """Simulated Windows install surface: env vars point at empty tmp dirs
    so the real machine's Git install can't leak into assertions."""
    pf = tmp_path / "Program Files"
    pf86 = tmp_path / "Program Files (x86)"
    lad = tmp_path / "LocalAppData"
    for d in (pf, pf86, lad):
        d.mkdir()
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))
    monkeypatch.setenv("LOCALAPPDATA", str(lad))
    monkeypatch.delenv("HERMES_GIT_BASH_PATH", raising=False)
    return SimpleNamespace(pf=pf, pf86=pf86, lad=lad, tmp=tmp_path)


def _which_returns(monkeypatch, result):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: result)


# ---------------------------------------------------------------------------
# Shared helper: hermes_cli._subprocess_compat.resolve_windows_git_bash
# ---------------------------------------------------------------------------


class TestResolveWindowsGitBash:
    def test_prefers_program_files_git_over_wsl_path_hit(self, win_bash_env, monkeypatch):
        """Known Git install locations beat a PATH hit that is the WSL
        launcher — the original bug had this priority inverted."""
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        git_bash = _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        _which_returns(monkeypatch, _WSL_LAUNCHER)
        assert resolve_windows_git_bash() == git_bash

    def test_program_files_bin_beats_usr_bin(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        bin_bash = _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        _mk_bash(win_bash_env.pf / "Git" / "usr" / "bin" / "bash.exe")
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == bin_bash

    def test_program_files_usr_bin_found_when_bin_missing(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        usr_bash = _mk_bash(win_bash_env.pf / "Git" / "usr" / "bin" / "bash.exe")
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == usr_bash

    def test_x86_and_per_user_install_locations(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        x86_bash = _mk_bash(win_bash_env.pf86 / "Git" / "bin" / "bash.exe")
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == x86_bash

        peruser_bash = _mk_bash(win_bash_env.lad / "Programs" / "Git" / "bin" / "bash.exe")
        # x86 still wins (earlier in the candidate order)…
        assert resolve_windows_git_bash() == x86_bash
        # …until it disappears.
        Path(x86_bash).unlink()
        assert resolve_windows_git_bash() == peruser_bash

    def test_refuses_wsl_launcher(self, win_bash_env, monkeypatch):
        """System32 bash is the WSL launcher — returning it would hand
        C:\\ paths to a Linux VM.  None is the correct answer."""
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        _which_returns(monkeypatch, _WSL_LAUNCHER)
        assert resolve_windows_git_bash() is None

    def test_refuses_windowsapps_store_stub(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        _which_returns(monkeypatch, _STORE_STUB)
        assert resolve_windows_git_bash() is None

    def test_accepts_non_wsl_path_bash(self, win_bash_env, monkeypatch):
        """A PATH bash that is neither System32 nor WindowsApps (e.g. a
        standalone MSYS2 install) is a legitimate last resort."""
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        msys_bash = str(win_bash_env.tmp / "msys64" / "usr" / "bin" / "bash.exe")
        _which_returns(monkeypatch, msys_bash)
        assert resolve_windows_git_bash() == msys_bash

    def test_hermes_git_bash_path_override_wins(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        override = _mk_bash(win_bash_env.tmp / "custom" / "bash.exe")
        _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        monkeypatch.setenv("HERMES_GIT_BASH_PATH", override)
        _which_returns(monkeypatch, _WSL_LAUNCHER)
        assert resolve_windows_git_bash() == override

    def test_hermes_git_bash_path_ignored_when_file_missing(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        git_bash = _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        monkeypatch.setenv(
            "HERMES_GIT_BASH_PATH", str(win_bash_env.tmp / "nope" / "bash.exe")
        )
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == git_bash

    def test_portable_hermes_git_beats_program_files(self, win_bash_env, monkeypatch):
        """Hermes' own portable Git (dropped by install.ps1) is checked
        before system installs so a broken system Git can't hijack it."""
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        portable = _mk_bash(win_bash_env.lad / "hermes" / "git" / "bin" / "bash.exe")
        _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == portable

    def test_portable_mingit_usr_bin_layout(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        mingit = _mk_bash(
            win_bash_env.lad / "hermes" / "git" / "usr" / "bin" / "bash.exe"
        )
        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() == mingit

    def test_returns_none_when_nothing_found(self, win_bash_env, monkeypatch):
        from hermes_cli._subprocess_compat import resolve_windows_git_bash

        _which_returns(monkeypatch, None)
        assert resolve_windows_git_bash() is None


# ---------------------------------------------------------------------------
# Terminal tool consumer: tools.environments.local._find_bash
# ---------------------------------------------------------------------------


class TestLocalFindBash:
    def test_prefers_git_bash_over_wsl_launcher(self, win_bash_env, monkeypatch):
        """THE original bug: Git Bash installed in Program Files (not on
        PATH), WSL launcher first on PATH — _find_bash must return Git
        Bash, never System32 bash."""
        from tools.environments import local

        git_bash = _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        monkeypatch.setattr(local, "_IS_WINDOWS", True)
        _which_returns(monkeypatch, _WSL_LAUNCHER)
        assert local._find_bash() == git_bash

    def test_raises_actionable_error_when_only_wsl_present(self, win_bash_env, monkeypatch):
        """No Git Bash anywhere + WSL launcher on PATH → a clear install
        hint, not a bash that silently can't see C:\\ paths."""
        from tools.environments import local

        monkeypatch.setattr(local, "_IS_WINDOWS", True)
        _which_returns(monkeypatch, _WSL_LAUNCHER)
        with pytest.raises(RuntimeError, match="Git Bash not found"):
            local._find_bash()

    def test_find_shell_alias_is_fixed_too(self, win_bash_env, monkeypatch):
        """process_registry spawns background shells via the _find_shell
        alias — it must resolve identically."""
        from tools.environments import local

        git_bash = _mk_bash(win_bash_env.pf / "Git" / "bin" / "bash.exe")
        monkeypatch.setattr(local, "_IS_WINDOWS", True)
        _which_returns(monkeypatch, _WSL_LAUNCHER)
        assert local._find_shell() == git_bash


# ---------------------------------------------------------------------------
# Cron consumer: cron.scheduler._resolve_bash delegates to the shared helper
# ---------------------------------------------------------------------------


class TestCronResolveBashDelegation:
    def test_windows_branch_delegates_to_shared_helper(self, monkeypatch):
        import cron.scheduler as scheduler

        sentinel = r"C:\Program Files\Git\bin\bash.exe"
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(scheduler, "resolve_windows_git_bash", lambda: sentinel)
        assert scheduler._resolve_bash() == sentinel

    def test_windows_branch_passes_none_through(self, monkeypatch):
        """None (no usable bash) must reach _run_job_script so it can emit
        the friendly 'bash not found' error."""
        import cron.scheduler as scheduler

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(scheduler, "resolve_windows_git_bash", lambda: None)
        assert scheduler._resolve_bash() is None


# ---------------------------------------------------------------------------
# Real-machine integration (Windows only) — mirrors the run-real-bash style
# of tests/cron/test_cron_no_agent.py's shell-script tests.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ON_WINDOWS, reason="Windows-only real-machine checks")
class TestWindowsRealMachine:
    def test_find_bash_never_returns_wsl_launcher(self):
        from tools.environments.local import _find_bash

        try:
            resolved = _find_bash()
        except RuntimeError:
            pytest.skip("no Git Bash installed on this machine")
        low = resolved.lower()
        assert "\\system32\\" not in low, f"WSL launcher leaked through: {resolved}"
        assert "\\windowsapps\\" not in low, f"Store stub leaked through: {resolved}"

    def test_local_environment_executes_command_end_to_end(self, tmp_path):
        """A terminal command must round-trip through the resolved bash.
        Under the WSL launcher this fails (snapshot/cwd files at C:/...
        don't resolve in the Linux VM)."""
        from tools.environments.local import LocalEnvironment, _find_bash

        try:
            _find_bash()
        except RuntimeError:
            pytest.skip("no Git Bash installed on this machine")

        env = LocalEnvironment(cwd=str(tmp_path), timeout=60)
        try:
            result = env.execute("echo hermes-bash-resolution-ok")
        finally:
            env.cleanup()
        assert result["returncode"] == 0, f"terminal command failed: {result}"
        assert "hermes-bash-resolution-ok" in result["output"]
