"""Native Mattermost slash-command HTTP endpoint.

Mattermost's client swallows any message starting with ``/`` as an
unregistered slash command, so gateway commands typed in Mattermost never
reach the WebSocket. The adapter's slash-command listener receives the
HTTP callbacks of *registered* server-side slash commands and injects them
into the gateway message pipeline as native COMMAND events — landing in
the SAME session a regular channel message would.

Covered here:
1. Valid token + payload → 200 ephemeral ack + a COMMAND MessageEvent with
   the right platform/chat_id/user_id/text, keyed to the same session the
   WebSocket path would produce.
2. Bad token → 401, no event injected.
3. No MATTERMOST_SLASH_TOKENS → listener never binds (fail closed).
4. allowed_channels configured + disallowed channel → 403, no event.
5. Wrong method / path / content-type → 404.
6. connect → disconnect → connect rebinds the same port (no leak).
7. The HTTP ack returns without waiting for the agent pipeline.
"""

import asyncio
import json
import socket
import time
from urllib.parse import urlencode

import aiohttp
import pytest
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from gateway.session import build_session_key

BOT_USER_ID = "bot11111111111111111111111111"
BOT_USERNAME = "hermesbot"
CHANNEL_ID = "ch22222222222222222222222222"
USER_ID = "u3333333333333333333333333333"
USER_NAME = "alice"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _slash_payload(command="/sethome", text="", token="tokA", channel_id=CHANNEL_ID):
    return {
        "token": token,
        "team_id": "team9999999999999999999999999",
        "team_domain": "example",
        "channel_id": channel_id,
        "channel_name": "town-square",
        "user_id": USER_ID,
        "user_name": USER_NAME,
        "command": command,
        "text": text,
        "response_url": "https://mm.example.com/hooks/resp",
        "trigger_id": "trig7777777777777777777777777",
    }


