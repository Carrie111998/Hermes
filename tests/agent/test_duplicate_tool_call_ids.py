"""Regression tests for ``sanitize_tool_call_pairing`` (#66429 family).

Some providers reuse tool_call ids across turns instead of emitting globally
unique ones. Kimi K3 (Moonshot, via OpenRouter) emits ids shaped
``<tool_name>_<n>``; a single observed session emitted ``terminal_46`` eight
separate times, and after a compression pass three of them were live in the
same context window at once.

Duplicate ids break result pairing: the results attach to the first assistant
carrying that id, the later assistant turns lose their ``tool_calls``, and one
is left with neither content nor tool_calls. Moonshot rejects that shape with a
**non-retryable** 400::

    Invalid request: the message at position 30 with role 'assistant'
    must not be empty

The failed turn is then persisted, so every subsequent request rebuilds the
same illegal transcript and fails in about a second. The agent never recovers
on its own - it presents as a hang rather than an error, which is why it was
originally reported as "it dies and won't come back".

This is a distinct producer of the empty-assistant shape tracked in #66429
(which is about the builder appending empty turns in a runaway loop) and of the
phantom ``content:""`` messages in #63200. The guard here covers both shapes on
the outbound copy.
"""

from agent.message_sanitization import sanitize_tool_call_pairing


def _call(cid, name="terminal"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _emitted_ids(messages):
    out = []
    for m in messages:
        for c in m.get("tool_calls") or []:
            out.append(c["id"])
    return out


def _result_ids(messages):
    return [m["tool_call_id"] for m in messages if m.get("tool_call_id")]


def test_duplicate_ids_are_made_unique_and_results_follow():
    """The same id reused across two turns must not collide, and each result
    must stay attached to the call it actually answered."""
    messages = [
        {"role": "user", "content": "run it twice"},
        {"role": "assistant", "content": "first", "tool_calls": [_call("terminal_46")]},
        {"role": "tool", "tool_call_id": "terminal_46", "content": "first result"},
        {"role": "assistant", "content": "second", "tool_calls": [_call("terminal_46")]},
        {"role": "tool", "tool_call_id": "terminal_46", "content": "second result"},
    ]

    assert sanitize_tool_call_pairing(messages) is True

    ids = _emitted_ids(messages)
    assert len(ids) == len(set(ids)), f"ids still collide: {ids}"

    # every result still resolves to a call that exists
    assert set(_result_ids(messages)) <= set(ids)

    # and the pairing is preserved: first result answers the first call
    assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]
    assert messages[4]["tool_call_id"] == messages[3]["tool_calls"][0]["id"]
    assert messages[2]["content"] == "first result"
    assert messages[4]["content"] == "second result"


def test_three_way_collision_in_one_window():
    """The observed failure had three copies of one id live at once."""
    messages = [{"role": "user", "content": "go"}]
    for _ in range(3):
        messages.append({"role": "assistant", "content": "step",
                         "tool_calls": [_call("terminal_46")]})
        messages.append({"role": "tool", "tool_call_id": "terminal_46", "content": "ok"})

    sanitize_tool_call_pairing(messages)

    ids = _emitted_ids(messages)
    assert len(ids) == 3 and len(set(ids)) == 3, ids
    for i in (1, 3, 5):
        assert messages[i + 1]["tool_call_id"] == messages[i]["tool_calls"][0]["id"]


def test_empty_assistant_without_tool_calls_gets_a_placeholder():
    """The exact shape strict providers reject: no content, no tool_calls."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
    ]
    assert sanitize_tool_call_pairing(messages) is True
    assert messages[1]["content"].strip(), "empty assistant reached the wire"


def test_empty_content_with_tool_calls_is_left_alone():
    """An assistant turn carrying tool_calls is legal with empty content and
    must not be rewritten - that would change model-visible semantics."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [_call("terminal_1")]},
        {"role": "tool", "tool_call_id": "terminal_1", "content": "ok"},
    ]
    sanitize_tool_call_pairing(messages)
    assert messages[1]["content"] == ""
    assert messages[1]["tool_calls"][0]["id"] == "terminal_1"


def test_none_content_assistant_is_handled():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ]
    assert sanitize_tool_call_pairing(messages) is True
    assert isinstance(messages[1]["content"], str)
    assert messages[1]["content"].strip()


def test_clean_transcript_is_untouched():
    """No duplicates and no empty turns means no changes and no churn."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "sure", "tool_calls": [_call("terminal_1")]},
        {"role": "tool", "tool_call_id": "terminal_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    before = [dict(m) for m in messages]
    assert sanitize_tool_call_pairing(messages) is False
    assert messages == before


def test_non_dict_entries_do_not_crash():
    messages = [None, {"role": "user", "content": "hi"}, "junk"]
    sanitize_tool_call_pairing(messages)  # must not raise
