"""Tests for the Buzz WebSocket transport (NIP-42) and Nostr signing module.

The signing module and WS transport were contributed in PR #73636 by
@ScaleLeanChris and consolidated onto the merged poll-based adapter; these
tests cover the crypto (against the official BIP-340 vector) and the WS
lifecycle as wired into BuzzAdapter.
"""

import asyncio
import json
import sys
import time

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_buzz_mod = load_plugin_adapter("buzz")
BuzzAdapter = _buzz_mod.BuzzAdapter

import importlib.util as _ilu
from pathlib import Path as _Path

_auth_path = _Path(_buzz_mod.__file__).with_name("nostr_auth.py")
_spec = _ilu.spec_from_file_location("plugin_adapter_buzz_nostr_auth", _auth_path)
nostr_auth = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(nostr_auth)

SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
# BIP-340 test vector 0 private key
TEST_PRIVATE_KEY = "00" * 31 + "03"
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._private_key = TEST_PRIVATE_KEY
    adapter._display_name = "Chip"
    return adapter


# ── nostr_auth: BIP-340 / NIP-42 ──────────────────────────────────────────


def test_schnorr_sign_matches_official_bip340_vector_zero():
    signature = nostr_auth.schnorr_sign(
        bytes(32), TEST_PRIVATE_KEY, auxiliary_randomness=bytes(32)
    )
    assert nostr_auth.public_key_hex(TEST_PRIVATE_KEY).upper() == (
        "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
    )
    assert signature.hex().upper() == (
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
        "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
    )


def test_decode_private_key_rejects_bad_input():
    with pytest.raises(ValueError):
        nostr_auth.decode_private_key("not-a-key")
    with pytest.raises(ValueError):
        nostr_auth.decode_private_key("00" * 32)  # zero — outside range
    with pytest.raises(ValueError):
        nostr_auth.decode_private_key("nsec1qqqqqqqq")  # bad checksum/length


def test_build_auth_event_shape_and_owner_tag():
    tag = json.dumps(["auth", "b" * 64, "", "c" * 128])
    event = nostr_auth.build_auth_event(
        private_key=TEST_PRIVATE_KEY,
        challenge="challenge-1",
        relay_url="wss://relay.example",
        auth_tag_json=tag,
        created_at=1_700_000_000,
        auxiliary_randomness=bytes(32),
    )
    assert event["kind"] == 22242
    assert ["relay", "wss://relay.example"] in event["tags"]
    assert ["challenge", "challenge-1"] in event["tags"]
    assert ["auth", "b" * 64, "", "c" * 128] in event["tags"]
    assert len(bytes.fromhex(event["sig"])) == 64
    assert event["pubkey"] == nostr_auth.public_key_hex(TEST_PRIVATE_KEY)


# ── Adapter WS wiring ─────────────────────────────────────────────────────


class _FakeWebSocket:
    """Replays a NIP-42 handshake: AUTH challenge, then OK for the reply."""

    def __init__(self):
        self.sent = []

    async def recv(self):
        if self.sent:
            auth_event = self.sent[0][1]
            return json.dumps(["OK", auth_event["id"], True, "authenticated"])
        return json.dumps(["AUTH", "relay-challenge"])

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_websocket_auth_raises_on_rejection():
    adapter = _make_adapter()

    class RejectingWs(_FakeWebSocket):
        async def recv(self):
            if self.sent:
                auth_event = self.sent[0][1]
                return json.dumps(["OK", auth_event["id"], False, "denied"])
            return json.dumps(["AUTH", "relay-challenge"])

    with pytest.raises(ConnectionError):
        await adapter._authenticate_websocket(RejectingWs())


# ── Presence (kind 20001 heartbeats) ────────────────────────────────────


def test_build_presence_event_shape():
    event = nostr_auth.build_presence_event(
        private_key=TEST_PRIVATE_KEY,
        status="online",
        created_at=1_700_000_000,
        auxiliary_randomness=bytes(32),
    )
    assert event["kind"] == 20001
    assert event["content"] == "online"
    assert event["tags"] == [["status", "online"]]
    assert event["pubkey"] == nostr_auth.public_key_hex(TEST_PRIVATE_KEY)
    assert len(bytes.fromhex(event["sig"])) == 64
    # No p-tags — the Desktop's live path trusts author = subject.
    assert not any(tag[0] == "p" for tag in event["tags"])


def test_build_presence_event_rejects_bad_status():
    with pytest.raises(ValueError):
        nostr_auth.build_presence_event(private_key=TEST_PRIVATE_KEY, status="brb")


@pytest.mark.asyncio
async def test_publish_presence_sends_event_over_ws():
    adapter = _make_adapter()
    ws = _FakeWebSocket()
    await adapter._publish_presence(ws, "online")
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame[0] == "EVENT"
    event = frame[1]
    assert event["kind"] == 20001
    assert event["content"] == "online"
    assert event["pubkey"] == nostr_auth.public_key_hex(TEST_PRIVATE_KEY)


@pytest.mark.asyncio
async def test_presence_heartbeat_publishes_immediately_then_cancels():
    adapter = _make_adapter()
    ws = _FakeWebSocket()
    task = asyncio.create_task(adapter._presence_heartbeat(ws, "online"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ws.sent) >= 1
    assert ws.sent[0][1]["content"] == "online"


def test_presence_disabled_via_env(monkeypatch):
    monkeypatch.setenv("BUZZ_NO_PRESENCE", "1")
    assert _make_adapter()._presence_enabled is False
    monkeypatch.delenv("BUZZ_NO_PRESENCE")
    assert _make_adapter()._presence_enabled is True


class _ConnectCtx:
    def __init__(self, ws_factory):
        self._ws_factory = ws_factory

    async def __aenter__(self):
        return self._ws_factory()

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_websocket_loop_publishes_presence_and_cancels_task(monkeypatch):
    adapter = _make_adapter()
    adapter._channel_state = {CHANNEL: {"chat_type": "group", "last_ts": 0, "seen": {}}}
    adapter._membership_since = int(time.time())
    sent = []

    class LoopWs:
        def __init__(self):
            self.sent = sent

        async def recv(self):
            if not any(f[0] == "AUTH" for f in self.sent):
                return json.dumps(["AUTH", "relay-challenge"])
            if self.sent and self.sent[0][0] == "AUTH":
                auth_event = self.sent[0][1]
                return json.dumps(["OK", auth_event["id"], True, "ok"])
            await asyncio.sleep(60)  # hang until the loop is cancelled

        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)  # hang until the loop is cancelled
            raise StopAsyncIteration

    import types

    fake_websockets = types.SimpleNamespace(connect=lambda *a, **k: _ConnectCtx(LoopWs))
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

    task = asyncio.create_task(adapter._websocket_loop())
    try:
        for _ in range(100):
            if any(
                f[0] == "EVENT" and f[1].get("kind") == 20001 for f in sent
            ):
                break
            await asyncio.sleep(0.02)
        presence_frames = [
            f for f in sent if f[0] == "EVENT" and f[1].get("kind") == 20001
        ]
        assert presence_frames, "presence event was never published"
        assert presence_frames[0][1]["content"] == "online"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


