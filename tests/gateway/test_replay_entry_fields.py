"""Tests for ``gateway.run._build_replay_entry``.

The gateway rebuilds ``agent_history`` from the persisted transcript on every
turn (unlike the CLI, which keeps the live in-memory message list).  When a
pure-text assistant turn (no ``tool_calls``) is replayed, the simple-text
branch in ``run_sync`` used to whitelist only three reasoning fields:
``reasoning``, ``reasoning_details``, ``codex_reasoning_items``.

That whitelist predated three fields the DB now persists:
``reasoning_content``, ``codex_message_items``, and ``finish_reason``.  The
unrecovered drop of ``codex_message_items`` in particular kills prefix-cache
hits for OpenAI Codex Responses API users — OpenAI's docs require the
``phase`` field be replayed on every assistant message.

These tests pin the expanded whitelist so it doesn't regress.
"""
from __future__ import annotations


from gateway.run import (
    _ASSISTANT_REPLAY_FIELDS,
    _build_gateway_agent_history,
    _build_replay_entry,
)


class TestBuildReplayEntry:
    def test_user_message_has_only_role_and_content(self):
        entry = _build_replay_entry(
            "user",
            "hello",
            {"role": "user", "content": "hello", "reasoning": "leak", "extra": "drop"},
        )
        assert entry == {"role": "user", "content": "hello"}

    def test_tool_message_has_only_role_and_content(self):
        # Tool messages aren't routed through this helper in production
        # (they take the rich-passthrough branch), but the helper itself
        # must not leak reasoning fields onto non-assistant roles even if
        # someone calls it incorrectly.
        entry = _build_replay_entry(
            "tool",
            "result",
            {"role": "tool", "content": "result", "reasoning": "leak"},
        )
        assert entry == {"role": "tool", "content": "result"}


    def test_assistant_drops_falsy_reasoning(self):
        """Empty/None reasoning fields stay dropped (matching PR #2974
        behaviour) — empty strings/lists for these fields carry no info."""
        msg = {
            "role": "assistant",
            "content": "answer",
            "reasoning": "",
            "reasoning_details": [],
            "codex_reasoning_items": [],
            "codex_message_items": [],
            "finish_reason": "",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert entry == {"role": "assistant", "content": "answer"}


    def test_assistant_preserves_all_six_fields_together(self):
        details = [{"type": "reasoning.summary", "summary": "s"}]
        codex_items = [{"type": "reasoning", "encrypted_content": "b"}]
        msg_items = [
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "x"}],
            }
        ]
        msg = {
            "role": "assistant",
            "content": "answer",
            "reasoning": "thinking",
            "reasoning_content": "structured",
            "reasoning_details": details,
            "codex_reasoning_items": codex_items,
            "codex_message_items": msg_items,
            "finish_reason": "stop",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert entry["reasoning"] == "thinking"
        assert entry["reasoning_content"] == "structured"
        assert entry["reasoning_details"] == details
        assert entry["codex_reasoning_items"] == codex_items
        assert entry["codex_message_items"] == msg_items
        assert entry["finish_reason"] == "stop"


    def test_replay_fields_constant_is_stable(self):
        """Pin the whitelist explicitly so accidental renames are caught."""
        assert _ASSISTANT_REPLAY_FIELDS == (
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "codex_reasoning_items",
            "codex_message_items",
            "finish_reason",
        )

    def test_unrelated_keys_are_ignored(self):
        """Random keys on the message must not leak into the replay entry."""
        msg = {
            "role": "assistant",
            "content": "answer",
            "timestamp": 12345.6,
            "internal_marker": "should not flow",
            "tool_call_id": "should not be set on simple-text branch",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert "timestamp" not in entry
        assert "internal_marker" not in entry
        assert "tool_call_id" not in entry


class TestReplayEntryApiContentSidecar:
    """The api_content sidecar (persist-what-you-send) must survive the
    gateway's transcript→agent_history rebuild, or the whole prompt-cache
    fix is inert on gateway platforms — but only when this pipeline did not
    rewrite the content (a rewrite means different bytes must replay)."""

    def test_user_forwards_api_content_when_content_unchanged(self):
        msg = {"role": "user", "content": "hi", "api_content": "hi\n\nCTX"}
        entry = _build_replay_entry("user", "hi", msg)
        assert entry["api_content"] == "hi\n\nCTX"
        assert entry["content"] == "hi"

    def test_assistant_forwards_api_content_when_content_unchanged(self):
        msg = {"role": "assistant", "content": "a", "api_content": "a <memory-context>"}
        entry = _build_replay_entry("assistant", "a", msg)
        assert entry["api_content"] == "a <memory-context>"


def _assistant_rows(history):
    """Rebuild replay history via the PRODUCTION builder and return the
    assistant rows.

    These tests deliberately exercise ``_build_gateway_agent_history``
    itself rather than a local copy of its filtering logic: the dispatch
    under test lives in the builder, so a replica would pass no matter what
    the builder does.
    """
    agent_history, _observed = _build_gateway_agent_history(history)
    return [m for m in agent_history if m.get("role") == "assistant"]


class TestReasoningOnlyAssistantTurnsSurviveReload:
    """An assistant turn whose only payload is reasoning state must survive
    the transcript -> agent_history rebuild.

    The builder's row dispatch keeps rich rows (tool_calls / tool results)
    and rows with truthy ``content``.  A row with empty ``content`` carrying
    only the fields named by ``_ASSISTANT_REPLAY_FIELDS`` matched neither
    arm and was dropped outright, losing multi-turn thinking fidelity on
    reload.
    """

    def test_reasoning_content_only_turn_is_preserved(self):
        """DeepSeek/Kimi thinking-mode shape: content empty, reasoning_content set."""
        assistant = _assistant_rows([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "reasoning_content": "step by step"},
        ])
        assert len(assistant) == 1
        assert assistant[0]["reasoning_content"] == "step by step"
        assert assistant[0]["content"] == ""

    def test_reasoning_only_turn_with_none_content_is_preserved(self):
        """A dead stream persists ``content=None``; the reasoning still replays."""
        assistant = _assistant_rows([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "reasoning": "thinking"},
        ])
        assert len(assistant) == 1
        assert assistant[0]["reasoning"] == "thinking"
        # `content` is normalized to "" -- the shape the API builders expect.
        assert assistant[0]["content"] == ""

    def test_reasoning_details_only_turn_is_preserved(self):
        """``content`` may be absent entirely, not merely falsy."""
        details = [{"type": "reasoning.summary", "summary": "s"}]
        assistant = _assistant_rows([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "reasoning_details": details},
        ])
        assert len(assistant) == 1
        assert assistant[0]["reasoning_details"] == details

    def test_reasoning_only_turn_keeps_position_between_user_turns(self):
        """The recovered row must replay in transcript order, not be appended."""
        agent_history, _observed = _build_gateway_agent_history([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "", "reasoning_content": "mulling"},
            {"role": "user", "content": "second"},
        ])
        assert [m["role"] for m in agent_history] == ["user", "assistant", "user"]
        assert agent_history[1]["reasoning_content"] == "mulling"

    def test_assistant_timestamp_is_still_dropped(self):
        """Assistant timestamps are deliberately not replayed; recovering the
        row must not start leaking them."""
        assistant = _assistant_rows([
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "thinking",
                "timestamp": 12345.6,
            },
        ])
        assert len(assistant) == 1
        assert "timestamp" not in assistant[0]

    def test_truthy_content_turn_is_unaffected(self):
        """Control: the pre-existing hot path is unchanged."""
        assistant = _assistant_rows([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer", "reasoning_content": "thinking"},
        ])
        assert assistant == [
            {"role": "assistant", "content": "answer", "reasoning_content": "thinking"}
        ]


class TestGatewayHistoryBuildForwardsSidecar:
    def test_end_to_end_history_build_keeps_sidecar(self):
        history = [
            {"role": "user", "content": "hi", "api_content": "hi\n\nCTX", "timestamp": 123.0},
            {"role": "assistant", "content": "hello"},
        ]
        agent_history, _obs = _build_gateway_agent_history(history)
        assert agent_history[0]["api_content"] == "hi\n\nCTX"

