"""Immutable, credential-free release artifact and evidence tests."""

from __future__ import annotations

import hashlib
import json
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from grover_runtime.release import (
    ReleaseSpec,
    create_release,
    verify_wheel_modules,
)


UPSTREAM = "1" * 40
ROLLBACK = "2" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_tree(root: Path) -> Path:
    source = root / "source"
    (source / "grover_runtime").mkdir(parents=True)
    (source / "grover_runtime" / "__init__.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (source / "grover_runtime" / "operations.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    (source / "uv.lock").write_text("lock-version = 1\n", encoding="utf-8")

    # Mutable state, credentials, and browser/session caches must never enter a release.
    (source / ".env").write_text("TOKEN=do-not-package\n", encoding="utf-8")
    (source / "credentials.json").write_text("{}", encoding="utf-8")
    (source / "state.db").write_bytes(b"db")
    (source / "state.db-wal").write_bytes(b"wal")
    (source / "state.db-shm").write_bytes(b"shm")
    (source / "runtime.log").write_text("log", encoding="utf-8")
    (source / "sessions").mkdir()
    (source / "sessions" / "one.json").write_text("{}", encoding="utf-8")
    (source / "browser_cache").mkdir()
    (source / "browser_cache" / "cookies").write_text("cookie", encoding="utf-8")
    return source


def _spec(root: Path, source: Path, output_name: str) -> ReleaseSpec:
    patch = root / "candidate.patch"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    return ReleaseSpec(
        release_id="grover-test-release",
        source_tree=source,
        patch_file=patch,
        output_dir=root / output_name,
        upstream_commit=UPSTREAM,
        rollback_target=ROLLBACK,
        required_paths=(
            "grover_runtime/__init__.py",
            "grover_runtime/operations.py",
        ),
    )


def test_release_manifest_pins_every_required_hash_and_excludes_mutable_data(
    tmp_path: Path,
):
    source = _source_tree(tmp_path)

    result = create_release(_spec(tmp_path, source, "out"))

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    evidence = json.loads(result.evidence.read_text(encoding="utf-8"))
    assert manifest["upstream_commit"] == UPSTREAM
    assert manifest["rollback_target"] == ROLLBACK
    assert manifest["patch_sha256"] == _sha256(tmp_path / "candidate.patch")
    assert manifest["dependency_lock"]["path"] == "uv.lock"
    assert manifest["dependency_lock"]["sha256"] == _sha256(source / "uv.lock")
    assert manifest["artifact_sha256"] == _sha256(result.artifact)
    assert evidence["manifest_sha256"] == _sha256(result.manifest)

    with tarfile.open(result.artifact, "r") as archive:
        names = archive.getnames()
    assert "grover_runtime/operations.py" in names
    for forbidden in (
        ".env",
        "credentials.json",
        "state.db",
        "state.db-wal",
        "state.db-shm",
        "runtime.log",
        "sessions/one.json",
        "browser_cache/cookies",
    ):
        assert forbidden not in names
    assert set(evidence["excluded_paths"]) >= {
        ".env",
        "credentials.json",
        "state.db",
        "state.db-wal",
        "state.db-shm",
        "runtime.log",
        "sessions/one.json",
        "browser_cache/cookies",
    }


def test_release_is_deterministic_exclusive_and_read_only(tmp_path: Path):
    source = _source_tree(tmp_path)
    first = create_release(_spec(tmp_path, source, "out-1"))
    second = create_release(_spec(tmp_path, source, "out-2"))

    assert _sha256(first.artifact) == _sha256(second.artifact)
    assert _sha256(first.manifest) == _sha256(second.manifest)
    assert not first.artifact.stat().st_mode & stat.S_IWUSR
    assert not first.manifest.stat().st_mode & stat.S_IWUSR
    assert not first.evidence.stat().st_mode & stat.S_IWUSR

    with pytest.raises(FileExistsError):
        create_release(_spec(tmp_path, source, "out-1"))


def test_release_refuses_missing_runtime_modules_and_bad_commit_pins(tmp_path: Path):
    source = _source_tree(tmp_path)
    spec = _spec(tmp_path, source, "out")
    spec = ReleaseSpec(**{
        **spec.__dict__,
        "required_paths": ("grover_runtime/missing.py",),
    })
    with pytest.raises(ValueError, match="required release path is missing"):
        create_release(spec)

    bad = ReleaseSpec(**{**spec.__dict__, "upstream_commit": "main"})
    with pytest.raises(ValueError, match="40-character commit"):
        create_release(bad)


def test_wheel_verification_requires_every_runtime_module(tmp_path: Path):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("grover_runtime/__init__.py", "")
        archive.writestr("grover_runtime/operations.py", "")

    verify_wheel_modules(
        wheel,
        required_modules=(
            "grover_runtime/__init__.py",
            "grover_runtime/operations.py",
        ),
    )
    with pytest.raises(ValueError, match="wheel is missing required modules"):
        verify_wheel_modules(
            wheel,
            required_modules=("grover_runtime/action_service_client.py",),
        )


def test_project_packaging_includes_shadow_guard_manifest():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = config["tool"]["setuptools"]["package-data"]
    assert "plugin.yaml" in package_data["plugins.grover_shadow_guard"]
