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




def _fake_official_ssh_git(args, *, cwd, timeout=5):
    if args == ["remote", "get-url", "origin"]:
        return "git@github.com:NousResearch/hermes-agent.git"
    if args == ["rev-parse", "HEAD"]:
        return "aaa111"
    raise AssertionError(f"unexpected git call: {args}")


def test_check_via_local_git_official_ssh_remote_returns_no_count_sentinel(tmp_path):
    """Official SSH origin + uncountable update ⇒ NO_COUNT sentinel, never a fake 1.

    FAIL-BEFORE: this branch mapped UPDATE_AVAILABLE_NO_COUNT to a literal `1`,
    which the banner and `hermes version` rendered as "1 commit behind" — the
    same lie the desktop UI told ("1 change included")."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()

    with patch.object(banner, "_git_stdout", side_effect=_fake_official_ssh_git), patch.object(
        banner, "_check_via_rev", return_value=banner.UPDATE_AVAILABLE_NO_COUNT
    ):
        assert banner._check_via_local_git(repo_dir) == banner.UPDATE_AVAILABLE_NO_COUNT


def test_check_via_local_git_official_ssh_remote_up_to_date_returns_zero(tmp_path):
    """Same SSH path with identical SHAs must keep reporting 0."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()

    with patch.object(banner, "_git_stdout", side_effect=_fake_official_ssh_git), patch.object(
        banner, "_check_via_rev", return_value=0
    ):
        assert banner._check_via_local_git(repo_dir) == 0


def test_print_version_info_unknown_count_shows_generic_update_line(capsys):
    """`hermes version` must say "Update available" — not "1 commit behind"."""
    from types import SimpleNamespace

    import hermes_cli.banner as banner
    import hermes_cli.main as main

    with patch(
        "hermes_cli.slash_exec.execute_command",
        return_value=SimpleNamespace(text="Hermes Agent v0.0.0"),
    ), patch("hermes_cli.config.detect_install_method", return_value="git"), patch.object(
        banner, "check_for_updates", return_value=banner.UPDATE_AVAILABLE_NO_COUNT
    ):
        main._print_version_info(check_updates=True)

    out = capsys.readouterr().out
    assert "Update available" in out
    assert "1 commit behind" not in out




