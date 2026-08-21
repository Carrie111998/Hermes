"""Behavioural regression guard: forum-thread mention gate (kanban t_9af80d29).

Companion to ``tests/plugins/platforms/test_discord_thread_require_mention_parent_leak.py``,
which unit-tests ``_discord_free_response_test_keys`` in isolation. That test
passes even if the helper is never *called* — it cannot detect an unwired fix.

These tests drive the three real ingress gates end-to-end
(``_handle_message``, ``_discord_message_admission``,
``_dispatch_recovered_message``) with a fake forum thread whose PARENT is
listed in ``free_response_channels``/``allowed_channels`` and
``thread_require_mention=true``, and assert on whether the adapter actually
dispatches to the agent.

Scenario mirrors the live incident: gatekeeper (Deadbolt) woke in a brand-new
#workbench forum thread it was never @-mentioned in, because the thread
inherited the parent forum's free-response status and short-circuited the
require-mention gate.

Coverage:
  * new thread, no mention           -> silent
  * new thread, with @mention        -> responds
  * follow-up in same thread, no mention -> still silent (thread/message parity)
  * thread_require_mention=false     -> parent cascade still works (no regression)
  * admission gate (someone else mentioned, we are not) -> not admitted
  * backfill/recovery path           -> silent
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


WORKBENCH_FORUM_ID = 1540061795971104788   # shared parent forum ("#workbench")
NEW_THREAD_ID = 1540354903367753808        # the "Magnific test" thread
BOT_USER_ID = 999
OTHER_BOT_ID = 555


class _DMChannel:
    """Fake DM channel — must be a distinct class from _Thread."""

    def __init__(self, channel_id: int = 1):
        self.id = channel_id
        self.name = "dm"


class _ForumChannel:
    def __init__(self, channel_id: int = WORKBENCH_FORUM_ID, name: str = "workbench"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name="Saylent Swarm", id=1)
        self.type = 15
        self.topic = None


class _Thread:
    """Fake forum thread under a parent forum channel."""

    def __init__(self, thread_id: int = NEW_THREAD_ID, name: str = "Magnific test",
                 parent=None):
        self.id = thread_id
        self.name = name
        self.parent = parent if parent is not None else _ForumChannel()
        self.parent_id = self.parent.id
        self.guild = self.parent.guild
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _empty():
            return
            yield
        return _empty()


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", _DMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", _Thread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "ForumChannel", _ForumChannel, raising=False)

    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_THREAD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_HISTORY_BACKFILL",
        "DISCORD_ALLOW_BOTS",
        "DISCORD_BOTS_REQUIRE_INLINE_MENTION",
        "DISCORD_IGNORE_NO_MENTION",
    ):
        monkeypatch.delenv(var, raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    # Real discord.ClientUser has .bot == True; the admission gate's
    # other_bots_mentioned check reads it off every entry in message.mentions.
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=BOT_USER_ID, bot=True, name="Deadbolt")
    )
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    return adapter


def _swarm_gates(monkeypatch, *, thread_require_mention: bool = True):
    """The swarm's standing anti-ack-loop config, identical on every profile."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv(
        "DISCORD_THREAD_REQUIRE_MENTION",
        "true" if thread_require_mention else "false",
    )
    # The parent forum is BOTH watched and free-response — this is the exact
    # config drift that produced the incident.
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", str(WORKBENCH_FORUM_ID))
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", str(WORKBENCH_FORUM_ID))
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")


def _message(*, channel, content, mentions=None, author_is_bot=False, msg_id=4242):
    author = SimpleNamespace(
        id=42 if not author_is_bot else OTHER_BOT_ID,
        display_name="Lucas",
        name="Lucas",
        bot=author_is_bot,
    )
    return SimpleNamespace(
        id=msg_id,
        content=content,
        mentions=list(mentions or []),
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=author,
        guild=getattr(channel, "guild", None),
        type=discord_platform.discord.MessageType.default,
    )


# ---------------------------------------------------------------------------
# _handle_message — the main ingress gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_forum_thread_without_mention_stays_silent(adapter, monkeypatch):
    """THE BUG. New forum thread under a free-response parent, no @mention:
    thread_require_mention=true must keep the bot silent."""
    _swarm_gates(monkeypatch)
    message = _message(channel=_Thread(), content="starting a topic, no mentions here")

    dispatched = await adapter._handle_message(message)

    assert dispatched is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_forum_thread_with_mention_responds(adapter, monkeypatch):
    """Same event WITH an explicit @mention must still wake the bot."""
    _swarm_gates(monkeypatch)
    bot_user = adapter._client.user
    message = _message(
        channel=_Thread(),
        content=f"<@{BOT_USER_ID}> take a look at this",
        mentions=[bot_user],
    )

    dispatched = await adapter._handle_message(message)

    assert dispatched is True
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "take a look at this"
    assert event.source.chat_id == str(NEW_THREAD_ID)


