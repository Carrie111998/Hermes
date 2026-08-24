"""Release write-set/stage-set and cross-runtime version invariants."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release  # noqa: E402


def _prepare_release_tree(monkeypatch, root: Path, *, desktop: bool = True) -> None:
    version_file = root / "hermes_cli" / "__init__.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(
        '__version__ = "0.13.0"\n__release_date__ = "2026.5.14"\n',
        encoding="utf-8",
    )
    pyproject_file = root / "pyproject.toml"
    pyproject_file.write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.13.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "REPO_ROOT", root)
    monkeypatch.setattr(release, "VERSION_FILE", version_file)
    monkeypatch.setattr(release, "PYPROJECT_FILE", pyproject_file)

    if not desktop:
        return

    desktop_dir = root / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text(
        json.dumps({"name": "hermes", "version": "0.13.0"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "hermes-agent"},
                    "apps/desktop": {"name": "hermes", "version": "0.13.0"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_writer_returns_every_core_desktop_and_lockfile_path(monkeypatch, tmp_path):
    _prepare_release_tree(monkeypatch, tmp_path)

    written = release.update_version_files("0.14.0", "2026.5.21")

    desktop_package = tmp_path / "apps" / "desktop" / "package.json"
    package_lock = tmp_path / "package-lock.json"
    assert written == [
        release.VERSION_FILE,
        release.PYPROJECT_FILE,
        desktop_package,
        package_lock,
    ]
    assert '__version__ = "0.14.0"' in release.VERSION_FILE.read_text(encoding="utf-8")
    assert 'version = "0.14.0"' in release.PYPROJECT_FILE.read_text(encoding="utf-8")
    assert json.loads(desktop_package.read_text(encoding="utf-8"))["version"] == "0.14.0"
    lock = json.loads(package_lock.read_text(encoding="utf-8"))
    assert lock["packages"]["apps/desktop"]["version"] == "0.14.0"


def test_stager_uses_the_writer_return_value_without_reconstructing_it(monkeypatch, tmp_path):
    _prepare_release_tree(monkeypatch, tmp_path)
    written = release.update_version_files("0.14.0", "2026.5.21")
    captured = {}

    def fake_git_result(*args):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(release, "git_result", fake_git_result)

    result = release.stage_version_files(written)

    assert result.returncode == 0
    assert captured["args"] == ("add", *(str(path) for path in written))


def test_older_tree_without_desktop_keeps_core_only_compatibility(monkeypatch, tmp_path):
    _prepare_release_tree(monkeypatch, tmp_path, desktop=False)

    written = release.update_version_files("0.14.0", "2026.5.21")

    assert written == [release.VERSION_FILE, release.PYPROJECT_FILE]


def test_malformed_lockfile_fails_before_any_version_surface_is_written(
    monkeypatch, tmp_path
):
    _prepare_release_tree(monkeypatch, tmp_path)
    package_lock = tmp_path / "package-lock.json"
    package_lock.write_text("{not json", encoding="utf-8")
    original = {
        path: path.read_text(encoding="utf-8")
        for path in (
            release.VERSION_FILE,
            release.PYPROJECT_FILE,
            tmp_path / "apps" / "desktop" / "package.json",
        )
    }

    with pytest.raises(json.JSONDecodeError):
        release.update_version_files("0.14.0", "2026.5.21")

    assert {path: path.read_text(encoding="utf-8") for path in original} == original


def test_checked_in_core_desktop_and_workspace_versions_are_equal():
    init_text = (REPO_ROOT / "hermes_cli" / "__init__.py").read_text(encoding="utf-8")
    core_match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert core_match is not None
    core_version = core_match.group(1)
    project_version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    desktop_version = json.loads(
        (REPO_ROOT / "apps" / "desktop" / "package.json").read_text(encoding="utf-8")
    )["version"]
    workspace_version = json.loads(
        (REPO_ROOT / "package-lock.json").read_text(encoding="utf-8")
    )["packages"]["apps/desktop"]["version"]

    assert core_version == project_version == desktop_version == workspace_version
