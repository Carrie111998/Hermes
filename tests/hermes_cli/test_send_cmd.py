"""Tests for the ``hermes send`` CLI subcommand.

Covers the argument parsing / stdin / file / list behavior of
``hermes_cli.send_cmd``. The underlying ``send_message_tool`` is stubbed so
no network I/O or gateway is required.
"""

from __future__ import annotations

import io
import json
import sys
import types

import pytest

from hermes_cli import send_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(argv):
    """Build the top-level parser and return the parsed args for ``argv``."""
    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    send_cmd.register_send_subparser(subparsers)
    return parser.parse_args(["send", *argv])


class _FakeTool:
    """Replacement for ``tools.send_message_tool.send_message_tool``."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, args, **_kw):
        self.calls.append(dict(args))
        return json.dumps(self.payload)


@pytest.fixture
def fake_tool(monkeypatch):
    """Install a fake send_message_tool and return the stub for inspection."""
    fake = _FakeTool({"success": True, "message_id": "m123"})

    mod = types.ModuleType("tools.send_message_tool")
    mod.send_message_tool = fake
    # Register the stub so ``from tools.send_message_tool import ...`` inside
    # cmd_send resolves to our fake. Also patch the parent ``tools`` package
    # entry so attribute lookup works.
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", mod)
    return fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_real_discord_command_uses_discovered_standalone_sender_and_exact_id(
    monkeypatch,
    capsys,
    tmp_path,
):
    """Exercise CLI -> tool -> discovered plugin -> Discord HTTP boundary.

    This is the installed command contract that previously returned
    ``No live adapter ... standalone_sender_fn`` when the sealed wheel omitted
    Discord's plugin manifest.  The HTTP boundary is mocked; the registry and
    command routing are real.
    """
    token = "test-discord-token-must-not-leak"
    channel_id = "123456789012345678"
    message_id = "987654321098765432"
    requests = []

    class _Response:
        status = 200

        async def json(self):
            return {"id": message_id}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return _Response()

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # A production Muncho gateway must use its privileged relay.  This test is
    # specifically the generic upstream standalone command contract.
    from plugins.platforms.discord import adapter as discord_adapter
    from tools import send_message_tool as send_tool_module

    monkeypatch.setattr(
        discord_adapter,
        "_discord_public_only_policy_required",
        lambda: False,
    )
    monkeypatch.setattr(
        send_tool_module,
        "_discord_privileged_connector_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "gateway.channel_directory.is_discord_public_target",
        lambda _target: True,
    )
    monkeypatch.setattr(
        "gateway.channel_directory.lookup_channel_type",
        lambda _platform, _target: "channel",
    )
    monkeypatch.setitem(
        sys.modules,
        "gateway.run",
        types.SimpleNamespace(_gateway_runner_ref=lambda: None),
    )
    monkeypatch.setattr("aiohttp.ClientSession", _Session)

    args = _parse(["--to", f"discord:{channel_id}", "--json", "release ready"])
    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exc.value.code == 0
    assert payload["success"] is True
    assert payload["message_id"] == message_id
    assert requests == [
        (
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            {
                "headers": {
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                },
                "json": {"content": "release ready"},
            },
        )
    ]
    assert token not in captured.out
    assert token not in captured.err










# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------




def test_file_decode_error_suggests_media_directive(fake_tool, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    bad = tmp_path / "bad-bytes.bin"
    bad.write_bytes(b"\xff\xfe\x00")

    args = _parse(["--to", "telegram", "--file", str(bad)])
    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not a text file" in err.lower()
    assert f"MEDIA:{bad}" in err
    assert "[[as_document]]" in err






# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------


def test_list_includes_configured_platform_without_discovered_channels(
    monkeypatch, capsys
):
    """A configured platform absent from the channel directory must still be
    listed (with a no-channels hint) instead of silently omitted."""
    import types
    import sys

    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    class _FakeGwConfig:
        def get_connected_platforms(self):
            return [_FakePlatform("simplex")]

    fake_gw_config = types.ModuleType("gateway.config")
    fake_gw_config.load_gateway_config = lambda: _FakeGwConfig()
    monkeypatch.setitem(sys.modules, "gateway.config", fake_gw_config)

    fake_dir = types.ModuleType("gateway.channel_directory")
    fake_dir.load_directory = lambda: {"updated_at": None, "platforms": {}}

    def _format(platforms=None):
        lines = []
        for name, channels in sorted((platforms or {}).items()):
            lines.append(f"{name}:")
            if not channels:
                lines.append("  (no channels discovered yet)")
        return "\n".join(lines)

    fake_dir.format_directory_for_display = _format
    monkeypatch.setitem(sys.modules, "gateway.channel_directory", fake_dir)

    rc = send_cmd._list_targets(None, json_mode=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "simplex" in out
    assert "no channels discovered yet" in out


def test_list_json_includes_configured_platform(monkeypatch, capsys):
    import types
    import sys

    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    class _FakeGwConfig:
        def get_connected_platforms(self):
            return [_FakePlatform("simplex"), _FakePlatform("local")]

    fake_gw_config = types.ModuleType("gateway.config")
    fake_gw_config.load_gateway_config = lambda: _FakeGwConfig()
    monkeypatch.setitem(sys.modules, "gateway.config", fake_gw_config)

    fake_dir = types.ModuleType("gateway.channel_directory")
    fake_dir.load_directory = lambda: {
        "updated_at": None,
        "platforms": {"telegram": [{"id": "1", "name": "home"}]},
    }
    fake_dir.format_directory_for_display = lambda platforms=None: ""
    monkeypatch.setitem(sys.modules, "gateway.channel_directory", fake_dir)

    rc = send_cmd._list_targets(None, json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["platforms"]["simplex"] == []
    assert "local" not in payload["platforms"]  # infra pseudo-platform skipped
    assert payload["platforms"]["telegram"]  # discovered entries preserved


# ---------------------------------------------------------------------------
# Parser registration contract
# ---------------------------------------------------------------------------


def test_register_send_subparser_is_reusable():
    """Sanity check: the registrar returns a parser and wires ``cmd_send``."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    send_parser = send_cmd.register_send_subparser(subparsers)
    assert send_parser is not None
    args = parser.parse_args(["send", "--to", "telegram", "hi"])
    assert args.func is send_cmd.cmd_send
    assert args.to == "telegram"
    assert args.message == "hi"


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------


def test_load_hermes_env_bridges_config_yaml_scalars(tmp_path, monkeypatch):
    """Top-level config.yaml scalars should be bridged into os.environ.

    This mirrors the gateway/run.py bootstrap behavior: without this, running
    ``hermes send`` from a fresh shell cannot resolve the home channel
    because ``TELEGRAM_HOME_CHANNEL`` (saved by ``hermes config set``) lives
    in config.yaml, not in .env — and the gateway's config loader reads via
    ``os.getenv(...)``.
    """
    import os

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("SOME_TOKEN=abc123\n")
    (hermes_home / "config.yaml").write_text(
        "TELEGRAM_HOME_CHANNEL: '5550001111'\nnested:\n  ignored: true\n"
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("SOME_TOKEN", raising=False)

    # Force get_hermes_home() to re-resolve under the patched env.
    from importlib import reload

    import hermes_cli.config as _hc_config
    reload(_hc_config)

    send_cmd._load_hermes_env()

    assert os.environ.get("SOME_TOKEN") == "abc123"
    assert os.environ.get("TELEGRAM_HOME_CHANNEL") == "5550001111"

