"""WhatsApp self-chat owner command config and bridge propagation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _mock_disconnected_health():
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"status": "disconnected"})
    session = MagicMock()
    session.get.return_value = _AsyncContext(response)
    session.close = AsyncMock()
    return MagicMock(return_value=_AsyncContext(session))


def _seed_bridge(tmp_path):
    from plugins.platforms.whatsapp.adapter import _file_content_hash

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    bridge_script = bridge_dir / "bridge.js"
    bridge_script.write_text("// test bridge\n", encoding="utf-8")
    package_json = bridge_dir / "package.json"
    package_json.write_text('{"name":"test-bridge"}\n', encoding="utf-8")
    node_modules = bridge_dir / "node_modules"
    node_modules.mkdir()
    (node_modules / ".hermes-pkg-hash").write_text(
        _file_content_hash(package_json), encoding="utf-8"
    )
    session_path = tmp_path / "session"
    session_path.mkdir()
    (session_path / "creds.json").write_text("{}", encoding="utf-8")
    return bridge_script, session_path


async def _spawned_bridge_env(tmp_path, extra):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    bridge_script, session_path = _seed_bridge(tmp_path)
    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "bridge_script": str(bridge_script),
                "session_path": str(session_path),
                **extra,
            },
        )
    )
    process = MagicMock(pid=12345, returncode=1)
    process.poll.return_value = 1

    with patch(
        "plugins.platforms.whatsapp.adapter.check_whatsapp_requirements",
        return_value=True,
    ), patch(
        "aiohttp.ClientSession", _mock_disconnected_health()
    ), patch(
        "plugins.platforms.whatsapp.adapter.asyncio.sleep", new_callable=AsyncMock
    ), patch(
        "plugins.platforms.whatsapp.adapter._kill_stale_bridge_by_pidfile"
    ), patch(
        "plugins.platforms.whatsapp.adapter._kill_port_process"
    ), patch(
        "plugins.platforms.whatsapp.adapter._write_bridge_pidfile"
    ), patch(
        "subprocess.Popen", return_value=process
    ) as popen, patch.object(
        adapter, "_acquire_platform_lock", return_value=True, create=True
    ):
        await adapter.connect()

    return popen.call_args.kwargs["env"]


def test_owner_commands_config_reaches_whatsapp_adapter_extra(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "whatsapp:\n"
        "  owner_commands:\n"
        "    - foto\n"
        "    - status\n",
        encoding="utf-8",
    )

    with patch("gateway.config.get_hermes_home", return_value=tmp_path):
        with patch.dict("os.environ", {"WHATSAPP_ENABLED": "true"}, clear=False):
            from gateway.config import load_gateway_config

            config = load_gateway_config()

    assert config.platforms[Platform.WHATSAPP].extra["owner_commands"] == [
        "foto",
        "status",
    ]


@pytest.mark.asyncio
async def test_owner_commands_are_passed_only_in_the_internal_bridge_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("_HERMES_WHATSAPP_OWNER_COMMANDS", raising=False)

    env = await _spawned_bridge_env(
        tmp_path, {"owner_commands": ["foto", "status"]}
    )

    assert env["_HERMES_WHATSAPP_OWNER_COMMANDS"] == '["foto","status"]'


@pytest.mark.asyncio
async def test_absent_owner_commands_do_not_set_the_internal_bridge_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("_HERMES_WHATSAPP_OWNER_COMMANDS", '["stale"]')

    env = await _spawned_bridge_env(tmp_path, {})

    assert "_HERMES_WHATSAPP_OWNER_COMMANDS" not in env


def test_owner_command_payload_reaches_gateway_as_a_slash_command(monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")
    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True)
    adapter._dm_policy = "open"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._mention_patterns = []
    adapter._whatsapp_free_response_chats = lambda: set()
    payload = {
        "messageId": "M-COMMAND-1",
        "chatId": "6281234567890@s.whatsapp.net",
        "senderId": "6281234567890@s.whatsapp.net",
        "senderName": "Customer",
        "chatName": "Customer",
        "isGroup": False,
        "body": "/foto SKU-123",
        "hasMedia": False,
        "mediaType": "",
        "mediaUrls": [],
        "fromOwner": True,
        "ownerCommand": True,
    }

    event = asyncio.run(adapter._build_message_event(payload))

    assert event is not None
    assert event.text == "/foto SKU-123"
    assert event.is_command()
    assert event.metadata["whatsapp_from_owner"] is True
