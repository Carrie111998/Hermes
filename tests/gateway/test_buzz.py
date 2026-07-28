from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter


buzz = load_plugin_adapter("buzz")
nostr_auth = sys.modules["plugin_adapter_buzz_nostr_auth"]
TEST_PRIVATE_KEY = "00" * 31 + "01"


def _config(**extra):
    return PlatformConfig(enabled=True, extra=extra)


def test_split_csv_normalizes_strings_and_lists():
    assert buzz._split_csv(" a, ,b ") == ["a", "b"]
    assert buzz._split_csv([" a ", "", "b"]) == ["a", "b"]


def test_thread_id_prefers_root_marker():
    tags = [["e", "parent", "", "reply"], ["e", "root", "", "root"]]
    assert buzz._thread_id(tags) == "root"


def test_addressing_accepts_pubkey_reply_or_wake_word():
    pubkey = "a" * 64
    assert buzz._is_for_agent(
        {"content": "hello", "tags": [["p", pubkey]]},
        agent_pubkey=pubkey,
        wake_words=["Hermes"],
        sent_ids=set(),
    )
    assert buzz._is_for_agent(
        {"content": "following up", "tags": [["e", "sent"]]},
        agent_pubkey=pubkey,
        wake_words=["Hermes"],
        sent_ids={"sent"},
    )
    assert buzz._is_for_agent(
        {"content": "@Hermes please check", "tags": []},
        agent_pubkey=pubkey,
        wake_words=["Hermes"],
        sent_ids=set(),
    )
    assert not buzz._is_for_agent(
        {"content": "general discussion", "tags": []},
        agent_pubkey=pubkey,
        wake_words=["Hermes"],
        sent_ids=set(),
    )


def test_check_requirements_needs_relay_and_private_key_only(monkeypatch):
    monkeypatch.delenv("BUZZ_RELAY_URL", raising=False)
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(buzz, "_resolve_cli", lambda config=None: None)
    assert buzz.check_requirements() is False

    monkeypatch.setenv("BUZZ_RELAY_URL", "https://buzz.example.test")
    assert buzz.check_requirements() is False

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    assert buzz.check_requirements() is True


def test_validate_config_checks_cli_url_and_private_key(monkeypatch):
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setattr(buzz, "_resolve_cli", lambda config=None: "/tmp/buzz")
    assert buzz.validate_config(_config(relay_url="https://buzz.example.test"))

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "invalid")
    assert not buzz.validate_config(_config(relay_url="https://buzz.example.test"))


def test_cli_send_uses_stdin_and_never_places_content_in_arguments(monkeypatch):
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    completed = SimpleNamespace(
        returncode=0, stdout='{"event_id":"abc","accepted":true}', stderr=""
    )
    with patch.object(buzz.subprocess, "run", return_value=completed) as run:
        result = buzz._run_cli_sync(
            "/tmp/buzz",
            "https://buzz.example.test",
            ["messages", "send", "--channel", "channel-1", "--content", "-"],
            input_text="secret-looking $content",
        )

    assert result["event_id"] == "abc"
    called_args = run.call_args.args[0]
    assert "secret-looking $content" not in called_args
    assert run.call_args.kwargs["input"] == "secret-looking $content"


def test_dm_send_stays_flat_unless_source_has_explicit_thread():
    instance = object.__new__(buzz.BuzzAdapter)
    instance._dm_channels = {"dm-id"}
    instance._sent_ids = set()
    instance._sent_order = buzz.deque()
    calls = []

    async def run_cli(args, *, input_text, timeout):
        calls.append((args, input_text, timeout))
        return {"event_id": f"sent-{len(calls)}", "accepted": True}

    instance._run_cli = run_cli

    asyncio.run(instance.send("dm-id", "flat reply", reply_to="trigger-id"))
    asyncio.run(
        instance.send(
            "dm-id",
            "thread reply",
            reply_to="trigger-id",
            metadata={"thread_id": "root-id"},
        )
    )
    asyncio.run(instance.send("group-id", "group reply", reply_to="trigger-id"))

    assert "--reply-to" not in calls[0][0]
    assert calls[1][0][-2:] == ["--reply-to", "root-id"]
    assert calls[2][0][-2:] == ["--reply-to", "trigger-id"]


def test_profile_publication_is_opt_in():
    instance = object.__new__(buzz.BuzzAdapter)
    instance._profile_name = ""
    instance._profile_about = ""
    calls = []

    async def run_cli(args, *, timeout):
        calls.append((args, timeout))

    instance._run_cli = run_cli
    asyncio.run(instance._publish_profile_best_effort())
    assert calls == []

    instance._profile_name = "Maximus"
    asyncio.run(instance._publish_profile_best_effort())
    assert calls == [(["users", "set-profile", "--name", "Maximus"], 20.0)]


def test_env_enablement_requires_complete_identity(monkeypatch):
    for name in (
        "BUZZ_RELAY_URL",
        "BUZZ_PRIVATE_KEY",
        "BUZZ_CHANNELS",
        "BUZZ_HOME_CHANNEL",
    ):
        monkeypatch.delenv(name, raising=False)
    assert buzz._env_enablement() is None

    monkeypatch.setenv("BUZZ_RELAY_URL", "https://buzz.example.test")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setenv("BUZZ_CHANNELS", "channel-1")
    monkeypatch.setenv("BUZZ_HOME_CHANNEL", "channel-home")
    seed = buzz._env_enablement()

    assert seed is not None
    assert seed["channels"] == ["channel-1"]
    assert seed["home_channel"]["chat_id"] == "channel-home"


def test_yaml_bridge_seeds_transport_and_auth_environment(monkeypatch):
    for name in (
        "BUZZ_RELAY_URL",
        "BUZZ_CHANNELS",
        "BUZZ_DM_CHANNELS",
        "BUZZ_DISCOVER_DMS",
        "BUZZ_HOME_CHANNEL",
        "BUZZ_ALLOWED_USERS",
        "BUZZ_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(name, raising=False)
    buzz._apply_yaml_config(
        {},
        {
            "extra": {
                "relay_url": "https://buzz.example.test",
                "channels": ["general-id"],
                "dm_channels": ["dm-id"],
                "discover_dms": False,
                "home_channel": {"chat_id": "home-id", "name": "Buzz"},
                "allowed_users": ["owner-id"],
                "allow_all_users": False,
            }
        },
    )
    assert os.environ["BUZZ_RELAY_URL"] == "https://buzz.example.test"
    assert os.environ["BUZZ_CHANNELS"] == "general-id"
    assert os.environ["BUZZ_DM_CHANNELS"] == "dm-id"
    assert os.environ["BUZZ_DISCOVER_DMS"] == "False"
    assert os.environ["BUZZ_HOME_CHANNEL"] == "home-id"
    assert os.environ["BUZZ_ALLOWED_USERS"] == "owner-id"
    assert os.environ["BUZZ_ALLOW_ALL_USERS"] == "False"


def test_websocket_url_converts_rest_relay_schemes():
    assert buzz._websocket_url("http://localhost:3000") == "ws://localhost:3000"
    assert buzz._websocket_url("https://buzz.example.test/community") == (
        "wss://buzz.example.test/community"
    )


def _lift_x(x: int):
    y_squared = (pow(x, 3, nostr_auth.FIELD_ORDER) + 7) % nostr_auth.FIELD_ORDER
    y = pow(y_squared, (nostr_auth.FIELD_ORDER + 1) // 4, nostr_auth.FIELD_ORDER)
    if pow(y, 2, nostr_auth.FIELD_ORDER) != y_squared:
        return None
    return x, y if y % 2 == 0 else nostr_auth.FIELD_ORDER - y


def _verify_schnorr(message: bytes, pubkey_hex: str, signature: bytes) -> bool:
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    scalar = int.from_bytes(signature[32:], "big")
    public_point = _lift_x(int(pubkey_hex, 16))
    if (
        public_point is None
        or r >= nostr_auth.FIELD_ORDER
        or scalar >= nostr_auth.CURVE_ORDER
    ):
        return False
    challenge = (
        int.from_bytes(
            nostr_auth._tagged_hash(
                "BIP0340/challenge",
                signature[:32] + bytes.fromhex(pubkey_hex) + message,
            ),
            "big",
        )
        % nostr_auth.CURVE_ORDER
    )
    negative_public = (public_point[0], (-public_point[1]) % nostr_auth.FIELD_ORDER)
    point = nostr_auth._point_add(
        nostr_auth._point_multiply(scalar),
        nostr_auth._point_multiply(challenge, negative_public),
    )
    return point is not None and point[1] % 2 == 0 and point[0] == r


def test_nostr_auth_event_has_valid_bip340_signature_and_owner_tag():
    auth_tag = ["auth", "b" * 64, "", "c" * 128]
    event = buzz.build_auth_event(
        private_key=TEST_PRIVATE_KEY,
        challenge="challenge-1",
        relay_url="wss://relay.example.test",
        auth_tag_json=json.dumps(auth_tag),
        created_at=1_700_000_000,
        auxiliary_randomness=b"\x00" * 32,
    )

    assert event["pubkey"] == (
        "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    )
    assert event["kind"] == 22242
    assert event["tags"] == [
        ["relay", "wss://relay.example.test"],
        ["challenge", "challenge-1"],
        auth_tag,
    ]
    serialized = json.dumps(
        [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"],
        ],
        separators=(",", ":"),
    ).encode()
    event_id = hashlib.sha256(serialized).digest()
    assert event["id"] == event_id.hex()
    assert _verify_schnorr(event_id, event["pubkey"], bytes.fromhex(event["sig"]))


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def recv(self):
        if self.sent:
            auth_event = self.sent[0][1]
            return json.dumps(["OK", auth_event["id"], True, "authenticated"])
        return json.dumps(["AUTH", "relay-challenge"])

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_websocket_auth_uses_nip42_and_nip_oa(monkeypatch):
    instance = object.__new__(buzz.BuzzAdapter)
    instance._relay_url = "https://buzz.example.test"
    websocket = _FakeWebSocket()
    auth_tag = json.dumps(["auth", "b" * 64, "", "c" * 128])
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setenv("BUZZ_AUTH_TAG", auth_tag)

    asyncio.run(instance._authenticate_websocket(websocket))

    assert websocket.sent[0][0] == "AUTH"
    event = websocket.sent[0][1]
    assert ["relay", "wss://buzz.example.test"] in event["tags"]
    assert ["challenge", "relay-challenge"] in event["tags"]
    assert json.loads(auth_tag) in event["tags"]


def test_dm_discovery_merges_relay_confirmed_conversations():
    instance = object.__new__(buzz.BuzzAdapter)
    instance._channels = ["general-id"]
    instance._dm_channels = set()

    async def run_cli(args, *, timeout):
        assert args == [
            "channels",
            "search",
            "--query",
            "DM",
            "--include-archived",
            "--limit",
            "1000",
        ]
        assert timeout == 20.0
        return [
            {"channel_id": "dm-one", "channel_type": "dm"},
            {"channel_id": "not-a-dm", "channel_type": "stream"},
            {"channel_id": "dm-two", "channel_type": "dm"},
        ]

    instance._run_cli = run_cli
    added = asyncio.run(instance._discover_dm_channels())

    assert added == 2
    assert instance._channels == ["general-id", "dm-one", "dm-two"]
    assert instance._dm_channels == {"dm-one", "dm-two"}


def test_websocket_subscriptions_resume_from_last_timestamp():
    instance = object.__new__(buzz.BuzzAdapter)
    instance._channels = ["general-id", "dm-one"]
    instance._since = {"general-id": 100, "dm-one": 200}
    instance._discover_dms = True
    instance._membership_since = 300
    instance._agent_pubkey = "a" * 64
    websocket = _FakeWebSocket()

    subscriptions = asyncio.run(instance._subscribe_websocket(websocket))

    assert subscriptions == {
        "hermes-buzz-0": "general-id",
        "hermes-buzz-1": "dm-one",
        buzz.MEMBERSHIP_SUBSCRIPTION_ID: None,
    }
    assert websocket.sent[0][2]["#h"] == ["general-id"]
    assert websocket.sent[0][2]["since"] == 99
    assert websocket.sent[1][2]["#h"] == ["dm-one"]
    assert websocket.sent[1][2]["since"] == 199
    assert websocket.sent[2][2]["since"] == 299


def test_event_id_dedup_routes_only_once(monkeypatch):
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setattr(buzz, "_resolve_cli", lambda config=None: "/tmp/buzz")
    instance = buzz.BuzzAdapter(_config(relay_url="https://buzz.example.test"))
    instance._dm_channels = {"dm-id"}
    received = []

    async def handle_message(event):
        received.append(event)

    instance.handle_message = handle_message
    event = {
        "id": "event-1",
        "pubkey": "b" * 64,
        "content": "hello",
        "created_at": 1_700_000_000,
        "tags": [["h", "dm-id"]],
    }
    asyncio.run(instance._dispatch_event("dm-id", event))
    asyncio.run(instance._dispatch_event("dm-id", event))

    assert len(received) == 1
    assert received[0].message_id == "event-1"


def test_connect_and_disconnect_acquire_and_release_scoped_lock(monkeypatch):
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setattr(buzz, "_resolve_cli", lambda config=None: "/tmp/buzz")
    instance = buzz.BuzzAdapter(_config(relay_url="https://buzz.example.test"))
    acquired = []
    released = []

    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda platform, key: acquired.append((platform, key)) or True,
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock",
        lambda platform, key: released.append((platform, key)),
    )

    async def run_cli(args, **kwargs):
        return []

    async def no_op(*args, **kwargs):
        return None

    async def websocket_loop():
        instance._ws_ready.set()
        await asyncio.Future()

    instance._run_cli = run_cli
    instance._discover_dm_channels = no_op
    instance._publish_profile_best_effort = no_op
    instance._set_presence_best_effort = no_op
    instance._websocket_loop = websocket_loop

    async def exercise():
        assert await instance.connect()
        await instance.disconnect()

    asyncio.run(exercise())

    assert acquired == [("buzz", f"https://buzz.example.test:{instance._agent_pubkey}")]
    assert released == acquired


def test_standalone_sender_uses_cli_without_constructing_adapter(monkeypatch):
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.setattr(buzz, "_resolve_cli", lambda config=None: "/tmp/buzz")
    calls = []

    async def run_cli(cli, relay_url, args, *, input_text, timeout):
        calls.append((cli, relay_url, args, input_text, timeout))
        return {"event_id": "sent-1", "accepted": True}

    monkeypatch.setattr(buzz, "_run_cli", run_cli)
    result = asyncio.run(
        buzz._standalone_send(
            _config(relay_url="https://buzz.example.test"),
            "channel-1",
            "hello",
            thread_id="root-1",
        )
    )

    assert result["success"] is True
    assert result["message_id"] == "sent-1"
    assert calls[0][2][-2:] == ["--reply-to", "root-1"]
    assert calls[0][3] == "hello"


def test_register_declares_plugin_only_integration_contract():
    captured = {}

    class Context:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    buzz.register(Context())

    assert captured["name"] == "buzz"
    assert captured["env_enablement_fn"] is buzz._env_enablement
    assert captured["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
    assert captured["standalone_sender_fn"] is buzz._standalone_send
    assert captured["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
    assert captured["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
