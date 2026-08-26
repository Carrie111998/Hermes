"""Concurrent turns in one Slack channel must not share a streaming slot.

``_active_streams`` used to be keyed on ``chat_id`` alone, so two turns in the
same channel competed for one slot. Each time a turn found the slot holding the
other turn's stream it sealed that stream and opened a fresh one, so a single
answer arrived as several separate Slack messages — the duplicate-message
symptom in #95430 (cause C).

Keying per ``(chat_id, draft_id)`` gives each turn its own stream. The
same-turn segment hand-off (transports that bump ``draft_id`` at a tool
boundary) must still seal its own predecessor, which is what distinguishes
this from simply never sealing.

Behaviour contract asserted here:
  * two concurrent turns → exactly one startStream each, no cross-sealing
  * each turn's final send seals its OWN stream, no chat.postMessage
  * a same-turn draft_id bump still seals the superseded segment
  * a sibling turn's stream survives that hand-off
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _make_adapter():
    config = PlatformConfig(enabled=True, token="xoxb-fake", extra={})
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    client = AsyncMock()
    counter = {"n": 0}

    async def _start(**_kwargs):
        # Real await boundary: the slot bookkeeping spans this call, which is
        # exactly the window the two turns used to interleave in.
        await asyncio.sleep(0)
        counter["n"] += 1
        return {"ok": True, "ts": f"ts-{counter['n']}"}

    client.chat_startStream = AsyncMock(side_effect=_start)
    client.chat_appendStream = AsyncMock(return_value={"ok": True})
    client.chat_stopStream = AsyncMock(return_value={"ok": True})
    client.chat_postMessage = AsyncMock(return_value={"ts": "999.000"})
    client.chat_update = AsyncMock(return_value={"ts": "999.000"})
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    adapter._running = True
    return adapter, client


# Distinct thread anchors: two people asking in the same channel each get
# their own thread, which is what the gateway stamps on every frame.
TURN_A = {"thread_id": "111.000", "user_id": "U1"}
TURN_B = {"thread_id": "222.000", "user_id": "U2"}


class TestConcurrentTurns:
    @pytest.mark.asyncio
    async def test_each_turn_opens_exactly_one_stream(self):
        adapter, client = _make_adapter()

        await asyncio.gather(
            adapter.send_draft("C1", 1, "turn one ", metadata=TURN_A),
            adapter.send_draft("C1", 2, "turn two ", metadata=TURN_B),
        )
        await asyncio.gather(
            adapter.send_draft("C1", 1, "turn one continued", metadata=TURN_A),
            adapter.send_draft("C1", 2, "turn two continued", metadata=TURN_B),
        )

        assert client.chat_startStream.await_count == 2, (
            "each turn must open exactly one stream; extra startStream calls "
            "mean the turns are taking the slot from each other"
        )
        assert client.chat_stopStream.await_count == 0, (
            "no stream should be sealed while both turns are still streaming"
        )
        assert ("C1", 1) in adapter._active_streams
        assert ("C1", 2) in adapter._active_streams
        assert adapter._active_streams[("C1", 1)]["ts"] != (
            adapter._active_streams[("C1", 2)]["ts"]
        )

    @pytest.mark.asyncio
    async def test_each_turn_finalizes_into_its_own_stream(self):
        adapter, client = _make_adapter()

        await adapter.send_draft("C1", 1, "answer one", metadata=TURN_A)
        await adapter.send_draft("C1", 2, "answer two", metadata=TURN_B)

        first = await adapter.send("C1", "answer one, done.", metadata=TURN_A)
        second = await adapter.send("C1", "answer two, done.", metadata=TURN_B)

        assert first.success and second.success
        assert first.message_id != second.message_id, (
            "both turns finalized into the same stream"
        )
        client.chat_postMessage.assert_not_awaited()
        assert not adapter._active_streams

    @pytest.mark.asyncio
    async def test_interleaved_frames_keep_their_own_deltas(self):
        """Appends must be computed against the sending turn's own text."""
        adapter, client = _make_adapter()

        await adapter.send_draft("C1", 1, "AAA", metadata=TURN_A)
        await adapter.send_draft("C1", 2, "BBB", metadata=TURN_B)
        await adapter.send_draft("C1", 1, "AAA111", metadata=TURN_A)
        await adapter.send_draft("C1", 2, "BBB222", metadata=TURN_B)

        deltas = [
            (c.kwargs["ts"], c.kwargs["markdown_text"])
            for c in client.chat_appendStream.await_args_list
        ]
        ts_a = adapter._active_streams[("C1", 1)]["ts"]
        ts_b = adapter._active_streams[("C1", 2)]["ts"]
        assert (ts_a, "111") in deltas
        assert (ts_b, "222") in deltas


class TestSameTurnSegmentHandoff:
    @pytest.mark.asyncio
    async def test_draft_id_bump_seals_the_superseded_segment(self):
        """Transports that bump draft_id per tool boundary still hand off."""
        adapter, client = _make_adapter()

        await adapter.send_draft("C1", 7, "segment one", metadata=TURN_A)
        await adapter.send_draft("C1", 8, "segment two", metadata=TURN_A)

        client.chat_stopStream.assert_awaited_once()
        assert ("C1", 7) not in adapter._active_streams
        assert ("C1", 8) in adapter._active_streams

    @pytest.mark.asyncio
    async def test_handoff_does_not_seal_a_sibling_turn(self):
        """The other turn's stream must survive a same-turn segment bump."""
        adapter, client = _make_adapter()

        await adapter.send_draft("C1", 1, "sibling turn", metadata=TURN_B)
        await adapter.send_draft("C1", 7, "segment one", metadata=TURN_A)
        sibling_ts = adapter._active_streams[("C1", 1)]["ts"]

        await adapter.send_draft("C1", 8, "segment two", metadata=TURN_A)

        assert ("C1", 1) in adapter._active_streams, (
            "a same-turn segment hand-off sealed another turn's stream"
        )
        sealed = [c.kwargs["ts"] for c in client.chat_stopStream.await_args_list]
        assert sibling_ts not in sealed
