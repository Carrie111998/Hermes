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


def test_get_git_banner_state_release_mode_uses_official_status(tmp_path):
    from hermes_cli import banner
    from hermes_cli import stable_update

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    status = {
        "error": None,
        "latest_tag": "v2026.7.20",
        "current_release_tag": None,
        "up_to_date": False,
    }

    official_status = MagicMock(return_value=status)
    with (
        patch.object(banner, "_load_update_check_settings", return_value=("release", {})),
        patch.object(banner, "_update_context", {"mode": "official-releases", **status}),
        patch.object(stable_update, "official_release_status", official_status),
        patch.object(banner, "_git_short_hash", return_value="af8aad31"),
    ):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {
        "mode": "release",
        "latest_tag": "v2026.7.20",
        "current_tag": None,
        "local": "af8aad31",
        "up_to_date": False,
        "error": None,
    }
    official_status.assert_not_called()


def test_get_git_banner_state_release_error_never_falls_through_to_branch(tmp_path):
    from hermes_cli import banner
    from hermes_cli import stable_update

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    official_status = MagicMock(return_value={"error": "offline"})
    with (
        patch.object(banner, "_load_update_check_settings", return_value=("release", {})),
        patch.object(banner, "_update_context", {"mode": "official-releases", "error": "offline"}),
        patch.object(stable_update, "official_release_status", official_status),
        patch.object(banner, "_git_short_hash", return_value="af8aad31") as short_hash,
    ):
        state = banner.get_git_banner_state(repo_dir)

    assert state is not None
    assert state["mode"] == "release"
    assert state["error"] == "offline"
    assert "upstream" not in state
    assert short_hash.call_count == 1
    official_status.assert_not_called()


def test_format_banner_version_label_release_mode_never_says_upstream():
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "mode": "release",
            "latest_tag": "v2026.7.20",
            "current_tag": None,
            "local": "af8aad31",
            "up_to_date": False,
        },
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· Release Track v2026.7.20 · local af8aad31")
    assert "upstream" not in value
    assert "commit" not in value


def test_official_release_notice_uses_release_command_and_units():
    from hermes_cli import banner

    value = banner._format_official_release_notice(
        {"mode": "official-releases", "target_tag": "v2026.7.20"},
        "hermes update",
    )

    assert "Release Track update v2026.7.20 available" in value
    assert "hermes update --release v2026.7.20" in value
    assert "commit" not in value
    assert "origin/main" not in value


