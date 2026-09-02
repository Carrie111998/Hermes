"""Free-response channels can opt back into auto-threading (`auto_thread_channels`).

`free_response_channels` answers "do I need to @mention the bot here?" and
`auto_thread` answers "should each topic get its own thread?". Those are
independent wishes, but the auto-thread branch used to fold them together:

    skip_thread = bool(channel_keys & no_thread_channels) or is_free_channel

so exempting a channel from @mentions silently also turned its threading off.
A project channel legitimately wants both — no mention to start a turn, and a
thread per topic so long-running work stays separable.

`auto_thread_channels` restores threading for a named free-response channel.
`no_thread_channels` still wins, so an explicit "never thread here" cannot be
overridden by accident.
"""

import os

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import (
    _GATE_ENV_KEYS,
    DiscordAdapter,
    _should_skip_auto_thread,
)

GATE_VARS = [
    "DISCORD_NO_THREAD_CHANNELS",
    "DISCORD_AUTO_THREAD_CHANNELS",
    "DISCORD_FREE_RESPONSE_CHANNELS",
]


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for var in GATE_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in GATE_VARS:
        os.environ.pop(var, None)


def _adapter(extra: dict | None = None) -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="x", extra=dict(extra or {}))
    adapter._gate_env_snapshot = None
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter


class TestAutoThreadPrecedence:
    """The decision itself, independent of Discord message plumbing."""

    def test_free_response_channel_stays_flat_by_default(self):
        # Unchanged historical behaviour: no auto_thread_channels configured.
        assert _should_skip_auto_thread({"111"}, set(), set(), True) is True

    def test_free_response_channel_can_opt_into_threading(self):
        # The regression this feature exists for.
        assert _should_skip_auto_thread({"111"}, set(), {"111"}, True) is False

    def test_no_thread_channels_wins_over_auto_thread_channels(self):
        # Explicit "never thread here" is not overridable.
        assert _should_skip_auto_thread({"111"}, {"111"}, {"111"}, True) is True

    def test_non_free_channel_threads_normally(self):
        # A mention-gated channel already threads; nothing here changes it.
        assert _should_skip_auto_thread({"111"}, set(), set(), False) is False

    def test_non_free_channel_still_honors_no_thread(self):
        assert _should_skip_auto_thread({"111"}, {"111"}, set(), False) is True

    def test_unlisted_channel_is_unaffected_by_other_channels_config(self):
        assert _should_skip_auto_thread({"999"}, {"111"}, {"222"}, False) is False
        assert _should_skip_auto_thread({"999"}, {"111"}, {"222"}, True) is True

    def test_channel_name_keys_work_like_ids(self):
        # channel_keys carries id, bare name and #name — any may be configured.
        assert _should_skip_auto_thread({"111", "trading-strategy", "#trading-strategy"}, set(), {"#trading-strategy"}, True) is False


class TestAutoThreadChannelsGate:
    """Reading the list follows the same per-profile isolation as its siblings."""

    def test_reads_from_config_extra(self):
        adapter = _adapter({"auto_thread_channels": "111,222"})

        assert adapter._get_auto_thread_channels() == {"111", "222"}

    def test_defaults_to_empty(self):
        assert _adapter()._get_auto_thread_channels() == set()

    def test_is_a_registered_gate_key(self):
        # Membership in _GATE_ENV_KEYS is what makes the value part of the
        # connect()-time per-profile snapshot. Without it, one profile's list
        # would leak into another under gateway.multiplex_profiles (#72348).
        assert "DISCORD_AUTO_THREAD_CHANNELS" in _GATE_ENV_KEYS

    def test_two_profiles_stay_isolated(self):
        a = _adapter({"auto_thread_channels": "111"})
        b = _adapter({"auto_thread_channels": "222"})
        a._gate_env_snapshot = {k: "" for k in _GATE_ENV_KEYS} | {"DISCORD_AUTO_THREAD_CHANNELS": "111"}
        b._gate_env_snapshot = {k: "" for k in _GATE_ENV_KEYS} | {"DISCORD_AUTO_THREAD_CHANNELS": "222"}

        assert a._get_auto_thread_channels() == {"111"}
        assert b._get_auto_thread_channels() == {"222"}

    def test_process_env_does_not_leak_into_snapshotted_adapter(self, monkeypatch):
        monkeypatch.setenv("DISCORD_AUTO_THREAD_CHANNELS", "999")
        b = _adapter({"auto_thread_channels": "222"})
        b._gate_env_snapshot = {k: "" for k in _GATE_ENV_KEYS} | {"DISCORD_AUTO_THREAD_CHANNELS": "222"}

        assert b._get_auto_thread_channels() == {"222"}
