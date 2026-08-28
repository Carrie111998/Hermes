from unittest.mock import MagicMock, patch


def test_format_banner_version_label_on_origin_main():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={"origin": "b2f477a3", "local": "b2f477a3", "ahead": 0},
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· origin b2f477a3")
    assert "local" not in value


def test_get_git_banner_state_reads_origin_and_head(tmp_path):
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
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

    assert state == {"origin": "b2f477a3", "local": "af8aad31", "ahead": 3}


def test_check_via_local_git_uses_origin_for_ssh_checkouts(tmp_path):
    """An SSH origin is probed over HTTPS but remains the selected origin."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    origin_url = "git@github.com:mikexia930/hermes-agent.git"
    head_sha = "b" * 40
    target_sha = "a" * 40
    calls = []

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return origin_url
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args == ["rev-parse", "HEAD"]:
            return head_sha
        if args == ["rev-parse", "FETCH_HEAD"]:
            return target_sha
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner.subprocess, "run", side_effect=fake_run),
        patch.object(banner, "_github_compare_behind", return_value=3),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == 3
    assert calls[0][0:4] == ["git", "fetch", "https://github.com/mikexia930/hermes-agent", "main"]
    assert "refs/remotes/origin/main" in calls[0][-1]


def test_check_via_local_git_uses_remote_name_for_https_checkouts(tmp_path):
    """HTTPS origins should use the configured remote, not expose its URL."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    calls = []

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/mikexia930/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args == ["rev-parse", "HEAD"]:
            return "a" * 40
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="0\n")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner.subprocess, "run", side_effect=fake_run),
    ):
        assert banner._check_via_local_git(repo_dir) == 0

    assert calls[0] == ["git", "fetch", "origin", "main", "--quiet"]


def test_check_via_local_git_origin_compare_offline_keeps_sentinel(tmp_path):
    """Behind + compare API unreachable = honest no-count sentinel, never 1."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/mikexia930/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40
        if args == ["rev-parse", "FETCH_HEAD"]:
            return "a" * 40
        raise AssertionError(f"unexpected git call: {args}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=0)),
        patch.object(banner, "_github_compare_behind", return_value=None),
    ):
        behind = banner._check_via_local_git(repo_dir)

    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT
