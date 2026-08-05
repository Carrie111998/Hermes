from unittest.mock import MagicMock, patch




def test_format_banner_version_label_on_upstream_main():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={"upstream": "b2f477a3", "local": "b2f477a3", "ahead": 0},
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· upstream b2f477a3")
    assert "local" not in value


def test_get_git_banner_state_reads_origin_and_head(tmp_path):
    """No upstream remote configured — falls back to origin/main, same as before."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): MagicMock(returncode=0, stdout="main\n"),
        ("git", "remote", "get-url", "upstream"): MagicMock(returncode=1, stdout=""),
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="3\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {"upstream": "b2f477a3", "local": "af8aad31", "ahead": 3}


def test_get_git_banner_state_prefers_upstream_when_configured(tmp_path):
    """Regression for the banner/--check disagreement: when an upstream
    remote exists on the default branch, the version-label suffix must
    compare against upstream/main, not the fork's origin/main."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): MagicMock(returncode=0, stdout="main\n"),
        ("git", "remote", "get-url", "upstream"): MagicMock(returncode=0, stdout="https://github.com/NousResearch/hermes-agent.git\n"),
        ("git", "rev-parse", "--short=8", "upstream/main"): MagicMock(returncode=0, stdout="deadbeef\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "upstream/main..HEAD"): MagicMock(returncode=0, stdout="1\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {"upstream": "deadbeef", "local": "af8aad31", "ahead": 1}


def test_get_git_banner_state_non_main_branch_uses_origin(tmp_path):
    """On a non-default branch, always compare against origin/<branch> — a
    fork's feature branch has no upstream counterpart to prefer."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): MagicMock(returncode=0, stdout="bb/gui\n"),
        # No "remote get-url upstream" probe expected — resolve_compare_ref
        # only checks it on the default branch.
        ("git", "rev-parse", "--short=8", "origin/bb/gui"): MagicMock(returncode=0, stdout="cafebabe\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "origin/bb/gui..HEAD"): MagicMock(returncode=0, stdout="2\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {"upstream": "cafebabe", "local": "af8aad31", "ahead": 2}


