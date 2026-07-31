"""Tests for follow-up fixes to the LSP integration (PR after #24168).

Covers:

1. ``typescript-language-server`` install recipe pulls in ``typescript``
   alongside the server, so the configured Node package manager targets both.
2. ``hermes lsp status`` surfaces a ``Backend warnings`` section when
   bash-language-server is installed but ``shellcheck`` is missing.
3. ``_check_lint`` returns ``skipped`` (not ``error``) when the linter
   command exists on PATH but couldn't actually run — e.g. ``npx tsc``
   without the typescript SDK installed.  This is what unblocks the
   LSP semantic tier on TypeScript files when the user doesn't also
   have a project-level ``tsc``.
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

from agent.lsp.install import INSTALL_RECIPES


# ---------------------------------------------------------------------------
# Fix 1: typescript install recipe carries the typescript SDK
# ---------------------------------------------------------------------------


def _write_fake_node_installer(bin_dir, name):
    script = bin_dir / name
    script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '%s\n' \"$PWD\" > \"$FAKE_NODE_LOG_DIR/cwd\"\n"
        "printf '%s\n' \"$@\" > \"$FAKE_NODE_LOG_DIR/args\"\n"
        "mkdir -p node_modules/.bin\n"
        "printf '#!/bin/sh\\n' > \"node_modules/.bin/$FAKE_NODE_BIN_NAME\"\n"
        "chmod +x \"node_modules/.bin/$FAKE_NODE_BIN_NAME\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _fake_node_env(tmp_path, monkeypatch, manager, bin_name, *, corepack=False):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    if corepack:
        _write_fake_node_installer(fake_bin, "corepack")
    else:
        _write_fake_node_installer(fake_bin, manager)
    log_dir = tmp_path / "node-log"
    log_dir.mkdir()
    # Keep the fake toolchain first and exclude developer Node shims so a real
    # Corepack installation cannot mask the direct-binary fallback path.
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}/bin{os.pathsep}/usr/bin")
    monkeypatch.setenv("FAKE_NODE_LOG_DIR", str(log_dir))
    monkeypatch.setenv("FAKE_NODE_BIN_NAME", bin_name)
    return log_dir


def _set_lsp_package_manager(monkeypatch, manager):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"lsp": {"package_manager": manager}},
    )


def _read_fake_invocation(log_dir):
    return (
        (log_dir / "cwd").read_text(encoding="utf-8").strip(),
        (log_dir / "args").read_text(encoding="utf-8").splitlines(),
    )


def test_install_uses_corepack_pnpm_and_passes_extras(tmp_path, monkeypatch):
    """Configured pnpm runs through Corepack in the managed staging tree."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod

    _set_lsp_package_manager(monkeypatch, "pnpm")
    log_dir = _fake_node_env(
        tmp_path, monkeypatch, "pnpm", "typescript-language-server", corepack=True
    )

    resolved = install_mod._install_node_package(
        "typescript-language-server",
        "typescript-language-server",
        extra_pkgs=["typescript"],
    )

    staging = tmp_path / "lsp"
    cwd, args = _read_fake_invocation(log_dir)
    assert cwd == str(staging)
    assert args == [
        "pnpm",
        "add",
        "--silent",
        "typescript-language-server",
        "typescript",
    ]
    assert resolved is not None
    assert (install_mod.hermes_lsp_bin_dir() / "typescript-language-server").exists()


def test_install_falls_back_to_direct_pnpm_when_corepack_missing(tmp_path, monkeypatch):
    """Backwards compat: pyright-style recipes (no extras) still install."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod

    _set_lsp_package_manager(monkeypatch, "pnpm")
    log_dir = _fake_node_env(tmp_path, monkeypatch, "pnpm", "pyright-langserver")

    resolved = install_mod._install_node_package("pyright", "pyright-langserver")

    staging = tmp_path / "lsp"
    cwd, args = _read_fake_invocation(log_dir)
    assert cwd == str(staging)
    assert args == ["add", "--silent", "pyright"]
    assert resolved is not None


def test_install_uses_yarn_add_in_staging(tmp_path, monkeypatch):
    """Yarn adds requested packages; ``yarn install <pkg>`` is not valid."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod

    _set_lsp_package_manager(monkeypatch, "yarn")
    log_dir = _fake_node_env(
        tmp_path, monkeypatch, "yarn", "typescript-language-server"
    )

    resolved = install_mod._install_node_package(
        "typescript-language-server",
        "typescript-language-server",
        extra_pkgs=["typescript"],
    )

    staging = tmp_path / "lsp"
    cwd, args = _read_fake_invocation(log_dir)
    assert cwd == str(staging)
    assert args == ["add", "--silent", "typescript-language-server", "typescript"]
    assert resolved is not None


