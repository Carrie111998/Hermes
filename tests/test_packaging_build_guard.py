"""Behavioral regression coverage for the wheel/sdist distribution guard."""

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOW_UNPACKAGED_ROOT_MODULES = {
    "mini_swe_runner",  # Standalone RL-training script, intentionally not installed.
    "setup",  # Build guard loaded by setuptools, not an importable project module.
}


def _load_setuptools_metadata() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["setuptools"]


def _build_artifact(kind: str, tmp_path, *, nix_build: bool) -> subprocess.CompletedProcess[str]:
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


@pytest.fixture(scope="module")
def allowed_artifacts(tmp_path_factory):
    artifacts = {}
    for kind, artifact_glob in (
        ("sdist", "hermes_agent-*.tar.gz"),
        ("wheel", "hermes_agent-*.whl"),
    ):
        output_dir = tmp_path_factory.mktemp(f"allowed-{kind}")
        result = _build_artifact(kind, output_dir, nix_build=True)
        assert result.returncode == 0, result.stderr
        artifacts[kind] = next(output_dir.glob(artifact_glob))
    return artifacts


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_artifact_build_rejects_nix_development_shell_environment(kind, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=False)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr


@pytest.mark.parametrize(
    ("kind", "artifact_glob"),
    [("sdist", "hermes_agent-*.tar.gz"), ("wheel", "hermes_agent-*.whl")],
)
def test_artifact_build_allows_explicit_nix_package_build_marker(
    kind, artifact_glob, allowed_artifacts
):
    assert allowed_artifacts[kind].match(artifact_glob)


def test_py_modules_cover_installable_root_modules():
    """Every installable top-level source module must be declared explicitly."""
    setuptools_metadata = _load_setuptools_metadata()
    py_modules = setuptools_metadata["py-modules"]
    assert py_modules == sorted(py_modules), "[tool.setuptools].py-modules must stay sorted"

    all_root_modules = {path.stem for path in PROJECT_ROOT.glob("*.py")}
    assert ALLOW_UNPACKAGED_ROOT_MODULES <= all_root_modules, (
        "stale unpackaged-module exemptions: "
        f"{sorted(ALLOW_UNPACKAGED_ROOT_MODULES - all_root_modules)}"
    )
    installable_root_modules = all_root_modules - ALLOW_UNPACKAGED_ROOT_MODULES
    declared_modules = set(py_modules)
    assert declared_modules == installable_root_modules, (
        "[tool.setuptools].py-modules must cover every installable root module; "
        f"missing={sorted(installable_root_modules - declared_modules)}, "
        f"unexpected={sorted(declared_modules - installable_root_modules)}"
    )


def test_wheel_contains_every_declared_py_module(allowed_artifacts):
    """The built wheel must contain every module promised by its metadata."""
    py_modules = _load_setuptools_metadata()["py-modules"]
    with zipfile.ZipFile(allowed_artifacts["wheel"]) as archive:
        wheel_members = set(archive.namelist())

    missing = [module for module in py_modules if f"{module}.py" not in wheel_members]
    assert not missing, f"declared py-modules missing from wheel: {missing}"
