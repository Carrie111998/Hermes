"""Regression tests for the terminal config → env fallback bridge.

``terminal_tool._get_env_config()`` reads all settings from TERMINAL_* env
vars, which the CLI / gateway / TUI-PTY launchers bridge from config.yaml at
startup. Processes that skip every launcher bridge (``hermes serve`` and the
Desktop app's in-process agents, the desktop cron ticker, ACP) used to fall
back silently to the local backend even when config.yaml selected
``terminal.backend: docker`` — commands the user intended to sandbox ran on
the host (#63141 / #54449 / #61115 / #65696).

``_ensure_terminal_env_bridged()`` closes that hole at the chokepoint:
when config.yaml has a ``terminal`` section, its values are authoritative
and override any existing TERMINAL_* env vars (including stale .env values).
When config.yaml has NO terminal section, only missing env vars are
backfilled so explicit shell exports and .env values keep working (#71137).
"""

import os

import pytest

import tools.terminal_tool as terminal_tool
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with an un-attempted bridge and no TERMINAL_ENV."""
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_IMAGE", raising=False)
    # The config layer caches by (path, mtime, size); leave it alone — each
    # test writes its own config.yaml which changes the signature.
    yield


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_unset_terminal_env_backfills_backend_from_config():
    """The core #63141 fix: config's docker backend reaches _get_env_config
    even when no launcher bridged TERMINAL_ENV into this process."""
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: custom/image:1\n"
    )

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "custom/image:1"
    assert os.environ.get("TERMINAL_ENV") == "docker"


def test_config_terminal_section_overrides_env(monkeypatch):
    """config.yaml terminal section is authoritative over env/.env values
    (aligned with CLI _file_has_terminal_config and gateway bridge)."""
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"


def test_config_terminal_section_overrides_sibling_vars(monkeypatch):
    """When config.yaml has a terminal section, its values override
    already-set sibling TERMINAL_* env vars (consistent with CLI path)."""
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: config/image:1\n"
    )
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "config/image:1"


def test_bridge_failure_falls_back_to_local(monkeypatch):
    """A broken config layer must not take the terminal tool down."""

    def _boom(*_a, **_k):
        raise RuntimeError("config exploded")

    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "apply_terminal_config_to_env", _boom)

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"


def test_bridge_only_attempted_once(monkeypatch):
    """The config load runs at most once per process when TERMINAL_ENV stays
    unset (e.g. empty config) — later calls skip the bridge entirely."""
    calls = []

    import hermes_cli.config as config_mod

    real = config_mod.apply_terminal_config_to_env

    def _counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(config_mod, "apply_terminal_config_to_env", _counting)
    _write_config("{}\n")

    terminal_tool._get_env_config()
    terminal_tool._get_env_config()

    assert len(calls) == 1


def test_stale_env_overridden_by_config_backend(monkeypatch):
    """#71137: .env TERMINAL_ENV=ssh must not override config.yaml
    terminal.backend=local on the fallback bridge path."""
    _write_config("terminal:\n  backend: local\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "1.2.3.4")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"


def test_no_terminal_section_env_wins(monkeypatch):
    """Without a terminal section in config.yaml, env values are kept
    (backfill-only mode preserves explicit shell exports and .env)."""
    _write_config("model:\n  default: some-model\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
