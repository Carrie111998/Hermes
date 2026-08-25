import subprocess
from types import SimpleNamespace

from hermes_cli import update_cmd


def test_official_ssh_remote_forms_are_detected():
    assert update_cmd._is_official_ssh_remote(
        "git@github.com:NousResearch/hermes-agent.git"
    )
    assert update_cmd._is_official_ssh_remote(
        "ssh://git@github.com/NousResearch/hermes-agent.git"
    )


def test_only_official_ssh_fetches_use_anonymous_https_override():
    git_cmd = ["git"]

    assert update_cmd._git_cmd_for_remote_fetch(
        git_cmd,
        "origin",
        "git@github.com:NousResearch/hermes-agent.git",
    ) == [
        "git",
        "-c",
        "remote.origin.url=https://github.com/NousResearch/hermes-agent.git",
    ]
    assert update_cmd._git_cmd_for_remote_fetch(
        git_cmd, "origin", "https://github.com/NousResearch/hermes-agent.git"
    ) == git_cmd
    assert update_cmd._git_cmd_for_remote_fetch(
        git_cmd, "origin", "git@github.com:example/hermes-agent.git"
    ) == git_cmd
    assert update_cmd._git_cmd_for_remote_fetch(
        git_cmd, "origin", "ssh://git@example.com/hermes-agent.git"
    ) == git_cmd


def test_official_ssh_remote_is_not_misclassified_as_fork():
    assert not update_cmd._is_fork(
        "ssh://git@github.com/NousResearch/hermes-agent.git"
    )


def test_active_update_fetch_uses_remote_fetch_helper():
    source = update_cmd._cmd_update_impl.__code__.co_consts
    assert any(
        isinstance(const, str) and "_git_cmd_for_remote_fetch" in const
        for const in source
    ) or "_git_cmd_for_remote_fetch" in update_cmd._cmd_update_impl.__code__.co_names


def test_update_check_fetches_official_ssh_origin_via_https(monkeypatch, tmp_path):
    repo = tmp_path / "hermes-agent"
    (repo / ".git").mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-3:] == ["rev-parse", "--is-shallow-repository"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        if cmd[-3:] == ["remote", "get-url", "upstream"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["rev-parse", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        if "rev-list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        update_cmd,
        "_m",
        lambda: SimpleNamespace(
            PROJECT_ROOT=repo,
            _get_origin_url=lambda *_args: (
                "git@github.com:NousResearch/hermes-agent.git"
            ),
        ),
    )
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda _root: "git"
    )
    monkeypatch.setattr("hermes_cli.config.is_nix_install_method", lambda _method: False)

    update_cmd._cmd_update_check()

    fetches = [cmd for cmd in calls if "fetch" in cmd]
    assert fetches == [
        [
            "git",
            "-c",
            "remote.origin.url=https://github.com/NousResearch/hermes-agent.git",
            "fetch",
            "origin",
            "main",
        ]
    ]
