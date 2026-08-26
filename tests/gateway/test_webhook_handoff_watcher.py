"""End-to-end routing invariants for webhook session handoff processing."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.profile_routing import ProfileRoute
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.webhook import WebhookAdapter
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.run import GatewayRunner
from gateway.session import (
    AsyncSessionStore,
    SessionSource,
    SessionStore,
    build_session_key,
)
from hermes_state import AsyncSessionDB, SessionDB


def _successful_handle_message(response):
    async def _handle(event):
        event.agent_run_failed = False
        return response

    return AsyncMock(side_effect=_handle)


def _discord_config(
    tmp_path,
    *,
    thread_sessions_per_user=False,
    home_user_id=None,
    home_scope_id="guild-1",
    multiplex_profiles=False,
    profile_routes=None,
):
    return GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        thread_sessions_per_user=thread_sessions_per_user,
        multiplex_profiles=multiplex_profiles,
        profile_routes=profile_routes or [],
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="test-token",
                home_channel=HomeChannel(
                    platform=Platform.DISCORD,
                    chat_id="parent-1",
                    name="Hermes Home",
                    user_id=home_user_id,
                    scope_id=home_scope_id,
                ),
            )
        },
    )


def _runner_with_store(config, store, db):
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)

    adapter = SimpleNamespace(
        create_handoff_thread=AsyncMock(return_value="thread-42"),
        get_chat_info=AsyncMock(
            return_value={
                "name": "Hermes Home",
                "type": "channel",
                "guild_id": "guild-1",
            }
        ),
        send=AsyncMock(return_value=SimpleNamespace(success=True)),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._evict_cached_agent = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._handle_message = _successful_handle_message(
        "Ready in the handoff thread."
    )
    return runner, adapter


class _ExecutorHandoffAdapter(BasePlatformAdapter):
    """Minimal real adapter surface for executor/cancellation boundary tests."""

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="test-token"),
            Platform.DISCORD,
        )
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def create_handoff_thread(self, chat_id, thread_name):
        return "thread-42"

    async def get_chat_info(self, chat_id):
        return {
            "name": "Hermes Home",
            "type": "channel",
            "guild_id": "guild-1",
        }

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="handoff-response")

    async def send_typing(self, chat_id, metadata=None):
        return None


def _configure_executor_runner(runner, adapter):
    """Install the minimal real-agent surface used by executor lifecycle tests."""
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._agent_cache = None
    runner._agent_cache_lock = None
    runner._draining = False
    runner._update_runtime_status = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )


class _RunningOnce:
    def __init__(self):
        self._states = iter([True, False])

    def __bool__(self):
        return next(self._states, False)


def _webhook_handoff_owner_json(
    store,
    session_key,
    *,
    token,
    pid,
    process_start_time,
):
    from gateway.drain_control import current_instantiation_epoch
    from hermes_state import _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL

    return json.dumps(
        {
            "token": token,
            "pid": pid,
            "process_start_time": process_start_time,
            "host": socket.gethostname().strip(),
            "instantiation_epoch": current_instantiation_epoch(),
            "lock_protocol": _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL,
            "routing_scope": store._routing_scope(),
            "source_session_key": session_key,
            "active_session_key": session_key,
        }
    )


def _webhook_handoff_owner_key(session_id):
    return f"webhook_handoff_owner:{session_id}"


_HARD_EXIT_WEBHOOK_CLAIM_SCRIPT = """
import json
import os
import socket
import sys
from pathlib import Path

from gateway.drain_control import current_instantiation_epoch
from gateway.status import get_process_start_time
from hermes_state import SessionDB

db_path, session_id, session_key, routing_scope = sys.argv[1:5]
pid = os.getpid()
process_start_time = get_process_start_time(pid)
if process_start_time is None or process_start_time <= 0:
    os._exit(11)
owner = {
    "token": "real-child-hard-exit",
    "pid": pid,
    "process_start_time": process_start_time,
    "host": socket.gethostname().strip(),
    "instantiation_epoch": current_instantiation_epoch(),
    "routing_scope": routing_scope,
    "source_session_key": session_key,
    "active_session_key": session_key,
}
db = SessionDB(db_path=Path(db_path))
claimed = db.claim_webhook_handoff(
    session_id,
    json.dumps(owner, sort_keys=True, separators=(",", ":")),
)
os._exit(0 if claimed else 12)
"""


async def _run_handoff_watcher_once(runner):
    runner._running = _RunningOnce()
    await GatewayRunner._handoff_watcher(runner, interval=0)


@pytest.mark.asyncio
async def test_webhook_handoff_moves_exact_session_and_next_reply_reuses_it(tmp_path):
    """The source key disappears and an organic thread event sees the same ID."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:build-finished:delivery-7",
        chat_name="build-finished",
        chat_type="dm",
        user_id="build-finished",
    )
    source_entry = store.get_or_create_session(source)
    source_key = source_entry.session_key
    session_id = source_entry.session_id
    db.append_message(session_id, "user", "Build 7 finished successfully")
    db.append_message(
        session_id,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
    )
    db.append_message(
        session_id,
        "tool",
        "checks: all green",
        tool_name="terminal",
        tool_call_id="call-1",
    )
    db.append_message(
        session_id,
        "assistant",
        "I verified the release artifacts.",
    )

    runner, adapter = _runner_with_store(config, store, db)
    row = db.get_session(session_id)
    row.update(
        {
            "handoff_platform": "discord",
            "_webhook_handoff_request": "discord",
            "source": "webhook",
            "session_key": source_key,
            "title": "Build 7",
        }
    )

    await runner._process_handoff(row)

    destination_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-42",
        chat_name="Hermes Home",
        chat_type="thread",
        user_id="someone-replying",
        thread_id="thread-42",
        scope_id="guild-1",
        parent_chat_id="parent-1",
    )
    destination_key = build_session_key(destination_source)

    assert store.lookup_by_session_key(source_key) is None
    moved = store.lookup_by_session_key(destination_key)
    assert moved is not None
    assert moved.session_id == session_id
    assert moved.origin is not None
    assert moved.origin.platform == Platform.DISCORD
    assert moved.origin.chat_id == "thread-42"
    assert moved.origin.thread_id == "thread-42"
    assert moved.origin.parent_chat_id == "parent-1"

    # This is the exact source shape a subsequent Discord thread event uses.
    next_turn = store.get_or_create_session(destination_source)
    assert next_turn.session_id == session_id

    transcript = store.load_transcript(session_id)
    assert [(message["role"], message["content"]) for message in transcript] == [
        ("user", "Build 7 finished successfully"),
        ("assistant", ""),
        ("tool", "checks: all green"),
        ("assistant", "I verified the release artifacts."),
    ]

    durable = db.get_session(session_id)
    assert durable["source"] == "discord"
    assert durable["session_key"] == destination_key
    assert durable["chat_id"] == "thread-42"
    assert durable["chat_type"] == "thread"
    assert durable["thread_id"] == "thread-42"

    synthetic_event = runner._handle_message.await_args.args[0]
    assert synthetic_event.source == moved.origin
    assert "from CLI" not in synthetic_event.text
    adapter.send.assert_awaited_once_with(
        "parent-1",
        "Ready in the handoff thread.",
        metadata={"thread_id": "thread-42"},
    )
    db.close()


@pytest.mark.asyncio
async def test_thread_per_user_handoff_keys_to_authenticated_home_user(tmp_path):
    """The production global setting keys handoff and next reply identically."""
    config = _discord_config(
        tmp_path,
        thread_sessions_per_user=True,
        home_user_id="discord-user-7",
    )
    assert "thread_sessions_per_user" not in config.platforms[Platform.DISCORD].extra
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:alerts:per-user-delivery",
        chat_type="webhook",
        user_id="webhook:alerts",
    )
    entry = store.get_or_create_session(source)
    runner, _adapter = _runner_with_store(config, store, db)
    row = db.get_session(entry.session_id)
    row.update(
        {
            "handoff_platform": "discord",
            "_webhook_handoff_request": "discord",
            "source": "webhook",
            "session_key": entry.session_key,
        }
    )

    await runner._process_handoff(row)

    next_reply_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-42",
        chat_name="Hermes Home",
        chat_type="thread",
        user_id="discord-user-7",
        thread_id="thread-42",
        scope_id="guild-1",
        parent_chat_id="parent-1",
    )
    next_reply_key = runner._session_key_for_source(next_reply_source)
    moved = store.lookup_by_session_key(next_reply_key)

    assert moved is not None
    assert moved.session_id == entry.session_id
    assert moved.origin is not None
    assert moved.origin.user_id == "discord-user-7"
    assert store.get_or_create_session(next_reply_source).session_id == entry.session_id
    assert next_reply_key.endswith(":discord-user-7")
    db.close()


@pytest.mark.asyncio
async def test_multiplex_default_handoff_matches_unprofiled_organic_reply(
    tmp_path,
):
    """An unprefixed default webhook and Discord reply share one namespace."""
    config = _discord_config(tmp_path, multiplex_profiles=True)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:alerts:default-profile",
        chat_type="webhook",
        user_id="webhook:alerts",
    )
    with patch(
        "hermes_cli.profiles.get_active_profile_name",
        return_value="default",
    ):
        entry = store.get_or_create_session(source)
        runner, _adapter = _runner_with_store(config, store, db)
        row = db.get_session(entry.session_id)
        row.update(
            {
                "handoff_platform": "discord",
                "_webhook_handoff_request": "discord",
                "source": "webhook",
                "session_key": entry.session_key,
            }
        )

        await runner._process_handoff(row)

        next_reply = SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-42",
            chat_name="Hermes Home",
            chat_type="thread",
            user_id="discord-user",
            thread_id="thread-42",
            scope_id="guild-1",
            guild_id="guild-1",
            parent_chat_id="parent-1",
        )
        destination_key = runner._session_key_for_source(next_reply)
        resumed = store.get_or_create_session(next_reply)

    assert entry.session_key.startswith("agent:main:")
    assert destination_key.startswith("agent:main:")
    assert resumed.session_id == entry.session_id
    assert store.lookup_by_session_key(entry.session_key) is None
    db.close()


