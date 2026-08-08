"""Tests for the Linux XDG desktop entry installed by ``hermes desktop``."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_cli import linux_desktop_entry as lde


@pytest.fixture
def xdg_home(tmp_path, monkeypatch) -> Path:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(lde.sys, "platform", "linux")
    return data_home


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    icon = root / "apps" / "desktop" / "assets" / "icon.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"\x89PNG fake")
    return root


def _parse(entry_text: str) -> dict:
    values = {}
    for line in entry_text.splitlines():
        if "=" in line and not line.startswith("["):
            key, val = line.split("=", 1)
            values[key] = val
    return values


def test_install_writes_entry_with_absolute_exec_and_icon(tmp_path, xdg_home, monkeypatch):
    root = _make_project(tmp_path)
    hermes_bin = tmp_path / "bin" / "hermes"
    hermes_bin.parent.mkdir()
    hermes_bin.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.relaunch.resolve_hermes_bin", lambda: str(hermes_bin)
    )
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)

    assert entry == xdg_home / "applications" / "hermes.desktop"
    values = _parse(entry.read_text(encoding="utf-8"))

    # Exec must be the absolute path of the resolved binary. The launcher
    # runs with a minimal PATH, so a bare `hermes` would not resolve.
    assert values["Exec"] == f"{hermes_bin} desktop"
    assert Path(values["Exec"].split(" ")[0]).is_absolute()

    # Icon must be an absolute path to the real icon in the checkout.
    icon_path = Path(values["Icon"])
    assert icon_path.is_absolute()
    assert icon_path == lde.icon_path(root)
    assert icon_path.read_bytes() == b"\x89PNG fake"

    assert values["Type"] == "Application"
    assert values["Name"] == "Hermes"
    assert values["Terminal"] == "false"


def test_installed_entry_is_executable(tmp_path, xdg_home, monkeypatch):
    root = _make_project(tmp_path)
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)

    assert entry.stat().st_mode & stat.S_IXUSR


def test_exec_falls_back_to_interpreter_module(tmp_path, xdg_home, monkeypatch):
    root = _make_project(tmp_path)
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: None)
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)
    exec_line = _parse(entry.read_text(encoding="utf-8"))["Exec"]

    assert exec_line.endswith("-m hermes_cli.main desktop")
    assert Path(exec_line.split(" ")[0]).is_absolute()


def test_exec_falls_back_to_unresolved_venv_interpreter_path(tmp_path, xdg_home, monkeypatch):
    """The interpreter fallback must keep the invocation path Python was
    actually started with, not resolve through a ``venv/bin/python ->
    base-interpreter`` symlink.

    Python's venv detection keys off the *invocation* path (it looks for a
    ``pyvenv.cfg`` beside it), not the symlink target. Resolving the
    symlink away therefore loses the venv's site-packages entirely from an
    unrelated cwd -- exactly what ``hermes desktop`` (launched from a
    packaged app in another directory) needs to survive.
    """
    root = _make_project(tmp_path)
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: None)
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)
    exec_line = _parse(entry.read_text(encoding="utf-8"))["Exec"]
    interpreter = exec_line.split(" ")[0]

    # Must be made absolute WITHOUT dereferencing a symlink.
    assert interpreter == os.path.abspath(lde.sys.executable)

    resolved = os.path.realpath(lde.sys.executable)
    if resolved == interpreter:
        pytest.skip("current interpreter is not a symlinked venv python; "
                     "the environment-safe probe needs a real symlink to be meaningful")

    # Environment-safe probe: the retained (unresolved) interpreter path
    # must still be able to import hermes_cli from a cwd that has nothing
    # to do with the checkout -- the resolved symlink target could not.
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    probe = subprocess.run(
        [interpreter, "-c", "import hermes_cli"],
        cwd=unrelated_cwd,
        capture_output=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")


def test_exec_does_not_persist_env_python_source_wrapper(tmp_path, xdg_home, monkeypatch):
    """A raw ``#!/usr/bin/env python3`` source-tree wrapper (e.g. repo-root
    ``hermes``) resolves whatever ``python3`` is first on PATH. A desktop
    session's PATH can differ from wherever ``hermes desktop`` was invoked
    from, so persisting that wrapper into ``Exec=`` risks a launcher that
    only works in one environment. Fall back to the current interpreter,
    already resolved absolute, instead.
    """
    root = _make_project(tmp_path)
    wrapper = tmp_path / "repo" / "hermes"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/usr/bin/env python3\nfrom hermes_cli.main import main\nmain()\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: str(wrapper))
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)
    exec_line = _parse(entry.read_text(encoding="utf-8"))["Exec"]

    assert exec_line == f"{os.path.abspath(lde.sys.executable)} -m hermes_cli.main desktop"


def _write_shebang(path: Path, shebang: str) -> Path:
    path.write_text(f"{shebang}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_env_python_wrapper_detects_usr_bin_env_python3(tmp_path):
    wrapper = _write_shebang(tmp_path / "hermes", "#!/usr/bin/env python3")
    assert lde._is_env_python_source_wrapper(wrapper) is True


def test_env_python_wrapper_detects_env_dash_s_python3(tmp_path):
    wrapper = _write_shebang(tmp_path / "hermes", "#!/usr/bin/env -S python3 -I")
    assert lde._is_env_python_source_wrapper(wrapper) is True


def test_env_python_wrapper_ignores_installed_python_under_envs_dir(tmp_path):
    """``/env`` appearing as a substring of a directory name (e.g. a real,
    hardcoded interpreter path under a conda/venv tree literally named
    ``envs``) is not the ``/usr/bin/env`` PATH-lookup shim. Must not be
    misclassified as an unsafe wrapper.
    """
    venv_bin = tmp_path / "home" / "user" / "envs" / "hermes" / "bin"
    venv_bin.mkdir(parents=True)
    wrapper = _write_shebang(tmp_path / "hermes", f"#!{venv_bin / 'python3'}")
    assert lde._is_env_python_source_wrapper(wrapper) is False


def test_env_python_wrapper_ignores_non_python_env_shebang(tmp_path):
    wrapper = _write_shebang(tmp_path / "hermes", "#!/usr/bin/env bash")
    assert lde._is_env_python_source_wrapper(wrapper) is False


def test_install_is_idempotent_and_skips_cache_refresh(tmp_path, xdg_home, monkeypatch):
    root = _make_project(tmp_path)
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: "/usr/bin/hermes")
    calls: list[Path] = []
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda d: calls.append(d) or [])

    lde.install_desktop_entry(root)
    assert len(calls) == 1

    # Unchanged content → no rewrite, no menu-cache churn on every launch.
    lde.install_desktop_entry(root)
    assert len(calls) == 1


def test_install_without_source_icon_uses_themed_name(tmp_path, xdg_home, monkeypatch):
    root = tmp_path / "hermes-agent"
    root.mkdir()
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)

    # A broken absolute path renders as no icon. The themed name resolves
    # when Hermes is installed some other way.
    assert _parse(entry.read_text(encoding="utf-8"))["Icon"] == "hermes"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_install_is_a_noop_off_linux(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(lde.sys, "platform", platform)
    assert lde.install_desktop_entry(_make_project(tmp_path)) is None


# ---------------------------------------------------------------------------
# Cache refresh tool gating
# ---------------------------------------------------------------------------


def _stub_tools(monkeypatch, available: "set[str]") -> "list[list[str]]":
    ran: list[list[str]] = []
    monkeypatch.setattr(
        lde.shutil, "which", lambda name: f"/usr/bin/{name}" if name in available else None
    )
    monkeypatch.setattr(lde, "_run_quiet", lambda cmd: ran.append(cmd) or True)
    return ran


def test_refresh_runs_kbuildsycoca6_when_present(monkeypatch, tmp_path):
    ran = _stub_tools(monkeypatch, {"update-desktop-database", "kbuildsycoca6"})

    tools = lde.refresh_desktop_databases(tmp_path)

    assert tools == ["update-desktop-database", "kbuildsycoca6"]
    assert ran == [
        ["/usr/bin/update-desktop-database", str(tmp_path)],
        ["/usr/bin/kbuildsycoca6", "--noincremental"],
    ]


def test_refresh_falls_back_to_kbuildsycoca5(monkeypatch, tmp_path):
    ran = _stub_tools(monkeypatch, {"kbuildsycoca5"})

    tools = lde.refresh_desktop_databases(tmp_path)

    assert tools == ["kbuildsycoca5"]
    assert ran == [["/usr/bin/kbuildsycoca5", "--noincremental"]]


def test_refresh_prefers_kbuildsycoca6_over_5(monkeypatch, tmp_path):
    ran = _stub_tools(monkeypatch, {"kbuildsycoca6", "kbuildsycoca5"})

    lde.refresh_desktop_databases(tmp_path)

    assert [cmd[0] for cmd in ran] == ["/usr/bin/kbuildsycoca6"]


def test_refresh_skips_missing_tools(monkeypatch, tmp_path):
    ran = _stub_tools(monkeypatch, set())

    assert lde.refresh_desktop_databases(tmp_path) == []
    assert ran == []


def test_refresh_reports_only_tools_that_succeeded(monkeypatch, tmp_path):
    monkeypatch.setattr(lde.shutil, "which", lambda name: f"/usr/bin/{name}")
    # update-desktop-database fails (exit != 0). kbuildsycoca6 succeeds.
    monkeypatch.setattr(lde, "_run_quiet", lambda cmd: "kbuildsycoca" in cmd[0])

    assert lde.refresh_desktop_databases(tmp_path) == ["kbuildsycoca6"]


def test_run_quiet_swallows_missing_binary(tmp_path):
    assert lde._run_quiet([str(tmp_path / "definitely-not-a-binary")]) is False


def test_exec_arg_quoting_handles_spaces(tmp_path, xdg_home, monkeypatch):
    root = _make_project(tmp_path)
    spaced = tmp_path / "my apps" / "hermes"
    spaced.parent.mkdir()
    spaced.write_text("", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.relaunch.resolve_hermes_bin", lambda: str(spaced))
    monkeypatch.setattr(lde, "refresh_desktop_databases", lambda _dir: [])

    entry = lde.install_desktop_entry(root)
    exec_line = _parse(entry.read_text(encoding="utf-8"))["Exec"]

    assert exec_line == f'"{spaced}" desktop'
