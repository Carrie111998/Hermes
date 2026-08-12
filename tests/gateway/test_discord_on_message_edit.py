"""Tests for ``DiscordAdapter._handle_message_edit`` (MESSAGE_UPDATE routing).

The adapter subscribes to ``on_message`` but not to ``on_message_edit``, so a
user who edits an existing message to add the bot's @-mention gets silence -
a natural flow when someone wants to address the bot after the fact.

These tests pin the routing rules:

  - guild channel: forward only when the edit *newly* addresses the bot, so a
    typo fix on a message we already answered does not produce a second reply
  - the mention test is ``_self_is_explicitly_mentioned`` - the same one
    inbound admission uses - so a raw ``<@ID>`` token counts even when the
    resolved ``mentions`` list is empty, which is exactly what happens on
    edited messages
  - DM: any genuine content change is a new turn (there is no "newly
    addressed" transition to detect)
  - embed-only updates (Discord resolving a link preview) are ignored
  - author / type / allow-list / bot-policy gates are reused from
    ``_discord_message_admission`` rather than reimplemented, with only the
    message-id dedup skipped - the original dispatch already claimed that id
  - replay protection is keyed on ``edit:<id>:<edited_at>``
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

# tests/gateway/conftest.py installs the discord mock at collection time.
import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


BOT_ID = 999
USER_ID = 7
_EDIT_TS = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


class _TextChannel:
    """Fake guild text channel - not a DMChannel, not a Thread."""

    def __init__(self, channel_id: int = 100):
        self.id = channel_id
        self.name = "general"
        self.guild = SimpleNamespace(name="Test Server", id=1)


def _dm_channel():
    return discord_platform.discord.DMChannel()


def _user(uid: int = USER_ID, *, bot: bool = False):
    return SimpleNamespace(id=uid, name="Alice", display_name="Alice", bot=bot)


def _bot_user():
    return SimpleNamespace(id=BOT_ID, name="Bot", display_name="Bot", bot=True)


def _message(
    *,
    msg_id: int = 42,
    content: str = "hello",
    author=None,
    mentions=None,
    channel=None,
    msg_type=None,
    edited_at=_EDIT_TS,
):
    channel = channel if channel is not None else _TextChannel()
    is_dm = isinstance(channel, discord_platform.discord.DMChannel)
    return SimpleNamespace(
        id=msg_id,
        content=content,
        author=author if author is not None else _user(),
        mentions=list(mentions or []),
        channel=channel,
        guild=None if is_dm else SimpleNamespace(name="Test Server", id=1),
        type=(
            msg_type
            if msg_type is not None
            else discord_platform.discord.MessageType.default
        ),
        edited_at=edited_at,
    )


@pytest.fixture
def adapter(monkeypatch):
    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_AUTO_THREAD",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "DISCORD_ALLOW_BOTS",
        "DISCORD_IGNORE_NO_MENTION",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")

    a = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    a._client = SimpleNamespace(user=_bot_user())
    a._ready_event.set()
    a._handle_message = AsyncMock(return_value=True)
    return a


# ---------------------------------------------------------------------------
# Content / timestamp preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_only_edit_is_ignored(adapter):
    """Discord reuses MESSAGE_UPDATE to attach a link preview; same content."""
    same = "look at https://example.com"
    bot = adapter._client.user
    before = _message(content=same, mentions=[bot])
    after = _message(content=same, mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_without_timestamp_is_ignored(adapter):
    """No edited_at means every edit shares one dedup key, which would swallow
    the next genuine edit. Bail out rather than guess a placeholder."""
    bot = adapter._client.user
    before = _message(content="question", mentions=[], edited_at=None)
    after = _message(content=f"<@{BOT_ID}> question", mentions=[bot], edited_at=None)

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


# ---------------------------------------------------------------------------
# The newly-addressed delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newly_added_mention_dispatches(adapter):
    bot = adapter._client.user
    before = _message(content="some question", mentions=[])
    after = _message(content=f"<@{BOT_ID}> some question", mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()
    assert adapter._handle_message.await_args.args[0] is after


@pytest.mark.asyncio
async def test_raw_mention_dispatches_when_mentions_list_is_empty(adapter):
    """``message.mentions`` is not always populated on edited messages - see
    ``_raw_mentioned_user_ids``. The delta must still see the raw token,
    otherwise the exact flow this feature exists for stays broken."""
    before = _message(content="some question", mentions=[])
    after = _message(content=f"<@{BOT_ID}> some question", mentions=[])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_typo_fix_on_answered_message_is_ignored(adapter):
    """The message was already mentioned before, so we already replied once."""
    bot = adapter._client.user
    before = _message(content=f"<@{BOT_ID}> origenal", mentions=[bot])
    after = _message(content=f"<@{BOT_ID}> original", mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_typo_fix_is_ignored_when_before_only_had_a_raw_mention(adapter):
    """Same guard, but ``before`` carried the mention only in its content."""
    bot = adapter._client.user
    before = _message(content=f"<@{BOT_ID}> origenal", mentions=[])
    after = _message(content=f"<@{BOT_ID}> original", mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_removing_the_mention_is_ignored(adapter):
    bot = adapter._client.user
    before = _message(content=f"<@{BOT_ID}> nvm", mentions=[bot])
    after = _message(content="nvm", mentions=[])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_free_response_channel_edit_without_mention_is_ignored(adapter, monkeypatch):
    """Deliberate: in a free-response channel the original message was already
    answered, so its edit is a correction to a finished turn, not a new one.
    Re-dispatching here would double-reply to every typo fix. Superseding an
    answered turn is tracked separately."""
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "100")
    before = _message(content="what about X", mentions=[])
    after = _message(content="what about Y", mentions=[])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_dm_edit_with_content_change_dispatches(adapter):
    """Every DM is addressed to us, so there is no transition to detect."""
    dm = _dm_channel()
    before = _message(content="ple", channel=dm)
    after = _message(content="please help", channel=dm)

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Gates reused from _discord_message_admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bots_own_edit_is_ignored(adapter):
    bot = adapter._client.user
    before = _message(content="a", author=bot)
    after = _message(content="b", author=bot)

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_non_default_message_type_is_ignored(adapter):
    """Pins, joins and other system messages never reach the agent."""
    bot = adapter._client.user
    pin = discord_platform.discord.MessageType.pins_add
    before = _message(content="a", mentions=[], msg_type=pin)
    after = _message(content=f"<@{BOT_ID}> b", mentions=[bot], msg_type=pin)

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_disallowed_user_is_ignored(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "false")
    bot = adapter._client.user
    before = _message(content="hello", mentions=[])
    after = _message(content=f"<@{BOT_ID}> hello", mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_bot_author_ignored_when_allow_bots_is_none(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
    bot = adapter._client.user
    other = _user(uid=200, bot=True)
    before = _message(content="hello", author=other, mentions=[])
    after = _message(content=f"<@{BOT_ID}> hello", author=other, mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_bot_author_dispatches_when_allow_bots_is_mentions(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")
    bot = adapter._client.user
    other = _user(uid=200, bot=True)
    before = _message(content="hello", author=other, mentions=[])
    after = _message(content=f"<@{BOT_ID}> hello", author=other, mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_role_authorization_is_forwarded(adapter):
    """``role_authorized`` comes back from admission and must reach
    ``_handle_message``; dropping it silently downgrades role-authorized
    users on the edit path only."""
    adapter._allowed_role_ids = {5}
    member = _user()
    member.roles = [SimpleNamespace(id=5)]
    bot = adapter._client.user
    before = _message(content="hello", author=member, mentions=[])
    after = _message(content=f"<@{BOT_ID}> hello", author=member, mentions=[bot])

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()
    assert adapter._handle_message.await_args.kwargs["role_authorized"] is True


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_is_not_blocked_by_the_original_message_id_claim(adapter):
    """The crux: an edit reuses ``message.id``, which the original dispatch
    already claimed. Without skipping the id dedup every edit is dropped."""
    bot = adapter._client.user
    before = _message(content="some question", mentions=[])
    after = _message(content=f"<@{BOT_ID}> some question", mentions=[bot])

    # Exactly what the first dispatch of this message did.
    assert adapter._dedup.is_duplicate(str(after.id)) is False
    assert adapter._dedup.contains(str(after.id)) is True

    await adapter._handle_message_edit(before, after)

    adapter._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_of_the_same_edit_is_deduped(adapter):
    """A RESUME after reconnect redelivers MESSAGE_UPDATE."""
    bot = adapter._client.user
    before = _message(content="hello", mentions=[])
    after = _message(content=f"<@{BOT_ID}> hello", mentions=[bot])

    await adapter._handle_message_edit(before, after)
    await adapter._handle_message_edit(before, after)

    assert adapter._handle_message.await_count == 1


@pytest.mark.asyncio
async def test_second_genuine_edit_dispatches_again(adapter):
    """Keyed on edited_at, so a later real edit still gets through."""
    dm = _dm_channel()
    t2 = _EDIT_TS + timedelta(minutes=1)

    v1 = _message(content="ple", channel=dm, edited_at=_EDIT_TS)
    v2 = _message(content="please", channel=dm, edited_at=_EDIT_TS)
    v3 = _message(content="please help", channel=dm, edited_at=t2)

    await adapter._handle_message_edit(v1, v2)
    await adapter._handle_message_edit(v2, v3)

    assert adapter._handle_message.await_count == 2
