"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

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


def test_check_via_local_git_fetch_failure_is_unknown(tmp_path, monkeypatch):
    """A stale origin/main cannot prove currentness after fetch fails (#82166)."""
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        raise AssertionError(f"stale refs must not be read after failed fetch: {args}")

    monkeypatch.setattr(banner, "_git_stdout", fake_git_stdout)
    monkeypatch.setattr(
        banner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert banner._check_via_local_git(repo_dir) is None


def test_check_for_updates_never_caches_unknown(tmp_path, monkeypatch):
    """A transient failure is retried on the next call, not hidden for six hours."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    (repo_dir / ".git").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_REVISION", raising=False)
    monkeypatch.setattr(banner, "__file__", str(tmp_path / "not-installed" / "banner.py"))
    monkeypatch.setattr(banner, "_check_via_local_git", lambda _repo: None)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
    monkeypatch.setattr("hermes_cli.config.get_project_root", lambda: repo_dir)

    assert banner.check_for_updates() is None
    assert not (tmp_path / ".update_check").exists()


def test_legacy_cached_unknown_is_ignored(tmp_path, monkeypatch):
    """Old releases may have persisted null; it must not suppress a live retry."""
    import hermes_cli.banner as banner
    from hermes_cli import __version__

    repo_dir = tmp_path / "hermes-agent"
    (repo_dir / ".git").mkdir(parents=True)
    (tmp_path / ".update_check").write_text(
        json.dumps({"ts": time.time(), "behind": None, "rev": None, "ver": __version__}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_REVISION", raising=False)
    monkeypatch.setattr(banner, "__file__", str(tmp_path / "not-installed" / "banner.py"))
    monkeypatch.setattr(banner, "_check_via_local_git", lambda _repo: 4)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
    monkeypatch.setattr("hermes_cli.config.get_project_root", lambda: repo_dir)

    assert banner.check_for_updates() == 4
    cached = json.loads((tmp_path / ".update_check").read_text(encoding="utf-8"))
    assert cached["behind"] == 4

