"""Tests for Discord auto-thread orphaned-seed-message cleanup.

When auto-threading is enabled, ``_auto_create_thread()`` first tries
``message.create_thread()``.  If that fails (commonly a Discord 429
rate-limit on thread creation), it falls back to posting a seed
announcement message ("🧵 Thread created by Hermes: ...") and creating
the thread *from that message*.

The bug: the seed announcement is posted **before** the fallback
``create_thread()`` is confirmed to succeed.  When the fallback also
fails (e.g. the rate-limit is still in effect), the announcement is left
orphaned in the channel — so users see a "Thread created by Hermes"
message with no thread behind it (#52422).

Fix: on fallback failure, delete the orphaned seed message so the
announcement only ever survives when a real thread exists.

Retry-loop specific: ``_auto_create_thread`` retries the whole
direct+fallback sequence twice (#20243), and **each attempt posts its own
seed message**.  A cleanup that only removed the last seed would still
leave one orphan behind, which is why the live 2026-08-03 failure showed
*two* "Thread created by Hermes" notices.  The cleanup therefore lives in
the per-attempt fallback handler, and the tests below assert every seed
is deleted, not merely one.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class _RateLimited(Exception):
    """Stand-in for discord.HTTPException 429."""


class _SeedMessage:
    """Fake seed message returned by channel.send()."""

    def __init__(self, create_thread_succeeds: bool, thread=None,
                 delete_raises: bool = False):
        self._create_thread_succeeds = create_thread_succeeds
        self._thread = thread
        self._delete_raises = delete_raises
        self.deleted = False
        self.create_thread = AsyncMock(side_effect=self._create_thread)
        self.delete = AsyncMock(side_effect=self._delete)

    async def _create_thread(self, *args, **kwargs):
        if self._create_thread_succeeds:
            return self._thread
        raise _RateLimited("Too many requests. Retry in 222.97 seconds.")

    async def _delete(self, *args, **kwargs):
        if self._delete_raises:
            raise _RateLimited("Too many requests. Retry in 5.00 seconds.")
        self.deleted = True


class _Channel:
    """Channel whose send() hands out a distinct seed message per call.

    Distinct objects matter: the retry loop posts one seed per attempt, so
    reusing a single fake would let a fix that deletes only the final seed
    pass while still orphaning the first one in production.
    """

    def __init__(self, *seed_messages):
        self.id = 100
        self.name = "general"
        self.guild = SimpleNamespace(name="Test Server", id=1)
        self.seeds = list(seed_messages)
        self.send = AsyncMock(side_effect=list(seed_messages))


class _Thread:
    def __init__(self, thread_id=55555, parent=None):
        self.id = thread_id
        self.name = "thread"
        self.parent = parent


def _make_message(*, channel, content="hello bot", direct_create_succeeds=False,
                  thread=None):
    async def _create_thread(*args, **kwargs):
        if direct_create_succeeds:
            return thread
        raise _RateLimited("Too many requests. Retry in 278.53 seconds.")

    return SimpleNamespace(
        id=42,
        content=content,
        channel=channel,
        author=SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False),
        created_at=datetime.now(timezone.utc),
        type=discord_platform.discord.MessageType.default,
        create_thread=AsyncMock(side_effect=_create_thread),
    )


@pytest.fixture
def adapter(monkeypatch):
    for var in ("DISCORD_AUTO_THREAD", "DISCORD_NO_THREAD_CHANNELS"):
        monkeypatch.delenv(var, raising=False)
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    return a


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Skip the 0.75s inter-attempt backoff so the suite stays fast."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(discord_platform.asyncio, "sleep", _instant)


class TestAutoThreadOrphanSeed:
    @pytest.mark.asyncio
    async def test_every_attempts_orphaned_seed_is_deleted(self, adapter):
        """Both attempts 429 → two seeds posted, and BOTH deleted."""
        first, second = (
            _SeedMessage(create_thread_succeeds=False),
            _SeedMessage(create_thread_succeeds=False),
        )
        channel = _Channel(first, second)
        message = _make_message(channel=channel, direct_create_succeeds=False)

        result = await adapter._auto_create_thread(message)

        assert result is None, "must return None when both create paths fail"
        assert channel.send.await_count == 2, (
            "the retry loop posts one seed announcement per attempt"
        )
        assert first.deleted is True and second.deleted is True, (
            "every orphaned seed announcement must be deleted — cleaning up "
            "only the last one still leaves a 'Thread created' message with "
            "no thread behind it (the live #52422 symptom was TWO notices)"
        )

    @pytest.mark.asyncio
    async def test_fallback_success_keeps_seed(self, adapter):
        """Direct create 429s but fallback create succeeds → seed kept."""
        thread = _Thread()
        seed = _SeedMessage(create_thread_succeeds=True, thread=thread)
        channel = _Channel(seed)
        message = _make_message(channel=channel, direct_create_succeeds=False)

        result = await adapter._auto_create_thread(message)

        assert result is thread
        channel.send.assert_awaited_once()
        assert seed.deleted is False, "seed must survive when a real thread was created"

    @pytest.mark.asyncio
    async def test_direct_success_posts_no_seed(self, adapter):
        """Direct create succeeds → no seed announcement, no fallback."""
        thread = _Thread()
        seed = _SeedMessage(create_thread_succeeds=True, thread=thread)
        channel = _Channel(seed)
        message = _make_message(
            channel=channel, direct_create_succeeds=True, thread=thread
        )

        result = await adapter._auto_create_thread(message)

        assert result is thread
        channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_seed_send_failure_does_not_mask_the_original_error(self, adapter):
        """If posting the seed itself fails there is nothing to delete.

        Guards the ``seed_msg = None`` initialisation: without it the cleanup
        branch would raise UnboundLocalError and replace a clean ``None``
        return with a crash inside the failure path.
        """
        channel = _Channel()
        channel.send = AsyncMock(side_effect=_RateLimited("cannot post"))
        message = _make_message(channel=channel, direct_create_succeeds=False)

        result = await adapter._auto_create_thread(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_failure_is_swallowed(self, adapter):
        """A failing cleanup must not mask the underlying thread failure."""
        first, second = (
            _SeedMessage(create_thread_succeeds=False, delete_raises=True),
            _SeedMessage(create_thread_succeeds=False, delete_raises=True),
        )
        channel = _Channel(first, second)
        message = _make_message(channel=channel, direct_create_succeeds=False)

        result = await adapter._auto_create_thread(message)

        assert result is None
        assert first.delete.await_count == 1 and second.delete.await_count == 1
        assert first.deleted is False and second.deleted is False
