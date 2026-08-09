"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5


def test_check_via_local_git_ownership_falls_back_to_ls_remote(tmp_path, monkeypatch):
    """Root-owned / non-fetchable checkouts must still report availability.

    Process-local safe.directory is used for read-only HEAD; fetch write
    failures fall back to ls-remote. Never treat this as up-to-date/null.
    """
    import hermes_cli.banner as banner

    repo = tmp_path / "install"
    repo.mkdir()
    (repo / ".git").mkdir()

    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    upstream = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def fake_run(args, **kwargs):
        # args is full argv including optional -c safe.directory=
        argv = list(args)
        # strip leading git [-c safe.directory=...]
        while argv and argv[0] == "git":
            argv = argv[1:]
        while len(argv) >= 2 and argv[0] == "-c":
            argv = argv[2:]

        class R:
            def __init__(self, rc=0, out="", err=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = err

        if argv[:2] == ["rev-parse", "HEAD"]:
            return R(0, head + "\n")
        if argv[:3] == ["remote", "get-url", "origin"]:
            return R(0, "https://github.com/NousResearch/hermes-agent.git\n")
        if argv[:2] == ["rev-parse", "--is-shallow-repository"]:
            return R(0, "false\n")
        if argv[:3] == ["fetch", "origin", "main"]:
            return R(1, "", "fatal: Unable to create temporary file: Permission denied\n")
        if argv[:2] == ["ls-remote", banner._UPSTREAM_REPO_URL]:
            return R(0, f"{upstream}\trefs/heads/main\n")
        if argv[:3] == ["rev-list", "--count", "HEAD..origin/main"]:
            return R(128, "", "fatal: bad revision\n")
        return R(128, "", "unexpected: " + " ".join(argv))

    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    behind, err, rev = banner._check_via_local_git(repo)
    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT
    assert err is None
    assert rev == head


def test_check_via_local_git_head_failure_is_error_not_zero(tmp_path, monkeypatch):
    """If even ownership-relaxed HEAD fails, return null + error_code."""
    import hermes_cli.banner as banner

    repo = tmp_path / "install"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_run(args, **kwargs):
        class R:
            returncode = 128
            stdout = ""
            stderr = (
                "fatal: detected dubious ownership in repository at "
                f"'{repo}'\n"
            )

        return R()

    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    behind, err, rev = banner._check_via_local_git(repo)
    assert behind is None
    assert err == "git-ownership"
    assert rev is None


def test_repo_install_writable_false_when_git_not_writable(tmp_path):
    from hermes_cli.banner import repo_install_writable

    repo = tmp_path / "install"
    git = repo / ".git"
    repo.mkdir()
    git.mkdir()
    git.chmod(0o555)
    repo.chmod(0o555)
    try:
        assert repo_install_writable(repo) is False
    finally:
        repo.chmod(0o755)
        git.chmod(0o755)

