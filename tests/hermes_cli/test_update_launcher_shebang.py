"""Regression tests for preserving the venv interpreter in the launcher."""

import sys

import pytest

from hermes_cli import update_cmd


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX venv symlinks are not representative on Windows",
)
def test_shebang_keeps_unresolved_venv_path_when_python_is_symlink(
    tmp_path, monkeypatch
):
    """The shebang must retain the venv path, not the symlink target."""
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    launcher.chmod(0o755)

    base_python = tmp_path / "base-python"
    base_python.write_text("", encoding="utf-8")
    venv_python = tmp_path / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)

    monkeypatch.setattr(update_cmd.sys, "executable", str(base_python))
    update_cmd._preserve_venv_shebang_in_launcher(
        launcher_path=launcher,
        venv_py=venv_python,
    )

    lines = launcher.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"#!{venv_python.absolute()}"
    assert launcher.stat().st_mode & 0o7777 == 0o755
    assert not list(tmp_path.glob(".hermes.tmp-*"))


def test_shebang_rewrite_is_idempotent_and_preserves_mode(tmp_path, monkeypatch):
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    launcher.chmod(0o751)
    venv_python = tmp_path / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(update_cmd.sys, "executable", str(venv_python))

    update_cmd._preserve_venv_shebang_in_launcher(
        launcher_path=launcher,
        venv_py=venv_python,
    )
    first_content = launcher.read_text(encoding="utf-8")
    first_mode = launcher.stat().st_mode & 0o7777

    update_cmd._preserve_venv_shebang_in_launcher(
        launcher_path=launcher,
        venv_py=venv_python,
    )

    assert launcher.read_text(encoding="utf-8") == first_content
    assert launcher.stat().st_mode & 0o7777 == first_mode == 0o751
    assert not list(tmp_path.glob(".hermes.tmp-*"))
