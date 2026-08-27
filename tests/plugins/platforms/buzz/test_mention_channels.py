"""Per-channel mention gating for BuzzAdapter.

``require_mention`` is a single global switch per agent. ``mention_channels``
refines it: channel UUIDs listed there require a mention even when
``require_mention`` is False. This enables the "offices + meeting room"
topology — an agent that answers everything in its own channel but only
speaks when addressed in a shared channel.

The gate lives inside ``_handle_event``, so that is what these tests drive:
each case feeds a real event through the real dispatch path and asserts on
whether ``_dispatch_message`` was reached. Only the two leaves that would
touch the relay (``_dispatch_message`` and ``_resolve_user_name``) are
stubbed; the gating logic itself is never re-implemented here, otherwise a
broken integration would still pass.
"""
from __future__ import annotations

from collections import OrderedDict

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.buzz.adapter import BuzzAdapter

_OFFICE = "9b8e7e29-a385-4180-9acf-14bf7df05fcf"
_SHARED = "f28ceae7-c1c9-4190-b36e-4cd7d5461e55"
_SENDER = "11" * 32
_SELF = "22" * 32

# Neither string contains the agent's display name, so they are unmentioned.
_PLAIN = "the deploy finished, logs look clean"
_ADDRESSED = "@chip can you check the deploy?"


def _make_adapter(
    monkeypatch: pytest.MonkeyPatch, extra: dict | None = None
) -> tuple[BuzzAdapter, list[dict]]:
    """Adapter wired for offline dispatch, plus the list it dispatches into."""
    monkeypatch.setenv("BUZZ_RELAY_URL", "https://example.communities.buzz.xyz")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "00" * 32)
    monkeypatch.delenv("BUZZ_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("BUZZ_CHANNELS", raising=False)
    adapter = BuzzAdapter(PlatformConfig(enabled=True, token="", extra=extra or {}))

    adapter._display_name = "chip"
    adapter._self_pubkey = _SELF

    dispatched: list[dict] = []

    async def _capture(**kwargs) -> None:
        dispatched.append(kwargs)

    async def _name(pubkey: str) -> str:
        return "tester"

    monkeypatch.setattr(adapter, "_dispatch_message", _capture)
    monkeypatch.setattr(adapter, "_resolve_user_name", _name)
    return adapter, dispatched


def _state(chat_type: str = "group") -> dict:
    return {"chat_type": chat_type, "last_ts": 0, "seen": OrderedDict()}


def _event(content: str, event_id: str = "evt-1") -> dict:
    return {
        "id": event_id,
        "created_at": 1_700_000_000,
        "kind": 9,
        "pubkey": _SENDER,
        "content": content,
    }


# ── dispatch through the real gate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_forced_channel_drops_unmentioned_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listed channel keeps requiring a mention even with the global flag off."""
    adapter, dispatched = _make_adapter(
        monkeypatch, {"require_mention": False, "mention_channels": [_SHARED]}
    )
    await adapter._handle_event(_SHARED, _state(), _event(_PLAIN))
    assert dispatched == []


@pytest.mark.asyncio
async def test_forced_channel_dispatches_when_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, dispatched = _make_adapter(
        monkeypatch, {"require_mention": False, "mention_channels": [_SHARED]}
    )
    await adapter._handle_event(_SHARED, _state(), _event(_ADDRESSED))
    assert len(dispatched) == 1
    assert dispatched[0]["chat_id"] == _SHARED
    assert dispatched[0]["chat_type"] == "group"
    # The leading mention is stripped before the agent sees the text.
    assert dispatched[0]["text"] == "can you check the deploy?"


@pytest.mark.asyncio
async def test_free_channel_dispatches_without_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlisted channel stays free-listening — that is the whole point."""
    adapter, dispatched = _make_adapter(
        monkeypatch, {"require_mention": False, "mention_channels": [_SHARED]}
    )
    await adapter._handle_event(_OFFICE, _state(), _event(_PLAIN))
    assert len(dispatched) == 1
    assert dispatched[0]["chat_id"] == _OFFICE
    assert dispatched[0]["text"] == _PLAIN


@pytest.mark.asyncio
async def test_dm_dispatches_even_when_channel_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DMs are never gated, listed channel or not."""
    adapter, dispatched = _make_adapter(
        monkeypatch, {"require_mention": True, "mention_channels": [_SHARED]}
    )
    await adapter._handle_event(_SHARED, _state("dm"), _event(_PLAIN))
    assert len(dispatched) == 1
    assert dispatched[0]["chat_type"] == "dm"


@pytest.mark.asyncio
async def test_global_flag_gates_unlisted_channels_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mention_channels only ever adds a requirement; it never lifts one."""
    adapter, dispatched = _make_adapter(
        monkeypatch, {"require_mention": True, "mention_channels": [_SHARED]}
    )
    await adapter._handle_event(_OFFICE, _state(), _event(_PLAIN))
    assert dispatched == []

    await adapter._handle_event(_OFFICE, _state(), _event(_ADDRESSED, "evt-2"))
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_listed_channel_matches_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UUIDs arrive from the relay in whatever case; the gate must still bite."""
    adapter, dispatched = _make_adapter(
        monkeypatch,
        {"require_mention": False, "mention_channels": [f"  {_SHARED.upper()}  "]},
    )
    await adapter._handle_event(_SHARED, _state(), _event(_PLAIN))
    assert dispatched == []


# ── configuration parsing ─────────────────────────────────────────────────


def test_defaults_to_no_forced_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = _make_adapter(monkeypatch, {"require_mention": False})
    assert adapter.mention_channels == set()


def test_entries_are_trimmed_lowered_and_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = _make_adapter(
        monkeypatch,
        {
            "require_mention": False,
            "mention_channels": [f"  {_SHARED.upper()}  ", "", "   "],
        },
    )
    assert adapter.mention_channels == {_SHARED}


def test_comma_separated_string_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """YAML users reasonably write a single string instead of a list."""
    adapter, _ = _make_adapter(
        monkeypatch,
        {"require_mention": False, "mention_channels": f"{_SHARED}, {_OFFICE}"},
    )
    assert adapter.mention_channels == {_SHARED, _OFFICE}
