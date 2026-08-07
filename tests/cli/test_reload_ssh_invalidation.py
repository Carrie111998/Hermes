"""Real-path tests for SSH cache invalidation on classic ``/reload``."""

import os
from unittest.mock import MagicMock

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture()
def isolated_reload_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    token = set_hermes_home_override(home)

    from hermes_cli.config import invalidate_env_cache
    from tools import terminal_tool as terminal_mod

    invalidate_env_cache()
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_PORT", raising=False)
    with terminal_mod._env_lock:
        terminal_mod._active_environments.clear()
        terminal_mod._last_activity.clear()

    try:
        yield home, terminal_mod
    finally:
        with terminal_mod._env_lock:
            terminal_mod._active_environments.clear()
            terminal_mod._last_activity.clear()
        invalidate_env_cache()
        reset_hermes_home_override(token)


def _make_cli_instance():
    from cli import HermesCLI

    cli = MagicMock(spec=HermesCLI)
    cli.process_command = HermesCLI.process_command.__get__(cli)
    return cli


def _uncached_ssh_environment(cleaned: list[str]):
    from tools.terminal_tool import _SSHEnvironment

    env = object.__new__(_SSHEnvironment)
    env.cleanup = lambda: cleaned.append("ssh")
    return env


def test_classic_reload_invalidates_only_ssh_environments(
    isolated_reload_state, capsys
):
    home, terminal_mod = isolated_reload_state
    cleaned: list[str] = []
    ssh_env = _uncached_ssh_environment(cleaned)
    local_env = object()
    cli = _make_cli_instance()

    (home / ".env").write_text(
        "TERMINAL_SSH_HOST=new.example\nTERMINAL_SSH_PORT=2202\n",
        encoding="utf-8",
    )
    os.environ["TERMINAL_SSH_HOST"] = "old.example"
    os.environ["TERMINAL_SSH_PORT"] = "22"
    with terminal_mod._env_lock:
        terminal_mod._active_environments.update(
            {"ssh-task": ssh_env, "local-task": local_env}
        )
        terminal_mod._last_activity.update({"ssh-task": 1, "local-task": 1})

    cli.process_command("/reload")

    assert os.environ["TERMINAL_SSH_HOST"] == "new.example"
    assert os.environ["TERMINAL_SSH_PORT"] == "2202"
    assert cleaned == ["ssh"]
    assert "ssh-task" not in terminal_mod._active_environments
    assert terminal_mod._active_environments["local-task"] is local_env
    assert "Cleared 1 cached SSH environment(s)" in capsys.readouterr().out


def test_classic_reload_keeps_cached_ssh_when_settings_do_not_change(
    isolated_reload_state,
):
    home, terminal_mod = isolated_reload_state
    cleaned: list[str] = []
    ssh_env = _uncached_ssh_environment(cleaned)
    cli = _make_cli_instance()

    (home / ".env").write_text(
        "TERMINAL_SSH_HOST=same.example\nTERMINAL_SSH_PORT=22\n",
        encoding="utf-8",
    )
    os.environ["TERMINAL_SSH_HOST"] = "same.example"
    os.environ["TERMINAL_SSH_PORT"] = "22"
    with terminal_mod._env_lock:
        terminal_mod._active_environments["ssh-task"] = ssh_env
        terminal_mod._last_activity["ssh-task"] = 1

    cli.process_command("/reload")

    assert terminal_mod._active_environments["ssh-task"] is ssh_env
    assert cleaned == []
