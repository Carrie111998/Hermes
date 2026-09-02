"""Tests for Telegram adapter early authorization check.

Verifies that unauthorized users are blocked before any text batching,
event building, or response generation occurs.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType


def _make_adapter(allow_from=None, allowed_chats=None, group_allowed_chats=None, callback_auth=None, **extra_overrides):
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except ModuleNotFoundError:  # PR branch before Telegram plugin extraction
        from gateway.platforms.telegram import TelegramAdapter

    extra = {}
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if group_allowed_chats is not None:
        extra["group_allowed_chats"] = group_allowed_chats
    extra.update(extra_overrides)

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    if callback_auth is not None:
        adapter._is_callback_user_authorized = callback_auth
    return adapter


def _make_message(text="hello", *, from_user_id=111, chat_id=-100, chat_type="group"):
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Test", is_forum=False),
        from_user=SimpleNamespace(id=from_user_id, full_name="Test User", first_name="Test"),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )


@pytest.mark.asyncio
async def test_unauthorized_user_blocked_before_event_building():
    """Unauthorized user's message should be blocked before _build_message_event."""
    adapter = _make_adapter(group_allow_from=["222"])  # Only user 222 allowed in groups

    build_called = False
    original_build = adapter._build_message_event

    def track_build(*a, **kw):
        nonlocal build_called
        build_called = True
        return original_build(*a, **kw)

    adapter._build_message_event = track_build

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(from_user_id=111, chat_type="group"),  # User 111 NOT in group_allow_from
        effective_message=None,
    )

    await adapter._handle_text_message(update, SimpleNamespace())

    assert build_called is False, "build_message_event should not be called for unauthorized user"


@pytest.mark.asyncio
async def test_command_from_unauthorized_user_blocked():
    """Commands from unauthorized users should be blocked."""
    adapter = _make_adapter(group_allow_from=["222"])
    adapter.handle_message = AsyncMock()

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(text="/start", from_user_id=111, chat_type="group"),
        effective_message=None,
    )

    await adapter._handle_command(update, SimpleNamespace())

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_location_from_unauthorized_user_blocked():
    """Location messages from unauthorized users should be blocked."""
    adapter = _make_adapter(group_allow_from=["222"])

    msg = _make_message(from_user_id=111, chat_type="group")
    msg.text = None
    msg.location = SimpleNamespace(latitude=53.3498, longitude=-6.2603)

    update = SimpleNamespace(
        update_id=1,
        message=msg,
        effective_message=None,
    )

    # Should not raise — just silently return
    await adapter._handle_location_message(update, SimpleNamespace())


def test_is_user_authorized_from_message_allow_from():
    """_is_user_authorized_from_message should respect adapter-level allow_from for DMs."""
    adapter = _make_adapter(allow_from=["111", "222"])

    msg = _make_message(from_user_id=111, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is True

    msg = _make_message(from_user_id=333, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_allowlist_dm_with_explicit_pair_behavior_reaches_gateway(monkeypatch):
    """Allowlist + unauthorized_dm_behavior:pair must not early-drop unknown DMs.

    Regression for the gap left by #40863: early intake rejection discarded
    unauthorized DMs before gateway pairing could run, even when the operator
    explicitly set telegram.unauthorized_dm_behavior: pair.
    """
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    class Runner:
        def _is_user_authorized(self, source):
            return source.user_id == "111"

        def _get_unauthorized_dm_behavior(self, platform, *, profile=None):
            assert platform == Platform.TELEGRAM
            return "pair"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=999, chat_id=999, chat_type="private")

    assert adapter._is_user_authorized_from_message(msg) is True


def test_allowlist_dm_without_pair_behavior_still_early_rejects(monkeypatch):
    """Allowlist without pairing opt-in keeps the #9337/#40863 silent drop."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    class Runner:
        def _is_user_authorized(self, source):
            return source.user_id == "111"

        def _get_unauthorized_dm_behavior(self, platform, *, profile=None):
            return "ignore"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=999, chat_id=999, chat_type="private")

    assert adapter._is_user_authorized_from_message(msg) is False


def test_allow_from_dm_with_pair_override_reaches_gateway():
    """Adapter allow_from + unauthorized_dm_behavior:pair still forwards DMs."""
    adapter = _make_adapter(
        allow_from=["111"],
        unauthorized_dm_behavior="pair",
    )
    msg = _make_message(from_user_id=999, chat_id=999, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is True


def test_allowlist_group_with_pair_behavior_still_early_rejects(monkeypatch):
    """Pairing is DM-only — unauthorized group senders stay blocked early."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    class Runner:
        def _is_user_authorized(self, source):
            return source.user_id == "111"

        def _get_unauthorized_dm_behavior(self, platform, *, profile=None):
            return "pair"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter(group_allow_from=["111"])
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=999, chat_id=-100, chat_type="group")

    assert adapter._is_user_authorized_from_message(msg) is False


@pytest.mark.asyncio
async def test_unauthorized_dm_with_pair_behavior_builds_event(monkeypatch):
    """Unknown DM under pair behavior must reach event construction."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    class Runner:
        def _is_user_authorized(self, source):
            return source.user_id == "111"

        def _get_unauthorized_dm_behavior(self, platform, *, profile=None):
            return "pair"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    build_called = False
    original_build = adapter._build_message_event

    def track_build(*a, **kw):
        nonlocal build_called
        build_called = True
        return original_build(*a, **kw)

    adapter._build_message_event = track_build
    adapter._enqueue_text_event = lambda event: None
    adapter._ensure_forum_commands = AsyncMock()
    adapter._cache_replied_media = AsyncMock()
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._should_process_message = lambda *a, **kw: True

    update = SimpleNamespace(
        update_id=1,
        message=_make_message(from_user_id=999, chat_id=999, chat_type="private"),
        effective_message=None,
    )
    await adapter._handle_text_message(update, SimpleNamespace())
    assert build_called is True


def test_runner_auth_gets_group_user_allowlist_context(monkeypatch):
    """Group user allowlists need a group-shaped source, not a DM-shaped one."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "111")
    seen_sources = []

    class Runner:
        def _is_user_authorized(self, source):
            seen_sources.append(source)
            return source.chat_type == "group" and source.chat_id == "-100" and source.user_id == "111"

        async def handle(self, event):
            return None

    runner = Runner()
    adapter = _make_adapter()
    adapter._message_handler = runner.handle
    msg = _make_message(from_user_id=111, chat_id=-100, chat_type="group")

    assert adapter._is_user_authorized_from_message(msg) is True
    assert seen_sources
    assert seen_sources[0].chat_type == "group"
    assert seen_sources[0].chat_id == "-100"


@pytest.mark.asyncio
async def test_unmentioned_group_location_from_removed_user_not_observed():
    """Removed users must not persist unmentioned group locations into observed context."""
    adapter = _make_adapter(
        group_allow_from=["222"],
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        require_mention=True,
        observe_unmentioned_group_messages=True,
    )
    observed = []
    adapter._observe_unmentioned_group_message = lambda *args, **kwargs: observed.append((args, kwargs))

    msg = _make_message(text=None, from_user_id=111, chat_id=-100, chat_type="group")
    msg.location = SimpleNamespace(latitude=53.3498, longitude=-6.2603)
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)

    await adapter._handle_location_message(update, SimpleNamespace())

    assert observed == []


