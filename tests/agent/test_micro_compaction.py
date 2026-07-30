"""Tests for per-turn micro-compaction in ``ContextCompressor``.

Micro-compaction amortizes the cost of context compression: instead of one
long pause when the window fills, each turn folds the single oldest
un-absorbed exchange into a rolling summary.

The invariants that matter:

* one call absorbs exactly one exchange (assistant + its tool results), so
  the per-turn cost stays bounded;
* the absorbed span is replaced by a summary marker carrying the usual
  ``_compressed_summary`` metadata, so resume/handoff treat it like a batch
  summary;
* the cursor advances, so successive calls walk forward rather than
  re-summarising the same exchange;
* protected head and tail messages are never touched;
* an exchange the summarizer cannot handle is retried a bounded number of
  times and then skipped, so a poison exchange can't stall every turn.
"""

from unittest.mock import patch

import pytest

from agent.context_compressor import (
    COMPRESSED_SUMMARY_HAS_USER_TURN_KEY,
    COMPRESSED_SUMMARY_METADATA_KEY,
    COMPRESSED_SUMMARY_SOURCE_KEY,
    HISTORICAL_TASK_HEADING,
    SUMMARY_PREFIX,
    ContextCompressor,
    _MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES,
    _SUMMARY_END_MARKER,
)


BATCH_ONLY_TEXT = "BATCH-ONLY CONTENT THE ROLLING SUMMARY NEVER SAW"


def _batch_marker(text: str = BATCH_ONLY_TEXT) -> dict:
    """A marker shaped like the one batch ``compress()`` emits.

    Note the absent source tag: batch does not claim micro-compaction's
    lineage, which is exactly what keeps supersede off it.
    """
    return {
        "role": "user",
        "content": (
            f"{SUMMARY_PREFIX}\n\n{HISTORICAL_TASK_HEADING}\n"
            f"{text}\n\n{_SUMMARY_END_MARKER}"
        ),
        COMPRESSED_SUMMARY_METADATA_KEY: True,
    }


def _worker_conversation(steps: int = 8) -> list:
    """Production-shaped transcript with no user turns left of its own.

    Two things differ from the other fixtures here, both deliberate: there is
    no ``role="system"`` row (the live ``messages`` list never carries one --
    the system prompt is prepended at request-build time), and the only user
    turn has already been folded into a batch marker, which is the shape the
    zero-user guard exists for.
    """
    msgs = [_batch_marker("seed prompt: work kanban task 42")]
    for i in range(steps):
        msgs.append({
            "role": "assistant",
            "content": f"step {i} " + "z" * 400,
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "sh", "arguments": "{}"},
            }],
        })
        msgs.append({
            "role": "tool", "tool_call_id": f"c{i}",
            "content": "output " + "y" * 300,
        })
    return msgs


def _compressor(summary="ROLLING SUMMARY") -> ContextCompressor:
    cc = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=1,
        protect_last_n=2,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    cc._micro_compact_enabled = True
    # Stand in for the auxiliary summarizer LLM.
    cc._micro_summarize_one = lambda _text: summary
    return cc


def _conversation(exchanges: int = 6) -> list:
    """A transcript shaped the way micro-compaction actually receives one.

    No ``role="system"`` row. ``_micro_compact`` is only ever called from
    ``finalize_turn`` on ``agent.messages``, and the system prompt is prepended
    to the wire copy at request-build time (``conversation_loop.py:1550``) --
    it is never a member of the stored list. Only the gateway ``/compress``
    path passes a system-bearing list, and that path is batch compaction, not
    this one.

    These fixtures used to open with a system row, which quietly kept
    ``_protect_head_size`` >= 1 and ``compress_start`` >= 1 in every test. The
    decayed-head shape (``compress_start == 0``, no predecessor before the
    absorbed span) was therefore never exercised, which is exactly where the
    marker-role and zero-user bugs live.
    """
    msgs = []
    for i in range(exchanges):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "z" * 400})
    return msgs


