"""Real-path coverage for TUI ``reload.env`` SSH invalidation."""

import os

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tui_gateway import server


@pytest.fixture()
def isolated_reload_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    token = set_hermes_home_override(home)

    from hermes_cli.config import invalidate_env_cache
    import tools.terminal_tool as terminal_mod

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


def test_tui_reload_invalidates_only_ssh_environments(isolated_reload_state):
    home, terminal_mod = isolated_reload_state
    cleaned: list[str] = []
    ssh_env = object.__new__(terminal_mod._SSHEnvironment)
    ssh_env.cleanup = lambda: cleaned.append("ssh")
    docker_env = object()

    (home / ".env").write_text(
        "TERMINAL_SSH_HOST=new.example\nTERMINAL_SSH_PORT=2202\n",
        encoding="utf-8",
    )
    os.environ["TERMINAL_SSH_HOST"] = "old.example"
    os.environ["TERMINAL_SSH_PORT"] = "22"
    with terminal_mod._env_lock:
        terminal_mod._active_environments.update(
            {"ssh-task": ssh_env, "docker-task": docker_env}
        )
        terminal_mod._last_activity.update({"ssh-task": 1, "docker-task": 1})

    response = server.handle_request(
        {"id": "reload-1", "method": "reload.env", "params": {}}
    )

    assert response["result"] == {
        "updated": 2,
        "ssh_environments_cleared": 1,
    }
    assert cleaned == ["ssh"]
    assert "ssh-task" not in terminal_mod._active_environments
    assert terminal_mod._active_environments["docker-task"] is docker_env
