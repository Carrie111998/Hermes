"""Resilience tests: a relay CLOSED for one per-channel subscription must
NOT tear down the whole WebSocket connection.

Live incident (2026-08-29/30): a channel subscription the relay rejects with
``CLOSED: restricted: not a channel member`` raised ConnectionError in the
WS loop, tore down ALL subscriptions, and reconnected with the frozen watch
set — which re-offered the phantom channel every time — producing ~51k
reconnects in ~22h and near-total loss of Buzz inbound delivery.

Contract under test:
1. CLOSED naming a channel subscription drops ONLY that subscription
   (mapping entry removed), logs a WARNING naming the subscription and the
   channel, and leaves the connection and all other subscriptions intact —
   subsequent EVENT frames on surviving subscriptions still dispatch.
2. CLOSED naming the membership subscription (`hermes-buzz-membership`)
   still triggers the total reconnect (ConnectionError), because that
   subscription is essential for DM discovery.
3. AUTH rejections and real transport errors keep the old behavior
   (total reconnect).

Test-harness note: the loop task is only ever cancelled at a deterministic
parking point (the fake relay signals `parked` when its frame iterator has
run dry and the loop suspends on it). Cancelling mid-handshake races with
``asyncio.wait_for(recv)``'s cancellation handling and can be swallowed —
that is a test-harness hazard, not a property of the code under test.
"""

import asyncio
import collections
import json

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_buzz_mod = load_plugin_adapter("buzz")
BuzzAdapter = _buzz_mod.BuzzAdapter
_WS_MEMBERSHIP_SUB_ID = _buzz_mod._WS_MEMBERSHIP_SUB_ID

SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
TEST_PRIVATE_KEY = "00" * 31 + "03"
CH_A = "aaaaaaaa-7a82-5a8f-8c4e-57a070cbe7cd"
CH_B = "bbbbbbbb-7a82-5a8f-8c4e-57a070cbe7cd"
PHANTOM = "cccccccc-7a82-5a8f-8c4e-57a070cbe7cd"


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._private_key = TEST_PRIVATE_KEY
    adapter._display_name = "Chip"
    return adapter


def _seed_channels(adapter, channel_ids):
    for channel_id in channel_ids:
        adapter._channel_state[channel_id] = {
            "chat_type": "channel",
            "last_ts": 0,
            "seen": collections.OrderedDict(),
        }


class _FakeWebSocket:
    """One fake connection: NIP-42 handshake on demand, then scripted frames.

    `_authenticate_websocket` drives `recv()`; the event loop drives
    `async for raw in websocket` (the websockets client is async-iterable).
    """

    def __init__(self, factory):
        self._factory = factory
        self.sent = []
        self._handshake_done = False

    async def recv(self):
        if not self._handshake_done:
            if not self.sent:
                return json.dumps(["AUTH", "relay-challenge"])
            self._handshake_done = True
            auth_event = self.sent[0][1]
            if self._factory.auth_ok:
                return json.dumps(["OK", auth_event["id"], True, "authenticated"])
            return json.dumps(["OK", auth_event["id"], False, "denied"])
        raise AssertionError("recv() after handshake; the loop must use async-for")

    def __aiter__(self):
        return self._iter_frames()

    async def _iter_frames(self):
        while True:
            if self._factory.frames:
                raw = self._factory.frames.pop(0)
                if isinstance(raw, Exception):
                    raise raw
                yield raw
                continue
            # Deterministic parking point: signal the test, then suspend.
            self._factory.parked.set()
            await asyncio.sleep(3600)

    async def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        self._factory.all_sent.append(frame)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRelay:
    """Drop-in for ``websockets.connect`` — counts connects, hands out fakes."""

    def __init__(self, frames=(), auth_ok=True):
        self.frames = list(frames)
        self.auth_ok = auth_ok
        self.all_sent = []
        self.connect_count = 0
        self.parked = asyncio.Event()

    def __call__(self, url, **kwargs):
        self.connect_count += 1
        return _FakeWebSocket(self)


def _buzz_websockets():
    """The module object the adapter's lazy ``import websockets`` resolves to."""
    import websockets

    return websockets


def _sub_ids_sent(relay):
    """Subscription ids in the order their REQ frames went out (all conns)."""
    return [frame[1] for frame in relay.all_sent if frame[0] == "REQ"]


