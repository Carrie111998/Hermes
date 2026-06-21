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

    assert state == {"mode": "branch", "upstream": "b2f477a3", "local": "af8aad31", "ahead": 3}


def test_format_banner_version_label_stable_tag_mode():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "mode": "stable-tags",
            "stable_tag": "v2026.5.16",
            "current_tag": "v2026.5.16",
            "local": "a91a57fa",
            "up_to_date": True,
        },
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· stable v2026.5.16")
    assert "origin/main" not in value
    assert "upstream" not in value