def test_install_defaults_to_npm_when_package_manager_unset(tmp_path, monkeypatch):
    """Hermes' upstream default stays npm unless lsp.package_manager is set."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {"lsp": {}})
    log_dir = _fake_node_env(tmp_path, monkeypatch, "npm", "pyright-langserver", corepack=True)

    resolved = install_mod._install_node_package("pyright", "pyright-langserver")

    staging = tmp_path / "lsp"
    cwd, args = _read_fake_invocation(log_dir)
    assert cwd == str(staging)
    assert args == [
        "npm",
        "install",
        "--prefix",
        str(staging),
        "--silent",
        "--no-fund",
        "--no-audit",
        "pyright",
    ]
    assert resolved is not None


def test_node_installer_retains_windows_safe_subprocess_kwargs(tmp_path, monkeypatch):
    """Node installs preserve UTF-8 decoding and Windows hide flags from main."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod
    import hermes_cli.config as config_mod

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        bin_dir = kwargs["cwd"] / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        launcher = bin_dir / "pyright-langserver"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(config_mod, "load_config", lambda: {"lsp": {}})
    monkeypatch.setattr(install_mod.shutil, "which", lambda c: "/usr/bin/npm" if c == "npm" else None)
    monkeypatch.setattr(install_mod, "windows_hide_flags", lambda: 12345)
    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

    resolved = install_mod._install_node_package("pyright", "pyright-langserver")

    assert resolved is not None
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["creationflags"] == 12345
    assert captured["kwargs"]["stdin"] is install_mod.subprocess.DEVNULL


def test_existing_binary_finds_windows_wrapper_in_staging(tmp_path, monkeypatch):
    """Installed Windows shims should satisfy later status/probe calls."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod

    wrapper = install_mod.hermes_lsp_bin_dir() / "pyright-langserver.cmd"
    wrapper.write_text("@echo off\n")
    wrapper.chmod(0o755)

    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(install_mod.shutil, "which", lambda _name: None)

    assert install_mod._existing_binary("pyright-langserver") == str(wrapper)
    assert install_mod.detect_status("pyright") == "installed"


def test_install_pip_finds_windows_scripts_launcher(tmp_path, monkeypatch):
    """pip console scripts can land in Scripts/ on native Windows."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent.lsp import install as install_mod

    def fake_run(cmd, **kwargs):
        scripts_dir = install_mod.hermes_lsp_bin_dir().parent / "python-packages" / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        launcher = scripts_dir / "fake-language-server.exe"
        launcher.write_text("launcher\n")
        launcher.chmod(0o755)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

    resolved = install_mod._install_pip("fake-lsp", "fake-language-server")

    assert resolved is not None
    assert resolved.endswith("fake-language-server.exe")
    assert (install_mod.hermes_lsp_bin_dir() / "fake-language-server.exe").exists()


# ---------------------------------------------------------------------------
# Fix 2: ``hermes lsp status`` surfaces shellcheck-missing for bash
# ---------------------------------------------------------------------------






def test_backend_warnings_fires_when_bash_installed_but_shellcheck_missing(tmp_path, monkeypatch):
    """The exact scenario from the bug report."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.lsp import cli as lsp_cli

    def which(name):
        if name == "bash-language-server":
            return "/fake/bin/bash-language-server"
        return None  # shellcheck missing

    with patch("shutil.which", side_effect=which):
        notes = lsp_cli._backend_warnings()
    assert len(notes) == 1
    assert "shellcheck" in notes[0].lower()
    assert "bash-language-server" in notes[0].lower()


def test_status_output_includes_backend_warnings_section(tmp_path, monkeypatch):
    """End-to-end: status command output includes the warning section."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Pretend bash-language-server is installed but shellcheck is missing
    def which(name):
        if name == "bash-language-server":
            return "/fake/bin/bash-language-server"
        return None

    from agent.lsp import cli as lsp_cli

    buf = io.StringIO()
    with patch("shutil.which", side_effect=which), redirect_stdout(buf):
        lsp_cli._cmd_status(emit_json=False)

    output = buf.getvalue()
    assert "Backend warnings" in output
    assert "shellcheck" in output


# ---------------------------------------------------------------------------
# Fix 3: tier-1 lint treats unusable linters as ``skipped``, not ``error``
# ---------------------------------------------------------------------------










def test_check_lint_returns_error_for_real_ts_type_errors(tmp_path):
    """Sanity: real TypeScript errors still go through the error path."""
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    ts_file = tmp_path / "bad.ts"
    ts_file.write_text("const x: string = 42;\n")

    env = LocalEnvironment()
    fops = ShellFileOperations(env)

    real_tsc_error = (
        "bad.ts:1:7 - error TS2322: Type 'number' is not assignable to type 'string'.\n"
        "1 const x: string = 42;\n"
        "        ~\n"
        "Found 1 error.\n"
    )

    def fake_exec(cmd, **kwargs):
        result = MagicMock()
        result.exit_code = 1
        result.stdout = real_tsc_error
        return result

    with patch.object(fops, "_exec", side_effect=fake_exec), \
         patch.object(fops, "_has_command", return_value=True):
        lint = fops._check_lint(str(ts_file))

    assert lint.skipped is False
    assert lint.success is False
    assert "TS2322" in lint.output


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