def _summary_markers(messages: list) -> list:
    return [m for m in messages if m.get(COMPRESSED_SUMMARY_METADATA_KEY)]


class TestMicroCompaction:
    def test_absorbs_one_exchange_and_leaves_a_summary_marker(self):
        cc = _compressor()
        messages = _conversation()

        result = cc._micro_compact(list(messages))

        # The absorbed assistant turn is gone from the transcript.
        assert any("answer 0" in str(m.get("content")) for m in messages)
        assert not any("answer 0" in str(m.get("content")) for m in result)
        markers = _summary_markers(result)
        assert len(markers) == 1
        assert "ROLLING SUMMARY" in markers[0]["content"]
        # The marker must alternate with BOTH neighbours, exactly as batch
        # compaction picks its summary role. Pinning it to "user" put it
        # straight after a real user turn, and the pre-send alternation repair
        # then merged the two and threw the marker's metadata away.
        assert markers[0]["role"] == "assistant"

    def test_disabled_is_a_no_op(self):
        cc = _compressor()
        cc._micro_compact_enabled = False
        messages = _conversation()

        assert cc._micro_compact(list(messages)) == messages

    def test_is_off_unless_explicitly_enabled(self):
        # A pass rewrites already-sent history, breaking the prompt-cache
        # prefix, so nobody inherits it from an update: it stays off until
        # `compression.micro_compact` opts in.
        cc = ContextCompressor(
            model="test-model",
            threshold_percent=0.75,
            protect_first_n=1,
            protect_last_n=2,
            quiet_mode=True,
            config_context_length=40960,
            provider="test",
        )
        cc._micro_summarize_one = lambda _text: "ROLLING SUMMARY"
        messages = _conversation()

        assert cc._micro_compact_enabled is False
        assert cc._micro_compact(list(messages)) == messages

    def test_cadence_of_one_runs_every_turn(self):
        cc = _compressor()
        cc._micro_compact_every_n_turns = 1
        messages = _conversation(exchanges=8)

        first = cc._micro_compact(list(messages))
        second = cc._micro_compact(list(first))

        assert cc._micro_compact_cursor > 0
        assert len(_summary_markers(first)) == 1
        # Each turn absorbed something, so the transcript kept shrinking.
        assert len(second) < len(first)

    def test_cadence_skips_turns_until_a_pass_is_due(self):
        cc = _compressor()
        cc._micro_compact_every_n_turns = 3
        messages = _conversation(exchanges=8)

        first = cc._micro_compact(list(messages))
        second = cc._micro_compact(list(first))

        # Cache prefix untouched on the turns in between.
        assert _summary_markers(first) == []
        assert _summary_markers(second) == []
        assert cc._micro_compact_cursor == 0

        third = cc._micro_compact(list(second))

        assert len(_summary_markers(third)) == 1
        assert not any("answer 0" in str(m.get("content")) for m in third)
        # Counter rearmed for the next window.
        assert cc._micro_compact_turns_since_pass == 0

    def test_cadence_is_clamped_to_at_least_one(self):
        # A bogus 0 or negative must not disable compaction silently, nor
        # divide-by-zero: it degrades to "every turn".
        for bogus in (0, -5):
            cc = _compressor()
            cc._micro_compact_every_n_turns = bogus
            messages = _conversation(exchanges=8)

            result = cc._micro_compact(list(messages))

            assert len(_summary_markers(result)) == 1

    def test_cursor_advances_across_successive_turns(self):
        cc = _compressor()
        messages = _conversation(exchanges=8)

        first = cc._micro_compact(list(messages))
        cursor_after_first = cc._micro_compact_cursor
        second = cc._micro_compact(list(first))

        assert cursor_after_first > 0
        assert cc._micro_compact_cursor >= cursor_after_first
        # Still exactly one marker: the second pass merges into the rolling
        # summary rather than stacking a second summary block.
        assert len(_summary_markers(second)) == 1

    def test_protected_head_and_tail_survive(self):
        cc = _compressor()
        messages = _conversation()

        result = cc._micro_compact(list(messages))

        assert result[0] == messages[0], "system prompt must be preserved"
        assert result[-1] == messages[-1], "most recent turn must be preserved"

    def test_user_messages_are_never_absorbed(self):
        """User turns stay verbatim for the life of the session — by design.

        Assistant output is largely an account of what was done and survives
        summarising; the user's own words are the intent everything else is
        derived from and can't be reconstructed from it. So an exchange starts
        at the assistant message and the walk skips past user turns.
        """
        cc = _compressor()
        messages = _conversation(exchanges=10)
        originals = [m["content"] for m in messages if m["role"] == "user"]

        for _ in range(5):
            messages = cc._micro_compact(messages)

        surviving = [
            m["content"] for m in messages
            if m.get("role") == "user" and not m.get(COMPRESSED_SUMMARY_METADATA_KEY)
        ]
        assert surviving == originals, "user turns must survive verbatim"

    def test_cursor_is_derived_from_the_spliced_list(self):
        """The cursor must never carry over a pre-splice index.

        A splice collapses an assistant plus its tool results -- often several
        messages -- into one marker, so every later index shifts. Reusing
        ``exchange_end`` left the cursor pointing inside a *later* exchange's
        tool group; the next pass walked forward to the following assistant
        and skipped that exchange entirely, so on tool-bearing conversations
        roughly half the work silently never happened.

        Tool-free fixtures cannot catch this: the span is one message, so
        nothing shifts.
        """
        cc = _compressor()
        msgs = []  # no system row: see _conversation's docstring
        for i in range(8):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append({
                "role": "assistant",
                "content": f"a{i}",
                "tool_calls": [
                    {"id": f"c{i}-{j}", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}}
                    for j in range(3)
                ],
            })
            for j in range(3):
                msgs.append({"role": "tool", "tool_call_id": f"c{i}-{j}",
                             "content": "T" * 500})

        for _ in range(4):
            msgs = cc._micro_compact(msgs)
            marker_idx = next(
                i for i, m in enumerate(msgs)
                if m.get(COMPRESSED_SUMMARY_METADATA_KEY)
            )
            assert cc._micro_compact_cursor == marker_idx + 1, (
                "cursor must sit just past the marker in the spliced list"
            )

    def test_resume_does_not_destroy_the_accumulated_summary(self):
        """A resumed session must not throw away compacted history.

        The rolling summary lives in memory; a resumed process starts with an
        empty one while the marker holding every previous exchange is still in
        the transcript. Superseding on that first pass would replace the whole
        history with a summary of one exchange.
        """
        msgs = _conversation(exchanges=10)
        first = _compressor(summary="IMPORTANT HISTORY: decisions and paths")
        for _ in range(3):
            msgs = first._micro_compact(msgs)
        assert any("IMPORTANT HISTORY" in m["content"] for m in _summary_markers(msgs))

        # Fresh compressor over the same transcript = resume.
        resumed = _compressor(summary="MERGED: history plus newest exchange")
        assert resumed._micro_compact_rolling_summary == ""
        result = resumed._micro_compact(msgs)

        markers = _summary_markers(result)
        assert len(markers) == 1
        assert "MERGED" in markers[0]["content"]

    def test_resume_keeps_the_old_marker_when_rehydration_fails(self):
        """If the prior summary can't be recovered, it must not be dropped."""
        msgs = _conversation(exchanges=10)
        first = _compressor(summary="IMPORTANT HISTORY: decisions and paths")
        for _ in range(3):
            msgs = first._micro_compact(msgs)

        resumed = _compressor(summary="BRAND NEW SUMMARY")
        resumed._rolling_summary_from_marker = staticmethod(lambda _c: "")
        result = resumed._micro_compact(msgs)

        markers = _summary_markers(result)
        assert len(markers) == 2, "must retain the un-carried history"
        assert any("IMPORTANT HISTORY" in m["content"] for m in markers)

    def test_rolling_summary_round_trips_through_a_marker(self):
        cc = _compressor()
        cc._micro_compact_rolling_summary = "decisions: use the existing helper"
        msgs = _conversation(exchanges=6)
        result = cc._micro_compact(msgs)
        marker = _summary_markers(result)[0]

        assert (cc._rolling_summary_from_marker(marker["content"])
                == cc._micro_compact_rolling_summary)

    def test_short_conversation_is_untouched(self):
        cc = _compressor()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

        assert cc._micro_compact(list(messages)) == messages

    def test_summarizer_failure_leaves_conversation_intact(self):
        cc = _compressor()
        cc._micro_summarize_one = lambda _text: None
        messages = _conversation()

        result = cc._micro_compact(list(messages))

        assert result == messages
        assert cc._micro_compact_consecutive_failures == 1

    def test_poison_exchange_is_skipped_after_repeated_failures(self):
        """A repeatedly unsummarizable exchange must not stall every turn."""
        cc = _compressor()
        cc._micro_summarize_one = lambda _text: None
        messages = _conversation()

        for _ in range(_MICRO_COMPACT_MAX_CONSECUTIVE_FAILURES):
            cc._micro_compact(list(messages))

        # The cursor has moved past the stuck exchange and the strike count
        # is reset, so the next turn attempts new material.
        assert cc._micro_compact_cursor > 0
        assert cc._micro_compact_consecutive_failures == 0

    def test_repeated_compaction_shrinks_context_and_keeps_one_marker(self):
        """The whole point: successive turns must reduce the transcript.

        The rolling summary is cumulative, so an earlier marker's text is a
        subset of the current one. Keeping the earlier markers stacked
        near-duplicate copies (each with its own heading/end-marker
        scaffolding) and made the transcript grow every turn — the opposite
        of what compaction is for.
        """
        from agent.model_metadata import estimate_messages_tokens_rough

        cc = _compressor()
        # Cumulative summary, like the real summarizer produces.
        state = {"n": 0}

        def growing(_text):
            state["n"] += 1
            return "SUMMARY " + " ".join(f"ex{i}" for i in range(state["n"]))

        cc._micro_summarize_one = growing

        messages = _conversation(exchanges=12)
        before = estimate_messages_tokens_rough(messages)
        for _ in range(6):
            messages = cc._micro_compact(messages)
        after = estimate_messages_tokens_rough(messages)

        assert len(_summary_markers(messages)) == 1
        assert after < before, f"context grew: {before} -> {after}"

    def test_emits_content_free_token_telemetry(self, caplog):
        """Each pass logs one JSON line with the token accounting.

        Message counts barely move even when the saving is large, so the token
        fields are what make the effect measurable in a real session.
        """
        import json
        import logging

        cc = _compressor()
        messages = _conversation(exchanges=8)

        with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
            result = cc._micro_compact(messages)

        lines = [
            r.getMessage() for r in caplog.records
            if "micro compaction telemetry:" in r.getMessage()
        ]
        assert len(lines) == 1
        payload = json.loads(lines[0].split("micro compaction telemetry: ", 1)[1])

        assert payload["event"] == "micro_compaction"
        assert payload["outcome"] == "absorbed"
        assert payload["tokens_saved_total"] == -payload["tokens_delta"]
        assert payload["passes_total"] == 1
        assert payload["messages_after"] == len(result)
        assert payload["exchange_tokens"] > 0
        # Content-free: no transcript text may ride along in the payload.
        blob = json.dumps(payload)
        assert "answer 0" not in blob and "question 0" not in blob

    def test_telemetry_reports_occupancy_without_forcing_resolution(self, caplog):
        """Occupancy is the headline: how full the window is being kept.

        It must be read from the cached threshold only. The public
        ``threshold_tokens`` property resolves lazily and can fire a
        synchronous /models probe (#32221); telemetry must never be what
        blocks a turn, so an unresolved window reports null instead.
        """
        import json
        import logging

        cc = _compressor()
        cc.threshold_tokens = 10_000  # pin; also populates the cache
        messages = _conversation(exchanges=8)

        with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
            cc._micro_compact(messages)

        line = next(r.getMessage() for r in caplog.records
                    if "micro compaction telemetry:" in r.getMessage())
        payload = json.loads(line.split("micro compaction telemetry: ", 1)[1])

        assert payload["threshold_tokens"] == 10_000
        assert payload["occupancy_pct"] == pytest.approx(
            payload["tokens_after"] / 10_000 * 100, abs=0.1
        )

    def test_emitter_never_forces_window_resolution(self, caplog):
        """The emitter reads the cached threshold, never the property.

        In a real pass the threshold is already resolved by the time
        telemetry runs (the tail calculation needs it), so occupancy is
        normally populated. This pins the safety property directly: with the
        cache empty, emitting reports null rather than triggering the lazy
        resolution — which can issue a synchronous /models probe (#32221).
        """
        import json
        import logging

        cc = _compressor()
        cc._threshold_tokens = None
        cc._resolved_context_length = None

        def explode(self):  # pragma: no cover - must never be called
            raise AssertionError("telemetry forced context-length resolution")

        with patch.object(type(cc), "threshold_tokens",
                          property(explode, lambda s, v: None)):
            with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
                cc._emit_micro_compaction_telemetry(
                    outcome="absorbed",
                    messages_before=10,
                    messages_after=9,
                    tokens_before=500,
                    tokens_after=400,
                )

        line = next(r.getMessage() for r in caplog.records
                    if "micro compaction telemetry:" in r.getMessage())
        payload = json.loads(line.split("micro compaction telemetry: ", 1)[1])

        assert payload["occupancy_pct"] is None
        assert payload["threshold_tokens"] is None

    def test_first_pass_costs_marker_overhead_then_pays_it_back(self):
        """The first pass can grow the transcript; later passes recover it.

        Inserting the summary marker costs a fixed ~400 tokens of scaffolding
        (the compaction preamble, the historical heading and the end marker).
        On pass one that overhead is paid against a single absorbed exchange,
        so the net can be positive. From pass two on the marker is replaced
        rather than added, so the scaffolding is already paid for and each
        absorbed exchange is pure saving. Anyone reading a single turn's
        telemetry needs to know this before concluding it made things worse.
        """
        from agent.model_metadata import estimate_messages_tokens_rough

        cc = _compressor()
        messages = _conversation(exchanges=10)
        start = estimate_messages_tokens_rough(messages)

        messages = cc._micro_compact(messages)
        after_first = estimate_messages_tokens_rough(messages)

        for _ in range(5):
            messages = cc._micro_compact(messages)
        after_many = estimate_messages_tokens_rough(messages)

        assert after_first > start, "expected one-time marker overhead"
        assert after_many < after_first, "later passes must recover it"

    def test_cumulative_savings_accumulate_across_passes(self):
        # Break-even is pass 5 on this fixture, not pass 4. The first pass pays
        # the marker's fixed scaffolding cost and each later pass recovers a
        # slice of it, so the crossing point moves whenever the marker's size
        # changes -- the provenance key that keeps supersede off foreign
        # markers added ~10 tokens to it. Assert the invariant this test is
        # named for (savings accumulate monotonically, and do turn positive)
        # rather than pinning the exact pass the sign flips on, which passed by
        # a 5-token margin and made an unrelated metadata change look like a
        # regression.
        cc = _compressor()
        messages = _conversation(exchanges=10)

        totals = []
        for _ in range(5):
            messages = cc._micro_compact(messages)
            totals.append(cc._micro_compact_tokens_saved_total)

        assert cc._micro_compact_passes == 5
        later = totals[1:]
        assert all(b > a for a, b in zip(later, later[1:])), (
            f"savings must improve every pass after the first: {totals}"
        )
        assert cc._micro_compact_tokens_saved_total > 0, totals

    def test_marker_survives_the_pre_send_alternation_repair(self):
        """The marker must still be a marker after the pre-request repair.

        ``conversation_loop`` runs ``repair_message_sequence_with_cursor``
        before every API call, and its pass 2 merges consecutive user rows by
        folding the second into the first and dropping the second dict. A
        marker emitted straight after a real user turn is therefore absorbed
        into that turn and loses ``_compressed_summary`` -- which is what
        supersede, cursor resolution and resume rehydration all key off. The
        repair also rewrites ``messages[:]`` in place, so the loss persists.
        """
        from unittest.mock import MagicMock

        from agent.agent_runtime_helpers import repair_message_sequence

        cc = _compressor()
        result = cc._micro_compact(list(_conversation(exchanges=8)))
        assert len(_summary_markers(result)) == 1

        repair_message_sequence(MagicMock(), result)

        assert len(_summary_markers(result)) == 1, (
            "marker metadata was destroyed by the pre-send alternation repair"
        )

    def test_repeated_passes_survive_the_repair_between_turns(self):
        """Simulate the real loop: pass, repair, pass, repair...

        Single-pass assertions hid the original accumulation bug, and the
        stress harness never ran the pre-send repair, so it could not have
        caught the marker being merged away either. Drive both together.
        """
        from unittest.mock import MagicMock

        from agent.agent_runtime_helpers import repair_message_sequence

        cc = _compressor()
        messages = _conversation(exchanges=12)
        agent = MagicMock()
        previous = len(messages)

        for turn in range(6):
            messages = cc._micro_compact(list(messages))
            repair_message_sequence(agent, messages)
            markers = _summary_markers(messages)
            assert len(markers) <= 1, f"turn {turn}: markers stacked ({len(markers)})"
            assert len(messages) <= previous, (
                f"turn {turn}: transcript grew {previous} -> {len(messages)}"
            )
            previous = len(messages)

        # The summary must still be discoverable after all that repairing --
        # supersede, cursor resolution and resume rehydration key off it.
        assert len(_summary_markers(messages)) == 1

    def test_decayed_protected_head_still_compacts_safely(self):
        """Once batch compaction has run, the protected head decays to zero.

        ``_effective_protect_first_n`` drops to 0 after the first compression,
        so ``_protect_head_size`` returns 0 and ``compress_start`` becomes 0 --
        the marker can then land at the very front of the transcript with no
        predecessor at all. Every fixture here used to open with a system row,
        which held ``compress_start`` at >= 1 and hid this shape entirely.
        """
        cc = _compressor()
        cc.compression_count = 1  # batch has run: head protection decays
        messages = _conversation(exchanges=8)

        assert cc._effective_protect_first_n() == 0
        assert cc._protect_head_size(messages) == 0

        result = cc._micro_compact(list(messages))

        markers = _summary_markers(result)
        assert len(markers) == 1
        idx = result.index(markers[0])
        if idx > 0:
            assert result[idx - 1]["role"] != markers[0]["role"]
        # A real user turn must still survive for strict backends.
        assert any(
            m.get("role") == "user" and not m.get(COMPRESSED_SUMMARY_METADATA_KEY)
            for m in result
        )

    def test_marker_that_absorbs_a_user_turn_is_not_persisted_hidden(self):
        """A marker about to swallow a user turn must not claim to be pure summary.

        Colliding forwards is sometimes unavoidable (backwards is worse), and
        pass 2 then folds the successor's text into the marker. ``run_agent``
        persists ``display_kind="hidden"`` for a marker whose has_user_turn is
        false, and every transcript surface drops hidden rows -- so a marker
        that absorbed a real user turn while still reporting False makes the
        user's own message vanish from the conversation.
        """
        from unittest.mock import MagicMock

        from agent.agent_runtime_helpers import repair_message_sequence

        cc = _compressor()
        cc._micro_compact_rolling_summary = "ROLLING SUMMARY"
        # Interim assistant before the exchange forces prev_role="assistant"
        # (pass 0 merges consecutive assistants but exempts codex interims),
        # and a real user turn follows the exchange -- so neither role is free.
        messages = [
            {"role": "user", "content": "opening ask"},
            {"role": "assistant", "content": "interim"},
        ]
        for i in range(8):
            messages.append({"role": "assistant", "content": f"answer {i} " + "z" * 400})
            messages.append({"role": "user", "content": f"REAL USER TURN {i}"})
        cc._micro_compact_cursor = 2

        result = cc._micro_compact(list(messages))
        repair_message_sequence(MagicMock(), result)

        markers = _summary_markers(result)
        assert len(markers) == 1
        marker = markers[0]
        if "REAL USER TURN 0" in str(marker.get("content")):
            assert marker.get(COMPRESSED_SUMMARY_HAS_USER_TURN_KEY) is True, (
                "marker absorbed a real user turn but still reports pure-summary "
                "provenance; run_agent will persist it display_kind='hidden' and "
                "the user's message disappears from every transcript surface"
            )

    def test_supersede_never_deletes_a_foreign_summary_marker(self):
        """A batch marker must survive micro-compaction passes.

        Supersede is only sound within micro-compaction's own cumulative
        lineage. A batch marker covers a different span, and the rolling
        summary has never read it -- ``_resolve_compact_cursor`` rehydrates
        from a marker only when the rolling summary is EMPTY, so in steady
        state nothing folds a foreign marker in before it is dropped. Deleting
        it destroyed the session's whole compacted history, silently.
        """
        cc = _compressor()
        # Steady state: micro has been running, so supersede is armed.
        cc._micro_compact_rolling_summary = "MICRO ROLLING SUMMARY"
        messages = [_batch_marker()] + _conversation(exchanges=8)[1:]

        result = cc._micro_compact(list(messages))

        assert any(BATCH_ONLY_TEXT in str(m.get("content")) for m in result), (
            "supersede deleted a batch summary; its content exists nowhere else"
        )

    def test_supersede_still_collapses_its_own_markers(self):
        """The foreign-marker fix must not disarm supersede for its own kind."""
        cc = _compressor()
        messages = _conversation(exchanges=10)

        first = cc._micro_compact(list(messages))
        second = cc._micro_compact(list(first))

        own = [
            m for m in second
            if m.get(COMPRESSED_SUMMARY_SOURCE_KEY) == "micro"
        ]
        assert len(own) == 1, "own markers stacked instead of superseding"

    def test_marker_never_collides_with_its_predecessor(self):
        """Colliding backwards is the fatal direction; never do it.

        ``repair_message_sequence`` pass 2 folds the second of two consecutive
        user rows into the first and drops the second dict. A marker that
        collides with the row BEFORE it is therefore the one destroyed, which
        is the original bug. Colliding with the row after is survivable.
        """
        for prev_role in ("user", "assistant", "tool", None):
            for next_role in ("user", "assistant", None):
                messages = []
                if prev_role is not None:
                    messages.append({"role": prev_role, "content": "before"})
                start = len(messages)
                messages.append({"role": "assistant", "content": "absorbed"})
                end = len(messages)
                if next_role is not None:
                    messages.append({"role": next_role, "content": "after"})

                role = ContextCompressor._micro_marker_role(
                    messages, start, end, []
                )
                assert role != prev_role, (
                    f"marker collides backwards with {prev_role!r} "
                    f"(next={next_role!r}) -- pass 2 will drop it"
                )

    def test_zero_user_transcript_is_never_produced(self):
        """Never hand a strict backend a transcript with no user row.

        vLLM/Qwen reject it with a non-retryable 400 "No user query found in
        messages", and every resume replays the same poisoned history. Batch
        compaction forces role="user" for this; micro-compaction needs the
        same backstop now that its role alternates and supersede can remove an
        earlier marker that was carrying "user".
        """
        cc = _compressor()
        cc._micro_compact_rolling_summary = "MICRO ROLLING SUMMARY"
        cc.compression_count = 1  # batch has run, so the protected head decays
        messages = _worker_conversation(steps=8)

        result = cc._micro_compact(list(messages))

        assert any(m.get("role") == "user" for m in result), (
            "no user-role message survives; this request 400s on vLLM/Qwen"
        )

    def test_retained_user_turns_survive_the_pre_send_repair(self):
        """Defrag keeps user turns -- the repair must not undo that."""
        from unittest.mock import MagicMock

        from agent.agent_runtime_helpers import repair_message_sequence

        cc = _compressor(summary="FRESH DEFRAGGED SUMMARY")
        cc._micro_compact_rolling_summary = "PRIOR SUMMARY " + "x" * 20_000
        cc._micro_compact_defrag_threshold_tokens = 10
        messages = _conversation(exchanges=8)
        originals = [m["content"] for m in messages if m["role"] == "user"]

        result = cc._micro_compact(list(messages))
        repair_message_sequence(MagicMock(), result)

        blob = "\n".join(
            str(m.get("content")) for m in result if m.get("role") == "user"
        )
        missing = [u for u in originals if u not in blob]
        assert not missing, f"repair lost retained user text: {missing}"

    def test_marker_never_sits_next_to_a_same_role_neighbour(self):
        cc = _compressor()
        result = cc._micro_compact(list(_conversation(exchanges=8)))

        idx = next(
            i for i, m in enumerate(result)
            if m.get(COMPRESSED_SUMMARY_METADATA_KEY)
        )
        role = result[idx]["role"]
        if idx > 0:
            assert result[idx - 1]["role"] != role, "marker collides with predecessor"
        if idx + 1 < len(result):
            assert result[idx + 1]["role"] != role, "marker collides with successor"

    def test_failed_defrag_commits_nothing(self):
        """A defrag whose summarizer fails must not splice.

        ``_defrag_rolling_summary`` leaves the rolling summary untouched when
        the auxiliary call returns nothing, but the splice used to run anyway
        -- replacing the whole middle with a marker built from the OLD summary,
        which by definition does not describe the messages just removed.
        """
        cc = _compressor()
        cc._micro_compact_rolling_summary = "PRIOR SUMMARY " + "x" * 20_000
        cc._micro_compact_defrag_threshold_tokens = 10
        cc._micro_summarize_one = lambda _t: ""  # summarizer fails
        messages = _conversation(exchanges=8)

        assert cc._needs_defrag() is True
        result = cc._micro_compact(list(messages))

        assert result == messages, "failed defrag committed a lossy splice"
        assert cc._micro_compact_rolling_summary.startswith("PRIOR SUMMARY")

    def test_defrag_preserves_user_turns_verbatim(self):
        """Defrag absorbs the whole middle -- user turns must survive it.

        The per-turn path starts each exchange at the assistant message, so it
        never touches user turns. Defrag splices ``[start:compress_end]``, a
        range that spans them, and the raw replacement dropped every one.
        """
        cc = _compressor(summary="FRESH DEFRAGGED SUMMARY")
        cc._micro_compact_rolling_summary = "PRIOR SUMMARY " + "x" * 20_000
        cc._micro_compact_defrag_threshold_tokens = 10
        messages = _conversation(exchanges=8)
        originals = [m["content"] for m in messages if m["role"] == "user"]

        result = cc._micro_compact(list(messages))

        survivors = [
            m["content"] for m in result
            if m.get("role") == "user" and not m.get(COMPRESSED_SUMMARY_METADATA_KEY)
        ]
        missing = [u for u in originals if u not in survivors]
        assert not missing, f"defrag dropped user turns: {missing}"

    def test_defrag_triggers_once_the_rolling_summary_grows(self):
        cc = _compressor(summary="FRESH DEFRAGGED SUMMARY")
        cc._micro_compact_rolling_summary = "x" * 40_000  # far over the threshold
        messages = _conversation(exchanges=8)

        assert cc._needs_defrag() is True
        result = cc._micro_compact(list(messages))

        assert cc._micro_compact_rolling_summary == "FRESH DEFRAGGED SUMMARY"
        markers = _summary_markers(result)
        assert len(markers) == 1
        assert "FRESH DEFRAGGED SUMMARY" in markers[0]["content"]
