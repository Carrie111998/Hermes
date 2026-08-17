"""Test that setup.py uses temporary output directories when the source
tree is read-only (as it is inside the Docker WebUI install surface).
"""
from __future__ import annotations

from pathlib import Path
import runpy
import shutil
import tempfile

import pytest
from setuptools import Distribution
import setuptools


REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@pytest.fixture
def reaped_mkdtemp(monkeypatch) -> list[str]:
    """Record and delete the dirs setup.py's read-only fallback really creates.

    ``_temporary_build_dir`` calls ``tempfile.mkdtemp`` for real, and nothing in
    setuptools ever removes what it hands back -- on a read-only install surface
    the build output has to outlive the command. Forcing that branch here
    therefore leaked four empty %TEMP% dirs (two ``hermes-agent-build-*``, two
    ``hermes-agent-egg-info-*``) per run, on pass and on failure alike, because
    the mkdtemp happens inside ``finalize_options`` before any assertion. 24 of
    them were still on this box on 2026-08-17; the daily temp janitor does not
    collect them (its allowlist is deliberately prefix-scoped and has no entry
    for these).

    Recording the real return value rather than redirecting mkdtemp keeps the
    prefix assertions below testing the actual naming, which is the behaviour
    this file exists to pin.
    """
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    try:
        yield created
    finally:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)


def test_setup_uses_temporary_outputs_when_source_tree_is_read_only(
    monkeypatch,
    reaped_mkdtemp: list[str],
) -> None:
    """WebUI installs from read-only /opt/hermes must not write build metadata."""
    captured: dict[str, object] = {}

    def capture_setup(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(setuptools, "setup", capture_setup)
    runpy.run_path(str(REPO_ROOT / "setup.py"))

    cmdclass = captured["cmdclass"]
    monkeypatch.setitem(
        cmdclass["build"].finalize_options.__globals__,
        "_source_tree_is_writable",
        lambda: False,
    )
    monkeypatch.setitem(
        cmdclass["egg_info"].finalize_options.__globals__,
        "_source_tree_is_writable",
        lambda: False,
    )

    build_cmd = cmdclass["build"](Distribution())
    build_cmd.initialize_options()
    build_cmd.finalize_options()
    assert not _is_under(build_cmd.build_base, REPO_ROOT)
    assert Path(build_cmd.build_base).name.startswith("hermes-agent-build")

    source_relative_build = cmdclass["build"](Distribution())
    source_relative_build.initialize_options()
    source_relative_build.build_base = "nested/build"
    source_relative_build.finalize_options()
    assert not _is_under(source_relative_build.build_base, REPO_ROOT)
    assert Path(source_relative_build.build_base).name.startswith("hermes-agent-build")

    egg_info_cmd = cmdclass["egg_info"](Distribution())
    egg_info_cmd.initialize_options()
    egg_info_cmd.finalize_options()
    assert egg_info_cmd.egg_base is not None
    assert not _is_under(egg_info_cmd.egg_base, REPO_ROOT)
    assert Path(egg_info_cmd.egg_base).name.startswith("hermes-agent-egg-info")

    source_relative_egg_info = cmdclass["egg_info"](Distribution())
    source_relative_egg_info.initialize_options()
    source_relative_egg_info.egg_base = "."
    source_relative_egg_info.finalize_options()
    assert source_relative_egg_info.egg_base is not None
    assert not _is_under(source_relative_egg_info.egg_base, REPO_ROOT)
    assert Path(source_relative_egg_info.egg_base).name.startswith(
        "hermes-agent-egg-info"
    )

    # Prove the reaper has something to reap: one dir per finalize_options call
    # above. Without this, a setup.py that stopped calling mkdtemp would leave
    # `reaped_mkdtemp` cleaning an empty list and the leak guard would pass
    # vacuously for the wrong reason.
    assert sorted(Path(p).name for p in reaped_mkdtemp) == sorted(
        [
            Path(build_cmd.build_base).name,
            Path(source_relative_build.build_base).name,
            Path(egg_info_cmd.egg_base).name,
            Path(source_relative_egg_info.egg_base).name,
        ]
    )