def test_group_allowlist_authorized_under_multiplex_closure_handler(monkeypatch):
    """group_allowed_chats must authorize a chat member under multiplex_profiles.

    Regression for #87132: with gateway.multiplex_profiles the primary message
    handler is a closure, so its ``__self__`` is absent and the early intake
    filter could not reach GatewayRunner._is_user_authorized — it fell back to
    env-only auth and default-denied every non-global sender in an explicitly
    allowlisted group. The platform-bound callback registered via
    set_authorization_check survives the closure wrapping and must be consulted.
    """
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100123")

    adapter = _make_adapter(group_allowed_chats=["-100123"])

    # Multiplex: the primary handler is a closure with no ``__self__`` runner.
    def closure_handler(event):
        return None

    adapter._message_handler = closure_handler
    assert getattr(closure_handler, "__self__", None) is None

    # The runner installs this callback at adapter registration; it routes
    # through the full auth chain (here: the chat allowlist) regardless of how
    # the message handler is wrapped.
    def auth_check(user_id, chat_type=None, chat_id=None):
        return str(chat_id) in {"-100123"}

    adapter.set_authorization_check(auth_check)

    # A sender absent from any user allowlist, posting in the allowlisted group,
    # is authorized via the chat allowlist.
    allowed = _make_message(from_user_id=555, chat_id=-100123, chat_type="group")
    assert adapter._is_user_authorized_from_message(allowed) is True

    # The same sender in a NON-allowlisted group is still rejected.
    denied = _make_message(from_user_id=555, chat_id=-100999, chat_type="group")
    assert adapter._is_user_authorized_from_message(denied) is False