@pytest.mark.asyncio
async def test_named_destination_profile_route_is_rejected_before_thread(
    tmp_path,
):
    """A named-profile Discord home cannot receive a default-profile handoff."""
    config = _discord_config(
        tmp_path,
        home_scope_id=None,
        multiplex_profiles=True,
        profile_routes=[
            ProfileRoute(
                name="work-guild",
                platform="discord",
                guild_id="guild-1",
                profile="work",
            )
        ],
    )
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:alerts:named-destination",
        chat_type="webhook",
        user_id="webhook:alerts",
        profile="default",
    )
    entry = store.get_or_create_session(source)
    runner, adapter = _runner_with_store(config, store, db)
    row = db.get_session(entry.session_id)
    row.update(
        {
            "handoff_platform": "discord",
            "_webhook_handoff_request": "discord",
            "source": "webhook",
            "session_key": entry.session_key,
        }
    )

    with pytest.raises(RuntimeError, match="named profile route 'work-guild'"):
        await runner._process_handoff(row)

    assert store.peek_session_id(entry.session_key) == entry.session_id
    adapter.get_chat_info.assert_awaited_once_with("parent-1")
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_relay_handoff_reuses_persisted_scope_for_profile_routing(
    tmp_path,
):
    """Relay homes use /sethome provenance without an unsupported info probe."""
    config = GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        multiplex_profiles=True,
        profile_routes=[
            ProfileRoute(
                name="default-guild",
                platform="discord",
                guild_id="guild-1",
                profile="default",
            )
        ],
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=False,
                home_channel=HomeChannel(
                    platform=Platform.DISCORD,
                    chat_id="parent-1",
                    name="Relay Discord Home",
                    user_id="discord-user-7",
                    scope_id="guild-1",
                ),
            ),
            Platform.RELAY: PlatformConfig(enabled=True),
        },
    )
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:alerts:relay-profile-route",
        chat_type="webhook",
        user_id="webhook:alerts",
        profile="default",
    )
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    descriptor = CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="discord",
        label="Discord",
        max_message_length=2000,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="discord",
        len_unit="chars",
        supported_ops=("send", "thread_create"),
    )
    sent = []

    class _ColdRelayTransport:
        _identities = [("discord", "bot-1")]

        async def send_outbound(self, action, *, platform=None):
            sent.append((action, platform))
            if action.get("op") == "thread_create":
                return {"success": True, "thread_id": "thread-42"}
            return {"success": True, "message_id": "message-1"}

    relay = RelayAdapter(
        config.platforms[Platform.RELAY],
        descriptor,
        _ColdRelayTransport(),
    )
    runner.adapters = {Platform.RELAY: relay}
    runner._evict_cached_agent = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._handle_message = _successful_handle_message(
        "Ready in the relay handoff thread."
    )
    row = db.get_session(entry.session_id)
    row.update(
        {
            "handoff_platform": "discord",
            "_webhook_handoff_request": "discord",
            "source": "webhook",
            "session_key": entry.session_key,
        }
    )

    await runner._process_handoff(row)

    next_reply = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-42",
        chat_type="thread",
        user_id="discord-user-7",
        thread_id="thread-42",
        scope_id="guild-1",
        guild_id="guild-1",
        parent_chat_id="parent-1",
    )
    destination_key = runner._session_key_for_source(next_reply)
    assert store.peek_session_id(entry.session_key) is None
    assert store.peek_session_id(destination_key) == entry.session_id
    assert [action["op"] for action, _platform in sent] == [
        "thread_create",
        "send",
    ]
    for action, logical_platform in sent:
        assert logical_platform == "discord"
        assert action["chat_id"] == "parent-1"
        assert action["metadata"]["scope_id"] == "guild-1"
        assert action["metadata"]["user_id"] == "discord-user-7"
    assert sent[1][0]["metadata"]["thread_id"] == "thread-42"
    db.close()


@pytest.mark.asyncio
async def test_destination_occupied_during_thread_creation_is_not_stolen(tmp_path):
    """A Discord reply racing thread publication keeps its newly-created owner."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:alerts:racing-delivery",
        chat_type="webhook",
        user_id="webhook:alerts",
    )
    source_entry = store.get_or_create_session(source)
    runner, adapter = _runner_with_store(config, store, db)
    destination_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-42",
        chat_name="Hermes Home",
        chat_type="thread",
        user_id="fast-replier",
        thread_id="thread-42",
        scope_id="guild-1",
        parent_chat_id="parent-1",
    )
    occupant = None

    async def _create_then_receive_reply(_parent_chat_id, _name):
        nonlocal occupant
        occupant = store.get_or_create_session(destination_source)
        return "thread-42"

    adapter.create_handoff_thread.side_effect = _create_then_receive_reply
    row = db.get_session(source_entry.session_id)
    row.update(
        {
            "handoff_platform": "discord",
            "_webhook_handoff_request": "discord",
            "source": "webhook",
            "session_key": source_entry.session_key,
        }
    )

    with pytest.raises(RuntimeError, match="could not route session key"):
        await runner._process_handoff(row)

    destination_key = runner._session_key_for_source(destination_source)
    assert occupant is not None
    assert store.peek_session_id(source_entry.session_key) == source_entry.session_id
    assert store.peek_session_id(destination_key) == occupant.session_id
    assert occupant.session_id != source_entry.session_id
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_interactive_handoff_keeps_legacy_discord_destination_shape(tmp_path):
    """CLI/TUI handoff does not adopt webhook-only provenance or home identity."""
    config = _discord_config(
        tmp_path,
        thread_sessions_per_user=True,
        home_user_id="discord-home-user",
    )
    # Interactive handoffs historically read the platform extra. Set it only
    # in this direct CLI/TUI regression instead of masking webhook tests in the
    # shared production-shape fixture.
    config.platforms[Platform.DISCORD].extra["thread_sessions_per_user"] = True
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    db.create_session("interactive-session", source="cli")
    runner, _adapter = _runner_with_store(config, store, db)

    await runner._process_handoff(
        {
            "id": "interactive-session",
            "source": "cli",
            "handoff_platform": "discord",
            "title": "Existing CLI session",
        }
    )

    synthetic_source = runner._handle_message.await_args.args[0].source
    assert synthetic_source == SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-42",
        chat_name="Hermes Home",
        chat_type="thread",
        user_id="system:handoff",
        user_name="Handoff",
        thread_id="thread-42",
    )
    destination_key = runner._session_key_for_source(synthetic_source)
    assert destination_key.endswith(":system:handoff")
    assert store.peek_session_id(destination_key) == "interactive-session"
    assert synthetic_source.user_id != config.get_home_channel(Platform.DISCORD).user_id
    db.close()


@pytest.mark.asyncio
async def test_interactive_retry_of_webhook_origin_session_uses_legacy_claim_path(
    tmp_path, monkeypatch
):
    """Producer intent, not historical source, selects autonomous webhook mode."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    entry = store.get_or_create_session(
        SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="webhook:route:later-interactive-retry",
            chat_type="webhook",
            user_id="webhook:route",
        )
    )
    assert db.request_handoff_once(entry.session_id, "discord") is True
    db.complete_handoff(entry.session_id)

    # A CLI/TUI user may explicitly resume this exact historical transcript
    # and request another handoff. request_handoff() clears the autonomous
    # producer marker while leaving the row's original source untouched.
    assert db.request_handoff(entry.session_id, "discord") is True
    [pending] = db.list_pending_handoffs()
    assert pending["source"] == "webhook"
    assert pending["_webhook_handoff_request"] is None
    assert GatewayRunner._is_webhook_handoff_row(pending) is False

    runner, adapter = _runner_with_store(config, store, db)
    states = iter([True, False])

    class _Running:
        def __bool__(self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner._running = _Running()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await GatewayRunner._handoff_watcher(runner, interval=0)

    assert db.get_handoff_state(entry.session_id)["state"] == "completed"
    assert db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is None
    assert store.peek_session_id(entry.session_key) is None
    synthetic_event = runner._handle_message.await_args.args[0]
    destination_key = runner._session_key_for_source(synthetic_event.source)
    assert store.peek_session_id(destination_key) == entry.session_id
    persisted = db.load_gateway_routing_entries(scope=store._routing_scope())
    assert set(
        key
        for key, entry_json in persisted.items()
        if json.loads(entry_json)["session_id"] == entry.session_id
    ) == {destination_key}
    adapter.create_handoff_thread.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_interactive_retry_after_webhook_cleanup_uses_legacy_route_creation(
    tmp_path, monkeypatch
):
    """A CLI-resumed webhook transcript does not require its retired source key."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    entry = store.get_or_create_session(
        SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="webhook:route:failed-then-interactive",
            chat_type="webhook",
            user_id="webhook:route",
        )
    )
    assert db.request_handoff_once(entry.session_id, "discord") is True
    assert db.claim_handoff(entry.session_id) is True
    assert store.remove_session_route_and_end(
        entry.session_key,
        entry.session_id,
        "webhook_handoff_failed",
        handoff_error="initial autonomous handoff failed",
    ) is True
    assert store.peek_session_id(entry.session_key) is None

    # CLI /resume reopens the transcript row but does not recreate a gateway
    # route. Its subsequent interactive /handoff must retain the established
    # destination create+switch behavior despite the row's historical source.
    db.reopen_session(entry.session_id)
    assert db.request_handoff(entry.session_id, "discord") is True
    [pending] = db.list_pending_handoffs()
    assert pending["source"] == "webhook"
    assert pending["_webhook_handoff_request"] is None

    runner, adapter = _runner_with_store(config, store, db)
    runner._running = _RunningOnce()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await GatewayRunner._handoff_watcher(runner, interval=0)

    synthetic_event = runner._handle_message.await_args.args[0]
    destination_key = runner._session_key_for_source(synthetic_event.source)
    assert db.get_handoff_state(entry.session_id)["state"] == "completed"
    assert store.peek_session_id(entry.session_key) is None
    assert store.peek_session_id(destination_key) == entry.session_id
    persisted = db.load_gateway_routing_entries(scope=store._routing_scope())
    assert set(
        key
        for key, entry_json in persisted.items()
        if json.loads(entry_json)["session_id"] == entry.session_id
    ) == {destination_key}
    adapter.create_handoff_thread.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_webhook_handoff_requires_a_destination_thread(tmp_path):
    """Webhook mode never falls back to the legacy parent-channel lane."""
    config = _discord_config(tmp_path)
    store = MagicMock()
    store.move_session_route.side_effect = AssertionError(
        "routing must not move when thread creation failed"
    )
    db = MagicMock()
    runner, adapter = _runner_with_store(config, store, db)
    adapter.create_handoff_thread.return_value = None

    with pytest.raises(RuntimeError, match="could not create a handoff thread"):
        await runner._process_handoff(
            {
                "id": "webhook-session",
                "_webhook_handoff_request": "discord",
                "source": "webhook",
                "session_key": "agent:main:webhook:dm:route:delivery",
                "handoff_platform": "discord",
            }
        )

    adapter.send.assert_not_awaited()
    runner._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_webhook_handoff_removes_route_and_ends_exact_session(tmp_path):
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:failed-delivery",
        chat_type="dm",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    assert db.claim_handoff(entry.session_id) is True
    runner, _adapter = _runner_with_store(config, store, db)
    row = {
        "id": entry.session_id,
        "source": "webhook",
        "session_key": entry.session_key,
    }

    await runner._finalize_failed_webhook_handoff(row, "test failure")

    assert store.lookup_by_session_key(entry.session_key) is None
    durable = db.get_session(entry.session_id)
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    db.close()


@pytest.mark.asyncio
async def test_post_move_send_failure_cleans_compressed_destination(
    tmp_path, monkeypatch
):
    """Cleanup follows a synthetic-turn compression child, not the stale root ID."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:compress-before-send",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    compressed_session_id = "handoff-compression-tip"

    async def _compress_during_synthetic_turn(event):
        destination_key = runner._session_key_for_source(event.source)
        db.end_session(entry.session_id, "compression")
        db.create_session(
            compressed_session_id,
            source="discord",
            parent_session_id=entry.session_id,
        )
        advanced = store.advance_compression_session(
            destination_key,
            entry.session_id,
            compressed_session_id,
        )
        assert advanced is not None
        event.agent_run_failed = False
        return "Compressed, but delivery will fail."

    runner._handle_message = AsyncMock(side_effect=_compress_during_synthetic_turn)
    adapter.send.return_value = SimpleNamespace(
        success=False,
        error="destination rejected message",
    )
    states = iter([True, False])

    class _Running:
        def __bool__(self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner._running = _Running()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await GatewayRunner._handoff_watcher(runner, interval=0)

    destination_source = runner._handle_message.await_args.args[0].source
    destination_key = runner._session_key_for_source(destination_source)
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(destination_key) is None
    assert db.get_session(entry.session_id)["end_reason"] == "compression"
    compressed = db.get_session(compressed_session_id)
    assert compressed["ended_at"] is not None
    assert compressed["end_reason"] == "webhook_handoff_failed"
    assert db.get_session(entry.session_id)["handoff_state"] == "failed"
    adapter.send.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_post_move_cancellation_cleans_destination(tmp_path, monkeypatch):
    """Cancellation after ownership moves cannot leave either routing alias live."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-after-move",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    runner._handle_message = AsyncMock(side_effect=asyncio.CancelledError())
    runner._running = True

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    with pytest.raises(asyncio.CancelledError):
        await GatewayRunner._handoff_watcher(runner, interval=0)

    destination_source = runner._handle_message.await_args.args[0].source
    destination_key = runner._session_key_for_source(destination_source)
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(destination_key) is None
    durable = db.get_session(entry.session_id)
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert durable["handoff_state"] == "failed"
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "termination_mode",
    ["cancellation", "inactivity_timeout"],
    ids=["cancellation", "inactivity-timeout"],
)
async def test_post_move_cancellation_interrupts_and_fences_executor_worker(
    tmp_path, monkeypatch, termination_mode
):
    """Terminal handoff exits wait for late executor writes before cleanup."""
    from gateway import run as gateway_run

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-live-executor",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True

    runner, _unused_adapter = _runner_with_store(config, store, db)
    adapter = _ExecutorHandoffAdapter()
    _configure_executor_runner(runner, adapter)

    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_returned = threading.Event()
    hard_interrupt_called = threading.Event()
    late_external_calls = []
    child_session_id = "cancelled-executor-compression-child"
    agents = []

    class _BlockingCompressionAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False
            self.interrupt_reasons = []
            agents.append(self)

        def hard_interrupt(self, reason=None):
            self.interrupt_reasons.append(reason)
            hard_interrupt_called.set()

        def get_activity_summary(self):
            return {
                "seconds_since_activity": (
                    60.0 if termination_mode == "inactivity_timeout" else 0.0
                ),
                "last_activity_desc": "blocked executor test",
                "current_tool": None,
                "api_call_count": 1,
                "max_iterations": 2,
            }

        def run_conversation(self, _message, **_kwargs):
            worker_started.set()
            assert worker_release.wait(timeout=5)
            # This write is deliberately after asyncio cancellation. Terminal
            # cleanup must wait for the executor and include its late canonical
            # compression child instead of committing a ghost-session window.
            db.end_session(entry.session_id, "compression")
            db.create_session(
                child_session_id,
                source="discord",
                parent_session_id=entry.session_id,
            )
            if not self.interrupt_reasons:
                late_external_calls.append("worker continued after terminal cleanup")
            self.session_id = child_session_id
            worker_returned.set()
            return {
                "final_response": "late executor response",
                "messages": [],
                "api_calls": 1,
                "tools": [],
                "completed": True,
                "response_previewed": False,
            }

        def shutdown_memory_provider(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr("run_agent.AIAgent", _BlockingCompressionAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )

    run_identity = {}

    async def _run_real_executor_turn(event):
        destination_key = runner._session_key_for_source(event.source)
        generation = runner._begin_session_run_generation(destination_key)
        require_quiescence = event.metadata.get(
            "_require_executor_quiescence_on_timeout"
        )
        assert require_quiescence is True
        run_identity.update(key=destination_key, generation=generation)
        return await runner._run_agent(
            message=event.text,
            context_prompt="",
            history=[],
            source=event.source,
            session_id=entry.session_id,
            session_key=destination_key,
            run_generation=generation,
            require_executor_quiescence_on_timeout=require_quiescence,
        )

    runner._handle_message = _run_real_executor_turn
    runner._running = True

    real_sleep = asyncio.sleep
    real_wait = asyncio.wait

    if termination_mode == "inactivity_timeout":
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0.01")
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT_WARNING", "0")

        async def _fast_timeout_poll(awaitables, *, timeout=None, return_when=None):
            if timeout == 5.0:
                timeout = 0.01
            kwargs = {"timeout": timeout}
            if return_when is not None:
                kwargs["return_when"] = return_when
            return await real_wait(awaitables, **kwargs)

        monkeypatch.setattr(gateway_run.asyncio, "wait", _fast_timeout_poll)

    async def _skip_watcher_start_delay(delay):
        if delay == 5:
            return None
        return await real_sleep(delay)

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _skip_watcher_start_delay)
    watcher_task = asyncio.create_task(runner._handoff_watcher(interval=3600))
    assert await asyncio.to_thread(worker_started.wait, 5)

    # Wait until track_agent publishes the concrete agent, otherwise a test
    # could cancel during construction and miss the live-worker contract.
    for _ in range(100):
        state = runner._peek_session_state(run_identity["key"])
        if state is not None and state.turn.agent is agents[0]:
            break
        await real_sleep(0.01)
    else:
        pytest.fail("executor agent was never published as running")

    if termination_mode == "cancellation":
        watcher_task.cancel()
    assert await asyncio.to_thread(hard_interrupt_called.wait, 5)
    if termination_mode == "inactivity_timeout":
        runner._running = False
    await real_sleep(0)
    assert watcher_task.done() is False
    if termination_mode == "cancellation":
        assert runner._is_session_run_current(
            run_identity["key"], run_identity["generation"]
        ) is False
    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "running"
    assert durable["ended_at"] is None
    assert db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is not None
    persisted_before_release = db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )
    assert entry.session_key not in persisted_before_release
    assert run_identity["key"] in persisted_before_release
    assert db.get_session(child_session_id) is None

    worker_release.set()
    assert await asyncio.to_thread(worker_returned.wait, 5)
    if termination_mode == "inactivity_timeout":
        for _ in range(100):
            if db.get_handoff_state(entry.session_id)["state"] == "failed":
                break
            await real_sleep(0.01)
        else:
            pytest.fail("timed-out handoff did not reach terminal cleanup")
        watcher_task.cancel()
    await asyncio.gather(watcher_task, return_exceptions=True)

    # Handoff termination is final only after the sync worker is quiescent.
    assert bool(agents[0].interrupt_reasons) is True
    assert late_external_calls == []
    durable = db.get_session(entry.session_id)
    child = db.get_session(child_session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["end_reason"] == "compression"
    assert child["ended_at"] is not None
    assert child["end_reason"] == "webhook_handoff_failed"
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(run_identity["key"]) is None
    assert db.load_gateway_routing_entries(scope=store._routing_scope()) == {}
    assert adapter.sent == []
    db.close()


@pytest.mark.asyncio
async def test_ordinary_timeout_returns_but_defers_close_for_late_executor_write(
    tmp_path, monkeypatch
):
    """Shutdown stays bounded without closing state.db under a timed-out worker."""
    from gateway import run as gateway_run
    from tests.gateway.restart_test_helpers import make_restart_runner

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="ordinary-timeout",
        chat_type="dm",
        user_id="ordinary-user",
    )
    entry = store.get_or_create_session(source)
    adapter = _ExecutorHandoffAdapter()
    runner, _unused_adapter = make_restart_runner(adapter)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner._restart_drain_timeout = 0.0
    _configure_executor_runner(runner, adapter)
    adapter.disconnect = AsyncMock()

    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_returned = threading.Event()
    hard_interrupt_called = threading.Event()
    late_write_errors = []

    class _BlockingOrdinaryAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False

        def hard_interrupt(self, reason=None):
            hard_interrupt_called.set()

        def get_activity_summary(self):
            return {
                "seconds_since_activity": 60.0,
                "last_activity_desc": "blocked ordinary executor",
                "current_tool": None,
                "api_call_count": 1,
                "max_iterations": 2,
            }

        def run_conversation(self, _message, **_kwargs):
            worker_started.set()
            assert worker_release.wait(timeout=5)
            try:
                db.set_meta("ordinary_timeout_late_write", "committed")
                return {
                    "final_response": "late ordinary response",
                    "messages": [],
                    "api_calls": 1,
                    "tools": [],
                    "completed": True,
                    "response_previewed": False,
                }
            except Exception as exc:
                late_write_errors.append(exc)
                raise
            finally:
                worker_returned.set()

        def shutdown_memory_provider(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr("run_agent.AIAgent", _BlockingOrdinaryAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0.01")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT_WARNING", "0")

    real_wait = asyncio.wait

    async def _fast_timeout_poll(awaitables, *, timeout=None, return_when=None):
        if timeout == 5.0:
            timeout = 0.01
        kwargs = {"timeout": timeout}
        if return_when is not None:
            kwargs["return_when"] = return_when
        return await real_wait(awaitables, **kwargs)

    monkeypatch.setattr(gateway_run.asyncio, "wait", _fast_timeout_poll)
    generation = runner._begin_session_run_generation(entry.session_key)
    run_task = asyncio.create_task(
        runner._run_agent(
            message="ordinary timeout",
            context_prompt="",
            history=[],
            source=source,
            session_id=entry.session_id,
            session_key=entry.session_key,
            run_generation=generation,
            require_executor_quiescence_on_timeout=False,
        )
    )
    close_spy = MagicMock(wraps=db.close)
    db.close = close_spy

    try:
        assert await asyncio.to_thread(worker_started.wait, 5)
        assert await asyncio.to_thread(hard_interrupt_called.wait, 5)
        done, _pending = await real_wait({run_task}, timeout=2)
        assert run_task in done
        result = run_task.result()
        assert result["failed"] is True
        assert "Agent inactive" in result["final_response"]
        assert worker_returned.is_set() is False
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("tools.process_registry.process_registry.kill_all", return_value=0),
            patch("tools.terminal_tool.cleanup_all_environments"),
            patch("tools.browser_tool.cleanup_all_browsers"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await asyncio.wait_for(runner.stop(), timeout=1.0)

        assert worker_returned.is_set() is False
        close_spy.assert_not_called()
    finally:
        worker_release.set()
        assert await asyncio.to_thread(worker_returned.wait, 5)
        if not run_task.done():
            await run_task

    for _ in range(100):
        if close_spy.called:
            break
        await asyncio.sleep(0.01)
    assert late_write_errors == []
    close_spy.assert_called_once_with()

    restarted_db = SessionDB(db_path=tmp_path / "state.db")
    assert restarted_db.get_meta("ordinary_timeout_late_write") == "committed"
    restarted_db.close()


@pytest.mark.asyncio
async def test_prepublication_cancellation_does_not_cache_late_agent(
    tmp_path, monkeypatch
):
    """Cleanup waits for a cancelled constructor and discards its late agent."""
    from gateway import run as gateway_run

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-before-agent-publication",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True

    runner, _unused_adapter = _runner_with_store(config, store, db)
    adapter = _ExecutorHandoffAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._draining = False
    runner._update_runtime_status = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )
    runner._evict_cached_agent = GatewayRunner._evict_cached_agent.__get__(
        runner, GatewayRunner
    )

    constructor_started = threading.Event()
    constructor_release = threading.Event()
    run_conversation_called = threading.Event()
    resources_released = threading.Event()

    class _BlockingConstructorAgent:
        def __init__(self, **kwargs):
            constructor_started.set()
            assert constructor_release.wait(timeout=5)
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.provider = "test-provider"
            self.base_url = "https://example.invalid"
            self.api_mode = "chat_completions"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False

        def hard_interrupt(self, _reason=None):
            return None

        def run_conversation(self, _message, **_kwargs):
            run_conversation_called.set()
            return {
                "final_response": "must not run",
                "messages": [],
                "api_calls": 1,
                "tools": [],
                "completed": True,
            }

        def release_clients(self):
            resources_released.set()

        def shutdown_memory_provider(self):
            resources_released.set()

        def close(self):
            resources_released.set()

    monkeypatch.setattr("run_agent.AIAgent", _BlockingConstructorAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )

    detached_executor_done = asyncio.Event()

    def _record_detached_executor_result(task):
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        detached_executor_done.set()

    monkeypatch.setattr(
        gateway_run,
        "consume_detached_task_result",
        _record_detached_executor_result,
    )

    run_identity = {}

    async def _run_real_executor_turn(event):
        destination_key = runner._session_key_for_source(event.source)
        generation = runner._begin_session_run_generation(destination_key)
        run_identity.update(key=destination_key, generation=generation)
        return await runner._run_agent(
            message=event.text,
            context_prompt="",
            history=[],
            source=event.source,
            session_id=entry.session_id,
            session_key=destination_key,
            run_generation=generation,
        )

    runner._handle_message = _run_real_executor_turn
    runner._running = True

    real_sleep = asyncio.sleep

    async def _skip_watcher_start_delay(delay):
        if delay == 5:
            return None
        return await real_sleep(delay)

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _skip_watcher_start_delay)
    watcher_task = asyncio.create_task(runner._handoff_watcher(interval=3600))
    assert await asyncio.to_thread(constructor_started.wait, 5)

    # The constructor has not returned, so TurnRunner cannot have published
    # the agent through agent_holder or inserted it into the prompt cache.
    assert run_identity["key"] not in runner._agent_cache

    watcher_task.cancel()
    for _ in range(100):
        if not runner._is_session_run_current(
            run_identity["key"], run_identity["generation"]
        ):
            break
        await real_sleep(0.01)
    else:
        pytest.fail("constructor cancellation never fenced the run generation")

    assert watcher_task.done() is False
    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "running"
    assert durable["handoff_error"] is None
    assert durable["ended_at"] is None
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(run_identity["key"]) is not None
    assert db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is not None

    constructor_release.set()
    await asyncio.gather(watcher_task, return_exceptions=True)
    await asyncio.wait_for(detached_executor_done.wait(), timeout=5)
    assert await asyncio.to_thread(resources_released.wait, 1)

    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "handoff processing was cancelled"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(run_identity["key"]) is None
    assert db.load_gateway_routing_entries(scope=store._routing_scope()) == {}
    assert db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is None
    assert run_conversation_called.is_set() is False
    with runner._agent_cache_lock:
        assert run_identity["key"] not in runner._agent_cache
    assert adapter.sent == []
    db.close()


@pytest.mark.asyncio
async def test_generation_invalidation_discards_fresh_agent_without_cancelling_task(
    tmp_path, monkeypatch
):
    """A /stop-style generation bump releases a constructor that lands late."""
    from gateway import run as gateway_run

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="generation-invalidated-constructor",
        chat_type="thread",
        user_id="discord-user",
        thread_id="generation-invalidated-constructor",
        scope_id="guild-1",
        parent_chat_id="parent-1",
    )
    entry = store.get_or_create_session(source)

    runner, _unused_adapter = _runner_with_store(config, store, db)
    runner.adapters = {Platform.DISCORD: _ExecutorHandoffAdapter()}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._draining = False
    runner._update_runtime_status = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )

    constructor_started = threading.Event()
    constructor_release = threading.Event()
    run_conversation_called = threading.Event()
    release_count = 0
    release_lock = threading.Lock()

    class _LateConstructorAgent:
        def __init__(self, **kwargs):
            constructor_started.set()
            assert constructor_release.wait(timeout=5)
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.provider = "test-provider"
            self.base_url = "https://example.invalid"
            self.api_mode = "chat_completions"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False

        def run_conversation(self, _message, **_kwargs):
            run_conversation_called.set()
            raise AssertionError("stale constructor must not start agent work")

        def release_clients(self):
            nonlocal release_count
            with release_lock:
                release_count += 1

    monkeypatch.setattr("run_agent.AIAgent", _LateConstructorAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )

    session_key = entry.session_key
    generation = runner._begin_session_run_generation(session_key)
    run_task = asyncio.create_task(
        runner._run_agent(
            message="do not run",
            context_prompt="",
            history=[],
            source=source,
            session_id=entry.session_id,
            session_key=session_key,
            run_generation=generation,
        )
    )
    assert await asyncio.to_thread(constructor_started.wait, 5)

    runner._invalidate_session_run_generation(
        session_key,
        reason="test /stop during construction",
    )
    constructor_release.set()
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result["interrupted"] is True
    assert result["completed"] is False
    assert run_conversation_called.is_set() is False
    with runner._agent_cache_lock:
        assert session_key not in runner._agent_cache
    with release_lock:
        assert release_count == 1
    db.close()


@pytest.mark.asyncio
async def test_recursive_followup_cancellation_releases_after_inner_worker_once(
    tmp_path, monkeypatch
):
    """Only the recursive frame owning cancellation may release its agent."""
    from gateway import run as gateway_run

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-recursive-cancel",
        chat_type="thread",
        user_id="discord-user",
        thread_id="thread-recursive-cancel",
        scope_id="guild-1",
        parent_chat_id="parent-1",
    )
    entry = store.get_or_create_session(source)
    session_key = entry.session_key

    runner, _unused_adapter = _runner_with_store(config, store, db)
    adapter = _ExecutorHandoffAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._draining = False
    runner._update_runtime_status = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )

    inner_worker_started = threading.Event()
    inner_worker_release = threading.Event()
    inner_worker_returned = threading.Event()
    hard_interrupt_called = threading.Event()
    resource_release_called = threading.Event()
    release_count = 0
    release_count_lock = threading.Lock()
    run_count = 0
    run_count_lock = threading.Lock()

    class _RecursiveAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.provider = "test-provider"
            self.base_url = "https://example.invalid"
            self.api_mode = "chat_completions"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False

        def hard_interrupt(self, _reason=None):
            hard_interrupt_called.set()

        def interrupt(self, _reason=None):
            return None

        def run_conversation(self, _message, **_kwargs):
            nonlocal run_count
            with run_count_lock:
                run_count += 1
                this_run = run_count
            if this_run == 1:
                adapter._pending_messages[session_key] = MessageEvent(
                    text="queued follow-up",
                    message_type=MessageType.TEXT,
                    source=source,
                )
                return {
                    "final_response": "first response",
                    "messages": [],
                    "api_calls": 1,
                    "tools": [],
                    "completed": True,
                    "response_previewed": False,
                }
            inner_worker_started.set()
            assert inner_worker_release.wait(timeout=5)
            inner_worker_returned.set()
            return {
                "final_response": "cancelled follow-up",
                "messages": [],
                "api_calls": 1,
                "tools": [],
                "completed": True,
                "response_previewed": False,
            }

        def release_clients(self):
            nonlocal release_count
            with release_count_lock:
                release_count += 1
            resource_release_called.set()

    monkeypatch.setattr("run_agent.AIAgent", _RecursiveAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )

    generation = runner._begin_session_run_generation(session_key)
    run_task = asyncio.create_task(
        runner._run_agent(
            message="initial turn",
            context_prompt="",
            history=[],
            source=source,
            session_id=entry.session_id,
            session_key=session_key,
            run_generation=generation,
        )
    )
    assert await asyncio.to_thread(inner_worker_started.wait, 5)

    run_task.cancel()
    assert await asyncio.to_thread(hard_interrupt_called.wait, 5)
    await asyncio.sleep(0)

    # The outer executor already completed before it recursed. It must not
    # release the shared cached agent while the inner executor still uses it.
    with release_count_lock:
        assert release_count == 0
    assert inner_worker_returned.is_set() is False

    inner_worker_release.set()
    assert await asyncio.to_thread(inner_worker_returned.wait, 5)
    assert await asyncio.to_thread(resource_release_called.wait, 5)
    await asyncio.gather(run_task, return_exceptions=True)

    with release_count_lock:
        assert release_count == 1
    assert run_count == 2
    assert runner._is_session_run_current(session_key, generation) is False
    with runner._agent_cache_lock:
        assert session_key not in runner._agent_cache
    db.close()


@pytest.mark.asyncio
async def test_gateway_stop_awaits_webhook_cancellation_cleanup_before_db_close(
    tmp_path, monkeypatch
):
    """Shutdown must let the watcher's token-fenced cleanup commit before DB close."""
    from gateway import run as gateway_run
    from tests.gateway.restart_test_helpers import make_restart_runner

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:stop-cancellation",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True

    runner, _shutdown_adapter = make_restart_runner()
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner._webhook_handoff_cleanup_pending = {}
    runner._evict_cached_agent = MagicMock()
    runner._release_running_agent_state = MagicMock()

    processing_started = asyncio.Event()

    async def _block_after_claim(_row):
        processing_started.set()
        await asyncio.Event().wait()

    runner._process_handoff = _block_after_claim

    # Skip only the watcher's startup grace period. Keep shutdown's own sleeps
    # real so this remains a lifecycle-ordering test rather than a busy loop.
    real_sleep = asyncio.sleep

    async def _skip_watcher_start_delay(delay):
        if delay == 5:
            return None
        return await real_sleep(delay)

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _skip_watcher_start_delay)
    watcher_task = asyncio.create_task(runner._handoff_watcher(interval=3600))
    runner._background_tasks.add(watcher_task)
    await asyncio.wait_for(processing_started.wait(), timeout=2)
    assert db.get_handoff_state(entry.session_id)["state"] == "running"

    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.write_runtime_status"),
        patch("tools.process_registry.process_registry.kill_all", return_value=0),
        patch("tools.terminal_tool.cleanup_all_environments"),
        patch("tools.browser_tool.cleanup_all_browsers"),
        patch("agent.auxiliary_client.shutdown_cached_clients"),
    ):
        await runner.stop()

    await asyncio.gather(watcher_task, return_exceptions=True)

    restarted_db = SessionDB(db_path=db_path)
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "handoff processing was cancelled"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted_db.load_gateway_routing_entries(scope=store._routing_scope()) == {}
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is None
    )
    restarted_db.close()


