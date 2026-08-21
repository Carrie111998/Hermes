from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from authority.manage import apply_authority, verify_manifest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "install"
    authority = tmp_path / "authority"
    repo.mkdir()
    authority.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Authority Test")
    managed = repo / "agent" / "route.py"
    managed.parent.mkdir()
    managed.write_text("route = 'upstream'\n", encoding="utf-8")
    _git(repo, "add", "agent/route.py")
    _git(repo, "commit", "-qm", "base")
    managed.write_text("route = 'authority'\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    (authority / "route-authority.patch").write_bytes(patch)
    _git(repo, "restore", "agent/route.py")
    return repo, authority, managed


def _manifest(authority: Path, install: Path, managed: Path) -> Path:
    path = authority / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authority_root": str(authority),
                "install_root": str(install),
                "patch_file": "route-authority.patch",
                "managed_files": [
                    {
                        "path": "agent/route.py",
                        "sha256": hashlib.sha256(
                            b"route = 'authority'\n"
                        ).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_verify_manifest_accepts_expected_managed_checksum(tmp_path: Path):
    install, authority, managed = _repo(tmp_path)
    manifest = _manifest(authority, install, managed)
    managed.write_text("route = 'authority'\n", encoding="utf-8")

    ok, detail = verify_manifest(manifest, install)

    assert ok is True
    assert "verified" in detail


def test_apply_authority_applies_patch_and_is_idempotent(tmp_path: Path):
    install, authority, managed = _repo(tmp_path)
    manifest = _manifest(authority, install, managed)

    assert apply_authority(manifest, install) == 0
    assert managed.read_text(encoding="utf-8") == "route = 'authority'\n"
    assert apply_authority(manifest, install) == 0


def test_apply_authority_fails_closed_on_conflict(tmp_path: Path):
    install, authority, managed = _repo(tmp_path)
    manifest = _manifest(authority, install, managed)
    managed.write_text("route = 'local-conflict'\n", encoding="utf-8")

    assert apply_authority(manifest, install) != 0
    assert managed.exists()


def test_verify_manifest_rejects_unlisted_file(tmp_path: Path):
    install, authority, managed = _repo(tmp_path)
    (install / "agent" / "other.py").write_text("other = True\n", encoding="utf-8")
    manifest = _manifest(authority, install, managed)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["managed_files"].append({"path": "agent/other.py", "sha256": "0" * 64})
    manifest.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = verify_manifest(manifest, install)

    assert ok is False
    assert "patch paths" in detail
