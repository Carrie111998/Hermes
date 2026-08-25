"""Tests for pre-model /senv — values must never appear in command output."""

from __future__ import annotations

import logging
import os

import pytest

from gateway.platforms.base import BasePlatformAdapter
from hermes_cli.senv import (
    DELETE_USER_MESSAGE_HINT,
    adapter_can_delete_user_message,
    parse_senv_args,
    redact_senv_args,
    redact_senv_hook_args,
    run_senv,
)


SECRET = "super-secret-booking-password-9f3"


@pytest.fixture
def senv_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: home)
    return home


def test_parse_rejects_invalid_keys_and_newlines():
    assert "error" in parse_senv_args("not a key")
    assert "error" in parse_senv_args("bad-key=value")
    assert "error" in parse_senv_args("KEY=line1\nline2")
    parsed = parse_senv_args('main BOOKING_PASSWORD="quoted value"')
    assert parsed["action"] == "set"
    assert parsed["key"] == "BOOKING_PASSWORD"
    assert parsed["value"] == "quoted value"


def test_redact_hides_assignment_values():
    redacted = redact_senv_args(f"main BOOKING_PASSWORD={SECRET}")
    assert SECRET not in redacted
    assert redacted.endswith("[redacted]")
    assert redact_senv_hook_args("senv", f"KEY={SECRET}") == "KEY=[redacted]"
    assert redact_senv_hook_args("model", "gpt-x") == "gpt-x"


def test_set_list_update_delete_main_never_echoes_value(senv_home, monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: senv_home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    first = run_senv(f"main BOOKING_PASSWORD={SECRET}", messenger=True)
    assert first.ok
    assert SECRET not in first.text
    assert first.leaked_secret == ""
    assert "BOOKING_PASSWORD" in first.text
    assert "active profile env" in first.text
    assert DELETE_USER_MESSAGE_HINT in first.text
    env_path = senv_home / ".env"
    assert env_path.is_file()
    stored = env_path.read_text()
    assert f"BOOKING_PASSWORD={SECRET}" in stored
    assert oct(env_path.stat().st_mode & 0o777) in {"0o600", "0o640"}

    listed = run_senv("list main")
    assert listed.ok
    assert "BOOKING_PASSWORD" in listed.text
    assert SECRET not in listed.text

    updated = run_senv(f"BOOKING_PASSWORD=second-{SECRET}")
    assert updated.ok
    assert SECRET not in updated.text
    assert f"second-{SECRET}" in env_path.read_text()
    assert env_path.read_text().count("BOOKING_PASSWORD=") == 1

    deleted = run_senv("delete main BOOKING_PASSWORD")
    assert deleted.ok
    assert SECRET not in deleted.text
    assert "BOOKING_PASSWORD" in deleted.text
    assert "BOOKING_PASSWORD=" not in env_path.read_text()


def test_skill_scope_writes_skill_env_not_profile(senv_home, monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: senv_home / ".env")
    skill_dir = senv_home / "skills" / "travel-manager"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# travel-manager\n")

    os.environ.pop("BOOKING_ACCOUNT_EMAIL", None)
    result = run_senv(f"skill travel-manager BOOKING_ACCOUNT_EMAIL={SECRET}")
    assert result.ok
    assert SECRET not in result.text
    assert (senv_home / ".env").exists() is False
    skill_env = skill_dir / ".env"
    assert f"BOOKING_ACCOUNT_EMAIL={SECRET}" in skill_env.read_text()
    assert os.environ.get("BOOKING_ACCOUNT_EMAIL") != SECRET

    missing = run_senv(f"skill no-such-skill KEY={SECRET}")
    assert not missing.ok
    assert SECRET not in missing.text


class _ConcreteAdapter(BasePlatformAdapter):
    def __init__(self):
        pass

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"name": "test", "type": "dm"}


def test_adapter_delete_fallback_when_unavailable():
    class _Override(_ConcreteAdapter):
        async def delete_message(self, chat_id, message_id):
            return True

    no_override = _ConcreteAdapter()
    override = _Override()

    assert adapter_can_delete_user_message(None) is False
    assert adapter_can_delete_user_message(object()) is False
    assert adapter_can_delete_user_message(no_override) is False
    assert adapter_can_delete_user_message(override) is True
    assert type(no_override).delete_message is BasePlatformAdapter.delete_message
    assert type(override).delete_message is not BasePlatformAdapter.delete_message


def test_run_senv_debug_logs_omit_secret(senv_home, monkeypatch, caplog):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: senv_home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.senv"):
        run_senv(f"main BOOKING_PASSWORD={SECRET}")
        run_senv("not a key")
    assert SECRET not in caplog.text


def test_pre_command_hook_redacts_senv(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    captured = {}

    class _FakeManager:
        def has_hook(self, name):
            return name == "pre_command"

        def invoke_hook(self, name, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: _FakeManager())
    plugins_mod.fire_pre_command_hook(
        surface="gateway",
        command="senv",
        alias_used="senv",
        args_raw=f"main BOOKING_PASSWORD={SECRET}",
    )
    assert SECRET not in captured["args_raw"]
    assert "[redacted]" in captured["args_raw"]