@pytest.mark.asyncio
async def test_bounded_adapter_shutdown_defers_close_through_late_compression_cleanup(
    tmp_path, monkeypatch
):
    """A detached adapter owner keeps state.db alive through terminal cleanup."""
    from gateway import run as gateway_run
    from tests.gateway.restart_test_helpers import make_restart_runner

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    store._db = db
    store._loaded = True

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "alerts": {
                        "secret": "test-secret",
                        "prompt": "{message}",
                        "handoff_to": "discord",
                    }
                },
            },
        )
    )
    runner, _ = make_restart_runner(adapter)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner._restart_drain_timeout = 0.0
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._agent_cache = None
    runner._agent_cache_lock = None
    runner._executor_closing = False
    runner._update_runtime_status = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    adapter.gateway_runner = runner
    adapter.disconnect = AsyncMock()

    source = adapter.build_source(
        chat_id="webhook:alerts:bounded-shutdown",
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
    )
    entry = store.get_or_create_session(source)
    event = MessageEvent(
        text="alert",
        source=source,
        message_id="bounded-shutdown",
        metadata={"_webhook_handoff_to": "discord"},
    )

    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_returned = threading.Event()
    hard_interrupt_called = threading.Event()
    child_session_id = "bounded-shutdown-compression-child"

    class _LateCompressionAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.model = kwargs.get("model") or "test-model"
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=0,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self._last_compaction_in_place = False
            self.interrupt_reasons = []

        def hard_interrupt(self, reason=None):
            self.interrupt_reasons.append(reason)
            hard_interrupt_called.set()

        def run_conversation(self, _message, **_kwargs):
            worker_started.set()
            assert worker_release.wait(timeout=5)
            assert hard_interrupt_called.is_set()
            db.end_session(entry.session_id, "compression")
            db.create_session(
                child_session_id,
                source="webhook",
                parent_session_id=entry.session_id,
            )
            self.session_id = child_session_id
            worker_returned.set()
            return {
                "final_response": "late executor response",
                "messages": [],
                "api_calls": 1,
                "tools": [],
                "completed": True,
                "response_previewed": False,
            }

        def shutdown_memory_provider(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr("run_agent.AIAgent", _LateCompressionAgent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "tool_progress": "off",
                "thinking_progress": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "test-key"},
    )
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")

    async def _run_real_executor_turn(current_event):
        try:
            result = await runner._run_agent(
                message=current_event.text,
                context_prompt="",
                history=[],
                source=current_event.source,
                session_id=entry.session_id,
                session_key=None,
                run_generation=None,
            )
        except asyncio.CancelledError:
            current_event.agent_run_failed = True
            raise
        current_event.agent_run_failed = bool(result.get("failed"))
        return result.get("final_response") or ""

    adapter.set_message_handler(_run_real_executor_turn)
    await adapter.handle_message(event)
    owner_task = next(iter(adapter._background_tasks))
    assert await asyncio.to_thread(worker_started.wait, 5)

    close_spy = MagicMock(wraps=db.close)
    db.close = close_spy
    try:
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("tools.process_registry.process_registry.kill_all", return_value=0),
            patch("tools.terminal_tool.cleanup_all_environments"),
            patch("tools.browser_tool.cleanup_all_browsers"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await asyncio.wait_for(runner.stop(), timeout=1.0)

        assert hard_interrupt_called.is_set()
        assert owner_task.done() is False
        assert db.get_session(child_session_id) is None
        close_spy.assert_not_called()
    finally:
        worker_release.set()
        await asyncio.gather(owner_task, return_exceptions=True)

    assert worker_returned.is_set()
    await asyncio.sleep(0)
    close_spy.assert_called_once_with()

    restarted_db = SessionDB(db_path=db_path)
    root = restarted_db.get_session(entry.session_id)
    child = restarted_db.get_session(child_session_id)
    assert root["end_reason"] == "compression"
    assert child["ended_at"] is not None
    assert child["end_reason"] == "webhook_handoff_cancelled"
    assert restarted_db.load_gateway_routing_entries(
        scope=store._routing_scope()
    ) == {}
    restarted_db.close()


@pytest.mark.asyncio
async def test_bounded_adapter_shutdown_waits_for_legacy_webhook_end_offload(
    tmp_path, monkeypatch
):
    """Legacy one-shot end_session must finish before deferred DB close."""
    from tests.gateway.restart_test_helpers import make_restart_runner

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    store._db = db
    store._loaded = True

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "alerts": {
                        "secret": "test-secret",
                        "prompt": "{message}",
                    }
                },
            },
        )
    )
    runner, _ = make_restart_runner(adapter)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner._restart_drain_timeout = 0.0
    adapter.gateway_runner = runner
    adapter.disconnect = AsyncMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))

    source = adapter.build_source(
        chat_id="webhook:alerts:legacy-shutdown",
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
    )
    entry = store.get_or_create_session(source)
    event = MessageEvent(
        text="legacy alert",
        source=source,
        message_id="legacy-shutdown",
    )

    end_started = threading.Event()
    end_release = threading.Event()
    end_finished = threading.Event()
    original_end_session = db.end_session

    def _slow_end_session(session_id, reason):
        end_started.set()
        try:
            assert end_release.wait(timeout=5)
            return original_end_session(session_id, reason)
        finally:
            end_finished.set()

    db.end_session = _slow_end_session
    close_spy = MagicMock(wraps=db.close)
    db.close = close_spy

    async def _successful_legacy_run(current_event):
        current_event.agent_run_failed = False
        return "legacy response"

    adapter.set_message_handler(_successful_legacy_run)
    await adapter.handle_message(event)
    owner_task = next(iter(adapter._background_tasks))
    assert await asyncio.to_thread(end_started.wait, 5)

    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    try:
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await asyncio.wait_for(runner.stop(), timeout=1.0)

        assert owner_task.done() is False
        close_spy.assert_not_called()
    finally:
        end_release.set()
        await asyncio.gather(owner_task, return_exceptions=True)
        assert await asyncio.to_thread(end_finished.wait, 5)

    await asyncio.sleep(0)
    close_spy.assert_called_once_with()
    restarted_db = SessionDB(db_path=db_path)
    ended = restarted_db.get_session(entry.session_id)
    assert ended["ended_at"] is not None
    assert ended["end_reason"] == "webhook_complete"
    restarted_db.close()