def test_multiplex_closure_handler_without_callback_falls_back_to_env(monkeypatch):
    """No registered callback + a closure handler (no runner) must not raise and
    falls back to env-only auth — the getattr guard keeps the legacy path safe."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    adapter = _make_adapter(group_allowed_chats=["-100123"])
    adapter._message_handler = lambda event: None  # closure, no __self__
    # No set_authorization_check() → _authorization_check is absent/None.

    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=111, chat_id=-100123, chat_type="group")
    ) is True
    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=555, chat_id=-100123, chat_type="group")
    ) is False


# ---------------------------------------------------------------------------
# groups.<chatId>.allow_from (#992a463a / uss-platform #2353 §17 slice 4/6)
#
# Mirrors WeCom's groups.<id>.allow_from (_resolve_group_cfg / _is_group_allowed,
# see tests/gateway/test_config_driven_access_policy.py::
# test_wecom_open_group_with_per_group_sender_allowlist_is_authorized).
# This is an ADDITIONAL, independent gate on top of chat-level policy /
# group_allow_from — not a replacement for it.
# ---------------------------------------------------------------------------


def test_per_chat_groups_allow_from_rejects_unlisted_sender():
    """A chat with groups.<chatId>.allow_from set rejects a sender not in it."""
    adapter = _make_adapter(groups={"-100123": {"allow_from": ["222"]}})

    msg = _make_message(from_user_id=111, chat_id=-100123, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_per_chat_groups_allow_from_accepts_listed_sender():
    """A chat with groups.<chatId>.allow_from set accepts a sender in it."""
    adapter = _make_adapter(groups={"-100123": {"allow_from": ["222"]}})

    msg = _make_message(from_user_id=222, chat_id=-100123, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is True


def test_chat_with_no_groups_entry_is_unaffected():
    """A chat absent from extra.groups is unaffected by this gate.

    No group_allow_from / chat allowlist configured either, so the message
    passes through the normal env-only fallback (no allowlist configured ->
    default allow), proving the new key is a true no-op when absent.
    """
    adapter = _make_adapter(groups={"-100123": {"allow_from": ["222"]}})

    # Different chat id -> no groups.<chatId> entry for it.
    msg = _make_message(from_user_id=999, chat_id=-100999, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is True


def test_per_chat_groups_allow_from_empty_list_blocks_everyone_in_that_chat():
    """allow_from: [] fails closed for that specific chat (blocks everyone)."""
    adapter = _make_adapter(groups={"-100123": {"allow_from": []}})

    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=111, chat_id=-100123, chat_type="group")
    ) is False
    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=222, chat_id=-100123, chat_type="group")
    ) is False

    # A different, unconfigured chat is not affected by the fail-closed entry.
    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=333, chat_id=-100999, chat_type="group")
    ) is True


def test_per_chat_groups_allow_from_is_additional_to_flat_group_allow_from():
    """groups.<chatId>.allow_from ANDs with the flat group_allow_from gate.

    Sender must pass BOTH: present in group_allow_from AND (when the chat has
    a groups.<chatId> entry) present in that chat's allow_from.
    """
    adapter = _make_adapter(
        group_allow_from=["111", "222"],
        groups={"-100123": {"allow_from": ["222"]}},
    )

    # In group_allow_from but not in the per-chat allow_from -> rejected.
    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=111, chat_id=-100123, chat_type="group")
    ) is False
    # In both -> authorized.
    assert adapter._is_user_authorized_from_message(
        _make_message(from_user_id=222, chat_id=-100123, chat_type="group")
    ) is True


def test_per_chat_groups_allow_from_does_not_apply_to_dms():
    """The groups.<chatId> gate only applies to group/forum/channel chats."""
    adapter = _make_adapter(
        allow_from=["111"],
        groups={"111": {"allow_from": ["222"]}},  # keyed by chat id, irrelevant to DM auth
    )

    msg = _make_message(from_user_id=111, chat_id=111, chat_type="dm")
    assert adapter._is_user_authorized_from_message(msg) is True


# ---------------------------------------------------------------------------
# Regression: groups.<chatId>.allow_from must never bypass the chat-level
# allowed_chats/group_allowed_chats gate (t_450b1946 / uss-platform #2353
# §17 slice 4/6 follow-up). Confirmed bug: PR-97104 let the per-chat sender
# gate independently decide `authorized` before the chat-level allowlist
# was consulted, so a sender listed in groups.<chatId>.allow_from could
# speak in a chat that was never allowlisted at the chat level at all.
# Mirrors WeCom's _is_group_allowed ordering: chat-level resolves FIRST,
# per-chat sender filter applies only as an additional AND on top of it.
# ---------------------------------------------------------------------------


def test_per_chat_allow_from_does_not_bypass_chat_level_allowed_chats():
    """Sender matches groups.<chatId>.allow_from but the chat itself is not
    in allowed_chats -> must be REJECTED, not authorized.

    Exact repro from the task: allowed_chats excludes -100999, but
    groups.-100999.allow_from lists sender 222 -- 222 must still be
    rejected because the chat never passed the chat-level gate.
    """
    adapter = _make_adapter(
        allowed_chats=["-100111"],
        groups={"-100999": {"allow_from": ["222"]}},
    )

    msg = _make_message(from_user_id=222, chat_id=-100999, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_per_chat_allow_from_does_not_bypass_group_allowed_chats():
    """Same bypass, but via the runner-facing group_allowed_chats gate
    instead of the response-facing allowed_chats gate.
    """
    adapter = _make_adapter(
        group_allowed_chats=["-100111"],
        groups={"-100999": {"allow_from": ["222"]}},
    )

    msg = _make_message(from_user_id=222, chat_id=-100999, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False


def test_chat_level_allowed_sender_level_rejected_is_still_rejected():
    """Inverse: chat-level allowlist accepts the chat, but the sender fails
    the per-chat groups.<chatId>.allow_from filter -> must be REJECTED.

    Exercises the real chat-level path (not a stub runner that never gets
    called): allowed_chats explicitly includes -100123, so chat-level
    resolves True, but sender 111 is not in groups.-100123.allow_from.
    """
    adapter = _make_adapter(
        allowed_chats=["-100123"],
        groups={"-100123": {"allow_from": ["222"]}},
    )

    msg = _make_message(from_user_id=111, chat_id=-100123, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False

    # Listed sender in the allowed chat is still authorized.
    msg_ok = _make_message(from_user_id=222, chat_id=-100123, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg_ok) is True


def test_per_chat_allow_from_bypass_blocked_even_with_flat_group_allow_from_matching():
    """Belt-and-suspenders: even when the flat group_allow_from gate would
    have separately authorized the sender, an excluded chat still rejects.
    """
    adapter = _make_adapter(
        allowed_chats=["-100111"],
        group_allow_from=["222"],
        groups={"-100999": {"allow_from": ["222"]}},
    )

    msg = _make_message(from_user_id=222, chat_id=-100999, chat_type="group")
    assert adapter._is_user_authorized_from_message(msg) is False
