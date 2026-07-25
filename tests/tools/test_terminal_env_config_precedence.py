"""Regression tests for issue #71137 — config.yaml terminal.backend precedence.

``TERMINAL_ENV`` (written to ``.env`` by ``hermes setup``) used to silently
override ``terminal.backend`` in config.yaml because ``_get_env_config``
read ``os.getenv("TERMINAL_ENV", "local")`` unconditionally.  When a user
set ``terminal.backend: local`` in config.yaml but ``.env`` still carried
``TERMINAL_ENV=ssh``, the stale env var won and the session bricked when
SSH was unreachable.

The fix (``_resolve_terminal_backend``) makes config.yaml authoritative:
when ``terminal.backend`` is explicitly set, it wins over ``TERMINAL_ENV``,
and a warning is logged so the override is visible.  ``TERMINAL_ENV`` only
applies when config.yaml doesn't pin a backend.
"""

import logging

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
    yield


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def test_config_backend_overrides_stale_terminal_env_ssh(monkeypatch, caplog):
    """#71137 core: config.yaml ``terminal.backend: local`` wins over a stale
    ``TERMINAL_ENV=ssh`` written to .env by ``hermes setup``."""
    _write_config("terminal:\n  backend: local\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    with caplog.at_level(logging.WARNING, logger="tools.terminal_tool"):
        config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    # The override must be surfaced — silent overrides are the bug.
    assert any(
        "terminal.backend" in rec.getMessage() and "overrides TERMINAL_ENV" in rec.getMessage()
        for rec in caplog.records
    )


def test_config_backend_overrides_env_for_docker_too(monkeypatch):
    """Config precedence holds regardless of the backend pair — docker in
    config wins over ssh in env."""
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"


def test_terminal_env_used_when_config_has_no_backend(monkeypatch):
    """When config.yaml has no ``terminal.backend``, TERMINAL_ENV still
    applies (fallback path unchanged)."""
    _write_config("terminal:\n  docker_image: img:1\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"


def test_terminal_env_used_when_no_terminal_section(monkeypatch):
    """No ``terminal`` section at all → TERMINAL_ENV / default wins."""
    _write_config("model:\n  name: test\n")
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"


def test_default_local_when_neither_set(monkeypatch):
    """Neither config.yaml nor TERMINAL_ENV → local (historical default)."""
    _write_config("{}\n")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"


def test_no_warning_when_config_and_env_agree(monkeypatch, caplog):
    """When config.yaml and TERMINAL_ENV agree, no override warning is
    logged — only disagreements are surfaced."""
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    with caplog.at_level(logging.WARNING, logger="tools.terminal_tool"):
        config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert not any(
        "overrides TERMINAL_ENV" in rec.getMessage() for rec in caplog.records
    )


def test_resolve_terminal_backend_helper_directly(monkeypatch):
    """``_resolve_terminal_backend`` returns config value when set."""
    _write_config("terminal:\n  backend: local\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    assert terminal_tool._resolve_terminal_backend() == "local"


def test_resolve_terminal_backend_falls_back_to_env(monkeypatch):
    """``_resolve_terminal_backend`` returns TERMINAL_ENV when config has no
    backend."""
    _write_config("terminal:\n  docker_image: img:1\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    assert terminal_tool._resolve_terminal_backend() == "ssh"


def test_resolve_terminal_backend_falls_back_to_local_default(monkeypatch):
    """``_resolve_terminal_backend`` returns 'local' when nothing is set."""
    _write_config("{}\n")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    assert terminal_tool._resolve_terminal_backend() == "local"


def test_resolve_terminal_backend_config_read_failure_falls_back(monkeypatch):
    """A broken config layer must not take the terminal tool down — fall back
    to TERMINAL_ENV / local."""

    def _boom(*_a, **_k):
        raise RuntimeError("config exploded")

    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "read_raw_config", _boom)
    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    # Should fall back to env, not raise.
    assert terminal_tool._resolve_terminal_backend() == "ssh"


def test_resolve_terminal_backend_empty_config_backend_uses_env(monkeypatch):
    """When ``terminal.backend`` is present but empty, fall back to
    TERMINAL_ENV rather than treating empty as authoritative."""
    _write_config("terminal:\n  backend: ''\n")
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    assert terminal_tool._resolve_terminal_backend() == "docker"