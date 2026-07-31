"""Tests for #7309: the terminal tool's ``pip`` must resolve to the Hermes venv.

The POSIX installer exposes Hermes through a launcher shim in ``~/.local/bin``,
so the venv's own ``bin`` dir never reaches the user's PATH.  With pyenv
installed, its shims sit at the front of the login shell's PATH, that PATH is
what the session snapshot captures, and every ``pip install`` the agent runs
lands in the user's pyenv-global site-packages instead of the Hermes venv.
"""

import os
import stat
import sys
from unittest.mock import patch

import pytest

from tools.environments import local as local_mod
from tools.environments.local import (
    LocalEnvironment,
    _make_run_env,
    _prepend_venv_bin_path_guard,
    _resolve_hermes_venv_bin_dir,
)


def _write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestResolveHermesVenvBinDir:
    def _reset_cache(self):
        local_mod._HERMES_VENV_BIN_DIR = local_mod._SENTINEL

    def test_resolves_interpreter_dir_inside_venv(self, monkeypatch, tmp_path):
        self._reset_cache()
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        monkeypatch.setattr(local_mod.sys, "prefix", str(tmp_path / "venv"))
        monkeypatch.setattr(local_mod.sys, "base_prefix", "/usr")
        monkeypatch.setattr(local_mod.sys, "executable", str(venv_bin / "python"))
        assert _resolve_hermes_venv_bin_dir() == str(venv_bin)

    def test_returns_none_outside_a_venv(self, monkeypatch):
        """A ``--no-venv`` / system-wide install has no Hermes env to prefer."""
        self._reset_cache()
        monkeypatch.setattr(local_mod.sys, "prefix", "/usr")
        monkeypatch.setattr(local_mod.sys, "base_prefix", "/usr")
        assert _resolve_hermes_venv_bin_dir() is None

    def test_result_is_cached(self, monkeypatch, tmp_path):
        self._reset_cache()
        local_mod._HERMES_VENV_BIN_DIR = str(tmp_path)
        monkeypatch.setattr(local_mod.sys, "prefix", "/usr")
        monkeypatch.setattr(local_mod.sys, "base_prefix", "/usr")
        assert _resolve_hermes_venv_bin_dir() == str(tmp_path)

    def teardown_method(self):
        local_mod._HERMES_VENV_BIN_DIR = local_mod._SENTINEL


class TestMakeRunEnvVenvPrecedence:
    def teardown_method(self):
        local_mod._HERMES_VENV_BIN_DIR = local_mod._SENTINEL
        local_mod._HERMES_BIN_DIR = local_mod._SENTINEL

    def test_venv_bin_outranks_pyenv_shims(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = "/opt/hermes/venv/bin"
        local_mod._HERMES_BIN_DIR = None
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        with patch.dict(
            os.environ, {"PATH": "/home/u/.pyenv/shims:/usr/bin:/bin"}, clear=True
        ):
            entries = _make_run_env({})["PATH"].split(os.pathsep)
        assert entries.index("/opt/hermes/venv/bin") < entries.index("/home/u/.pyenv/shims")

    def test_no_duplicate_when_already_present(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = "/opt/hermes/venv/bin"
        local_mod._HERMES_BIN_DIR = None
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        with patch.dict(
            os.environ, {"PATH": "/opt/hermes/venv/bin:/usr/bin"}, clear=True
        ):
            entries = _make_run_env({})["PATH"].split(os.pathsep)
        assert entries.count("/opt/hermes/venv/bin") == 1

    def test_noop_outside_a_venv(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = None
        local_mod._HERMES_BIN_DIR = None
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
            entries = _make_run_env({})["PATH"].split(os.pathsep)
        assert entries[0] == "/usr/bin"


class TestVenvBinPathGuard:
    def teardown_method(self):
        local_mod._HERMES_VENV_BIN_DIR = local_mod._SENTINEL

    def test_guard_exports_path_before_the_script(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = "/opt/hermes/venv/bin"
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        out = _prepend_venv_bin_path_guard("echo hi")
        assert out.endswith("echo hi")
        assert "/opt/hermes/venv/bin" in out
        assert "export PATH" in out

    def test_guard_escapes_single_quotes(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = "/opt/o'malley/venv/bin"
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        out = _prepend_venv_bin_path_guard("echo hi")
        assert "o'\\''malley" in out

    def test_guard_noop_outside_a_venv(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = None
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
        assert _prepend_venv_bin_path_guard("echo hi") == "echo hi"

    def test_guard_noop_on_windows(self, monkeypatch):
        local_mod._HERMES_VENV_BIN_DIR = r"C:\hermes\venv\Scripts"
        monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
        assert _prepend_venv_bin_path_guard("echo hi") == "echo hi"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX login-shell profile behaviour"
)
@pytest.mark.skipif(
    os.environ.get("CI") == "true" and not os.path.isfile("/bin/bash"),
    reason="Requires bash; CI sandbox may strip it.",
)
class TestPyenvShimsEndToEnd:
    """Real LocalEnvironment against a pyenv-style ``~/.profile``."""

    def teardown_method(self):
        local_mod._HERMES_VENV_BIN_DIR = local_mod._SENTINEL

    def _fake_host(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        shims = home / ".pyenv" / "shims"
        shims.mkdir(parents=True)
        _write_executable(shims / "pip", "#!/bin/sh\necho pyenv-global-pip\n")

        venv_bin = tmp_path / "hermes" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_executable(venv_bin / "pip", "#!/bin/sh\necho hermes-venv-pip\n")

        # What `eval "$(pyenv init -)"` effectively leaves in the profile.
        (home / ".profile").write_text(
            f'export PATH="{shims}:$PATH"\n', encoding="utf-8"
        )
        return home, shims, venv_bin

    def _run(self, tmp_path, command):
        with patch(
            "tools.environments.local._read_terminal_shell_init_config",
            return_value=([], True),
        ):
            env = LocalEnvironment(cwd=str(tmp_path), timeout=20)
            try:
                return env.execute(command)
            finally:
                env.cleanup()

    def test_pip_resolves_to_hermes_venv_despite_pyenv_shims(
        self, tmp_path, monkeypatch
    ):
        home, _shims, venv_bin = self._fake_host(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        local_mod._HERMES_VENV_BIN_DIR = str(venv_bin)

        result = self._run(tmp_path, "command -v pip; pip")

        output = result.get("output", "")
        assert str(venv_bin / "pip") in output
        assert "hermes-venv-pip" in output
        assert "pyenv-global-pip" not in output

    def test_project_venv_activation_still_wins(self, tmp_path, monkeypatch):
        """The guard sets the session baseline; it must not fight the agent.

        Activating a project venv mid-session has to keep working, otherwise
        the agent can no longer install into the project it is working on.
        """
        home, _shims, venv_bin = self._fake_host(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        local_mod._HERMES_VENV_BIN_DIR = str(venv_bin)

        project_bin = tmp_path / "project" / ".venv" / "bin"
        project_bin.mkdir(parents=True)
        _write_executable(project_bin / "pip", "#!/bin/sh\necho project-pip\n")
        (project_bin / "activate").write_text(
            f'export PATH="{project_bin}:$PATH"\n', encoding="utf-8"
        )

        with patch(
            "tools.environments.local._read_terminal_shell_init_config",
            return_value=([], True),
        ):
            env = LocalEnvironment(cwd=str(tmp_path), timeout=20)
            try:
                env.execute(f"source {project_bin / 'activate'}")
                result = env.execute("command -v pip; pip")
            finally:
                env.cleanup()

        output = result.get("output", "")
        assert str(project_bin / "pip") in output
        assert "project-pip" in output