async def _wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise asyncio.TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


async def _run_loop_and_stop(adapter, relay, predicate, timeout=5.0):
    """Start _websocket_loop, wait for predicate(), then stop it cleanly.

    The stop waits for the relay's `parked` event (the loop suspended in its
    frame-iterator idle sleep) so the cancel lands at a known suspension
    point instead of racing the AUTH handshake's wait_for.
    """
    task = asyncio.create_task(adapter._websocket_loop())
    try:
        await _wait_for(predicate, timeout=timeout)
        await _wait_for(relay.parked.is_set, timeout=2.0)
        await asyncio.sleep(0.05)  # let the task reach the actual suspension
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _capture_dispatch(adapter, sink):
    async def capture(**kwargs):
        sink.append(kwargs)

    adapter._dispatch_message = capture


# ── Unit: _handle_ws_closed_frame ─────────────────────────────────────────


def test_closed_frame_channel_sub_dropped_others_kept():
    adapter = _make_adapter()
    subscriptions = {"hermes-buzz-0": CH_A, "hermes-buzz-1": CH_B}

    adapter._handle_ws_closed_frame(
        ["CLOSED", "hermes-buzz-0", "restricted: not a channel member"], subscriptions
    )

    assert subscriptions == {"hermes-buzz-1": CH_B}


def test_closed_frame_membership_sub_raises_connection_error():
    adapter = _make_adapter()
    subscriptions = {"hermes-buzz-0": CH_A, _WS_MEMBERSHIP_SUB_ID: None}

    with pytest.raises(ConnectionError):
        adapter._handle_ws_closed_frame(
            ["CLOSED", _WS_MEMBERSHIP_SUB_ID, "restricted: expired"], subscriptions
        )
    # Nothing dropped — the reconnect rebuilds everything.
    assert subscriptions == {"hermes-buzz-0": CH_A, _WS_MEMBERSHIP_SUB_ID: None}


def test_closed_frame_unknown_sub_id_ignored():
    adapter = _make_adapter()
    subscriptions = {"hermes-buzz-0": CH_A}

    adapter._handle_ws_closed_frame(["CLOSED", "hermes-buzz-999", "ghost"], subscriptions)

    assert subscriptions == {"hermes-buzz-0": CH_A}


# ── End-to-end through the real WS loop ───────────────────────────────────


@pytest.mark.asyncio
async def test_closed_channel_subscription_keeps_loop_and_other_subs(monkeypatch, caplog):
    """AC1: the relay sends CLOSED for hermes-buzz-1 → the loop stays on the
    same connection, only that sub is dropped, later EVENT frames on the
    surviving subs dispatch, and the WARNING names subscription + channel."""
    adapter = _make_adapter()
    _seed_channels(adapter, [CH_A, CH_B, PHANTOM])
    dispatched = []
    _capture_dispatch(adapter, dispatched)

    relay = _FakeRelay(
        frames=[
            json.dumps(["CLOSED", "hermes-buzz-1", "restricted: not a channel member"]),
            json.dumps(
                [
                    "EVENT",
                    "hermes-buzz-0",
                    {
                        "id": "evt-a-1",
                        "pubkey": "b" * 64,
                        "content": "hi @Chip",
                        "created_at": 1234,
                        "kind": 9,
                        "tags": [["h", CH_A]],
                    },
                ]
            ),
            # Event routed to the DROPPED subscription must not dispatch.
            json.dumps(
                [
                    "EVENT",
                    "hermes-buzz-1",
                    {
                        "id": "evt-phantom-1",
                        "pubkey": "d" * 64,
                        "content": "hi @Chip",
                        "created_at": 1234,
                        "kind": 9,
                        "tags": [["h", PHANTOM]],
                    },
                ]
            ),
            # Event on another surviving sub still dispatches.
            json.dumps(
                [
                    "EVENT",
                    "hermes-buzz-2",
                    {
                        "id": "evt-c-1",
                        "pubkey": "c" * 64,
                        "content": "hi @Chip",
                        "created_at": 1235,
                        "kind": 9,
                        "tags": [["h", PHANTOM]],
                    },
                ]
            ),
        ]
    )
    monkeypatch.setattr(_buzz_websockets(), "connect", relay)

    with caplog.at_level("WARNING", logger="plugin_adapter_buzz"):
        await _run_loop_and_stop(adapter, relay, lambda: len(dispatched) == 2)

    # Only the events on surviving subscriptions dispatched — the one on the
    # dropped sub did not (mapping entry removed inside the live loop).
    assert [d["chat_id"] for d in dispatched] == [CH_A, PHANTOM]
    # No reconnect: one connection only, every REQ sent exactly once.
    assert relay.connect_count == 1
    assert _sub_ids_sent(relay) == [
        "hermes-buzz-0",
        "hermes-buzz-1",
        "hermes-buzz-2",
        _WS_MEMBERSHIP_SUB_ID,
    ]
    # The WARNING names the dropped subscription and the channel.
    assert any(
        r.levelname == "WARNING"
        and "hermes-buzz-1" in r.getMessage()
        and CH_B in r.getMessage()
        for r in caplog.records
    ), "expected a WARNING naming the dropped subscription and channel"


