"""Regression guard: thread_require_mention must not be bypassed via a
free-response/allowed PARENT channel (kanban t_0e17e660).

Bug: ``free_response_channels``/``allowed_channels`` gates test a channel-key
set that always includes the parent channel id/name (so a forum-wide
free-response entry cascades to every thread under it — the documented,
desired behaviour). But a fresh forum thread under a *watched* parent (e.g.
in DISCORD_ALLOWED_CHANNELS as a shared/monitored channel) silently inherited
that parent's free-response status too, which meant the require-mention gate
never even got evaluated — DISCORD_THREAD_REQUIRE_MENTION=true had no effect
on brand-new threads. Symptom: gatekeeper (Deadbolt) self-triggered in a new
#workbench forum thread it was never @-mentioned in.

Fix: ``_discord_free_response_test_keys`` strips the parent-derived keys
before the free-response test specifically when thread_require_mention is
on, so a thread can only be exempted by its OWN id/name being listed, never
by inheriting the parent's.
"""

import types

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(extra: dict | None = None) -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="x", extra=dict(extra or {}))
    adapter._gate_env_snapshot = None
    return adapter


def _thread_channel(thread_id="900", parent_id="1540061795971104788", parent_name="workbench"):
    parent = types.SimpleNamespace(id=int(parent_id), name=parent_name)
    return types.SimpleNamespace(id=int(thread_id), name="new-thread", parent=parent, parent_id=int(parent_id))


class TestFreeResponseParentLeak:
    def test_thread_require_mention_strips_parent_keys(self):
        """thread_require_mention=true: a brand-new thread must NOT inherit
        free-response eligibility from its parent channel being listed."""
        adapter = _adapter({"thread_require_mention": True})
        channel = _thread_channel()
        # channel_keys as built by _discord_channel_keys_from_channel: own id/name
        # PLUS the parent's id/name (the cascading behaviour for channels).
        channel_keys = {"900", "new-thread", "#new-thread", "1540061795971104788", "workbench", "#workbench"}

        test_keys = adapter._discord_free_response_test_keys(channel, channel_keys, is_thread=True)

        free_channels = {"1540061795971104788"}  # only the PARENT is free-response
        assert not (test_keys & free_channels), (
            "thread inherited free-response status from its parent even though "
            "thread_require_mention=true"
        )

    def test_thread_require_mention_still_honors_own_id(self):
        """The thread's OWN id being explicitly listed still exempts it."""
        adapter = _adapter({"thread_require_mention": True})
        channel = _thread_channel()
        channel_keys = {"900", "new-thread", "#new-thread", "1540061795971104788", "workbench", "#workbench"}

        test_keys = adapter._discord_free_response_test_keys(channel, channel_keys, is_thread=True)

        free_channels = {"900"}  # the thread itself, not the parent
        assert test_keys & free_channels

    def test_thread_require_mention_false_keeps_parent_cascade(self):
        """Default (thread_require_mention=false): parent cascade still works
        — this is the documented, desired behaviour for normal threads."""
        adapter = _adapter({"thread_require_mention": False})
        channel = _thread_channel()
        channel_keys = {"900", "new-thread", "#new-thread", "1540061795971104788", "workbench", "#workbench"}

        test_keys = adapter._discord_free_response_test_keys(channel, channel_keys, is_thread=True)

        assert test_keys == channel_keys

    def test_non_thread_channel_unaffected(self):
        """Plain (non-thread) channels are never touched by this helper."""
        adapter = _adapter({"thread_require_mention": True})
        channel = types.SimpleNamespace(id=111, name="general", parent=None, parent_id=None)
        channel_keys = {"111", "general", "#general"}

        test_keys = adapter._discord_free_response_test_keys(channel, channel_keys, is_thread=False)

        assert test_keys == channel_keys
