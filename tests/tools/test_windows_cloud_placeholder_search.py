"""Windows Cloud Files placeholder-safe broad searches (#97898).

Unit tests are deterministic (fake env, fake platform). Real-rg integration
tests skip on non-Windows: the exclusion globs are platform-correct by
design, so forcing them cross-platform would test the OS, not the code.
"""

import shutil
import sys
from pathlib import Path

import pytest

import tools.file_operations as file_operations
from tools.file_operations import ShellFileOperations, _windows_cloud_search_exclusions
from tools.environments.local import LocalEnvironment

class RecordingEnvironment:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.commands = []

    def execute(self, command, cwd=None, **kwargs):
        self.commands.append(command)
        if command.startswith("test -e"):
            return {"output": "exists\n", "returncode": 0}
        if command.startswith("command -v"):
            return {"output": "yes\n", "returncode": 0}
        return {"output": "", "returncode": 1}


def _fake_env(profile: Path) -> dict:
    return {
        "USERPROFILE": str(profile),
        "OneDrive": str(profile / "OneDrive"),
        "OneDriveConsumer": str(profile / "OneDriveConsumer"),
        "iCloudDrive": str(profile / "iCloudDrive"),
    }


def _patch_windows(monkeypatch, profile: Path):
    monkeypatch.setattr(file_operations, "_HOME", str(profile))
    monkeypatch.setattr(file_operations.sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(profile))
    for name in ("OneDrive", "OneDriveConsumer", "OneDrivePublic",
                 "iCloudDrive", "I_CLOUD_DRIVE_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OneDrive", str(profile / "OneDrive"))
    monkeypatch.setenv("OneDriveConsumer", str(profile / "OneDriveConsumer"))
    monkeypatch.setenv("iCloudDrive", str(profile / "iCloudDrive"))


def test_broad_profile_search_prunes_cloud_roots(tmp_path):
    profile = tmp_path / "Users" / "jeff"
    exclusions = _windows_cloud_search_exclusions(
        str(profile), cwd=str(tmp_path), env=_fake_env(profile), platform="win32"
    )
    lowered = {item.lower() for item in exclusions}
    expected = {"onedrive", "onedriveconsumer", "iclouddrive",
                "icloud photos", "pictures/icloud photos"}
    assert expected <= lowered
    for item in exclusions:
        assert "\\" not in item  # rg globs must be forward-slash


def test_explicit_cloud_root_search_is_not_pruned(tmp_path):
    profile = tmp_path / "Users" / "jeff"
    got = _windows_cloud_search_exclusions(
        str(profile / "OneDrive"), cwd=str(tmp_path),
        env=_fake_env(profile), platform="win32")
    assert got == []


def test_nested_search_under_cloud_root_is_not_pruned(tmp_path):
    profile = tmp_path / "Users" / "jeff"
    got = _windows_cloud_search_exclusions(
        str(profile / "OneDrive" / "work"), cwd=str(tmp_path),
        env=_fake_env(profile), platform="win32")
    assert got == []


def test_non_windows_search_has_no_cloud_exclusions(tmp_path):
    profile = tmp_path / "home" / "jeff"
    got = _windows_cloud_search_exclusions(
        str(profile), cwd=str(tmp_path), env=_fake_env(profile), platform="linux")
    assert got == []


def test_repo_root_search_outside_profile_has_no_exclusions(tmp_path):
    repo = tmp_path / "repos" / "app"
    got = _windows_cloud_search_exclusions(
        str(repo), cwd=str(repo),
        env=_fake_env(tmp_path / "Users" / "jeff"), platform="win32")
    assert got == []


def test_broad_file_search_passes_cloud_globs_to_ripgrep(tmp_path, monkeypatch):
    profile = tmp_path / "Users" / "jeff"
    profile.mkdir(parents=True)
    env = RecordingEnvironment(profile)
    ops = ShellFileOperations(env)
    _patch_windows(monkeypatch, profile)

    result = ops.search("*.txt", path=str(profile), target="files")

    rg_command = next(c for c in env.commands if c.startswith("rg --files"))
    lowered = rg_command.lower()
    assert "!onedrive/**" in lowered
    assert "!iclouddrive/**" in lowered
    assert "!pictures/icloud photos/**" in lowered
    assert result.warning is not None
    assert "hydration" in result.warning


def test_broad_content_search_passes_cloud_globs_to_ripgrep(tmp_path, monkeypatch):
    profile = tmp_path / "Users" / "jeff"
    profile.mkdir(parents=True)
    env = RecordingEnvironment(profile)
    ops = ShellFileOperations(env)
    _patch_windows(monkeypatch, profile)

    ops.search("needle", path=str(profile), target="content")

    rg_command = next(c for c in env.commands if c.startswith("set -o pipefail; rg"))
    assert "!onedrive/**" in rg_command.lower()


def test_remote_backend_never_prunes_cloud(tmp_path, monkeypatch):
    profile = tmp_path / "Users" / "jeff"
    profile.mkdir(parents=True)
    env = RecordingEnvironment(profile)
    env.is_local = False
    ops = ShellFileOperations(env)
    _patch_windows(monkeypatch, profile)

    result = ops.search("*.txt", path=str(profile), target="files")

    rg_command = next(c for c in env.commands if c.startswith("rg --files"))
    assert "!onedrive/**" not in rg_command.lower()
    assert result.warning is None


WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="cloud exclusion globs are Windows-spelled; forcing them "
           "cross-platform would test the OS, not the code",
)


@WINDOWS_ONLY
def test_real_ripgrep_does_not_descend_into_cloud_folder(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    profile = tmp_path / "Users" / "jeff"
    (profile / "OneDrive").mkdir(parents=True)
    (profile / "src").mkdir()
    (profile / "OneDrive" / "big.txt").write_text("needle\n")
    (profile / "src" / "a.txt").write_text("needle\n")
    _patch_windows(monkeypatch, profile)
    ops = ShellFileOperations(LocalEnvironment(cwd=str(profile)))

    result = ops.search("needle", path=str(profile), target="content")

    paths = [m.path.replace("\\", "/") for m in result.matches]
    assert any("src/a.txt" in p for p in paths)
    assert all("OneDrive" not in p for p in paths)
