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
    "BUZZ_REQUIRE_MENTION",
    "BUZZ_MENTION_REQUIRED_USERS",
    "BUZZ_MENTION_ALIASES",
    "BUZZ_MAX_AGENT_HOPS",
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [
            {"mention_required_users": ["not-a-pubkey"], "max_agent_hops": 4},
            {"mention_required_users": ["   "], "max_agent_hops": 4},
            {"mention_required_users": [OTHER_PUBKEY], "max_agent_hops": 0},
            {"mention_aliases": {"Warren": "not-a-pubkey"}},
            {"mention_aliases": {"Bot": OTHER_PUBKEY, "bot": "b" * 64}},
            {"max_agent_hops": float("inf")},
            {"max_agent_hops": float("nan")},
            {"max_agent_hops": 1.5},
            {"max_agent_hops": -1},
            {"max_agent_hops": True},
            {"max_agent_hops": "bad"},
            {"max_agent_hops": "9" * 5000},
        ],
    )
    async def test_invalid_mutual_agent_config_fails_closed(self, extra):
        adapter = _make_adapter(extra)

        assert await adapter.connect() is False
        assert adapter.fatal_error_code == "config_invalid"
        assert adapter.fatal_error_retryable is False

    @pytest.mark.asyncio
    async def test_blank_mention_required_env_override_fails_closed(self, monkeypatch):
        monkeypatch.setenv("BUZZ_MENTION_REQUIRED_USERS", "   ")
        adapter = _make_adapter({
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })

        assert await adapter.connect() is False
        assert adapter.fatal_error_code == "config_invalid"
        assert adapter.fatal_error_retryable is False


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
    async def test_seed_reconstructs_agent_reply_hops(self):
        second_agent = "b" * 64
        adapter = _make_adapter({"mention_required_users": [OTHER_PUBKEY, second_agent]})
        adapter._dispatched = []

        first = _event("e1", pubkey=OTHER_PUBKEY, content="@Chip first", created_at=100)
        second = _event("e2", pubkey=second_agent, content="@Chip second", created_at=101)
        second["tags"].append(["e", "e1", "", "reply"])
        cli = _ScriptedCli()
        cli.script("messages", "get", [first, second])
        adapter._run_cli = cli

        await adapter._seed_channel(CHANNEL, chat_type="group")

        assert adapter._agent_hops["e1"] == 1
        assert adapter._agent_hops["e2"] == 2

    @pytest.mark.asyncio
    async def test_seed_counts_own_profile_events_as_agent_hops(self):
        adapter = _make_adapter({
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })
        first = _event("e1", pubkey=OTHER_PUBKEY, content="@Chip first", created_at=100)
        own_reply = _event("e2", pubkey=SELF_PUBKEY, content="reply", created_at=101)
        own_reply["tags"].append(["e", "e1", "", "reply"])
        third = _event("e3", pubkey=OTHER_PUBKEY, content="@Chip third", created_at=102)
        third["tags"].append(["e", "e2", "", "reply"])
        cli = _ScriptedCli()
        cli.script("messages", "get", [first, own_reply, third])
        adapter._run_cli = cli

        await adapter._seed_channel(CHANNEL, chat_type="group")

        assert adapter._agent_hops == {"e1": 1, "e2": 2, "e3": 3}

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

    @pytest.mark.asyncio
    async def test_configured_bot_sender_requires_tagged_mention(self):
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY],
        })
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}

        cli = _ScriptedCli()
        text_only = _event("e1", content="@Chip text without a p-tag", created_at=10)
        bare = _event("e2", content="Chip bare name with a p-tag", created_at=11)
        bare["tags"].append(["p", SELF_PUBKEY])
        tagged = _event("e3", content="@Chip tagged", created_at=12)
        tagged["tags"].append(["p", SELF_PUBKEY])
        cli.script("messages", "get", [text_only, bare, tagged])
        adapter._run_cli = cli

        await adapter._poll_channel(CHANNEL)

        assert [d["message_id"] for d in adapter._dispatched] == ["e3"]

    @pytest.mark.asyncio
    async def test_configured_self_alias_counts_as_visible_mention(self):
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY],
            "mention_aliases": {"Warren": SELF_PUBKEY},
        })
        adapter._display_name = "Warren · Hermes"
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        event = _event("e1", content="@Warren please review", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])
        cli = _ScriptedCli()
        cli.script("messages", "get", [event])
        adapter._run_cli = cli

        await adapter._poll_channel(CHANNEL)

        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]

    @pytest.mark.asyncio
    async def test_longer_peer_alias_does_not_match_self_alias_prefix(self):
        second_agent = "b" * 64
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY],
            "mention_aliases": {
                "Bot": SELF_PUBKEY,
                "Bot-2": second_agent,
            },
        })
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        state = {"chat_type": "group", "last_ts": 0, "seen": {}}
        event = _event("e-prefix", content="@Bot-2 please review", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])

        await adapter._handle_event(CHANNEL, state, event)

        assert adapter._dispatched == []

    def test_configured_self_alias_is_stripped_from_leading_prompt(self):
        adapter = _make_adapter({"mention_aliases": {"Warren": SELF_PUBKEY}})
        adapter._display_name = "Warren · Hermes"

        assert adapter._strip_mention("@Warren please review") == "please review"

    @pytest.mark.asyncio
    async def test_agent_reply_chain_stops_after_configured_hop_limit(self):
        second_agent = "b" * 64
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY, second_agent],
            "max_agent_hops": 2,
        })
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}

        first = _event("e1", pubkey=OTHER_PUBKEY, content="@Chip first", created_at=10)
        first["tags"].append(["p", SELF_PUBKEY])
        second = _event("e2", pubkey=second_agent, content="@Chip second", created_at=11)
        second["tags"] += [["e", "e1", "", "reply"], ["p", SELF_PUBKEY]]
        third = _event("e3", pubkey=OTHER_PUBKEY, content="@Chip third", created_at=12)
        third["tags"] += [["e", "e2", "", "reply"], ["p", SELF_PUBKEY]]
        cli = _ScriptedCli()
        cli.script("messages", "get", [first, second, third])
        adapter._run_cli = cli

        await adapter._poll_channel(CHANNEL)

        assert [d["message_id"] for d in adapter._dispatched] == ["e1", "e2"]

    @pytest.mark.asyncio
    async def test_reverse_order_poll_still_enforces_agent_hop_limit(self):
        second_agent = "b" * 64
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY, second_agent],
            "max_agent_hops": 2,
        })
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}

        first = _event("e1", pubkey=OTHER_PUBKEY, content="@Chip first", created_at=10)
        first["tags"].append(["p", SELF_PUBKEY])
        second = _event("e2", pubkey=second_agent, content="@Chip second", created_at=10)
        second["tags"] += [["e", "e1", "", "reply"], ["p", SELF_PUBKEY]]
        third = _event("e3", pubkey=OTHER_PUBKEY, content="@Chip third", created_at=10)
        third["tags"] += [["e", "e2", "", "reply"], ["p", SELF_PUBKEY]]
        cli = _ScriptedCli()
        cli.script("messages", "get", [third, second, first])
        adapter._run_cli = cli

        await adapter._poll_channel(CHANNEL)

        assert [d["message_id"] for d in adapter._dispatched] == ["e1", "e2"]

    @pytest.mark.asyncio
    async def test_push_event_with_unknown_reply_parent_fails_closed(self):
        adapter = _make_adapter({
            "require_mention": False,
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 2,
        })
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        state = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._channel_state[CHANNEL] = state

        child = _event("child", content="@Chip child", created_at=11)
        child["tags"] += [["e", "missing-parent", "", "reply"], ["p", SELF_PUBKEY]]
        parent = _event("parent", content="@Chip parent", created_at=10)
        parent["tags"].append(["p", SELF_PUBKEY])

        await adapter._handle_event(CHANNEL, state, child)
        await adapter._handle_event(CHANNEL, state, parent)

        assert [d["message_id"] for d in adapter._dispatched] == ["parent"]


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
    async def test_send_adds_explicit_pubkey_for_configured_agent_alias(self):
        adapter = _make_adapter({"mention_aliases": {"Warren": OTHER_PUBKEY}})
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-mention"})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "Please ask @Warren to review this.")

        assert result.success is True
        args, _stdin_text = cli.calls[0]
        mention_index = args.index("--mention")
        assert args[mention_index + 1] == OTHER_PUBKEY

    @pytest.mark.asyncio
    async def test_send_longer_alias_does_not_tag_prefix_alias(self):
        second_agent = "b" * 64
        adapter = _make_adapter({
            "mention_aliases": {
                "Bot": OTHER_PUBKEY,
                "Bot-2": second_agent,
            }
        })
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-mention"})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "@Bot-2 please review")

        assert result.success is True
        args, _stdin_text = cli.calls[0]
        mentions = [args[index + 1] for index, value in enumerate(args) if value == "--mention"]
        assert mentions == [second_agent]

    @pytest.mark.asyncio
    async def test_send_with_agent_mention_recovers_missing_reply_parent(self):
        adapter = _make_adapter({
            "mention_aliases": {"Warren": OTHER_PUBKEY},
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._active_agent_events[CHANNEL] = ("agent-parent", 2)
        adapter._agent_hops["agent-parent"] = 2
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-recovered-parent"})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "@Warren continue this delegation")

        assert result.success is True
        args, _stdin_text = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "agent-parent"
        assert adapter._agent_hops["evt-recovered-parent"] == 3

    @pytest.mark.asyncio
    async def test_send_with_agent_mention_overrides_stale_lower_hop_parent(self):
        adapter = _make_adapter({
            "mention_aliases": {"Warren": OTHER_PUBKEY},
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._active_agent_events[CHANNEL] = ("current-agent-parent", 4)
        adapter._agent_hops.update({"stale-parent": 1, "current-agent-parent": 4})
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-safe-parent"})
        adapter._run_cli = cli

        result = await adapter.send(
            CHANNEL,
            "@Warren continue this delegation",
            reply_to="stale-parent",
        )

        assert result.success is True
        args, _stdin_text = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "current-agent-parent"
        assert adapter._agent_hops["evt-safe-parent"] == 5

    @pytest.mark.asyncio
    async def test_new_agent_root_replaces_higher_hop_active_chain(self):
        adapter = _make_adapter({
            "require_mention": False,
            "mention_aliases": {"Self": SELF_PUBKEY, "Peer": OTHER_PUBKEY},
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })
        adapter._active_agent_events[CHANNEL] = ("old-chain", 3)
        adapter._agent_hops["old-chain"] = 3
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        state = {"chat_type": "group", "last_ts": 0, "seen": {}}
        new_root = _event("new-root", content="@Self start another task", created_at=20)
        new_root["tags"].append(["p", SELF_PUBKEY])

        await adapter._handle_event(CHANNEL, state, new_root)

        assert adapter._active_agent_events[CHANNEL] == ("new-root", 1)

        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "new-reply"})
        adapter._run_cli = cli
        result = await adapter.send(CHANNEL, "@Peer continue", reply_to="new-root")

        assert result.success is True
        args, _stdin_text = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "new-root"
        assert adapter._agent_hops["new-reply"] == 2

    @pytest.mark.asyncio
    async def test_authorized_human_message_clears_active_agent_chain(self):
        adapter = _make_adapter({
            "mention_required_users": [OTHER_PUBKEY],
            "mention_aliases": {"Self": SELF_PUBKEY},
            "max_agent_hops": 4,
            "require_mention": False,
        })
        adapter._active_agent_events[CHANNEL] = ("old-agent-event", 3)
        adapter._allowed_pubkeys = set()
        state = {"chat_type": "group", "last_ts": 0, "seen": {}}

        await adapter._handle_event(CHANNEL, state, _event("human-root", "human-key", "new request"))

        assert CHANNEL not in adapter._active_agent_events

    @pytest.mark.asyncio
    async def test_send_records_outbound_agent_hop_from_reply_parent(self):
        adapter = _make_adapter({
            "mention_required_users": [OTHER_PUBKEY],
            "max_agent_hops": 4,
        })
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._agent_hops["parent-event"] = 2
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-hop"})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "reply", reply_to="parent-event")

        assert result.success is True
        assert adapter._agent_hops["evt-hop"] == 3


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

    @pytest.mark.asyncio
    async def test_send_image_applies_agent_mentions_and_hop_accounting(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter({
            "mention_aliases": {"Warren": OTHER_PUBKEY},
            "max_agent_hops": 4,
        })
        adapter._agent_hops["parent-event"] = 2
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-image-hop"})
        adapter._run_cli = cli

        result = await adapter.send_image(
            CHANNEL,
            str(img),
            caption="@Warren review this",
            reply_to="parent-event",
        )

        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--mention") + 1] == OTHER_PUBKEY
        assert adapter._agent_hops["evt-image-hop"] == 3


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

    @pytest.mark.asyncio
    async def test_standalone_send_resolves_configured_agent_alias(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured["args"] = args
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        config = PlatformConfig(
            enabled=True,
            extra={"mention_aliases": {"Warren": OTHER_PUBKEY}},
        )

        result = await _standalone_send(config, CHANNEL, "@Warren review")

        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][captured["args"].index("--mention") + 1] == OTHER_PUBKEY

    @pytest.mark.asyncio
    async def test_standalone_media_send_resolves_configured_agent_alias(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        media = tmp_path / "proof.png"
        media.write_bytes(b"png")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured["args"] = args
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        config = PlatformConfig(
            enabled=True,
            extra={"mention_aliases": {"Warren": OTHER_PUBKEY}},
        )

        result = await _standalone_send(
            config,
            CHANNEL,
            "@Warren see attachment",
            media_files=[str(media)],
        )

        assert result == {"success": True, "message_id": "evt-cron"}
        args = captured["args"]
        assert args[args.index("--mention") + 1] == OTHER_PUBKEY
        assert args[args.index("--file") + 1] == str(media)
