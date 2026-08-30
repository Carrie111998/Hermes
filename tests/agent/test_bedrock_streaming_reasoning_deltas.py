"""Regression tests for corrupted ``reasoning_content`` on the Bedrock streaming path.

``ConverseStream`` delivers thinking as a long run of ``reasoningContent`` deltas
that each carry a *fragment* — frequently a fragment of a single word. The
accumulator appended every fragment to ``reasoning_parts`` as though it were a
whole reasoning block, then joined the list with ``"\\n\\n"``, so the block
separator was welded between every streamed token::

    deltas  : 'Let'  ' me list'  ' divis'  'ors other than 1 and itself'
    stored  : 'Let\\n\\n me list\\n\\n divis\\n\\nors other than 1 and itself'
    correct : 'Let me list divisors other than 1 and itself'

Two properties made this hard to spot:

* ``on_reasoning_delta`` was always handed the correct raw chunk, so the live TUI
  rendered the thinking perfectly. Only the *stored* value was corrupted — which
  is what feeds conversation history, context compression, replay and token
  accounting.
* The inflation is exactly ``2 * (non_empty_deltas - 1)`` bytes, which merely
  looks like verbose model output rather than a bug.

The fix mirrors the text path directly above it in the same loop: buffer
contiguous deltas, ``"".join`` them on the ``contentBlockStop`` boundary, and keep
``"\\n\\n"`` for separating genuinely distinct reasoning blocks.

The primary fixture below is a real delta sequence captured from
``us.anthropic.claude-sonnet-4-6`` in ``us-east-2``
(``x-amzn-RequestId: 824382e9-5ff1-4f42-8d5c-43f28914e3df``, botocore 1.42.97),
kept verbatim — including the trailing empty delta and the model's own ``\\n\\n``
paragraph breaks, which must survive untouched.
"""

from agent.bedrock_adapter import stream_converse_with_callbacks

# Captured verbatim from a live ConverseStream call with thinking enabled.
REAL_DELTAS = [
    "Let", " me list", " the first 30 prime numbers step", " by step.",
    "\n\nA prime number is a number", " greater than 1 that has no", " divis",
    "ors other than 1 and itself", ".\n\n1", ". 2", "\n2. 3\n3. ", "5\n4. 7\n5",
    ". 11\n6. 13", "\n7. 17\n8.", " 19\n9. 23", "\n10. 29\n11. ",
    "31\n12. 37\n13", ". 41\n14. 43", "\n15. 47\n16.", " 53\n17. 59",
    "\n18. 61\n19. ", "67\n20. 71\n21", ". 73\n22. 79", "\n", "23. 83\n24. ",
    "89\n25. 97\n26", ". 101\n27. 103", "\n28. 107\n29.", " 109\n30. 113",
    "\n\nNow", " I'm", " adding", " them", " all", " together", " to",
    " get the sum", " of the first 30 pr", "imes.", "\n\nContinuing", " the",
    " running", " total", "...", "",
]

# Concatenating the deltas in order IS the model's thinking text, by definition
# of a delta stream. Everything else is measured against this.
REAL_TRUTH = "".join(REAL_DELTAS)


def _reasoning_delta(text, index=0):
    return {"contentBlockDelta": {"contentBlockIndex": index,
                                  "delta": {"reasoningContent": {"text": text}}}}


def _text_delta(text, index=0):
    return {"contentBlockDelta": {"contentBlockIndex": index,
                                  "delta": {"text": text}}}


def _stop(index=0):
    return {"contentBlockStop": {"contentBlockIndex": index}}


def _run(events, **callbacks):
    return stream_converse_with_callbacks({"stream": iter(events)}, **callbacks)


def _reasoning_of(events, **callbacks):
    return _run(events, **callbacks).choices[0].message.reasoning_content


def _real_stream():
    return [_reasoning_delta(d) for d in REAL_DELTAS] + [_stop()]


# ── the real captured stream ──────────────────────────────────────────────────

def test_real_stream_reasoning_is_verbatim():
    """The stored thinking text is byte-identical to what the model streamed."""
    assert _reasoning_of(_real_stream()) == REAL_TRUTH


def test_real_stream_has_no_separator_inflation():
    """No bytes are invented. The old accumulator added 2 * (44 - 1) == 86."""
    non_empty = len([d for d in REAL_DELTAS if d])
    assert non_empty == 44
    assert len(_reasoning_of(_real_stream())) == len(REAL_TRUTH) == 450
    # What the bug produced, stated explicitly so a regression is unmistakable.
    assert len("\n\n".join(d for d in REAL_DELTAS if d)) == 450 + 2 * (non_empty - 1)


