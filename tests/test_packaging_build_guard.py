"""Behavioral regression coverage for installable Charterforge artifacts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_artifact(kind: str, tmp_path) -> subprocess.CompletedProcess[str]:
    """Invoke the real PEP 517 hook (build_sdist / build_wheel)."""
    env = os.environ.copy()
    env["NIX_BUILD_TOP"] = "/build/devshell"
    # Redirect setuptools' scratch dirs (build/, *.egg-info) into tmp_path so
    # the allowed-marker build doesn't litter the real worktree.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    extra_cfg = tmp_path / "dist-extra.cfg"
    extra_cfg.write_text(
        f"[build]\nbuild_base = {scratch / 'build'}\n\n[egg_info]\negg_base = {scratch}\n",
        encoding="utf-8",
    )
    env["DIST_EXTRA_CONFIG"] = str(extra_cfg)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_{kind}; build_{kind}(r'{out}')".format(
                kind=kind, out=tmp_path
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("kind", "artifact_glob"),
    [("sdist", "charterforge-*.tar.gz"), ("wheel", "charterforge-*.whl")],
)
def test_artifact_build_produces_installable_charterforge_artifact(
    kind, artifact_glob, tmp_path
):
    result = _build_artifact(kind, tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob(artifact_glob))