@pytest.mark.asyncio
async def test_closed_membership_subscription_triggers_reconnect(monkeypatch):
    """AC2: CLOSED on the membership sub → ConnectionError → teardown and
    reconnect (second connection re-sends the REQs)."""
    adapter = _make_adapter()
    _seed_channels(adapter, [CH_A])
    relay = _FakeRelay(frames=[json.dumps(["CLOSED", _WS_MEMBERSHIP_SUB_ID, "restricted: expired"])])
    monkeypatch.setattr(_buzz_websockets(), "connect", relay)

    await _run_loop_and_stop(adapter, relay, lambda: relay.connect_count >= 2)

    assert relay.connect_count >= 2, "membership-sub CLOSED must trigger a reconnect"
    assert _sub_ids_sent(relay).count("hermes-buzz-0") >= 2, "REQs must be re-sent after the reconnect"


@pytest.mark.asyncio
async def test_closed_channel_subscription_does_not_reconnect(monkeypatch):
    """A channel-sub CLOSED must not tear down the socket: even repeated
    CLOSED frames for the same sub leave the loop on the first connection."""
    adapter = _make_adapter()
    _seed_channels(adapter, [CH_A, PHANTOM])
    closed = json.dumps(["CLOSED", "hermes-buzz-1", "restricted: not a channel member"])
    relay = _FakeRelay(frames=[closed, closed])
    monkeypatch.setattr(_buzz_websockets(), "connect", relay)

    await _run_loop_and_stop(adapter, relay, lambda: relay.parked.is_set and relay.connect_count >= 1)

    assert relay.connect_count == 1, "channel-sub CLOSED must NOT trigger a reconnect"
    assert _sub_ids_sent(relay).count("hermes-buzz-0") == 1


@pytest.mark.asyncio
async def test_auth_failure_still_reconnects(monkeypatch):
    """AUTH rejection behaves unchanged: the loop reconnects (and then
    authenticates fine, so the harness can park and stop it cleanly)."""
    adapter = _make_adapter()
    _seed_channels(adapter, [CH_A])
    relay = _FakeRelay(auth_ok=False)
    monkeypatch.setattr(_buzz_websockets(), "connect", relay)

    # Flip to accepted after the first rejection so the loop settles parked.
    original_call = relay.__call__

    def connect_then_accept(url, **kwargs):
        ws = original_call(url, **kwargs)
        if relay.connect_count >= 2:
            relay.auth_ok = True
        return ws

    monkeypatch.setattr(_buzz_websockets(), "connect", connect_then_accept)

    await _run_loop_and_stop(adapter, relay, lambda: relay.connect_count >= 2)

    assert relay.connect_count >= 2, "AUTH rejection must trigger a reconnect"


@pytest.mark.asyncio
async def test_genuine_disconnect_still_reconnects(monkeypatch):
    """A real transport error still tears down and reconnects with backoff."""
    adapter = _make_adapter()
    _seed_channels(adapter, [CH_A])
    relay = _FakeRelay(frames=[ConnectionError("connection reset")])
    monkeypatch.setattr(_buzz_websockets(), "connect", relay)

    await _run_loop_and_stop(adapter, relay, lambda: relay.connect_count >= 2)

    assert relay.connect_count >= 2, "transport error must trigger a reconnect"
