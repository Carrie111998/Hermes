"""Tests for the Telegram command-menu opt-out (#96025).

``platforms.telegram.extra.command_menu.enabled: false`` stops Hermes from
calling ``set_my_commands`` — every gateway restart otherwise overwrites the
user's BotFather customizations (e.g. localized command descriptions) with
the built-in English menu.
"""

from unittest.mock import patch

from hermes_cli.commands import telegram_menu_enabled


def _config_with(menu_cfg):
    def _read_raw_config():
        return {"platforms": {"telegram": {"extra": {"command_menu": menu_cfg}}}}

    return _read_raw_config


class TestTelegramMenuEnabled:
    def test_defaults_to_enabled(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={},
        ):
            assert telegram_menu_enabled() is True

    def test_explicit_false_opts_out(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            side_effect=_config_with({"enabled": False}),
        ):
            assert telegram_menu_enabled() is False

    def test_string_false_opts_out(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            side_effect=_config_with({"enabled": "false"}),
        ):
            assert telegram_menu_enabled() is False

    def test_other_menu_keys_do_not_disable(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            side_effect=_config_with({"max_commands": 30, "priority": ["new"]}),
        ):
            assert telegram_menu_enabled() is True