@pytest.mark.asyncio
async def test_claim_cancellation_reconciles_running_webhook_handoff(
    tmp_path, monkeypatch
):
    """Cancellation cannot strand a committed claim outside the pending scan."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-during-claim",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    runner._running = True
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()

    async def _claim_after_release(session_id, owner_json):
        claim_started.set()
        await release_claim.wait()
        return db.claim_webhook_handoff(session_id, owner_json)

    runner._session_db.claim_webhook_handoff = AsyncMock(
        side_effect=_claim_after_release
    )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    watcher_task = asyncio.create_task(
        GatewayRunner._handoff_watcher(runner, interval=0)
    )
    await claim_started.wait()
    watcher_task.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await watcher_task

    assert store.lookup_by_session_key(entry.session_key) is None
    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "handoff claim was cancelled"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_behavior",
    ["return", "raise-before-commit"],
)
async def test_real_state_db_offloaded_claim_cancellation_is_reconciled(
    tmp_path, monkeypatch, claim_behavior
):
    """Cancellation reconciles a real offloaded claim result or late error."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-real-offloaded-claim",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    runner._running = True

    claim_started = threading.Event()
    release_claim = threading.Event()
    real_claim = db.claim_webhook_handoff
    event_loop_thread_id = threading.get_ident()
    claim_thread_ids = []

    def _blocked_real_claim(session_id, owner_json):
        claim_thread_ids.append(threading.get_ident())
        claim_started.set()
        if not release_claim.wait(timeout=5):
            raise TimeoutError("test did not release the real SQLite claim")
        if claim_behavior == "raise-before-commit":
            runner._running = False
            raise RuntimeError("simulated pre-commit claim failure")
        return real_claim(session_id, owner_json)

    monkeypatch.setattr(db, "claim_webhook_handoff", _blocked_real_claim)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    watcher_task = asyncio.create_task(
        GatewayRunner._handoff_watcher(runner, interval=0)
    )
    assert await asyncio.to_thread(claim_started.wait, 5)
    watcher_task.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await watcher_task

    durable = db.get_session(entry.session_id)
    if claim_behavior == "raise-before-commit":
        assert store.lookup_by_session_key(entry.session_key) is not None
        assert durable["handoff_state"] == "pending"
        assert durable["handoff_error"] is None
        assert durable["ended_at"] is None
        assert durable["end_reason"] is None
    else:
        assert store.lookup_by_session_key(entry.session_key) is None
        assert durable["handoff_state"] == "failed"
        assert durable["handoff_error"] == "handoff claim was cancelled"
        assert durable["ended_at"] is not None
        assert durable["end_reason"] == "webhook_handoff_failed"
    assert claim_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in claim_thread_ids)
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_real_route_move_error_after_cancellation_cleans_source(
    tmp_path, monkeypatch
):
    """A real offloaded move error cannot replace cancellation or leak source."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-real-offloaded-move",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    runner._running = True

    move_started = threading.Event()
    release_move = threading.Event()
    event_loop_thread_id = threading.get_ident()
    move_thread_ids = []
    destination_keys = []

    def _blocked_real_move(*args, **kwargs):
        move_thread_ids.append(threading.get_ident())
        destination_keys.append(args[1])
        move_started.set()
        if not release_move.wait(timeout=5):
            raise TimeoutError("test did not release the real route move")
        runner._running = False
        raise RuntimeError("simulated route move failure")

    monkeypatch.setattr(store, "move_session_route", _blocked_real_move)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    watcher_task = asyncio.create_task(
        GatewayRunner._handoff_watcher(runner, interval=0)
    )
    assert await asyncio.to_thread(move_started.wait, 5)
    watcher_task.cancel()
    release_move.set()
    with pytest.raises(asyncio.CancelledError):
        await watcher_task

    assert destination_keys
    destination_key = destination_keys[0]
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(destination_key) is None
    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "handoff processing was cancelled"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert move_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in move_thread_ids)
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_run_failed", [True, None], ids=["failed", "missing"])
async def test_synthetic_agent_failure_cleans_moved_destination(
    tmp_path, monkeypatch, agent_run_failed
):
    """A normalized agent error response cannot complete a webhook handoff."""
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:synthetic-agent-failed",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)

    async def _failed_synthetic_turn(event):
        event.agent_run_failed = agent_run_failed
        return "Sorry, I encountered an unexpected error."

    runner._handle_message = AsyncMock(side_effect=_failed_synthetic_turn)
    states = iter([True, False])

    class _Running:
        def __bool__(self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner._running = _Running()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await GatewayRunner._handoff_watcher(runner, interval=0)

    destination_source = runner._handle_message.await_args.args[0].source
    destination_key = runner._session_key_for_source(destination_source)
    assert store.lookup_by_session_key(entry.session_key) is None
    assert store.lookup_by_session_key(destination_key) is None
    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "synthetic destination agent run failed"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert db.get_meta(_webhook_handoff_owner_key(entry.session_id)) is None
    adapter.send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_pending_handoff_recovers_after_restart_and_missing_home_fails_cleanly(
    tmp_path, monkeypatch
):
    """A persisted request is claimed after restart; pre-move failure leaves no ghost."""
    config = GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        write_sessions_json=False,
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="test-token",
                home_channel=None,
            )
        },
    )
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    original_store._db = db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:pending-before-restart",
        chat_type="dm",
    )
    entry = original_store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True

    # Close every original handle, then load both routing ownership and the
    # pending handoff through a freshly opened state.db connection.
    original_store.close_all_db_handles()
    db.close()
    restarted_db = SessionDB(db_path=tmp_path / "state.db")
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)
    states = iter([True, False])

    class _Running:
        def __bool__(self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner._running = _Running()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await GatewayRunner._handoff_watcher(runner, interval=0)

    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert "no home channel configured" in durable["handoff_error"]
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_pending_handoff_recovers_after_real_db_restart_and_creates_one_thread(
    tmp_path, monkeypatch
):
    """A restarted watcher moves one persisted request exactly once."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:pending-success-before-restart",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    class _RunningOnce:
        def __init__(self):
            self._states = iter([True, False])

        def __bool__(self):
            return next(self._states, False)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    runner._running = _RunningOnce()
    await GatewayRunner._handoff_watcher(runner, interval=0)
    # A second scan of the same durable store must find no pending work.
    runner._running = _RunningOnce()
    await GatewayRunner._handoff_watcher(runner, interval=0)

    synthetic_event = runner._handle_message.await_args.args[0]
    destination_key = runner._session_key_for_source(synthetic_event.source)
    moved = restarted_store.lookup_by_session_key(destination_key)
    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    assert moved is not None
    assert moved.session_id == entry.session_id
    assert restarted_db.get_handoff_state(entry.session_id) == {
        "state": "completed",
        "platform": "discord",
        "error": None,
    }
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_awaited_once()
    runner._handle_message.assert_awaited_once()
    adapter.send.assert_awaited_once()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_dead_owned_handoff_before_move_is_failed_after_real_db_restart(
    tmp_path, monkeypatch
):
    """A dead running owner is cleaned without replaying ambiguous effects."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:dead-owner-before-move",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    stale_token = "dead-owner-before-move"
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token=stale_token,
        pid=2_147_483_647,
        process_start_time=1,
    )
    assert (
        original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    )
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)
    await _run_handoff_watcher_once(runner)

    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_real_child_process_hard_exit_claim_is_failed_without_replay(
    tmp_path, monkeypatch
):
    """A real abruptly-exited claim owner is fenced and terminally cleaned."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:real-child-hard-exit",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    routing_scope = original_store._routing_scope()
    original_store.close_all_db_handles()
    original_db.close()

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HARD_EXIT_WEBHOOK_CLAIM_SCRIPT,
            str(db_path),
            entry.session_id,
            entry.session_key,
            routing_scope,
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        child_stdout, child_stderr = child.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        child_stdout, child_stderr = child.communicate()
        pytest.fail(
            "hard-exit claim child timed out: "
            f"stdout={child_stdout!r} stderr={child_stderr!r}"
        )
    assert child.returncode == 0, (
        f"claim child exited {child.returncode}: "
        f"stdout={child_stdout!r} stderr={child_stderr!r}"
    )

    restarted_db = SessionDB(db_path=db_path)
    claimed = restarted_db.list_claimed_webhook_handoffs()
    assert [row["id"] for row in claimed] == [entry.session_id]
    durable_owner = json.loads(claimed[0]["_handoff_claim_owner"])
    assert durable_owner["pid"] == child.pid
    assert durable_owner["process_start_time"] > 0
    assert durable_owner["host"] == socket.gethostname().strip()
    assert durable_owner["routing_scope"] == routing_scope

    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)

    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_dead_owned_handoff_after_real_route_move_is_cleaned_without_replay(
    tmp_path, monkeypatch
):
    """Recovery follows the claim's durable active key after a committed move."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:dead-owner-after-move",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    stale_token = "dead-owner-after-move"
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token=stale_token,
        pid=2_147_483_647,
        process_start_time=1,
    )
    assert (
        original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    )

    destination_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-before-crash",
        chat_name="Hermes Home",
        chat_type="thread",
        user_id="system:handoff",
        user_name="Handoff",
        thread_id="thread-before-crash",
        scope_id="guild-1",
        guild_id="guild-1",
        parent_chat_id="parent-1",
    )
    destination_key = build_session_key(destination_source)
    moved = original_store.move_session_route(
        entry.session_key,
        destination_key,
        entry.session_id,
        destination_source,
        handoff_claim_token=stale_token,
    )
    assert moved is not None
    assert moved.session_id == entry.session_id
    assert original_store.lookup_by_session_key(entry.session_key) is None
    assert original_store.lookup_by_session_key(destination_key) is not None
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)
    await _run_handoff_watcher_once(runner)

    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    assert restarted_store.lookup_by_session_key(destination_key) is None
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_live_owned_handoff_is_not_reclaimed_after_real_db_restart(
    tmp_path, monkeypatch
):
    """A second gateway must leave a claim owned by this live process alone."""
    from gateway.status import get_process_start_time

    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:live-owner",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    pid = os.getpid()
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token="live-owner",
        pid=pid,
        process_start_time=get_process_start_time(pid),
    )
    assert (
        original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    )
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)
    await _run_handoff_watcher_once(runner)

    owned = restarted_store.lookup_by_session_key(entry.session_key)
    assert owned is not None
    assert owned.session_id == entry.session_id
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "running"
    assert durable["handoff_error"] is None
    assert durable["ended_at"] is None
    assert durable["end_reason"] is None
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_stale_claim_token_cannot_complete_after_dead_owner_recovery(
    tmp_path, monkeypatch
):
    """A crashed worker cannot publish completion after recovery fenced it out."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:stale-completion-token",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    stale_token = "stale-completion-token"
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token=stale_token,
        pid=2_147_483_647,
        process_start_time=1,
    )
    assert (
        original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    )
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)

    assert (
        restarted_db.complete_claimed_webhook_handoff(
            entry.session_id,
            stale_token,
        )
        is False
    )
    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    assert (
        restarted_db.get_meta(_webhook_handoff_owner_key(entry.session_id))
        is None
    )
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_two_restarted_runners_race_dead_owner_recovery_without_dispatch(
    tmp_path, monkeypatch
):
    """Concurrent recovery is idempotent and never replays external effects."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:concurrent-dead-owner-recovery",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    token = "concurrent-dead-owner"
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token=token,
        pid=2_147_483_647,
        process_start_time=1,
    )
    assert original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    owner_key = _webhook_handoff_owner_key(entry.session_id)
    original_store.close_all_db_handles()
    original_db.close()

    restarted = []
    for _index in range(2):
        db = SessionDB(db_path=db_path)
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(
                sessions_dir=config.sessions_dir,
                config=config,
            )
        store._db = db
        store._ensure_loaded()
        runner, adapter = _runner_with_store(config, store, db)
        restarted.append((db, store, runner, adapter))

    both_claim_reads = threading.Barrier(2)
    for db, _store, _runner, _adapter in restarted:
        real_list_claimed = db.list_claimed_webhook_handoffs

        def _list_claimed_then_wait(real_list_claimed=real_list_claimed):
            rows = real_list_claimed()
            both_claim_reads.wait(timeout=5)
            return rows

        monkeypatch.setattr(
            db,
            "list_claimed_webhook_handoffs",
            _list_claimed_then_wait,
        )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await asyncio.gather(
        *(
            _run_handoff_watcher_once(runner)
            for _db, _store, runner, _adapter in restarted
        )
    )

    durable = restarted[0][0].get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted[0][0].get_meta(owner_key) is None
    for db, _store, runner, adapter in restarted:
        assert entry.session_key not in db.load_gateway_routing_entries(
            scope=original_store._routing_scope()
        )
        adapter.create_handoff_thread.assert_not_awaited()
        runner._handle_message.assert_not_awaited()
        adapter.send.assert_not_awaited()

    for db, store, _runner, _adapter in restarted:
        store.close_all_db_handles()
        db.close()


@pytest.mark.asyncio
async def test_dead_owner_recovery_delete_failure_rolls_back_then_retries(
    tmp_path, monkeypatch
):
    """Owner deletion and route/session cleanup share one SQLite transaction."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:owner-delete-rollback",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    token = "owner-delete-rollback"
    owner_json = _webhook_handoff_owner_json(
        original_store,
        entry.session_key,
        token=token,
        pid=2_147_483_647,
        process_start_time=1,
    )
    assert original_db.claim_webhook_handoff(entry.session_id, owner_json) is True
    owner_key = _webhook_handoff_owner_key(entry.session_id)
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    def _install_delete_failure(conn):
        conn.execute(
            "CREATE TRIGGER fail_webhook_handoff_owner_delete "
            "BEFORE DELETE ON state_meta "
            "WHEN OLD.key LIKE 'webhook_handoff_owner:%' "
            "BEGIN SELECT RAISE(ABORT, 'owner delete failed'); END"
        )

    restarted_db._execute_write(_install_delete_failure)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)

    rolled_back = restarted_db.get_session(entry.session_id)
    assert rolled_back["handoff_state"] == "running"
    assert rolled_back["handoff_error"] is None
    assert rolled_back["ended_at"] is None
    assert rolled_back["end_reason"] is None
    owned = restarted_store.lookup_by_session_key(entry.session_key)
    assert owned is not None
    assert owned.session_id == entry.session_id
    assert json.loads(restarted_db.get_meta(owner_key))["token"] == token
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()

    restarted_db._execute_write(
        lambda conn: conn.execute(
            "DROP TRIGGER fail_webhook_handoff_owner_delete"
        )
    )
    await _run_handoff_watcher_once(runner)

    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    assert restarted_db.get_meta(owner_key) is None
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


