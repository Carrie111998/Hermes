"""Tests for the Buzz WebSocket transport (NIP-42) and Nostr signing module.

The signing module and WS transport were contributed in PR #73636 by
@ScaleLeanChris and consolidated onto the merged poll-based adapter; these
tests cover the crypto (against the official BIP-340 vector) and the WS
lifecycle as wired into BuzzAdapter.
"""

import asyncio
import json
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




@pytest.mark.asyncio
async def test_membership_event_adopts_new_named_channel_when_unpinned():
    """A new community membership must subscribe without a gateway restart (#75107)."""
    adapter = _make_adapter()
    new_channel = "new-community-channel"

    async def run_cli(args, **_kwargs):
        if args == ["dms", "list"]:
            return 0, "[]", ""
        if args == ["channels", "list"]:
            return 0, json.dumps([
                {"channel_id": new_channel, "name": "release-team", "description": "Announcements"}
            ]), ""
        if args[:2] == ["messages", "get"]:
            return 0, json.dumps([{"id": "pre-join", "created_at": 41}]), ""
        raise AssertionError(f"unexpected CLI command: {args}")

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    adapter._run_cli = run_cli
    websocket = WebSocket()
    subscriptions = {"membership": None}
    await adapter._handle_membership_event(websocket, subscriptions, {"created_at": 42})

    state = adapter._channel_state[new_channel]
    assert state["chat_type"] == "group"
    assert state["last_ts"] == 41
    assert "pre-join" in state["seen"]
    subscription_id = next(key for key, value in subscriptions.items() if value == new_channel)
    assert websocket.sent == [[
        "REQ", subscription_id, {"kinds": [9], "#h": [new_channel], "since": 40}
    ]]


@pytest.mark.asyncio
async def test_membership_event_does_not_adopt_channel_outside_explicit_watch_list():
    adapter = _make_adapter({"channels": [CHANNEL]})

    async def run_cli(args, **_kwargs):
        if args == ["dms", "list"]:
            return 0, "[]", ""
        if args == ["channels", "list"]:
            return 0, json.dumps([
                {"channel_id": "not-configured", "name": "release-team", "description": "Announcements"}
            ]), ""
        raise AssertionError(f"unexpected CLI command: {args}")

    adapter._run_cli = run_cli
    await adapter._handle_membership_event(_FakeWebSocket(), {"membership": None}, {"created_at": 42})
    assert "not-configured" not in adapter._channel_state


@pytest.mark.asyncio
async def test_membership_event_leaves_state_unchanged_when_channel_lookup_fails():
    adapter = _make_adapter()

    async def run_cli(args, **_kwargs):
        if args == ["dms", "list"]:
            return 0, "[]", ""
        if args == ["channels", "list"]:
            return 1, "", "unavailable"
        raise AssertionError(f"unexpected CLI command: {args}")

    adapter._run_cli = run_cli
    subscriptions = {"membership": None}
    await adapter._handle_membership_event(_FakeWebSocket(), subscriptions, {"created_at": 42})
    assert adapter._channel_state == {}
    assert subscriptions == {"membership": None}
