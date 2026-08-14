"""Tests for pre-API-call message-sequence repair.

Covers ``_repair_message_sequence`` and the extended
``_drop_trailing_empty_response_scaffolding`` behavior that rewinds past
orphan tool-result tails. Together these prevent the self-reinforcing empty-
response loop observed in session 20260507_044111_fa7e65, where a tool-result
followed directly by a user message produced silent empty responses from
providers (violating role alternation), which retriggered the empty-retry
recovery every turn.
"""

from run_agent import AIAgent


def _bare_agent():
    return AIAgent.__new__(AIAgent)


# ── _drop_trailing_empty_response_scaffolding ──────────────────────────────

def test_drop_scaffolding_rewinds_orphan_tool_tail():
    """When scaffolding is stripped, also rewind the orphan assistant+tool pair."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "assistant", "content": "(empty)",
         "_empty_terminal_sentinel": True},
    ]

    AIAgent._drop_trailing_empty_response_scaffolding(agent, messages)

    assert messages == [{"role": "user", "content": "task"}]






# ── _repair_message_sequence ───────────────────────────────────────────────

def test_repair_merges_consecutive_user_messages():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 1
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "first\n\nsecond"


def test_repair_preserves_user_content_when_one_side_empty():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "real message"},
    ]

    AIAgent._repair_message_sequence(agent, messages)

    assert messages == [{"role": "user", "content": "real message"}]


def test_repair_does_not_rewind_ongoing_dialog_tool_pair():
    """assistant(tool_calls) + tool + user is a VALID pattern (user redirect
    before the model gets its continuation turn). Repair must not touch it —
    only the flag-gated scaffolding strip rewinds, and only when the
    empty-recovery scaffolding was actually present.
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "user", "content": "Q2"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_drops_stray_tool_with_unknown_tool_call_id():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "tool_call_id": "orphan", "content": "stray"},
        {"role": "user", "content": "real"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    assert all(m.get("role") != "tool" for m in messages)


def test_repair_keeps_tool_matching_codex_call_id():
    """A valid tool result must survive when the assistant tool_call carries a
    Codex-format ``call_id`` distinct from ``id`` and the result matches on
    ``call_id`` (#58168).

    Before the fix, Pass 1 registered only ``tc.get("id")`` (``fc_...``) in the
    known-id set, so a result keyed on ``call_id`` (``call_...``) looked
    orphaned and was dropped -- leaving the assistant tool_call unanswered and
    triggering an HTTP 400 on strict providers (DeepSeek, Kimi):
    "Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'".
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "fc_123", "call_id": "call_ABC",
                         "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_ABC", "content": "result"},
        {"role": "user", "content": "next"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2]["tool_call_id"] == "call_ABC"


def test_repair_keeps_tool_matching_only_call_id():
    """Same as above but the assistant tool_call carries ONLY ``call_id`` (no
    ``id``). The result keyed on ``call_id`` must still be recognized (#58168).
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"call_id": "call_XYZ", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_XYZ", "content": "result"},
        {"role": "user", "content": "next"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert any(m.get("role") == "tool" for m in messages)














# ── repair_message_sequence_with_cursor (#44837) ───────────────────────────

from agent.agent_runtime_helpers import repair_message_sequence_with_cursor


def test_cursor_clamped_when_compaction_shrinks_below_cursor():
    """Cursor past the new end of the list must come back in range so the
    turn-end flush doesn't skip the assistant/tool chain (#44837)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    agent._last_flushed_db_idx = 2  # both rows already flushed

    repairs = repair_message_sequence_with_cursor(agent, messages)

    assert repairs == 1
    assert len(messages) == 1
    assert agent._last_flushed_db_idx == 1


def test_cursor_rewinds_when_compaction_happens_before_cursor():
    """Repair that drops/merges messages at indexes BELOW the cursor must
    rewind it by the number removed, or unflushed rows get skipped.
    A plain min() clamp does NOT catch this case."""
    agent = _bare_agent()
    flushed_a = {"role": "user", "content": "first"}
    flushed_b = {"role": "user", "content": "second"}  # merged into flushed_a
    unflushed_assistant = {"role": "assistant", "content": "answer"}
    messages = [flushed_a, flushed_b, unflushed_assistant]
    agent._last_flushed_db_idx = 2  # the two user rows are flushed

    repairs = repair_message_sequence_with_cursor(agent, messages)

    assert repairs == 1
    assert len(messages) == 2
    # Cursor must now point at the assistant (index 1), not stay at 2 —
    # min(2, len=2) would leave it at 2 and the flush would skip it.
    assert agent._last_flushed_db_idx == 1
    assert messages[agent._last_flushed_db_idx] is unflushed_assistant






def test_flush_guard_clamps_overshooting_cursor():
    """_flush_messages_to_session_db safety net: an overshooting cursor must
    not produce a negative-start slice that skips everything (#44837)."""

    class _DB:
        def __init__(self):
            self.rows = []

        def append_message(self, **kw):
            self.rows.append(kw)

        def append_messages_batch(self, session_id, messages, **kw):
            for m in messages:
                self.rows.append(dict(m, session_id=session_id))
            return list(range(1, len(messages) + 1))

    agent = _bare_agent()
    agent._session_db = _DB()
    agent._session_db_created = True
    agent.session_id = "s1"
    agent._persist_user_message_override = None
    agent._last_flushed_db_idx = 5  # stale — past end of compacted list
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]

    AIAgent._flush_messages_to_session_db(agent, messages, conversation_history=[])

    # min(5, 2) = 2 → nothing skipped below start_idx, cursor settles at 2
    assert agent._last_flushed_db_idx == 2


# ── Pass 0: merge consecutive assistant messages (issue #29148, #49147) ─────



















# ── tool_call_id de-duplication (#58327) ────────────────────────────────────
# Strict providers (DeepSeek) reject a payload where the same tool_call_id
# appears more than once with HTTP 400 "Duplicate value for 'tool_call_id'".




def test_sanitize_deduplicates_duplicate_tool_results():
    """sanitize_api_messages (final pre-API chokepoint) drops duplicate tool
    results sharing a tool_call_id."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_X", "type": "function",
                         "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_X", "content": "A"},
        {"role": "tool", "tool_call_id": "call_X", "content": "B (duplicate)"},
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_api_messages(list(messages))
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == ["call_X"]  # exactly one survives


def test_sanitize_deduplicates_duplicate_assistant_tool_call_ids():
    """sanitize_api_messages collapses duplicate tool_calls sharing an id
    WITHIN a single assistant message (the message[6] shape from #58327)."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_Y", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}},
            {"id": "call_Y", "type": "function",
             "function": {"name": "bar", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_Y", "content": "r"},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    ids = [tc["id"] for tc in assistant["tool_calls"]]
    assert ids == ["call_Y"]  # duplicate collapsed


def test_sanitize_preserves_distinct_tool_call_ids():
    """Negative control: legitimate DISTINCT tool_call_ids must NOT be dropped
    (guards against over-dedup)."""
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_A", "type": "function",
             "function": {"name": "a", "arguments": "{}"}},
            {"id": "call_B", "type": "function",
             "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_A", "content": "ra"},
        {"role": "tool", "tool_call_id": "call_B", "content": "rb"},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["call_A", "call_B"]
    assert sorted(m["tool_call_id"] for m in out if m.get("role") == "tool") == ["call_A", "call_B"]


def test_sanitize_drops_empty_tool_calls_array():
    """sanitize_api_messages strips ``tool_calls: []`` from assistant messages.

    DeepSeek v4 rejects an empty tool_calls array with HTTP 400 "Invalid
    'messages[N].tool_calls': empty array" (#58755). The empty array is
    semantically "no tool calls", so the key is dropped while content is
    preserved.
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "answer", "tool_calls": []},
    ]
    out = sanitize_api_messages(list(messages))
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert "tool_calls" not in assistant
    assert assistant["content"] == "answer"


def test_sanitize_drops_empty_tool_calls_created_by_dedup():
    """Dedup must not leave ``tool_calls: []`` behind when it empties a
    duplicate call.

    Regression from production: a webui transcript carried the same
    tool_call id twice — once answered, once as an orphaned replay after an
    aborted turn. The dedup pass collapsed the later message's call to
    ``[]``, and because that pass runs AFTER the empty-array drop (see
    ``test_sanitize_drops_empty_tool_calls_array``), the empty list survived
    to the wire. Strict OpenAI-compatible providers (DeepSeek, opencode.ai)
    reject it with HTTP 400 "Invalid 'messages[N].tool_calls': empty array",
    and since every retry re-sends the same shape the session stalls
    permanently. The replayed message must lose the ``tool_calls`` KEY
    entirely, matching the empty==absent semantics.
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answered", "tool_calls": [
            {"id": "call_dup", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_dup", "content": "result"},
        {"role": "user", "content": "again"},
        # orphaned replay of the same call id (aborted turn persisted twice)
        {"role": "assistant", "content": "orphaned replay", "tool_calls": [
            {"id": "call_dup", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}}]},
    ]
    out = sanitize_api_messages(list(messages))
    assistants = [m for m in out if m.get("role") == "assistant"]
    # the first (answered) assistant keeps its call; the replay drops the key
    assert assistants[0]["tool_calls"][0]["id"] == "call_dup"
    assert "tool_calls" not in assistants[1]
    assert assistants[1]["content"] == "orphaned replay"
    # and no empty tool_calls array survives anywhere on the wire payload
    assert all(m.get("tool_calls") != [] for m in out)


def test_sanitize_reorders_scattered_tool_results_adjacent():
    """Tool results scattered far from their assistant are moved adjacent.

    A context rollup (compression window / session rebuild) can replay an
    assistant message carrying tool_calls while its tool results stay at
    their original positions much later in the transcript. Strict
    OpenAI-compatible providers (DeepSeek, opencode.ai) 400 with "An
    assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'. (insufficient tool messages
    following tool_calls message)". sanitize_api_messages must place each
    assistant's results immediately after it (in call order) and drop
    results whose call no longer exists.
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "first turn, two calls",
         "tool_calls": [
             {"id": "call_A", "type": "function",
              "function": {"name": "foo", "arguments": "{}"}},
             {"id": "call_B", "type": "function",
              "function": {"name": "foo", "arguments": "{}"}},
         ]},
        {"role": "user", "content": "interjection"},
        {"role": "assistant", "content": "second turn, one call",
         "tool_calls": [
             {"id": "call_C", "type": "function",
              "function": {"name": "foo", "arguments": "{}"}},
         ]},
        # results for the FIRST assistant, sitting far below after other turns
        {"role": "tool", "tool_call_id": "call_B", "content": "result B"},
        {"role": "tool", "tool_call_id": "call_A", "content": "result A"},
        {"role": "tool", "tool_call_id": "call_C", "content": "result C"},
    ]
    out = sanitize_api_messages(list(messages))
    idx_a = next(i for i, m in enumerate(out)
                 if m.get("role") == "assistant" and m.get("tool_calls")
                 and m["tool_calls"][0]["id"] == "call_A")
    # both results immediately follow their assistant, in call order
    assert out[idx_a + 1]["tool_call_id"] == "call_A"
    assert out[idx_a + 2]["tool_call_id"] == "call_B"
    idx_c = next(i for i, m in enumerate(out)
                 if m.get("role") == "assistant" and m.get("tool_calls")
                 and m["tool_calls"][0]["id"] == "call_C")
    assert out[idx_c + 1]["tool_call_id"] == "call_C"
    # every tool result exactly once, no orphans, no duplicates
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == ["call_A", "call_B", "call_C"]


def test_sanitize_normalizes_replayed_tool_calls_and_answers():
    """A compressed rollup replaying an answered call id gets normalized.

    Same tool_call id appears twice: once answered in the original turn,
    once replayed in a rollup block with the results sitting elsewhere.
    The survivor keeps its call and the result moves adjacent; the replay
    loses the duplicate call key entirely (empty == absent, #58755/#59110).
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    messages = [
        {"role": "user", "content": "rollup context"},
        # rollup replay of the answered turn — calls only, no results
        {"role": "assistant", "content": "replayed call",
         "tool_calls": [
             {"id": "call_X", "type": "function",
              "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "user", "content": "later"},
        # the original answered turn, further down the transcript
        {"role": "assistant", "content": "original call",
         "tool_calls": [
             {"id": "call_X", "type": "function",
              "function": {"name": "foo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_X", "content": "result X"},
    ]
    out = sanitize_api_messages(list(messages))
    assistants = [m for m in out if m.get("role") == "assistant"]
    # first assistant keeps the call; the replay drops it
    assert assistants[0]["tool_calls"][0]["id"] == "call_X"
    assert "tool_calls" not in assistants[1]
    # exactly one tool result, placed adjacent to the surviving call
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == ["call_X"]
    idx = next(i for i, m in enumerate(out)
               if m.get("role") == "assistant" and m.get("tool_calls"))
    assert out[idx + 1]["tool_call_id"] == "call_X"








# ── Self-recovery: heal empty-content non-final messages ──────────────────
# Repro of the production incident: a dead stream persisted an empty-content
# assistant stub mid-transcript, and every later request 400'd with
# "all messages must have non-empty content except for the optional final
# assistant message" (INVALID_REQUEST_BODY). sanitize_api_messages now heals
# such turns on the per-call copy so the session recovers itself in memory.












