"""Tests for the unified input-needed terminal alert."""
from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import cli as cli_module
from hermes_cli import config as config_module


def _set_enabled(monkeypatch, enabled: bool):
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"display": {"input_alert": enabled}},
    )


def test_notify_writes_osc9_and_bel_to_tty(monkeypatch):
    _set_enabled(monkeypatch, True)
    fake_tty = MagicMock()
    opened = mock_open()
    opened.return_value.__enter__.return_value = fake_tty

    with patch("cli.sys.stdout.isatty", return_value=True), patch("builtins.open", opened):
        cli_module._notify_input_needed("hello world")

    opened.assert_called_once_with("/dev/tty", "w", buffering=1, encoding="utf-8")
    assert [call.args[0] for call in fake_tty.write.call_args_list] == [
        "\x1b]9;hello world\x07",
        "\a",
    ]


def test_notify_disabled_via_config(monkeypatch):
    _set_enabled(monkeypatch, False)

    with patch("cli.sys.stdout.isatty", return_value=True), patch("builtins.open") as opened:
        cli_module._notify_input_needed("hello")

    opened.assert_not_called()


def test_notify_skipped_when_stdout_not_tty(monkeypatch):
    _set_enabled(monkeypatch, True)

    with patch("cli.sys.stdout.isatty", return_value=False), patch("builtins.open") as opened:
        cli_module._notify_input_needed("hello")

    opened.assert_not_called()


def test_notify_falls_back_to_stdout_when_tty_open_fails(monkeypatch):
    _set_enabled(monkeypatch, True)

    with (
        patch("cli.sys.stdout.isatty", return_value=True),
        patch("builtins.open", side_effect=OSError("no tty")),
        patch("cli.sys.stdout.write") as write,
        patch("cli.sys.stdout.flush") as flush,
    ):
        cli_module._notify_input_needed("hello")

    assert [call.args[0] for call in write.call_args_list] == [
        "\x1b]9;hello\x07",
        "\a",
    ]
    flush.assert_called_once_with()


def test_notify_fallback_bel_survives_osc_write_error(monkeypatch):
    _set_enabled(monkeypatch, True)
    write = MagicMock(side_effect=[OSError("OSC unsupported"), None])

    with (
        patch("cli.sys.stdout.isatty", return_value=True),
        patch("builtins.open", side_effect=OSError("no tty")),
        patch("cli.sys.stdout.write", write),
        patch("cli.sys.stdout.flush") as flush,
    ):
        cli_module._notify_input_needed("hello")

    assert write.call_args_list[1].args[0] == "\a"
    flush.assert_called_once_with()


def test_notify_reads_current_config_on_each_call(monkeypatch):
    enabled = False
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"display": {"input_alert": enabled}},
    )
    fake_tty = MagicMock()
    opened = mock_open()
    opened.return_value.__enter__.return_value = fake_tty

    with patch("cli.sys.stdout.isatty", return_value=True), patch("builtins.open", opened):
        cli_module._notify_input_needed("first")
        enabled = True
        cli_module._notify_input_needed("second")

    opened.assert_called_once()


def test_notify_sanitizes_control_chars(monkeypatch):
    _set_enabled(monkeypatch, True)
    fake_tty = MagicMock()
    opened = mock_open()
    opened.return_value.__enter__.return_value = fake_tty

    with patch("cli.sys.stdout.isatty", return_value=True), patch("builtins.open", opened):
        cli_module._notify_input_needed("hello\x1b[2J\x07world\x00")

    payload = fake_tty.write.call_args_list[0].args[0]
    body = payload.removeprefix("\x1b]9;").removesuffix("\x07")
    assert body == "hello[2Jworld"


def test_slash_confirmation_alerts_before_no_app_fallback():
    cli = object.__new__(cli_module.HermesCLI)
    cli._app = None
    cli._prompt_text_input = MagicMock(return_value="1")

    with patch("cli._notify_input_needed") as notify:
        result = cli._prompt_text_input_modal(
            title="Reset session",
            detail="Continue?",
            choices=[("1", "yes", "Yes")],
        )

    assert result == "1"
    notify.assert_called_once_with("Hermes: Reset session")


def test_slash_confirmation_alerts_before_scheduling_failure_fallback():
    cli = object.__new__(cli_module.HermesCLI)
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = RuntimeError("closed")
    cli._app = MagicMock(loop=loop)
    cli._invalidate = MagicMock()
    cli._prompt_text_input = MagicMock(return_value="1")

    with (
        patch("cli.threading.current_thread", return_value=object()),
        patch("cli.threading.main_thread", return_value=object()),
        patch("cli._notify_input_needed") as notify,
    ):
        result = cli._prompt_text_input_modal(
            title="Reset session",
            detail="Continue?",
            choices=[("1", "yes", "Yes")],
        )

    assert result == "1"
    notify.assert_called_once_with("Hermes: Reset session")


def test_windows_off_main_thread_cancellation_does_not_alert():
    cli = object.__new__(cli_module.HermesCLI)
    cli._app = MagicMock(loop=None)
    cli._invalidate = MagicMock()
    cli._prompt_text_input = MagicMock()

    with (
        patch("cli.threading.current_thread", return_value=object()),
        patch("cli.threading.main_thread", return_value=object()),
        patch("cli.sys.platform", "win32"),
        patch("cli._notify_input_needed") as notify,
    ):
        result = cli._prompt_text_input_modal(
            title="Reset session",
            detail="Continue?",
            choices=[("1", "yes", "Yes")],
        )

    assert result is None
    notify.assert_not_called()