def test_real_stream_preserves_only_the_models_own_paragraph_breaks():
    """The model emitted 4 blank lines; the bug reported 47."""
    assert _reasoning_of(_real_stream()).count("\n\n") == REAL_TRUTH.count("\n\n") == 4


def test_word_split_across_deltas_is_not_broken_apart():
    """'divis' + 'ors' is one word. The bug turned it into 'divis\\n\\nors'."""
    result = _reasoning_of(_real_stream())
    assert "divisors other than 1 and itself" in result
    assert "divis\n\nors" not in result


def test_leading_whitespace_in_deltas_is_preserved():
    """Deltas carry their own spacing; the accumulator must not trim or add any."""
    result = _reasoning_of(_real_stream())
    assert result.startswith("Let me list the first 30 prime numbers step by step.")
    assert result.endswith("Continuing the running total...")


# ── the live callback contract must not change ────────────────────────────────

def test_reasoning_delta_callback_still_fires_once_per_raw_chunk():
    """Buffering for storage must not batch or delay the streaming callback —
    that is the half of this path that was already correct."""
    chunks = []
    _reasoning_of(_real_stream(), on_reasoning_delta=chunks.append)
    assert chunks == [d for d in REAL_DELTAS if d]
    assert len(chunks) == 44
    assert "".join(chunks) == REAL_TRUTH


# ── block separation is still meaningful ──────────────────────────────────────

def test_distinct_reasoning_blocks_remain_separated():
    """Fragments within a block are concatenated; separate blocks keep "\\n\\n"."""
    events = [
        _reasoning_delta("First", 0), _reasoning_delta(" thought", 0), _stop(0),
        _reasoning_delta("Second", 1), _reasoning_delta(" thought", 1), _stop(1),
    ]
    assert _reasoning_of(events) == "First thought\n\nSecond thought"


def test_single_block_is_not_prefixed_or_suffixed_with_separators():
    events = [_reasoning_delta("only"), _reasoning_delta(" one"), _stop()]
    assert _reasoning_of(events) == "only one"


# ── partial and truncated streams ─────────────────────────────────────────────

def test_reasoning_is_flushed_when_stream_ends_without_content_block_stop():
    """A truncated stream still yields the thinking received so far."""
    events = [_reasoning_delta("half a "), _reasoning_delta("thought")]
    assert _reasoning_of(events) == "half a thought"


def test_reasoning_is_flushed_when_interrupted_mid_block():
    """Interrupt breaks out of the loop before contentBlockStop; partial
    thinking is better than silently dropping all of it."""
    events = [_reasoning_delta("keep "), _reasoning_delta("this"),
              _reasoning_delta(" not this"), _stop()]
    calls = {"n": 0}

    def interrupted():
        calls["n"] += 1
        return calls["n"] > 2

    assert _reasoning_of(events, on_interrupt_check=interrupted) == "keep this"


# ── interaction with the other content types ─────────────────────────────────

def test_reasoning_and_text_accumulate_independently():
    events = [
        _reasoning_delta("thinking", 0), _reasoning_delta(" hard", 0), _stop(0),
        _text_delta("the ", 1), _text_delta("answer", 1), _stop(1),
    ]
    msg = _run(events).choices[0].message
    assert msg.reasoning_content == "thinking hard"
    assert msg.content == "the answer"


def test_reasoning_survives_a_following_tool_use_block():
    events = [
        _reasoning_delta("I should ", 0), _reasoning_delta("call a tool", 0), _stop(0),
        {"contentBlockStart": {"contentBlockIndex": 1,
                               "start": {"toolUse": {"toolUseId": "tu_1", "name": "bash"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1,
                               "delta": {"toolUse": {"input": '{"cmd": "ls"}'}}}},
        _stop(1),
    ]
    msg = _run(events).choices[0].message
    assert msg.reasoning_content == "I should call a tool"
    assert [tc.function.name for tc in msg.tool_calls] == ["bash"]


# ── degenerate deltas ─────────────────────────────────────────────────────────

def test_empty_deltas_do_not_create_spurious_separators():
    events = [_reasoning_delta("a"), _reasoning_delta(""), _reasoning_delta("b"), _stop()]
    assert _reasoning_of(events) == "ab"


def test_signature_only_delta_produces_no_reasoning_text():
    """The final reasoning delta often carries only a signature, no text."""
    events = [
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "reasoningContent": {"signature": "AbCdEf=="}}}},
        _stop(),
    ]
    assert _reasoning_of(events) is None


def test_no_reasoning_at_all_yields_none():
    events = [_text_delta("just text"), _stop()]
    assert _reasoning_of(events) is None


def test_non_dict_reasoning_content_is_ignored():
    events = [{"contentBlockDelta": {"contentBlockIndex": 0,
                                     "delta": {"reasoningContent": "unexpected"}}}, _stop()]
    assert _reasoning_of(events) is None
