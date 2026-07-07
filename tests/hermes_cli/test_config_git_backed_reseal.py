"""Tests that save_config()/restore_config() keep the *git-backed* Config
Integrity Watchdog baseline in sync, not just the local ``.sha256`` sidecar.

Regression coverage for the bug where an authorized write through
``save_config()`` (model scanner, ``/model`` command, platform setup flows)
updated config.yaml and the local sidecar but left the external git-backed
baseline (``hermes config verify`` / config-integrity-watchdog skill) stale
— so the watchdog cron job flagged the legitimate change as tampering.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli.config import (
    _reseal_git_backed_integrity_baseline,
    get_config_path,
    restore_config,
    save_config,
)


def _init_dotfiles_repo(dotfiles: Path) -> None:
    hermes_dir = dotfiles / "hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(dotfiles)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=dotfiles, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=dotfiles, check=True, capture_output=True,
    )
    (dotfiles / "README.md").write_text("dotfiles\n")
    subprocess.run(["git", "add", "README.md"], cwd=dotfiles, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=dotfiles, check=True, capture_output=True)


def _log_path(dotfiles: Path) -> Path:
    return dotfiles / "hermes" / "config_integrity.jsonl"


def _seal_entries(dotfiles: Path) -> list[dict]:
    log_path = _log_path(dotfiles)
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


@pytest.fixture()
def hermes_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    dotfiles = tmp_path / "dotfiles"
    monkeypatch.setenv("HERMES_DOTFILES_DIR", str(dotfiles))
    return {"dotfiles": dotfiles}


class TestNoDotfilesConfigured:
    def test_save_config_is_a_noop_without_dotfiles_repo(self, hermes_env):
        """No watchdog configured on this machine -- save_config() must not error."""
        save_config({"model": {"default": "test-model"}})
        assert not hermes_env["dotfiles"].exists()


class TestGitBackedReseal:
    def test_save_config_seals_git_backed_baseline(self, hermes_env):
        _init_dotfiles_repo(hermes_env["dotfiles"])
        save_config({"model": {"default": "test-model"}})

        entries = _seal_entries(hermes_env["dotfiles"])
        assert len(entries) == 1
        assert entries[0]["event"] == "seal"

    def test_git_backed_baseline_matches_config_hash(self, hermes_env):
        import hashlib

        _init_dotfiles_repo(hermes_env["dotfiles"])
        save_config({"model": {"default": "test-model"}})

        expected = hashlib.sha256(get_config_path().read_bytes()).hexdigest()
        entries = _seal_entries(hermes_env["dotfiles"])
        assert entries[-1]["hash"] == expected

    def test_successive_saves_keep_baseline_current(self, hermes_env):
        """Simulates a scanner/automation making several legitimate writes --
        the watchdog baseline must track each one, not just the first."""
        _init_dotfiles_repo(hermes_env["dotfiles"])

        save_config({"model": {"default": "first"}})
        save_config({"model": {"default": "second"}})

        entries = _seal_entries(hermes_env["dotfiles"])
        assert len(entries) == 2

        import hashlib
        expected = hashlib.sha256(get_config_path().read_bytes()).hexdigest()
        assert entries[-1]["hash"] == expected

    def test_legitimate_write_does_not_trigger_watchdog_tamper(self, hermes_env):
        """End-to-end: after an authorized save_config() write, a `hermes
        config verify`-equivalent call against the git-backed log must pass
        without a manual `hermes config seal`."""
        from hermes_cli.config_integrity_cli import _find_core_module

        _init_dotfiles_repo(hermes_env["dotfiles"])
        save_config({"model": {"default": "scanner-picked-model"}})

        core = _find_core_module()
        assert core is not None
        rc = core.verify(config_path=get_config_path(), dotfiles_dir=hermes_env["dotfiles"])
        assert rc == 0

    def test_restore_config_also_reseals_git_backed_baseline(self, hermes_env):
        _init_dotfiles_repo(hermes_env["dotfiles"])
        save_config({"model": {"default": "original"}})
        restore_config({"model": {"default": "restored"}}, reason="test")

        from hermes_cli.config_integrity_cli import _find_core_module
        core = _find_core_module()
        rc = core.verify(config_path=get_config_path(), dotfiles_dir=hermes_env["dotfiles"])
        assert rc == 0

    def test_reseal_helper_swallows_git_failures(self, hermes_env, monkeypatch):
        """A broken/unavailable dotfiles git repo must never break the
        primary config write."""
        dotfiles = hermes_env["dotfiles"]
        dotfiles.mkdir(parents=True, exist_ok=True)  # exists but not a git repo
        # Should not raise even though `git` will fail inside it.
        save_config({"model": {"default": "test-model"}})
        _reseal_git_backed_integrity_baseline(get_config_path())