@pytest.mark.asyncio
async def test_followup_in_same_thread_without_mention_stays_silent(adapter, monkeypatch):
    """Parity check: after the bot has participated, a follow-up with no
    @mention must ALSO stay silent while thread_require_mention=true — the
    thread-creation path and the message path must agree."""
    _swarm_gates(monkeypatch)
    adapter._threads.mark(str(NEW_THREAD_ID))  # bot has already replied here

    message = _message(
        channel=_Thread(),
        content="follow-up with no mention",
        msg_id=4243,
    )

    dispatched = await adapter._handle_message(message)

    assert dispatched is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_cascade_preserved_when_thread_require_mention_off(adapter, monkeypatch):
    """No-regression guard: with thread_require_mention=false the documented
    parent free-response cascade must still wake the bot without a mention."""
    _swarm_gates(monkeypatch, thread_require_mention=False)
    message = _message(channel=_Thread(), content="no mention, cascade should apply")

    dispatched = await adapter._handle_message(message)

    assert dispatched is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_listed_by_own_id_still_free_response(adapter, monkeypatch):
    """The escape hatch must survive: listing the THREAD's own id in
    free_response_channels exempts it even under thread_require_mention."""
    _swarm_gates(monkeypatch)
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", str(NEW_THREAD_ID))
    monkeypatch.setenv(
        "DISCORD_ALLOWED_CHANNELS", f"{WORKBENCH_FORUM_ID},{NEW_THREAD_ID}"
    )
    message = _message(channel=_Thread(), content="explicitly opted in")

    dispatched = await adapter._handle_message(message)

    assert dispatched is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_named_after_parent_cannot_inherit_by_name(adapter, monkeypatch):
    """Thread names are user-controlled. Naming a new forum thread exactly
    after its parent channel must NOT re-inherit the parent's name-form
    free-response entry — that would restore the bypass for anyone who
    configures free_response_channels by name instead of by snowflake."""
    _swarm_gates(monkeypatch)
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "workbench")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "workbench")
    message = _message(
        channel=_Thread(name="workbench"),  # same name as the parent forum
        content="no mention, name collides with parent",
    )

    dispatched = await adapter._handle_message(message)

    assert dispatched is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_with_distinct_name_still_free_response_by_name(adapter, monkeypatch):
    """No over-blocking: a thread listed by its OWN distinct name is still
    exempt under thread_require_mention."""
    _swarm_gates(monkeypatch)
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "magnific-test")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "workbench")
    message = _message(channel=_Thread(name="magnific-test"), content="opted in by name")

    dispatched = await adapter._handle_message(message)

    assert dispatched is True
    adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# _discord_message_admission — the pre-dispatch admission gate
# ---------------------------------------------------------------------------

def test_admission_rejects_thread_message_mentioning_someone_else(adapter, monkeypatch):
    """When a message mentions a *different* human/bot in the thread, the
    ignore_no_mention branch consults free-response; the parent must not
    exempt us under thread_require_mention."""
    _swarm_gates(monkeypatch)
    other = SimpleNamespace(id=7777, bot=False, display_name="someone", name="someone")
    message = _message(
        channel=_Thread(),
        content="<@7777> can you look at this?",
        mentions=[other],
    )

    admitted, _role = adapter._discord_message_admission(message, claim=False)

    assert admitted is False


def test_admission_accepts_thread_message_mentioning_us(adapter, monkeypatch):
    _swarm_gates(monkeypatch)
    bot_user = adapter._client.user
    message = _message(
        channel=_Thread(),
        content=f"<@{BOT_USER_ID}> ping",
        mentions=[bot_user],
    )

    admitted, _role = adapter._discord_message_admission(message, claim=False)

    assert admitted is True


# ---------------------------------------------------------------------------
# _dispatch_recovered_message — the missed-message backfill path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_path_does_not_replay_unmentioned_thread_message(adapter, monkeypatch):
    """Backfill replays history through the same gates; a gap here would
    resurrect the bug after every gateway restart.

    ``_handle_message`` is stubbed out deliberately: it carries its own copy
    of the gate and would mask an unwired free-response check inside
    ``_dispatch_recovered_message``. Isolating the recovery gate is the only
    way this test can actually fail when that one fire site regresses
    (verified by mutation: unwiring it survives an end-to-end assertion).
    """
    _swarm_gates(monkeypatch)
    inner = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_handle_message", inner)
    message = _message(channel=_Thread(), content="recovered message, no mention")

    dispatched = await adapter._dispatch_recovered_message(message)

    assert dispatched is False
    inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_path_replays_mentioned_thread_message(adapter, monkeypatch):
    """Counterpart: the recovery gate must not over-block a real @mention."""
    _swarm_gates(monkeypatch)
    inner = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_handle_message", inner)
    bot_user = adapter._client.user
    message = _message(
        channel=_Thread(),
        content=f"<@{BOT_USER_ID}> recovered, mentioned",
        mentions=[bot_user],
    )

    dispatched = await adapter._dispatch_recovered_message(message)

    assert dispatched is True
    inner.assert_awaited_once()