@pytest.mark.asyncio
async def test_live_owner_cleanup_failure_retries_without_replaying_work(tmp_path):
    """A transient finalizer failure is retried after local work has stopped."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:live-owner-cleanup-retry",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    row = {
        "id": entry.session_id,
        "source": "webhook",
        "session_key": entry.session_key,
    }
    owner = runner._new_webhook_handoff_claim_owner(row)
    assert db.claim_webhook_handoff(
        entry.session_id,
        json.dumps(owner),
    ) is True
    row["_handoff_claim_token"] = owner["token"]
    row["_handoff_source_session_key"] = owner["source_session_key"]
    row["_handoff_active_session_key"] = owner["active_session_key"]
    owner_key = _webhook_handoff_owner_key(entry.session_id)

    def _install_delete_failure(conn):
        conn.execute(
            "CREATE TRIGGER fail_live_owner_cleanup_delete "
            "BEFORE DELETE ON state_meta "
            "WHEN OLD.key LIKE 'webhook_handoff_owner:%' "
            "BEGIN SELECT RAISE(ABORT, 'owner delete failed'); END"
        )

    db._execute_write(_install_delete_failure)
    with pytest.raises(RuntimeError, match="routing transition failed"):
        await runner._finalize_failed_webhook_handoff(
            row,
            "destination delivery failed",
        )

    assert runner._webhook_handoff_cleanup_pending[entry.session_id] == (
        owner["token"],
        "destination delivery failed",
    )
    assert db.get_handoff_state(entry.session_id)["state"] == "running"
    assert store.lookup_by_session_key(entry.session_key) is not None
    assert json.loads(db.get_meta(owner_key))["token"] == owner["token"]

    db._execute_write(
        lambda conn: conn.execute("DROP TRIGGER fail_live_owner_cleanup_delete")
    )
    await runner._recover_dead_webhook_handoffs()

    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == "destination delivery failed"
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert store.lookup_by_session_key(entry.session_key) is None
    assert db.get_meta(owner_key) is None
    assert entry.session_id not in runner._webhook_handoff_cleanup_pending
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    store.close_all_db_handles()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_record",
    [None, "missing-host-and-epoch"],
    ids=["unowned", "malformed-owner"],
)
async def test_unrecoverable_running_webhook_rows_remain_untouched_without_dispatch(
    tmp_path, monkeypatch, owner_record
):
    """Rows without a trustworthy owner fence are never guessed or replayed."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=f"webhook:route:{owner_record or 'unowned'}",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    assert original_db.claim_handoff(entry.session_id) is True
    owner_key = _webhook_handoff_owner_key(entry.session_id)
    if owner_record is not None:
        malformed_owner = {
            "token": "malformed-owner",
            "pid": os.getpid(),
            "process_start_time": 1,
            "routing_scope": original_store._routing_scope(),
            "active_session_key": entry.session_key,
        }
        original_db.set_meta(owner_key, json.dumps(malformed_owner))
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    await _run_handoff_watcher_once(runner)
    await _run_handoff_watcher_once(runner)

    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "running"
    assert durable["handoff_error"] is None
    assert durable["ended_at"] is None
    assert durable["end_reason"] is None
    owned = restarted_store.lookup_by_session_key(entry.session_key)
    assert owned is not None
    assert owned.session_id == entry.session_id
    if owner_record is None:
        assert restarted_db.get_meta(owner_key) is None
    else:
        assert restarted_db.get_meta(owner_key) == json.dumps(malformed_owner)
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()


