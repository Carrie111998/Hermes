"""Install a checkout-local `hermes` launcher for stress e2e (#93136).

The previous harness wrote an extensionless POSIX shim and joined PATH with
``:``. On native Windows that silently resolved to the user-installed
``hermes.EXE`` instead of the worktree under test.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def prepend_shim_to_path(shim_dir: Path | str, path: str | None = None) -> str:
    """Join *shim_dir* onto PATH using this OS's path separator."""
    existing = os.environ.get("PATH", "") if path is None else path
    prefix = str(shim_dir)
    if not existing:
        return prefix
    return f"{prefix}{os.pathsep}{existing}"


def install_checkout_hermes_shim(shim_dir: Path | str, *, python_exe: str) -> Path:
    """Write a native launcher that execs ``python -m hermes_cli.main``."""
    dest = Path(shim_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = dest / "hermes.cmd"
        launcher.write_text(
            f'@echo off\r\n"{python_exe}" -m hermes_cli.main %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = dest / "hermes"
        launcher.write_text(
            f'#!/bin/sh\nexec "{python_exe}" -m hermes_cli.main "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def assert_which_is_shim(shim_dir: Path | str) -> Path:
    """Fail closed if ``hermes`` on PATH is not the checkout shim."""
    resolved = shutil.which("hermes")
    if resolved is None:
        raise RuntimeError("hermes shim is not resolvable on PATH")
    resolved_path = Path(resolved).resolve()
    expected_dir = Path(shim_dir).resolve()
    if resolved_path.parent != expected_dir:
        raise RuntimeError(
            f"hermes resolved to {resolved_path}, not the checkout shim in {expected_dir}"
        )
    return resolved_path
