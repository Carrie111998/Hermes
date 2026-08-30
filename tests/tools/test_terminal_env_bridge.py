"""Behavioral regressions for the terminal config → env bridge.

``terminal_tool._get_env_config()`` reads TERMINAL_* variables.  The bridge
must let explicitly configured terminal keys override stale launcher/.env
values while preserving environment values for terminal keys omitted from
config.yaml.
"""

import os

import pytest

import tools.terminal_tool as terminal_tool
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with an un-attempted bridge and clean mapped env."""
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_state", None)
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
    assert os.environ["TERMINAL_ENV"] == "docker"


def test_explicit_config_backend_overrides_stale_env(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert os.environ["TERMINAL_ENV"] == "docker"


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

    assert os.environ["TERMINAL_CWD"] == "~"
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
    assert os.environ["TERMINAL_ENV"] == "local"


def test_bridge_only_attempted_once(monkeypatch):
    calls = []

    import hermes_cli.config as config_mod

    real = config_mod.apply_terminal_config_to_env

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(config_mod, "apply_terminal_config_to_env", _counting)
    _write_config("{}\n")

    terminal_tool._get_env_config()
    terminal_tool._get_env_config()

    assert len(calls) == 1


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


def test_profile_switch_serves_each_profiles_terminal_backend(tmp_path):
    """A unified dashboard backend serves non-default profiles under a
    context-local HERMES_HOME override; each profile must execute commands
    with ITS configured terminal backend, not the launch profile's (#98581 —
    profiles with ``terminal.backend: docker`` ran on the host instead)."""
    launch_home = tmp_path / "root"
    docker_home = tmp_path / "root" / "profiles" / "web"
    docker_home.mkdir(parents=True)
    (launch_home / "config.yaml").write_text("terminal:\n  backend: local\n")
    (docker_home / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  docker_image: sandbox/image:1\n"
    )

    launch_token = set_hermes_home_override(str(launch_home))
    try:
        assert terminal_tool._get_env_config()["env_type"] == "local"

        profile_token = set_hermes_home_override(str(docker_home))
        try:
            config = terminal_tool._get_env_config()
        finally:
            reset_hermes_home_override(profile_token)

        assert config["env_type"] == "docker"
        assert config["docker_image"] == "sandbox/image:1"
        assert os.environ["TERMINAL_ENV"] == "docker"

        # Back on the launch profile: the profile's docker selection must
        # not stick, and the launch backend applies again.
        assert terminal_tool._get_env_config()["env_type"] == "local"
        assert os.environ["TERMINAL_ENV"] == "local"
    finally:
        reset_hermes_home_override(launch_token)


def test_profile_switch_drops_launch_terminal_selection(tmp_path):
    """TERMINAL_* vars owned by the launch profile's bridge must not survive a
    switch to a profile without a terminal section — the unconfigured profile
    falls back to the local default, not the launch profile's backend."""
    launch_home = tmp_path / "root"
    plain_home = tmp_path / "root" / "profiles" / "plain"
    plain_home.mkdir(parents=True)
    (launch_home / "config.yaml").write_text("terminal:\n  backend: docker\n")
    (plain_home / "config.yaml").write_text("agent:\n  max_turns: 100\n")

    launch_token = set_hermes_home_override(str(launch_home))
    try:
        assert terminal_tool._get_env_config()["env_type"] == "docker"
        assert os.environ["TERMINAL_ENV"] == "docker"

        profile_token = set_hermes_home_override(str(plain_home))
        try:
            config = terminal_tool._get_env_config()
        finally:
            reset_hermes_home_override(profile_token)

        assert config["env_type"] == "local"
        assert os.environ["TERMINAL_ENV"] == "local"
    finally:
        reset_hermes_home_override(launch_token)