def _owner_identity(**overrides):
    owner = {
        "token": "owner-identity",
        "pid": 12345,
        "process_start_time": 67890,
        "host": "local-host",
        "instantiation_epoch": "local-epoch",
        "routing_scope": "/tmp/test-sessions",
        "source_session_key": "agent:main:webhook:webhook:route:delivery",
        "active_session_key": "agent:main:webhook:webhook:route:delivery",
    }
    owner.update(overrides)
    return owner


def test_webhook_handoff_owner_on_foreign_host_is_unknown_without_pid_probe():
    owner = _owner_identity(host="foreign-host")

    with (
        patch("gateway.run.socket.gethostname", return_value="local-host"),
        patch("gateway.status.get_process_start_time") as get_start_time,
        patch("psutil.Process") as process,
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is None

    get_start_time.assert_not_called()
    process.assert_not_called()


@pytest.mark.parametrize(
    ("owner_epoch", "current_epoch"),
    [
        ("old-epoch", "current-epoch"),
        ("", "current-epoch"),
        ("old-epoch", ""),
    ],
    ids=["different", "owner-empty", "current-empty"],
)
def test_webhook_handoff_owner_epoch_mismatch_is_unknown_without_pid_probe(
    owner_epoch, current_epoch
):
    owner = _owner_identity(instantiation_epoch=owner_epoch)

    with (
        patch("gateway.run.socket.gethostname", return_value="local-host"),
        patch(
            "gateway.drain_control.current_instantiation_epoch",
            return_value=current_epoch,
        ),
        patch("gateway.status.get_process_start_time") as get_start_time,
        patch("psutil.Process") as process,
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is None

    get_start_time.assert_not_called()
    process.assert_not_called()


def test_webhook_handoff_zombie_owner_is_dead_before_start_time_match():
    import psutil

    owner = _owner_identity()
    process = MagicMock()
    process.status.return_value = psutil.STATUS_ZOMBIE

    with (
        patch("gateway.run.socket.gethostname", return_value=owner["host"]),
        patch(
            "gateway.drain_control.current_instantiation_epoch",
            return_value=owner["instantiation_epoch"],
        ),
        patch("psutil.Process", return_value=process),
        patch(
            "gateway.status.get_process_start_time",
            return_value=owner["process_start_time"],
        ) as get_start_time,
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is False

    get_start_time.assert_not_called()


def test_webhook_handoff_owner_access_denied_is_unknown():
    import psutil

    owner = _owner_identity()
    process = MagicMock()
    process.status.side_effect = psutil.AccessDenied(pid=owner["pid"])

    with (
        patch("gateway.run.socket.gethostname", return_value=owner["host"]),
        patch(
            "gateway.drain_control.current_instantiation_epoch",
            return_value=owner["instantiation_epoch"],
        ),
        patch("psutil.Process", return_value=process),
        patch("gateway.status.get_process_start_time") as get_start_time,
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is None

    get_start_time.assert_not_called()


def test_webhook_handoff_owner_unreadable_start_time_is_unknown():
    import psutil

    owner = _owner_identity()
    process = MagicMock()
    process.status.return_value = psutil.STATUS_RUNNING

    with (
        patch("gateway.run.socket.gethostname", return_value=owner["host"]),
        patch(
            "gateway.drain_control.current_instantiation_epoch",
            return_value=owner["instantiation_epoch"],
        ),
        patch("psutil.Process", return_value=process),
        patch("gateway.status.get_process_start_time", return_value=None),
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is None


def test_webhook_handoff_owner_start_time_mismatch_is_dead():
    import psutil

    owner = _owner_identity(process_start_time=111)
    process = MagicMock()
    process.status.return_value = psutil.STATUS_RUNNING

    with (
        patch("gateway.run.socket.gethostname", return_value=owner["host"]),
        patch(
            "gateway.drain_control.current_instantiation_epoch",
            return_value=owner["instantiation_epoch"],
        ),
        patch("psutil.Process", return_value=process),
        patch("gateway.status.get_process_start_time", return_value=222),
    ):
        assert GatewayRunner._webhook_handoff_claim_owner_alive(owner) is False


@pytest.mark.asyncio
async def test_live_foreign_owner_lock_leaves_durable_state_untouched_without_dispatch(
    tmp_path, monkeypatch
):
    """A foreign owner stays protected while its claim lock is live."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:foreign-owner-unknown",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    owner = json.loads(
        _webhook_handoff_owner_json(
            original_store,
            entry.session_key,
            token="foreign-owner-unknown",
            pid=12345,
            process_start_time=67890,
        )
    )
    owner["host"] = "foreign-host"
    assert (
        original_db.claim_webhook_handoff(
            entry.session_id,
            json.dumps(owner),
        )
        is True
    )
    owner_key = _webhook_handoff_owner_key(entry.session_id)

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    with (
        patch("gateway.run.socket.gethostname", return_value="local-host"),
        patch("gateway.status.get_process_start_time") as get_start_time,
        patch("psutil.Process") as process,
    ):
        await _run_handoff_watcher_once(runner)
        await _run_handoff_watcher_once(runner)

    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "running"
    assert durable["handoff_error"] is None
    assert durable["ended_at"] is None
    assert durable["end_reason"] is None
    owned = restarted_store.lookup_by_session_key(entry.session_key)
    assert owned is not None
    assert owned.session_id == entry.session_id
    assert json.loads(restarted_db.get_meta(owner_key)) == owner
    get_start_time.assert_not_called()
    process.assert_not_called()
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()
    original_store.close_all_db_handles()
    original_db.close()


@pytest.mark.asyncio
async def test_released_foreign_owner_lock_recovers_without_dispatch(
    tmp_path, monkeypatch
):
    """A replacement can clean a foreign claim only after its lock releases."""
    config = _discord_config(tmp_path)
    config.write_sessions_json = False
    with patch("gateway.session.SessionStore._ensure_loaded"):
        original_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_store._db = original_db
    original_store._loaded = True
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:foreign-owner-released",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = original_store.get_or_create_session(source)
    assert original_db.request_handoff_once(entry.session_id, "discord") is True
    owner = json.loads(
        _webhook_handoff_owner_json(
            original_store,
            entry.session_key,
            token="foreign-owner-released",
            pid=12345,
            process_start_time=67890,
        )
    )
    owner["host"] = "foreign-host"
    assert original_db.claim_webhook_handoff(
        entry.session_id, json.dumps(owner)
    ) is True
    original_store.close_all_db_handles()
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        restarted_store = SessionStore(
            sessions_dir=config.sessions_dir,
            config=config,
        )
    restarted_store._db = restarted_db
    restarted_store._ensure_loaded()
    runner, adapter = _runner_with_store(config, restarted_store, restarted_db)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    with (
        patch("gateway.run.socket.gethostname", return_value="local-host"),
        patch("gateway.status.get_process_start_time") as get_start_time,
        patch("psutil.Process") as process,
    ):
        await _run_handoff_watcher_once(runner)

    durable = restarted_db.get_session(entry.session_id)
    assert durable["handoff_state"] == "failed"
    assert durable["handoff_error"] == (
        "handoff owner process exited before completion"
    )
    assert durable["ended_at"] is not None
    assert durable["end_reason"] == "webhook_handoff_failed"
    assert restarted_store.lookup_by_session_key(entry.session_key) is None
    get_start_time.assert_not_called()
    process.assert_not_called()
    adapter.create_handoff_thread.assert_not_awaited()
    runner._handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()
    restarted_store.close_all_db_handles()
    restarted_db.close()

@pytest.mark.asyncio
async def test_completion_cancellation_does_not_retract_delivered_handoff(
    tmp_path, monkeypatch
):
    """Cancellation racing the offloaded completion must reconcile its result.

    Once _process_handoff has returned, the thread exists, the route moved,
    and the reply was delivered. A cancelled ``asyncio.to_thread`` never stops
    the queued completion UPDATE — it always executes eventually — so the
    CancelledError cleanup races it on commit order. This test forces the
    adverse order (the completion commit is held until any failure finalizer
    has fully run): the watcher must reconcile the completion result instead
    of retracting the already-delivered handoff.
    """
    config = _discord_config(tmp_path)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:cancel-during-completion",
        chat_type="webhook",
        user_id="webhook:route",
    )
    entry = store.get_or_create_session(source)
    assert db.request_handoff_once(entry.session_id, "discord") is True
    runner, adapter = _runner_with_store(config, store, db)
    runner._running = True

    processed = asyncio.Event()
    finalize_ran = threading.Event()

    real_process_handoff = GatewayRunner._process_handoff.__get__(runner)

    async def _process_then_signal(row):
        await real_process_handoff(row)
        processed.set()

    runner._process_handoff = _process_then_signal

    real_completion = db.complete_claimed_webhook_handoff

    def _completion_after_any_finalize(session_id, claim_token):
        # Hold the completion commit until a failure finalizer (if any) has
        # fully committed, deterministically producing the adverse order.
        finalize_ran.wait(timeout=1.0)
        return real_completion(session_id, claim_token)

    monkeypatch.setattr(
        db, "complete_claimed_webhook_handoff", _completion_after_any_finalize
    )

    real_remove_and_end = store.remove_session_route_and_end

    def _marking_remove_and_end(*args, **kwargs):
        try:
            return real_remove_and_end(*args, **kwargs)
        finally:
            finalize_ran.set()

    monkeypatch.setattr(
        store, "remove_session_route_and_end", _marking_remove_and_end
    )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    watcher_task = asyncio.create_task(
        GatewayRunner._handoff_watcher(runner, interval=0)
    )
    await asyncio.wait_for(processed.wait(), timeout=5)
    # Let the watcher advance from _process_handoff to the completion await.
    for _ in range(10):
        await asyncio.sleep(0)
    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher_task, timeout=10)

    durable = db.get_session(entry.session_id)
    assert durable["handoff_state"] == "completed"
    assert durable["handoff_error"] is None
    assert durable["ended_at"] is None
    # The delivered destination route survives; only the source moved away.
    synthetic_event = runner._handle_message.await_args.args[0]
    destination_key = runner._session_key_for_source(synthetic_event.source)
    assert store.peek_session_id(destination_key) == entry.session_id
    assert store.peek_session_id(entry.session_key) is None
    adapter.send.assert_awaited()
    db.close()
