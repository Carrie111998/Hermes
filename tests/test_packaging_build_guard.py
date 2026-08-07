"""Behavioral regression coverage for the wheel/sdist distribution guard."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_artifact(
    kind: str,
    tmp_path,
    *,
    nix_build: bool,
    sealed_build: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real PEP 517 hook (build_sdist / build_wheel) as a subprocess.

    The wheel and sdist guards live in SEPARATE cmdclass entries in setup.py
    (the bdist_wheel one behind a try/except ImportError), so each hook needs
    its own regression coverage — a passing sdist test proves nothing about
    the wheel path.
    """
    env = os.environ.copy()
    # nix develop exports this too, so it must not grant permission to build
    # a distributable artifact.
    env["NIX_BUILD_TOP"] = "/build/devshell"
    if nix_build:
        env["HERMES_NIX_BUILD"] = "1"
    else:
        env.pop("HERMES_NIX_BUILD", None)
    if sealed_build is None:
        env.pop("HERMES_SEALED_RELEASE_BUILD", None)
    else:
        env["HERMES_SEALED_RELEASE_BUILD"] = sealed_build
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


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_artifact_build_rejects_nix_development_shell_environment(kind, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=False)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr


@pytest.mark.parametrize(
    ("kind", "artifact_glob"),
    [("sdist", "hermes_agent-*.tar.gz"), ("wheel", "hermes_agent-*.whl")],
)
def test_artifact_build_allows_explicit_nix_package_build_marker(kind, artifact_glob, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=True)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob(artifact_glob))


@pytest.mark.parametrize(
    "sealed_build",
    [
        "canonical-writer-release-v1",
        "muncho-legacy-production-release-v1",
        "owner-runtime-v1",
    ],
)
def test_wheel_build_allows_exact_sealed_release_roles(sealed_build, tmp_path):
    result = _build_artifact(
        "wheel",
        tmp_path,
        nix_build=False,
        sealed_build=sealed_build,
    )

    assert result.returncode == 0, result.stderr
    wheels = list(tmp_path.glob("hermes_agent-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        packaged = set(archive.namelist())
    required_manifests = {
        "plugins/muncho_canary_evidence/plugin.yaml",
        "plugins/platforms/discord/plugin.yaml",
    }
    assert required_manifests <= packaged
    assert {
        name
        for name in packaged
        if name.startswith("plugins/") and name.endswith("/plugin.yaml")
    } == required_manifests


@pytest.mark.parametrize(
    ("kind", "sealed_build"),
    [
        ("wheel", "owner-runtime-v1-near-miss"),
        ("wheel", "canonical-writer-release-v1-near-miss"),
        ("wheel", "muncho-legacy-production-release-v1-near-miss"),
        ("sdist", "owner-runtime-v1"),
        ("sdist", "canonical-writer-release-v1"),
        ("sdist", "muncho-legacy-production-release-v1"),
    ],
)
def test_sealed_wheel_role_is_exact_and_never_authorizes_sdist(
    kind,
    sealed_build,
    tmp_path,
):
    result = _build_artifact(
        kind,
        tmp_path,
        nix_build=False,
        sealed_build=sealed_build,
    )

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr
