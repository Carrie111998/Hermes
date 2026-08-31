"""Tests for the Buzz platform adapter plugin."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/buzz/adapter.py under a unique module name
# (plugin_adapter_buzz) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_buzz_mod = load_plugin_adapter("buzz")

BuzzAdapter = _buzz_mod.BuzzAdapter
hex_to_npub = _buzz_mod.hex_to_npub
npub_to_hex = _buzz_mod.npub_to_hex
_normalize_user_ref = _buzz_mod._normalize_user_ref
_cli_error_message = _buzz_mod._cli_error_message
_resolve_private_key = _buzz_mod._resolve_private_key
check_requirements = _buzz_mod.check_requirements
validate_config = _buzz_mod.validate_config
register = _buzz_mod.register
_env_enablement = _buzz_mod._env_enablement
_standalone_send = _buzz_mod._standalone_send

# Real key pair (Chip's public identity — public information, not a secret)
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
SELF_NPUB = "npub1nl2u0wnd8mezfknc74q7pl9ec58h9nrrakce4tnk434qgaxl4psqe5twr6"
OTHER_PUBKEY = "a" * 64
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
# Real DM conversation as materialized by a hosted relay: `dms list` returns
# [] for it (#68871) while `channels list` shows it as name "DM", empty
# description, indistinguishable from a channel except via message p-tags.
DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"

_ENV_VARS = (
    "BUZZ_RELAY_URL",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_CHANNELS",
    "BUZZ_HOME_CHANNEL",
    "BUZZ_ALLOWED_USERS",
    "BUZZ_ALLOW_ALL_USERS",
    "BUZZ_POLL_INTERVAL",
    "BUZZ_CLI_PATH",
    "BUZZ_CREDENTIALS_FILE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Keep tests hermetic: no ambient Buzz env vars or real credentials."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _event(event_id, pubkey=OTHER_PUBKEY, content="hello", created_at=1000, kind=9):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": [["h", CHANNEL]],
    }


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = SELF_NPUB
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    return adapter


class _ScriptedCli:
    """Fake ``_run_cli`` that routes on the buzz subcommand and records calls."""

    def __init__(self):
        self.responses = {}  # (group, cmd) -> list of (code, stdout, stderr)
        self.calls = []

    def script(self, group, cmd, payload, code=0, stderr=""):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.setdefault((group, cmd), []).append((code, stdout, stderr))

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        queue = self.responses.get((args[0], args[1]), [])
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        return 0, "[]", ""


# ── bech32 / identity helpers ─────────────────────────────────────────────


class TestBech32Helpers:

    def test_hex_to_npub_known_pair(self):
        assert hex_to_npub(SELF_PUBKEY) == SELF_NPUB

    def test_npub_to_hex_known_pair(self):
        assert npub_to_hex(SELF_NPUB) == SELF_PUBKEY


# ── Adapter init / config precedence ──────────────────────────────────────


class TestBuzzAdapterInit:


    def test_init_from_config_extra(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://cfg.relay",
                "channels": ["ccc"],
                "poll_interval": 2,
                "home_channel": "ccc",
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://cfg.relay"
        assert adapter.channels == ["ccc"]
        assert adapter.poll_interval == 2.0
        assert adapter.home_channel == "ccc"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://env.relay")
        from gateway.config import PlatformConfig
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"}))
        assert adapter.relay_url == "https://env.relay"


# ── CLI error contract ────────────────────────────────────────────────────


class TestCliErrorContract:

    def test_parses_json_error(self):
        msg = _cli_error_message('{"error":"relay_error","message":"boom","retryable":false}', 2)
        assert "relay_error" in msg and "boom" in msg and "exit 2" in msg


# ── Seeding / high-water mark / de-dupe ───────────────────────────────────


class TestPollingDedupe:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_seed_sets_high_water_mark_without_dispatch(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip old history", created_at=100),
            _event("e2", content="@Chip newer history", created_at=200),
        ])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        state = adapter._channel_state[CHANNEL]
        assert state["last_ts"] == 200
        assert set(state["seen"]) == {"e1", "e2"}
        # Seeding must never replay history into the agent
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_new_event_dispatched_once(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", content="@Chip hi", created_at=100)])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Poll 1: seeded event + a genuinely new mention
        cli.responses.clear()
        cli.script("messages", "get", [
            _event("e1", content="@Chip hi", created_at=100),
            _event("e2", content="hey @Chip, ping", created_at=150),
        ])
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["e2"]
        assert adapter._dispatched[0]["text"] == "hey @Chip, ping"
        assert adapter._channel_state[CHANNEL]["last_ts"] == 150

        # Poll 2: identical response — the seen-id set must de-dupe
        await adapter._poll_channel(CHANNEL)
        assert len(adapter._dispatched) == 1


# ── Mention gating / DMs / authorization ──────────────────────────────────


class TestMentionGating:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_ignored(self, adapter):
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_name_mention_dispatched(self, adapter):
        await self._poll_with(adapter, _event("e1", content="hey @Chip can you help?", created_at=10))
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized(self, adapter):
        adapter._allowed_pubkeys = {"b" * 64}
        await self._poll_with(adapter, _event("e1", content="@Chip hello", created_at=10))
        assert adapter._dispatched == []


# ── DM classification via p-tags (issue #68871) ──────────────────────────
#
# `buzz dms list` returns [] on some hosted relays, so DM conversations leak
# in via `channels list` and get seeded chat_type="group".  The adapter must
# reclassify them from the Nostr tags of real traffic: DM messages are
# p-tagged to our own pubkey WITHOUT the text mentioning us, while channel
# messages only ever p-tag us when the text visibly @mentions us.


def _tagged_event(event_id, channel, *, content, pubkey=OTHER_PUBKEY,
                  created_at=1000, kind=9, p=None, reply_to=None):
    """Event with the tag shapes observed on a live relay (h/p/e tags)."""
    tags = [["h", channel]]
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


class TestDmClassification:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        # Metadata exactly as `channels list` returns it on the hosted relay.
        a._channel_meta = {
            DM_CHANNEL: {"channel_id": DM_CHANNEL, "name": "DM", "description": ""},
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_names = {DM_CHANNEL: "DM", CHANNEL: "general"}
        # Both leaked in as group — the bug under test.
        a._channel_state[DM_CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, channel, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(channel)

    @pytest.mark.asyncio
    async def test_unmentioned_ptagged_dm_latches_and_dispatches(self, adapter):
        """The reported bug: a DM without an @mention must dispatch."""
        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="here's a test message", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


    @pytest.mark.asyncio
    async def test_general_reply_ptagging_self_stays_channel(self, adapter):
        """A #general reply to us p-tags our pubkey (observed live) — that
        must NOT reclassify the channel; mention gating still applies."""
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="@chip what's up?",
                          p=SELF_PUBKEY, reply_to="root-event"),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # It carried a mention, so it dispatches — but as a group message.
        assert [d["chat_type"] for d in adapter._dispatched] == ["group"]

        # And once the mention is absent, the channel gate drops the message
        # even though the earlier reply p-tagged us.
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e2", CHANNEL, content="thanks everyone", created_at=1001),
        )
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_channel_like_metadata_blocks_latch_even_without_mention(self, adapter):
        """Second guard on its own: even a p-tagged, un-mentioned message
        cannot reclassify a conversation whose metadata says real channel."""
        adapter._channel_meta[CHANNEL]["description"] = ""
        adapter._channel_meta[CHANNEL]["name"] = "announcements"
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="fyi everyone", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        assert adapter._dispatched == []


    @pytest.mark.asyncio
    async def test_dm_shaped_channel_discovered_when_dms_list_empty(self):
        """Fallback discovery: with `dms list` broken (returns []), a
        DM-shaped `channels list` entry gets watched; real channels not
        already watched are left alone."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": DM_CHANNEL, "name": "DM", "description": "", "created_at": 1},
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        # Watched as group; the p-tag latch flips it on the first real DM.
        assert a._channel_state[DM_CHANNEL]["chat_type"] == "group"
        assert a._may_reclassify_as_dm(DM_CHANNEL) is True
        assert CHANNEL not in a._channel_state
        assert a._may_reclassify_as_dm(CHANNEL) is False


# ── Sending ───────────────────────────────────────────────────────────────


class TestBuzzAdapterSend:

    @pytest.mark.asyncio
    async def test_send_success_via_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt123", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello **markdown**")
        assert result.success is True
        assert result.message_id == "evt123"

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        # Content travels via stdin (--content -), never argv
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "hello **markdown**"
        # Our own event id is marked seen for echo suppression
        assert "evt123" in adapter._channel_state[CHANNEL]["seen"]


    @pytest.mark.asyncio
    async def test_send_image_local_file_uses_file_flag(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="screenshot")
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)


# ── Lifecycle ─────────────────────────────────────────────────────────────


class TestBuzzAdapterLifecycle:


    @pytest.mark.asyncio
    async def test_disconnect_releases_scoped_lock(self, monkeypatch):
        """The identity lock taken in connect() must be released on disconnect."""
        import gateway.status as gateway_status

        released = []
        monkeypatch.setattr(
            gateway_status,
            "release_scoped_lock",
            lambda platform, key: released.append((platform, key)),
        )
        adapter = _make_adapter()
        adapter._lock_key = "wss://relay.example:" + SELF_PUBKEY
        await adapter.disconnect()
        assert released == [("buzz", "wss://relay.example:" + SELF_PUBKEY)]
        assert adapter._lock_key is None

    @pytest.mark.asyncio
    async def test_connect_fails_when_identity_lock_held(self, monkeypatch):
        """A second profile using the same relay+pubkey must fail fast."""
        import gateway.status as gateway_status

        monkeypatch.setattr(
            gateway_status, "acquire_scoped_lock", lambda platform, key: False
        )
        adapter = _make_adapter()
        adapter.cli_path = "/fake/buzz"
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1test")
        cli = _ScriptedCli()
        cli.script(
            "users", "get",
            [{"pubkey": SELF_PUBKEY, "display_name": "Chip"}],
        )
        adapter._run_cli = cli
        assert await adapter.connect() is False
        assert adapter._lock_key is None


# ── Credentials / requirements ────────────────────────────────────────────


class TestCredentialResolution:

    def test_env_key_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        assert _resolve_private_key() == "nsec1fromenv"

    def test_credentials_file_fallback(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "npub": "npub1x"}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert _resolve_private_key() == "nsec1fromfile"


# ── Env enablement / registration / standalone send ──────────────────────


class TestEnvEnablement:

    def test_returns_none_when_unconfigured(self):
        assert _env_enablement() is None


class TestBuzzPluginRegistration:

    def test_registers_standard_message_link_reader_in_buzz_toolset(self):
        ctx = MagicMock()

        register(ctx)

        ctx.register_tool.assert_called_once()
        kwargs = ctx.register_tool.call_args.kwargs
        assert kwargs["name"] == "buzz_read_message_link"
        assert kwargs["toolset"] == "buzz"
        assert kwargs["is_async"] is True

    def test_reader_check_accepts_standard_yaml_config(
        self, monkeypatch, tmp_path
    ):
        import hermes_cli.config as config_mod

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setattr(
            config_mod,
            "load_config",
            lambda: {
                "gateway": {
                    "platforms": {
                        "buzz": {
                            "enabled": True,
                            "extra": {
                                "relay_url": "https://yaml-relay.example",
                                "cli_path": str(fake_cli),
                            },
                        }
                    }
                }
            },
        )
        ctx = MagicMock()

        register(ctx)

        assert ctx.register_tool.call_args.kwargs["check_fn"]() is True

    def test_real_registration_preserves_core_tools_with_link_reader(self):
        """Drive the adapter's real registration seam, not a synthetic probe."""
        from dataclasses import fields

        from gateway.platform_registry import PlatformEntry, platform_registry
        from hermes_cli.tools_config import _get_platform_tools
        from tools.registry import registry
        from toolsets import resolve_toolset

        tool_name = "buzz_read_message_link"
        previous_tool = registry.snapshot_registration(tool_name)
        previous_platform = platform_registry.get("buzz")

        class RealRegistrationContext:
            def register_tool(self, **kwargs):
                registry.register(**kwargs, override=True)

            def register_platform(self, **kwargs):
                accepted = {field.name for field in fields(PlatformEntry)}
                platform_registry.register(
                    PlatformEntry(
                        **{key: value for key, value in kwargs.items() if key in accepted}
                    )
                )

        try:
            register(RealRegistrationContext())

            composite = resolve_toolset("hermes-buzz")
            enabled = _get_platform_tools({}, "buzz")

            assert tool_name in composite
            assert "terminal" in enabled
            assert "file" in enabled
            assert "read_file" in composite
            assert "write_file" in composite
        finally:
            current_tool = registry.snapshot_registration(tool_name)
            if current_tool is not None:
                registry.restore_registration(tool_name, current_tool, previous_tool)
            platform_registry.unregister("buzz")
            if previous_platform is not None:
                platform_registry.register(previous_platform)

    def test_register_platform_contract(self):
        from gateway.platform_registry import platform_registry

        platform_registry.unregister("buzz")
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "buzz"
        assert kwargs["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
        assert callable(kwargs["standalone_sender_fn"])
        assert callable(kwargs["env_enablement_fn"])
        assert set(kwargs["required_env"]) == {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"}
        assert "buzz://message" in kwargs["platform_hint"]
        assert "buzz_read_message_link" in kwargs["platform_hint"]


class TestBuzzMessageLinkReader:
    LINK = (
        "buzz://message?"
        f"channel={CHANNEL}&"
        f"id={'1' * 64}&"
        f"thread={'2' * 64}"
    )

    def test_parses_only_canonical_message_links(self):
        assert _buzz_mod._parse_buzz_message_link(self.LINK) == {
            "channel": CHANNEL,
            "id": "1" * 64,
            "thread": "2" * 64,
        }
        invalid = [
            "https://example.com/message?channel=x&id=y",
            f"buzz://message?channel=not-a-uuid&id={'1' * 64}",
            f"buzz://message?channel={CHANNEL}&id=ABC",
            f"buzz://message?channel={CHANNEL}&id={'1' * 64}&thread=bad",
            f"buzz://message?channel={CHANNEL}&id={'1' * 64}&extra=1",
            f"buzz://user@message?channel={CHANNEL}&id={'1' * 64}",
            f"buzz://message:443?channel={CHANNEL}&id={'1' * 64}",
        ]
        for link in invalid:
            with pytest.raises(ValueError):
                _buzz_mod._parse_buzz_message_link(link)

    def test_rejects_trailing_slash_as_noncanonical(self):
        trailing_slash = self.LINK.replace("buzz://message?", "buzz://message/?")

        with pytest.raises(ValueError):
            _buzz_mod._parse_buzz_message_link(trailing_slash)

    def test_rejects_channel_uuid_with_invalid_variant(self):
        invalid_channel = CHANNEL[:19] + "1" + CHANNEL[20:]
        invalid_link = self.LINK.replace(CHANNEL, invalid_channel)

        with pytest.raises(ValueError):
            _buzz_mod._parse_buzz_message_link(invalid_link)

    def test_normalizes_uppercase_identifiers_like_buzz(self):
        uppercase_link = (
            "buzz://message?"
            f"channel={CHANNEL.upper()}&"
            f"id={'A' * 64}&"
            f"thread={'B' * 64}"
        )

        assert _buzz_mod._parse_buzz_message_link(uppercase_link) == {
            "channel": CHANNEL,
            "id": "a" * 64,
            "thread": "b" * 64,
        }

    def test_normalizes_uri_scheme_and_host_like_buzz(self):
        uppercase_authority = self.LINK.replace("buzz://message", "BUZZ://MESSAGE")

        assert _buzz_mod._parse_buzz_message_link(uppercase_authority) == {
            "channel": CHANNEL,
            "id": "1" * 64,
            "thread": "2" * 64,
        }

    @pytest.mark.parametrize(
        "noncanonical",
        [
            f"{LINK}#",
            f" {LINK}",
            f"{LINK} ",
            LINK.replace("buzz://message", "buzz:\n//message"),
            LINK.replace("buzz://message", "buzz://mes\tsage"),
        ],
    )
    def test_rejects_inputs_urlsplit_would_normalize(self, noncanonical):
        with pytest.raises(ValueError):
            _buzz_mod._parse_buzz_message_link(noncanonical)

    @pytest.mark.asyncio
    async def test_handler_returns_safe_error_for_invalid_link(self):
        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": "not-a-link"})
        )
        assert result == {"error": "expected a canonical buzz://message link"}

    @pytest.mark.asyncio
    async def test_handler_fails_closed_when_buzz_is_unconfigured(self, monkeypatch):
        async def unexpected_exec(*args, **kwargs):
            raise AssertionError("CLI must not run without Buzz configuration")

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", unexpected_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {"error": "Buzz is not configured"}

    def test_classifies_only_marked_replies_as_thread_members(self):
        root = "a" * 64
        parent = "b" * 64

        assert (
            _buzz_mod._buzz_message_thread_root(
                {
                    "tags": [
                        ["e", root, "", "root"],
                        ["e", parent, "", "reply"],
                    ]
                }
            )
            == root
        )
        assert (
            _buzz_mod._buzz_message_thread_root(
                {"tags": [["e", parent, "", "reply"]]}
            )
            == parent
        )
        assert (
            _buzz_mod._buzz_message_thread_root(
                {"tags": [["e", root, "", "root"]]}
            )
            == ""
        )
        assert (
            _buzz_mod._buzz_message_thread_root({"tags": [["e", root]]}) == ""
        )

    @pytest.mark.asyncio
    async def test_reads_exact_link_without_putting_secret_in_argv(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        target = {
            "id": "1" * 64,
            "pubkey": "3" * 64,
            "content": "linked report",
            "created_at": 1234,
            "kind": 9,
            "tags": [
                ["h", CHANNEL],
                ["e", "2" * 64, "", "root"],
                ["e", "4" * 64, "", "reply"],
            ],
        }
        captured = {}

        async def fake_exec(cli_path, args, **kwargs):
            captured.update(cli_path=cli_path, args=list(args), **kwargs)
            return 0, json.dumps([target]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        raw = await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})

        assert json.loads(raw) == {
            "channel": CHANNEL,
            "id": "1" * 64,
            "thread": "2" * 64,
            "pubkey": "3" * 64,
            "created_at": 1234,
            "content": "linked report",
        }
        assert captured["args"] == [
            "messages",
            "thread",
            "--channel",
            CHANNEL,
            "--event",
            "1" * 64,
            "--limit",
            "1",
        ]
        assert captured["relay_url"] == "https://relay.example"
        assert captured["private_key"] == "nsec1secret"
        assert all("nsec1secret" not in str(arg) for arg in captured["args"])
        assert "nsec1secret" not in raw

    @pytest.mark.asyncio
    async def test_cli_failure_returns_safe_error_without_stderr(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        async def fake_exec(*args, **kwargs):
            return 2, "", '{"error":"auth","message":"rejected nsec1secret"}'

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        raw = await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})

        assert json.loads(raw) == {"error": "Buzz CLI failed (exit 2)"}
        assert "nsec1secret" not in raw
        assert "rejected" not in raw

    @pytest.mark.asyncio
    async def test_queries_exact_event_id_without_timestamp_pagination(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        target = {
            "id": "1" * 64,
            "content": "older exact event",
            "created_at": 499,
            "tags": [
                ["h", CHANNEL],
                ["e", "2" * 64, "", "root"],
                ["e", "4" * 64, "", "reply"],
            ],
        }
        calls = []

        async def fake_exec(cli_path, args, **kwargs):
            calls.append(list(args))
            if args != [
                "messages",
                "thread",
                "--channel",
                CHANNEL,
                "--event",
                "1" * 64,
                "--limit",
                "1",
            ]:
                raise AssertionError("lookup did not use the exact event-id query")
            return 0, json.dumps([target]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result["id"] == "1" * 64
        assert result["content"] == "older exact event"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_rejects_exact_event_from_another_channel(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        event = {
            "id": "1" * 64,
            "content": "wrong channel",
            "created_at": 1234,
            "tags": [
                ["h", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
                ["e", "2" * 64, "", "reply"],
            ],
        }

        async def fake_exec(*args, **kwargs):
            return 0, json.dumps([event]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {
            "error": "linked event does not belong to the requested channel"
        }

    @pytest.mark.asyncio
    async def test_rejects_event_outside_linked_thread(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        event = {
            "id": "1" * 64,
            "content": "wrong thread",
            "created_at": 1234,
            "tags": [
                ["h", CHANNEL],
                ["e", "4" * 64, "", "root"],
                ["e", "5" * 64, "", "reply"],
            ],
        }

        async def fake_exec(*args, **kwargs):
            return 0, json.dumps([event]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {
            "error": "linked event does not belong to the requested thread"
        }

    @pytest.mark.asyncio
    async def test_reply_cannot_claim_its_own_id_as_thread_root(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        event = {
            "id": "1" * 64,
            "content": "reply with another root",
            "created_at": 1234,
            "tags": [
                ["h", CHANNEL],
                ["e", "4" * 64, "", "root"],
                ["e", "5" * 64, "", "reply"],
            ],
        }
        self_root_link = (
            "buzz://message?"
            f"channel={CHANNEL}&"
            f"id={'1' * 64}&"
            f"thread={'1' * 64}"
        )

        async def fake_exec(*args, **kwargs):
            return 0, json.dumps([event]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self_root_link})
        )

        assert result == {
            "error": "linked event does not belong to the requested thread"
        }

    @pytest.mark.asyncio
    async def test_returns_safe_not_found_error(self, monkeypatch, tmp_path):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        async def fake_exec(*args, **kwargs):
            return 0, json.dumps([]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {"error": "linked Buzz message was not found"}


    @pytest.mark.asyncio
    async def test_reads_connection_from_standard_yaml_config(
        self, monkeypatch, tmp_path
    ):
        import hermes_cli.config as config_mod

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setattr(
            config_mod,
            "load_config",
            lambda: {
                "gateway": {
                    "platforms": {
                        "buzz": {
                            "enabled": True,
                            "extra": {
                                "relay_url": "https://yaml-relay.example",
                                "cli_path": str(fake_cli),
                            },
                        }
                    }
                }
            },
        )
        captured = {}
        event = {
            "id": "1" * 64,
            "content": "yaml configured",
            "created_at": 1234,
            "tags": [
                ["h", CHANNEL],
                ["e", "2" * 64, "", "root"],
                ["e", "4" * 64, "", "reply"],
            ],
        }

        async def fake_exec(cli_path, args, **kwargs):
            captured.update(cli_path=cli_path, **kwargs)
            return 0, json.dumps([event]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result["content"] == "yaml configured"
        assert captured["cli_path"] == str(fake_cli)
        assert captured["relay_url"] == "https://yaml-relay.example"

    @pytest.mark.asyncio
    async def test_rejects_malformed_cli_payload(self, monkeypatch, tmp_path):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        async def fake_exec(*args, **kwargs):
            return 0, '{"unexpected":"object"}', ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {"error": "buzz messages thread returned malformed data"}

    @pytest.mark.asyncio
    async def test_rejects_matching_event_with_malformed_tags(
        self, monkeypatch, tmp_path
    ):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://relay.example")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1secret")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        event = {
            "id": "1" * 64,
            "content": "malformed event",
            "created_at": 1234,
            "tags": None,
        }

        async def fake_exec(*args, **kwargs):
            return 0, json.dumps([event]), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = json.loads(
            await _buzz_mod._handle_buzz_read_message_link({"link": self.LINK})
        )

        assert result == {
            "error": "buzz messages thread returned malformed event data"
        }


class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_success(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured.update(cli_path=cli_path, args=args, relay_url=relay_url, input_text=input_text)
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(PlatformConfig(enabled=True, extra={}), CHANNEL, "cron says hi")
        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][:2] == ["messages", "send"]
        assert captured["input_text"] == "cron says hi"
        # The private key must never be part of argv
        assert all("nsec1x" not in str(a) for a in captured["args"])


