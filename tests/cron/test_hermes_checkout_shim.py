"""#93136: stress e2e must never resolve `hermes` to a globally installed binary."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.cron.hermes_checkout_shim import (
    assert_which_is_shim,
    install_checkout_hermes_shim,
    prepend_shim_to_path,
)


def test_path_join_uses_os_pathsep():
    shim = Path("C:/tmp/shim") if os.name == "nt" else Path("/tmp/shim")
    joined = prepend_shim_to_path(shim, "elsewhere")
    assert joined.split(os.pathsep)[0] == str(shim)
    assert os.pathsep in joined or joined == str(shim)


def test_install_shim_is_what_which_resolves(tmp_path, monkeypatch):
    shim_dir = tmp_path / "bin"
    install_checkout_hermes_shim(shim_dir, python_exe="python")
    monkeypatch.setenv("PATH", prepend_shim_to_path(shim_dir, os.environ.get("PATH", "")))
    resolved = shutil.which("hermes")
    assert resolved is not None
    assert_which_is_shim(shim_dir)
    assert Path(resolved).resolve().parent == shim_dir.resolve()


def test_assert_which_is_shim_rejects_foreign_binary(tmp_path, monkeypatch):
    shim_dir = tmp_path / "bin"
    foreign = tmp_path / "other"
    foreign.mkdir()
    if os.name == "nt":
        (foreign / "hermes.cmd").write_text("@echo off\r\n", encoding="utf-8")
    else:
        launcher = foreign / "hermes"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
    install_checkout_hermes_shim(shim_dir, python_exe="python")
    monkeypatch.setenv("PATH", f"{foreign}{os.pathsep}{shim_dir}")
    with pytest.raises(RuntimeError, match="not the checkout shim"):
        assert_which_is_shim(shim_dir)
