"""ZIP fallback must refuse a dirty checkout instead of silently clobbering it.

Issue #91962: on Windows, ``_update_via_zip`` extracted a release archive
over the working tree and moved HEAD. Uncommitted edits were overwritten,
``git status`` went clean, and the update reported success. The git path
already aborts when it cannot stash; the ZIP path must match that floor.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd


def _porcelain_run(stdout: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "status" in joined and "--porcelain" in joined:
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_zip_overlay_allowed_without_git(tmp_path):
    assert update_cmd._zip_overlay_block_reason(tmp_path) is None


def test_zip_overlay_blocked_on_modified_file(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        update_cmd.subprocess, "run", _porcelain_run(" M hermes_cli/update_cmd.py\n")
    )
    reason = update_cmd._zip_overlay_block_reason(tmp_path)
    assert reason is not None
    assert "uncommitted" in reason


def test_zip_overlay_blocked_on_untracked_file(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(update_cmd.subprocess, "run", _porcelain_run("?? notes.md\n"))
    reason = update_cmd._zip_overlay_block_reason(tmp_path)
    assert reason is not None
    assert "untracked" in reason


def test_zip_overlay_blocked_when_git_status_fails(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        _porcelain_run("", returncode=128),
    )
    reason = update_cmd._zip_overlay_block_reason(tmp_path)
    assert reason is not None
    assert "could not check" in reason


def test_zip_overlay_allowed_on_clean_git_checkout(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(update_cmd.subprocess, "run", _porcelain_run(""))
    assert update_cmd._zip_overlay_block_reason(tmp_path) is None


def test_zip_status_asks_for_all_untracked_files(tmp_path, monkeypatch):
    """A user-level status.showUntrackedFiles=no must not blind the guard."""
    (tmp_path / ".git").mkdir()
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    assert update_cmd._zip_overlay_block_reason(tmp_path) is None
    assert any("--untracked-files=all" in cmd for cmd in seen)


def test_update_via_zip_aborts_before_download_when_dirty(
    tmp_path, monkeypatch, capsys
):
    """The live tree must not be touched, and the ZIP must not be fetched."""
    fake_root = tmp_path / "install"
    fake_root.mkdir()
    (fake_root / ".git").mkdir()
    local = fake_root / "keep-me.txt"
    local.write_text("local work\n", encoding="utf-8")
    untracked_dir = fake_root / "agent" / "scratch"
    untracked_dir.mkdir(parents=True)
    (untracked_dir / "wip.py").write_text("print('wip')\n", encoding="utf-8")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        _porcelain_run(" M keep-me.txt\n?? agent/scratch/wip.py\n"),
    )

    with patch("urllib.request.urlretrieve") as download:
        with pytest.raises(SystemExit) as exc_info:
            update_cmd._update_via_zip(SimpleNamespace(branch=None))

    assert exc_info.value.code == 1
    download.assert_not_called()
    assert local.read_text(encoding="utf-8") == "local work\n"
    assert (untracked_dir / "wip.py").read_text(encoding="utf-8") == "print('wip')\n"
    out = capsys.readouterr().out
    assert "ZIP fallback refused" in out
    assert "Commit, stash, or clean up your local changes" in out
    assert "Downloading latest version" not in out
