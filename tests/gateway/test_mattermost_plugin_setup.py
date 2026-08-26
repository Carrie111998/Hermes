"""Tests for the Mattermost plugin's interactive_setup wizard home-channel flow.

The interactive_setup wizard lazy-imports its CLI helpers from
``hermes_cli.config`` (get_env_value / save_env_value / remove_env_value) and
``hermes_cli.cli_output`` (prompt / prompt_yes_no / print_*); we patch those
source modules. Covers the home-channel clear-on-blank behavior added in
PR #58421 and extended in the follow-up.
"""
import json
from unittest.mock import AsyncMock

import pytest
import hermes_cli.config as config_mod
import hermes_cli.cli_output as cli_output_mod

from gateway.platforms.base import MessageType
from plugins.platforms.mattermost.adapter import _apply_yaml_config, interactive_setup


def test_command_prefix_flows_from_yaml_to_platform_extras():
    assert _apply_yaml_config({}, {"command_prefix": " ? "}) == {
        "command_prefix": "?"
    }


def test_command_prefix_can_be_disabled_in_yaml():
    assert _apply_yaml_config({}, {"command_prefix": ""}) == {
        "command_prefix": ""
    }


def test_gateway_loader_applies_command_prefix_extra(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        'mattermost:\n  command_prefix: "?"\n', encoding="utf-8"
    )

    from gateway.config import Platform, load_gateway_config
    from plugins.platforms.mattermost.adapter import MattermostAdapter

    config = load_gateway_config()
    platform_config = config.platforms[Platform.MATTERMOST]
    assert platform_config.extra["command_prefix"] == "?"
    assert MattermostAdapter(platform_config).typed_command_prefix == "?"


@pytest.mark.asyncio
async def test_disabled_prefix_survives_loader_and_real_event(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        'mattermost:\n  command_prefix: ""\n', encoding="utf-8"
    )

    from gateway.config import Platform, load_gateway_config
    from plugins.platforms.mattermost.adapter import MattermostAdapter

    config = load_gateway_config()
    adapter = MattermostAdapter(config.platforms[Platform.MATTERMOST])
    assert adapter._command_prefix == ""
    assert adapter.typed_command_prefix == "/"

    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    await adapter._handle_ws_event(
        {
            "event": "posted",
            "data": {
                "post": json.dumps(
                    {
                        "id": "post_disabled_prefix",
                        "user_id": "user_123",
                        "channel_id": "chan_dm",
                        "message": "!new",
                    }
                ),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }
    )

    event = adapter.handle_message.call_args[0][0]
    assert event.text == "!new"
    assert event.message_type is MessageType.TEXT


def _patch_setup_io(monkeypatch, prompts, saved, removed, existing):
    prompt_iter = iter(prompts)
    monkeypatch.setattr(config_mod, "get_env_value", lambda key: existing.get(key, ""))
    monkeypatch.setattr(config_mod, "save_env_value", lambda k, v: saved.update({k: v}))

    def _remove(key):
        removed.append(key)
        return existing.pop(key, None) is not None

    monkeypatch.setattr(config_mod, "remove_env_value", _remove)
    monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(prompt_iter))
    monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: False)
    for name in ("print_header", "print_info", "print_success", "print_warning"):
        monkeypatch.setattr(cli_output_mod, name, lambda *_a, **_kw: None)


# Mattermost prompts: server_url, bot_token (password), allowed_users, home_channel.
_PROMPTS_NONEMPTY = ["https://mm.example.com", "«redacted:mm-token»", "", "town-square-id"]
_PROMPTS_BLANK = ["https://mm.example.com", "«redacted:mm-token»", "", ""]
_PROMPTS_WHITESPACE = ["https://mm.example.com", "«redacted:mm-token»", "", "   "]


class TestMattermostHomeChannelClear:
    """Blank home-channel answer must clear MATTERMOST_HOME_CHANNEL (#12423)."""

    def test_blank_removes_existing_home_channel(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        saved, removed = {}, []
        _patch_setup_io(
            monkeypatch,
            _PROMPTS_BLANK,
            saved,
            removed,
            existing={"MATTERMOST_HOME_CHANNEL": "old-channel-id"},
        )
        interactive_setup()
        assert "MATTERMOST_HOME_CHANNEL" in removed
        assert "MATTERMOST_HOME_CHANNEL" not in saved


