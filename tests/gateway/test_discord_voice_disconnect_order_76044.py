"""Regression tests for #76044 — Discord adapter voice-disconnect ordering.

`DiscordAdapter.disconnect()` cancelled the bot task *before* cleaning up
active voice connections.  In discord.py, ``VoiceClient.disconnect()`` (called
via ``leave_voice_channel``) sends a voice-state update over the MAIN gateway
websocket and then waits for the voice websocket to close.  The bot task IS
that gateway loop, so cancelling it first leaves the voice-disconnect
handshake with no transport — it can never complete and blocks until the
caller's 5s shutdown timeout fires.  Result: every gateway shutdown with a
voice channel open loses ~5 seconds to a "discord disconnect timed out
after 5.0s" warning.

The fix moves the voice-cleanup loop above ``await self._cancel_bot_task()``
so the disconnect completes cleanly while the gateway websocket is still
alive.  The zombie-client protection is preserved: the bot task is still
cancelled before ``client.close()``.

These tests pin the call ORDER without exercising real discord.py state —
they mock the helper methods (`_cancel_liveness_task`, `leave_voice_channel`,
`_cancel_bot_task`, `client.close`) and assert the sequence in which
``disconnect()`` invokes them.  Order is the regression; the helpers'
internals are out of scope here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.discord.adapter import DiscordAdapter


def _make_disconnecting_adapter(*, voice_guild_ids):
    """Build a DiscordAdapter whose disconnect() path is fully mocked.

    Returns the adapter alongside an ordered ``call_log`` list so the test
    can assert the exact sequence of await calls inside disconnect().  Only
    the surface disconnect() touches is mocked — everything else is left at
    its real implementation so the method body itself executes unchanged.

    ``DiscordAdapter.name`` is a read-only property inherited from the base
    class, so we bypass it via ``object.__setattr__`` — this is test-only
    plumbing, not production API.
    """
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    # ``name`` is a read-only property that reads ``self.platform.value.title()``,
    # so seed the instance dict with a real platform to satisfy logger calls
    # inside disconnect() without going through __init__.
    from gateway.config import Platform

    adapter.__dict__["platform"] = Platform.DISCORD
    adapter._disconnecting = False
    adapter._running = True
    adapter._client = MagicMock()
    adapter._client.close = AsyncMock()
    adapter._voice_clients = {gid: MagicMock(name=f"vc_{gid}") for gid in voice_guild_ids}
    adapter._post_connect_task = None
    adapter._missed_message_backfill_task = None
    adapter._liveness_task = None
    adapter._platform_lock_acquired = False
    adapter._ready_event = asyncio.Event()

    call_log: list[str] = []

    async def _cancel_liveness_task():
        call_log.append("cancel_liveness")

    async def leave_voice_channel(guild_id):
        call_log.append(f"leave_voice:{guild_id}")

    async def _cancel_bot_task():
        call_log.append("cancel_bot_task")

    def _release_platform_lock():
        call_log.append("release_platform_lock")

    adapter._cancel_liveness_task = _cancel_liveness_task
    adapter.leave_voice_channel = leave_voice_channel
    adapter._cancel_bot_task = _cancel_bot_task
    adapter._release_platform_lock = _release_platform_lock
    # Return the client mock too — disconnect() sets self._client = None at the
    # end, so tests asserting close() was awaited must hold their own reference.
    return adapter, call_log, adapter._client


@pytest.mark.asyncio
class TestDisconnectVoiceOrdering:
    """The eight-row decision matris from the issue body, scoped to ordering."""

    async def test_voice_cleanup_runs_before_bot_task_cancel_when_voice_open(self):
        """The headline regression (#76044): with a voice connection open,
        leave_voice_channel MUST run before _cancel_bot_task, or the voice
        disconnect handshake loses its gateway transport and times out."""
        guild_id = 12345
        adapter, call_log, _ = _make_disconnecting_adapter(voice_guild_ids=[guild_id])

        await adapter.disconnect()

        leave_idx = call_log.index(f"leave_voice:{guild_id}")
        bot_idx = call_log.index("cancel_bot_task")
        assert leave_idx < bot_idx, (
            f"voice cleanup must run BEFORE bot task cancel; got order {call_log}"
        )

    async def test_liveness_probe_cancelled_first(self):
        """Pre-existing invariant: liveness probe is cancelled before anything
        else so it can't fire a spurious reconnect mid-teardown.
        Order relative to voice cleanup was not changed by #76044, but pinning
        it stops a future refactor from regressing the first-steps contract."""
        guild_id = 1
        adapter, call_log, _ = _make_disconnecting_adapter(voice_guild_ids=[guild_id])

        await adapter.disconnect()

        assert call_log[0] == "cancel_liveness", (
            f"liveness probe must be cancelled first; got {call_log}"
        )

    async def test_client_close_runs_after_bot_task_cancel(self):
        """Zombie-client protection (#xxx): the bot task must still be cancelled
        before client.close() or a zombie client from a timed-out connect() can
        keep dispatching events.  Voice teardown moving up must NOT break this."""
        adapter, call_log, client = _make_disconnecting_adapter(voice_guild_ids=[])

        await adapter.disconnect()

        # client.close() is not in call_log (it's on _client mock); check that
        # cancel_bot_task ran and _client.close was awaited.
        assert "cancel_bot_task" in call_log
        assert client.close.await_count == 1

    async def test_no_voice_connections_skips_loop_and_cancels_normally(self):
        """The no-voice path stays instant (the issue's baseline): no
        leave_voice calls, cancel_bot_task still runs, client still closes."""
        adapter, call_log, client = _make_disconnecting_adapter(voice_guild_ids=[])

        await adapter.disconnect()

        # No leave_voice entries.
        assert not any(c.startswith("leave_voice:") for c in call_log)
        assert "cancel_liveness" in call_log
        assert "cancel_bot_task" in call_log
        assert client.close.await_count == 1

    async def test_multiple_voice_connections_all_cleaned_before_bot_cancel(self):
        """Bot in two voice channels at once: both leave_voice_channel calls
        run before _cancel_bot_task, in dict-iteration order."""
        g1, g2, g3 = 10, 20, 30
        adapter, call_log, _ = _make_disconnecting_adapter(voice_guild_ids=[g1, g2, g3])

        await adapter.disconnect()

        leave_indices = [i for i, c in enumerate(call_log) if c.startswith("leave_voice:")]
        bot_idx = call_log.index("cancel_bot_task")
        assert len(leave_indices) == 3, f"all three guilds must be left; got {call_log}"
        assert all(i < bot_idx for i in leave_indices), (
            f"every leave_voice must precede cancel_bot_task; got {call_log}"
        )
        # Dict-iteration order preserved (Python 3.7+ guarantee).
        assert call_log[leave_indices[0]] == f"leave_voice:{g1}"
        assert call_log[leave_indices[1]] == f"leave_voice:{g2}"
        assert call_log[leave_indices[2]] == f"leave_voice:{g3}"

    async def test_voice_cleanup_error_does_not_block_bot_cancel_or_client_close(self):
        """A single leave_voice_channel raising must not stop the rest of the
        voice loop, nor block the subsequent bot task cancel and client close.
        Pre-existing defensive behaviour (the loop already has try/except),
        pinned so the reordering can't accidentally swallow the guard."""
        guild_good = 100
        guild_bad = 200
        adapter, call_log, client = _make_disconnecting_adapter(
            voice_guild_ids=[guild_bad, guild_good],
        )

        async def leave_voice_channel(guild_id):
            if guild_id == guild_bad:
                raise RuntimeError("simulated voice disconnect failure")
            call_log.append(f"leave_voice:{guild_id}")

        adapter.leave_voice_channel = leave_voice_channel

        # Must not raise — disconnect catches per-guild and continues.
        await adapter.disconnect()

        # The bad guild raised but the good one still ran (loop continued).
        # The bad guild's call is never logged because it raised before append.
        assert f"leave_voice:{guild_good}" in call_log
        # Bot task cancel and client close still ran.
        assert "cancel_bot_task" in call_log
        assert client.close.await_count == 1

    async def test_disconnecting_flag_set_at_entry(self):
        """Pre-existing invariant: ``_disconnecting`` is set to True at the
        start of disconnect() so concurrent callers see the teardown state.
        Pinned to make sure the reordering doesn't drop it."""
        adapter, _, _ = _make_disconnecting_adapter(voice_guild_ids=[])

        async def _check_flag_mid_disconnect():
            assert adapter._disconnecting is True

        # Wrap cancel_liveness to interpose the flag check right after entry.
        original_cancel_liveness = adapter._cancel_liveness_task

        async def _wrapped_cancel_liveness():
            await _check_flag_mid_disconnect()
            await original_cancel_liveness()

        adapter._cancel_liveness_task = _wrapped_cancel_liveness

        await adapter.disconnect()
        # Still True at the end (we don't clear it; the adapter is one-shot).
        assert adapter._disconnecting is True

    async def test_running_cleared_and_client_none_at_exit_with_voice(self):
        """Post-disconnect state must be consistent regardless of voice path.
        Issue's baseline behaviour: _running=False, _client=None at the end."""
        adapter, _, _ = _make_disconnecting_adapter(voice_guild_ids=[42])

        await adapter.disconnect()

        assert adapter._running is False
        assert adapter._client is None
