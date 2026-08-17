"""Tests for the symlink-capability probe itself.

These are deliberately platform-agnostic: each asserts a relationship that
holds whether or not this machine can create symlinks, so the probe is
verified on Windows (where it reports False without Developer Mode) and on
POSIX (where it reports True) by the same assertions.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.symlink_support import requires_symlinks, symlinks_supported


def test_probe_returns_a_bool() -> None:
    assert isinstance(symlinks_supported(), bool)


def test_probe_agrees_with_a_real_symlink_attempt() -> None:
    """The probe must match what actually happens, not guess from the platform."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        target = d / "t"
        target.write_text("x", encoding="utf-8")
        try:
            (d / "l").symlink_to(target)
            actually_worked = (d / "l").is_symlink()
        except (OSError, NotImplementedError, AttributeError):
            actually_worked = False

    assert symlinks_supported() is actually_worked


@requires_symlinks
def test_guarded_test_can_build_a_directory_symlink_fixture(tmp_path: Path) -> None:
    """Several guarded tests link directories, so the mark must cover that too.

    Windows treats file and directory symlinks as distinct reparse types; a
    probe that only proved file symlinks would let these fail at fixture time.
    """
    target_dir = tmp_path / "real_dir"
    target_dir.mkdir()
    (target_dir / "inside.txt").write_text("data", encoding="utf-8")

    link = tmp_path / "dir_link"
    link.symlink_to(target_dir, target_is_directory=True)

    assert link.is_symlink()
    assert link.is_dir()
    assert (link / "inside.txt").read_text(encoding="utf-8") == "data"


def test_probe_is_cached() -> None:
    """Collection-time marks call this; it must not re-probe the filesystem."""
    symlinks_supported()
    hits_before = symlinks_supported.cache_info().hits
    symlinks_supported()
    assert symlinks_supported.cache_info().hits == hits_before + 1


def test_requires_symlinks_is_a_skipif_mark_tracking_the_probe() -> None:
    assert requires_symlinks.name == "skipif"
    # skipif skips when the condition is truthy, so it must be the negation.
    assert requires_symlinks.args == (not symlinks_supported(),)
    assert "SeCreateSymbolicLinkPrivilege" in requires_symlinks.kwargs["reason"]


@requires_symlinks
def test_guarded_test_can_build_a_symlink_fixture(tmp_path: Path) -> None:
    """A test wearing the mark must never hit WinError 1314 in its fixture."""
    target = tmp_path / "real"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "data"
