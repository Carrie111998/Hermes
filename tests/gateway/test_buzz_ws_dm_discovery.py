"""Periodic DM discovery on the Buzz WebSocket transport (#93557).

Some relays never emit a kind-44100 membership event for a fresh DM, and
the WS loop relied on those events alone — a conversation opened while the
gateway was running stayed invisible (nothing subscribed, nothing
dispatched) until the next WS reconnect. The poll loop already re-runs
discovery every _DM_DISCOVERY_EVERY sweeps; the WS loop now runs the same
sweep at the same cadence by bounding each receive with the remaining time
to the next sweep.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_buzz_mod = load_plugin_adapter("buzz")
BuzzAdapter = _buzz_mod.BuzzAdapter

SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
TEST_PRIVATE_KEY = "00" * 31 + "03"
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
NEW_DM = "ddd2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "relay_url": "https://test.relay",
            "poll_interval": 0.05,  # discovery every 5 * 0.05s = 0.25s
            **(extra or {}),
        },
    )
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._private_key = TEST_PRIVATE_KEY
    adapter._display_name = "Chip"
    return adapter


class _QuietWebSocket:
    """A websocket whose frame stream never yields and records sends."""

    def __init__(self):
        self.sent = []
        self._done = asyncio.Event()

    async def recv(self):
        await self._done.wait()
        raise ConnectionError("test shutdown")

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._done.wait()
        raise ConnectionError("test shutdown")


@pytest.mark.asyncio
async def test_ws_loop_discovers_midsession_dm_without_membership_events(monkeypatch):
    """A DM opened mid-session is discovered and subscribed even when the
    relay emits no kind-44100 membership event for it (#93557)."""
    adapter = _make_adapter()
    adapter.poll_interval = 0.2  # discovery every 5 * 0.2s = 1s in the test
    ws = _QuietWebSocket()
    discovery_calls = []

    async def fake_discover_dms(*, seed):
        discovery_calls.append(seed)
        if len(discovery_calls) >= 2:
            # Mid-session shape: first sweep at connect found nothing new;
            # by the second sweep the user opened a DM.
            adapter._channel_state[NEW_DM] = {
                "chat_type": "dm", "last_ts": 0, "seen": {},
            }

    async def fake_auth(websocket):
        return None

    async def fake_subscribe(websocket):
        # Watched-channel + membership subscription map at connect time.
        return {"hermes-buzz-0": CHANNEL, _buzz_mod._WS_MEMBERSHIP_SUB_ID: None}

    async def fake_send_sub(websocket, sub_id, channel_id):
        ws.sent.append(["REQ", sub_id, channel_id])

    monkeypatch.setattr(adapter, "_discover_dms", fake_discover_dms)
    monkeypatch.setattr(adapter, "_authenticate_websocket", fake_auth)
    monkeypatch.setattr(adapter, "_subscribe_websocket", fake_subscribe)
    monkeypatch.setattr(adapter, "_send_channel_subscription", fake_send_sub)
    monkeypatch.setattr(adapter, "_websocket_url", lambda: "wss://test.relay")

    import hermes_cli  # noqa: F401  (ensure package import works under loader)

    real_connect = None
    try:
        import websockets
        real_connect = websockets.connect

        # The real websockets.connect returns an async-context-manager
        # object, not a coroutine — mirror that shape so `async with` in
        # _websocket_loop binds straight to the fake websocket.
        def fake_connect(*a, **kw):
            return ws

        websockets.connect = fake_connect
    except ImportError:
        pytest.skip("websockets not installed")

    try:
        task = asyncio.create_task(adapter._websocket_loop())
        await asyncio.wait_for(_wait_for_condition(
            lambda: any(
                cmd and len(cmd) > 2 and cmd[2] == NEW_DM for cmd in ws.sent
            )
        ), timeout=8)
    finally:
        if real_connect is not None:
            websockets.connect = real_connect
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ConnectionError):
            pass

    # Discovery ran more than once (periodic, not connect-only) and a
    # subscription frame for the new DM was sent on the live socket.
    assert len(discovery_calls) >= 2
    subscribed = [cmd[2] for cmd in ws.sent if cmd and len(cmd) > 2]
    assert NEW_DM in subscribed


async def _wait_for_condition(predicate, poll_s: float = 0.02):
    while not predicate():
        await asyncio.sleep(poll_s)


@pytest.mark.asyncio
async def test_discover_and_subscribe_new_only_touches_new_channels(monkeypatch):
    """The shared sweep helper subscribes ONLY channels discovery added —
    already-watched channels are not re-subscribed."""
    adapter = _make_adapter()
    ws = _QuietWebSocket()
    subscriptions = {"hermes-buzz-0": CHANNEL}
    adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}

    async def fake_discover_dms(*, seed):
        adapter._channel_state[NEW_DM] = {
            "chat_type": "dm", "last_ts": 0, "seen": {},
        }

    sent = []

    async def fake_send_sub(websocket, sub_id, channel_id):
        sent.append(channel_id)

    monkeypatch.setattr(adapter, "_discover_dms", fake_discover_dms)
    monkeypatch.setattr(adapter, "_send_channel_subscription", fake_send_sub)

    await adapter._discover_and_subscribe_new(ws, subscriptions)

    assert sent == [NEW_DM]
    assert any(v == NEW_DM for v in subscriptions.values())
