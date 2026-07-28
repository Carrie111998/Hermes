from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.canary import full_canary_owner_launcher as launcher


def _executable_tree(root: Path) -> tuple[Path, Path]:
    trusted = root / "trusted"
    executable = trusted / "bin" / "tool"
    executable.parent.mkdir(parents=True)
    trusted.chmod(0o700)
    executable.parent.chmod(0o700)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return trusted, executable


def _pinned(executable: Path) -> launcher._PinnedExecutablePath:
    return launcher._PinnedExecutablePath(
        str(executable),
        invalid_code="test_executable_invalid",
        changed_code="test_executable_changed",
    )


def test_unrelated_ancestor_timestamp_drift_preserves_exact_executable(
    tmp_path: Path,
) -> None:
    trusted, executable = _executable_tree(tmp_path)
    pinned = _pinned(executable)

    before = trusted.stat()
    os.utime(
        trusted,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )

    assert pinned.absolute_path() == str(executable)


def test_exact_ancestor_replacement_remains_fail_closed(tmp_path: Path) -> None:
    trusted, executable = _executable_tree(tmp_path)
    pinned = _pinned(executable)
    displaced = tmp_path / "trusted-displaced"

    trusted.rename(displaced)
    _new_trusted, replacement = _executable_tree(tmp_path)

    assert replacement.read_bytes() == (displaced / "bin" / "tool").read_bytes()
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="test_executable_changed",
    ):
        pinned.absolute_path()


def test_exact_executable_byte_change_remains_fail_closed(tmp_path: Path) -> None:
    _trusted, executable = _executable_tree(tmp_path)
    pinned = _pinned(executable)

    executable.write_bytes(b"#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="test_executable_changed",
    ):
        pinned.absolute_path()
