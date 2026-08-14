"""Tests for gateway runtime code metadata."""

from types import SimpleNamespace

from gateway import run


def test_running_git_commit_returns_short_sha(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="abc1234\n"),
    )

    assert run._running_git_commit() == "abc1234"


def test_running_git_commit_soft_fails_without_git(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert run._running_git_commit() == "unknown"