class SlashAdapterHarness:
    """A MattermostAdapter with the Mattermost REST/WS side stubbed out.

    ``connect()`` runs for real (real HTTP listener, real session
    lifecycle); ``_api_get`` is stubbed for ``users/me`` + channel lookups
    and ``_ws_loop`` is a no-op so no outbound traffic happens. The message
    handler records every MessageEvent that reaches the gateway pipeline.
    """

    def __init__(self, monkeypatch, tokens="tokA,tokB", extra=None):
        from plugins.platforms.mattermost.adapter import MattermostAdapter

        self.port = _free_port()
        cfg_extra = {
            "url": "https://mm.example.com",
            "slash_command_host": "127.0.0.1",
            "slash_command_port": self.port,
        }
        cfg_extra.update(extra or {})
        config = PlatformConfig(enabled=True, token="test-token", extra=cfg_extra)
        self.adapter = MattermostAdapter(config)

        if tokens is None:
            monkeypatch.delenv("MATTERMOST_SLASH_TOKENS", raising=False)
        else:
            monkeypatch.setenv("MATTERMOST_SLASH_TOKENS", tokens)

        async def fake_api_get(path):
            if path == "users/me":
                return {"id": BOT_USER_ID, "username": BOT_USERNAME}
            if path.startswith("channels/"):
                return {
                    "id": path.split("/", 1)[1],
                    "type": "O",
                    "display_name": "Town Square",
                    "name": "town-square",
                }
            return {}

        self.adapter._api_get = fake_api_get
        self.adapter._ws_loop = AsyncMock()
        self.adapter.send_typing = AsyncMock()

        self.events = []
        self.event_arrived = asyncio.Event()

        async def handler(event):
            self.events.append(event)
            self.event_arrived.set()
            return None

        self.adapter.set_message_handler(handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def connect(self) -> bool:
        return await self.adapter.connect()

    async def wait_for_event(self, timeout=5.0):
        await asyncio.wait_for(self.event_arrived.wait(), timeout)
        return self.events[-1]


@pytest.fixture
def harness(monkeypatch):
    return SlashAdapterHarness(monkeypatch)


async def _post(harness, payload=None, *, path="/", headers=None, skip_ct=False, method="POST"):
    body = urlencode(payload if payload is not None else _slash_payload())
    kwargs = {"data": body.encode()}
    if skip_ct:
        kwargs["skip_auto_headers"] = ("Content-Type",)
    else:
        kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
    async with aiohttp.ClientSession() as session:
        requester = getattr(session, method.lower())
        async with requester(f"{harness.url}{path}", **kwargs) as resp:
            return resp.status, await resp.json()


class TestSlashCommandEndpoint:
    @pytest.mark.asyncio
    async def test_valid_token_injects_command_event(self, harness):
        """Valid token → 200 ephemeral ack + COMMAND event in the pipeline."""
        assert await harness.connect() is True

        status, body = await _post(harness, _slash_payload(command="/sethome"))
        assert status == 200
        assert body["response_type"] == "ephemeral"
        assert "/sethome" in body["text"]

        event = await harness.wait_for_event()
        assert event.message_type is MessageType.COMMAND
        assert event.text == "/sethome"
        assert event.source.platform is Platform.MATTERMOST
        assert event.source.chat_id == CHANNEL_ID
        assert event.source.user_id == USER_ID

        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_command_with_args_reconstructs_text(self, harness):
        """``command`` + ``text`` → "/status mattermost"."""
        assert await harness.connect() is True
        status, _ = await _post(harness, _slash_payload(command="/status", text="mattermost"))
        assert status == 200
        event = await harness.wait_for_event()
        assert event.text == "/status mattermost"
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_session_key_matches_ws_path(self, harness, monkeypatch):
        """The injected event keys into the SAME session as the WS path.

        Drives the real ``_handle_ws_event`` with an equivalent channel
        message (leading-space workaround: "␣/sethome") and compares the
        session keys both paths produce. require_mention is disabled so the
        WS message passes channel gating (the slash path bypasses it by
        design — explicit intent).
        """
        monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "false")
        assert await harness.connect() is True

        captured = []
        captured_evt = asyncio.Event()

        async def capture_handle_message(event):
            captured.append(event)
            captured_evt.set()

        monkeypatch.setattr(harness.adapter, "handle_message", capture_handle_message)

        ws_event = {
            "event": "posted",
            "data": {
                "channel_type": "O",
                "sender_name": f"@{USER_NAME}",
                "post": json.dumps({
                    "id": "post55555555555555555555555555",
                    "user_id": USER_ID,
                    "channel_id": CHANNEL_ID,
                    "message": " /sethome",
                    "root_id": "",
                    "file_ids": [],
                }),
            },
        }
        await harness.adapter._handle_ws_event(ws_event)
        assert len(captured) == 1  # WS path captured (handle_message patched)

        status, _ = await _post(harness, _slash_payload(command="/sethome"))
        assert status == 200
        await asyncio.wait_for(captured_evt.wait(), 5.0)
        assert len(captured) == 2  # slash path captured too

        ws_path_event = captured[0]
        slash_event = captured[1]

        ws_key = build_session_key(ws_path_event.source)
        slash_key = build_session_key(slash_event.source)
        assert slash_key == ws_key
        assert slash_event.source.chat_type == ws_path_event.source.chat_type
        assert slash_event.source.user_name == ws_path_event.source.user_name

        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_bad_token_returns_401_no_event(self, harness):
        assert await harness.connect() is True
        status, body = await _post(harness, _slash_payload(token="wrong-token"))
        assert status == 401
        assert body == {"error": "unauthorized"}
        await asyncio.sleep(0.15)  # no task is ever scheduled on 401
        assert harness.events == []
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_missing_token_returns_401_no_event(self, harness):
        assert await harness.connect() is True
        payload = _slash_payload()
        payload.pop("token")
        status, _ = await _post(harness, payload)
        assert status == 401
        await asyncio.sleep(0.15)
        assert harness.events == []
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_listener_disabled_without_tokens(self, monkeypatch):
        """No MATTERMOST_SLASH_TOKENS → connect still works, nothing binds."""
        h = SlashAdapterHarness(monkeypatch, tokens=None)
        assert await h.connect() is True
        assert h.adapter._slash_runner is None

        # Nothing is listening on the configured port.
        with pytest.raises(aiohttp.ClientConnectorError):
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    h.url, data=b"token=tokA", timeout=aiohttp.ClientTimeout(total=2)
                ):
                    pass
        await h.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_allowed_channels_rejects_disallowed(self, harness, monkeypatch):
        monkeypatch.setenv("MATTERMOST_ALLOWED_CHANNELS", "ch_allowed99999999999999999")
        assert await harness.connect() is True

        status, body = await _post(harness, _slash_payload(channel_id="ch_other8888888888888888"))
        assert status == 403
        assert body == {"error": "forbidden"}
        await asyncio.sleep(0.15)
        assert harness.events == []

        # An allowed channel still goes through.
        status, _ = await _post(harness, _slash_payload(channel_id="ch_allowed99999999999999999"))
        assert status == 200
        event = await harness.wait_for_event()
        assert event.source.chat_id == "ch_allowed99999999999999999"
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_wrong_method_path_or_content_type_404(self, harness):
        assert await harness.connect() is True

        async with aiohttp.ClientSession() as session:
            async with session.get(harness.url) as resp:
                assert resp.status == 404
            async with session.post(
                f"{harness.url}/nope", data=b"token=tokA"
            ) as resp:
                assert resp.status == 404
            async with session.post(
                f"{harness.url}/",
                data=json.dumps(_slash_payload()),
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 404

        await asyncio.sleep(0.15)
        assert harness.events == []
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_missing_content_type_tolerated(self, harness):
        """Mattermost always sends form content-type, but we tolerate none."""
        assert await harness.connect() is True
        status, _ = await _post(harness, skip_ct=True)
        assert status == 200
        event = await harness.wait_for_event()
        assert event.text == "/sethome"
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_connect_disconnect_connect_no_port_leak(self, harness):
        """Repeated lifecycle on the same port must not leak the bind."""
        assert await harness.connect() is True
        status, _ = await _post(harness, _slash_payload(command="/first"))
        assert status == 200
        await harness.wait_for_event()

        await harness.adapter.disconnect()
        assert harness.adapter._slash_runner is None

        # Reconnect on the SAME port (reconnect cycles happen in practice).
        assert await harness.connect() is True
        assert harness.adapter._slash_runner is not None
        status, _ = await _post(harness, _slash_payload(command="/second"))
        assert status == 200
        event = await harness.wait_for_event()
        assert event.text == "/second"
        await harness.adapter.disconnect()

    @pytest.mark.asyncio
    async def test_ack_does_not_wait_for_agent_pipeline(self, monkeypatch):
        """Mattermost enforces ~5s — the ack must not block on the agent."""
        h = SlashAdapterHarness(monkeypatch)
        pipeline_done = asyncio.Event()

        async def slow_handler(event):
            await asyncio.sleep(2.0)  # simulated agent turn
            pipeline_done.set()
            return None

        h.adapter.set_message_handler(slow_handler)
        assert await h.connect() is True

        started = time.monotonic()
        status, body = await _post(h, _slash_payload(command="/slowcmd"))
        elapsed = time.monotonic() - started

        assert status == 200
        assert body["response_type"] == "ephemeral"
        # The pipeline sleeps 2s; the ack must return far earlier.
        assert elapsed < 1.0

        # ...but the injection does eventually complete.
        await asyncio.wait_for(pipeline_done.wait(), 5.0)
        await h.adapter.disconnect()
