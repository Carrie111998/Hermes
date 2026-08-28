"""Behavioral regressions for profile-scoped terminal configuration.

``terminal_tool._get_env_config()`` reads TERMINAL_* variables.  The bridge
must let explicitly configured terminal keys override stale launcher/.env
values while preserving environment values for terminal keys omitted from
config.yaml.  It must never write one Desktop session's profile config into
the process environment used by another session.
"""

import os

import pytest

import tools.terminal_tool as terminal_tool
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with clean mapped process environment."""
    for name in (
        "TERMINAL_ENV",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_IMAGE",
        "TERMINAL_SSH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def test_unset_terminal_env_backfills_backend_from_config():
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: custom/image:1\n"
    )

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "custom/image:1"
    assert "TERMINAL_ENV" not in os.environ


def test_explicit_config_backend_overrides_stale_env(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert os.environ["TERMINAL_ENV"] == "local"


def test_partial_terminal_config_preserves_unrelated_env_values(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "env/image:2"
    assert os.environ["TERMINAL_DOCKER_IMAGE"] == "env/image:2"


def test_explicit_config_key_overrides_matching_env_value(monkeypatch):
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: config/image:1\n"
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "config/image:1"


def test_ssh_config_preserves_remote_tilde_cwd(monkeypatch):
    """SSH ``~`` belongs to the remote user, not the Hermes host/container."""
    _write_config("terminal:\n  backend: ssh\n  cwd: '~'\n")
    monkeypatch.setenv("HOME", "/opt/data/home")
    monkeypatch.setenv("USERPROFILE", r"C:\opt\data\home")

    config = terminal_tool._get_env_config()

    assert "TERMINAL_CWD" not in os.environ
    assert config["cwd"] == "~"


def test_env_is_preserved_when_config_has_no_terminal_section(monkeypatch):
    _write_config("agent:\n  max_turns: 100\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"


def test_defaults_backfill_when_neither_config_nor_env_selects_backend():
    _write_config("{}\n")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert "TERMINAL_ENV" not in os.environ


def test_profile_contexts_do_not_share_docker_settings(tmp_path):
    """A Desktop worker must read its own profile, not a prior session's."""
    home_a = tmp_path / "profiles" / "coder"
    home_b = tmp_path / "profiles" / "reviewer"
    (home_a / "config.yaml").parent.mkdir(parents=True)
    (home_b / "config.yaml").parent.mkdir(parents=True)
    (home_a / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  docker_image: coder/image:1\n"
        "  docker_volumes: ['~/coder:/workspace']\n"
    )
    (home_b / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  docker_image: reviewer/image:2\n"
        "  docker_volumes: ['~/reviewer:/workspace']\n"
    )

    token_a = set_hermes_home_override(home_a)
    try:
        coder = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token_a)
    token_b = set_hermes_home_override(home_b)
    try:
        reviewer = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token_b)

    assert coder["docker_image"] == "coder/image:1"
    assert coder["docker_volumes"] == ["~/coder:/workspace"]
    assert reviewer["docker_image"] == "reviewer/image:2"
    assert reviewer["docker_volumes"] == ["~/reviewer:/workspace"]
    assert "TERMINAL_DOCKER_IMAGE" not in os.environ


def test_bridge_config_failure_does_not_crash(monkeypatch):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config read failed")),
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"
