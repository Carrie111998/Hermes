from __future__ import annotations

import copy

import pytest

from agent.speculative_compression import (
    build_candidate,
    capture_snapshot,
    fingerprint_messages,
)


class SnapshotCompressor:
    def _protect_head_size(self, messages):
        return 1

    def _find_tail_cut_by_tokens(self, messages, head_end):
        return 3

    def _align_boundary_forward(self, messages, cut):
        while cut < len(messages):
            if messages[cut].get("role") == "tool":
                cut += 1
                continue
            if (
                cut > 0
                and messages[cut - 1].get("role") == "assistant"
                and messages[cut - 1].get("tool_calls")
            ):
                cut += 1
                continue
            break
        return cut


def _messages():
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old context", "meta": {"n": 1}},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "user", "content": "current request"},
    ]


def test_snapshot_is_deep_immutable_and_moves_boundary_past_tool_pair():
    messages = _messages()
    snapshot = capture_snapshot(messages, SnapshotCompressor(), 100, "session-1")

    assert snapshot.cut_index == 4
    assert snapshot.messages[1]["meta"]["n"] == 1
    assert (
        fingerprint_messages(messages[: snapshot.cut_index])
        == snapshot.source_fingerprint
    )

    messages[1]["meta"]["n"] = 99
    messages[4]["content"] = "changed live suffix"
    assert snapshot.messages[1]["meta"]["n"] == 1
    with pytest.raises(TypeError):
        snapshot.messages[1]["meta"]["n"] = 2


def test_snapshot_rejects_a_boundary_that_splits_a_tool_batch():
    class UnsafeCompressor(SnapshotCompressor):
        def _align_boundary_forward(self, messages, cut):
            return cut

    with pytest.raises(ValueError, match="tool-call/result batch"):
        capture_snapshot(_messages(), UnsafeCompressor(), 100, "session-1")


def test_candidate_preserves_current_tail_verbatim():
    snapshot = capture_snapshot(_messages(), SnapshotCompressor(), 100, "session-1")

    class Worker:
        _last_summary_fallback_used = False

        def compress(self, messages, **_kwargs):
            return [
                {"role": "user", "content": "prepared summary"},
                *messages[snapshot.cut_index :],
            ]

    candidate = build_candidate(snapshot, lambda: Worker())
    current = copy.deepcopy(_messages())
    current.append({"role": "tool", "tool_call_id": "late", "content": "late result"})
    assembled = candidate.assemble(current, "session-1")

    assert assembled[0]["content"] == "prepared summary"
    assert assembled[1:] == current[snapshot.cut_index :]
    assert all("_speculative_tail_marker" not in message for message in assembled)


def test_candidate_rejects_changed_boundary_and_prefix():
    snapshot = capture_snapshot(_messages(), SnapshotCompressor(), 100, "session-1")

    class Worker:
        def compress(self, messages, **_kwargs):
            return [
                {"role": "user", "content": "summary"},
                *messages[snapshot.cut_index :],
            ]

    candidate = build_candidate(snapshot, lambda: Worker())
    changed_prefix = _messages()
    changed_prefix[1]["content"] = "not the captured prefix"
    changed_boundary = _messages()
    changed_boundary[snapshot.cut_index]["content"] = "not the captured boundary"
    truncated = _messages()[: snapshot.cut_index]

    assert not candidate.matches(changed_prefix, "session-1")
    assert not candidate.matches(changed_boundary, "session-1")
    assert not candidate.matches(truncated, "session-1")
    with pytest.raises(ValueError, match="no longer matches"):
        candidate.assemble(changed_boundary, "session-1")


def test_fingerprint_strips_only_top_level_bookkeeping():
    """_db_persisted / tail-marker keys are bookkeeping ONLY at the message
    root; a nested payload key with the same name must still be hashed."""
    with_marker = [{"role": "user", "content": "x", "_db_persisted": 1}]
    without_marker = [{"role": "user", "content": "x"}]
    assert fingerprint_messages(with_marker) == fingerprint_messages(without_marker)

    nested_a = [{"role": "user", "content": "x", "extra": {"_db_persisted": 1}}]
    nested_b = [{"role": "user", "content": "x", "extra": {"_db_persisted": 2}}]
    assert fingerprint_messages(nested_a) != fingerprint_messages(nested_b)

    tail_a = [{"role": "user", "content": "x", "nested": {"_speculative_tail_marker": "m1"}}]
    tail_b = [{"role": "user", "content": "x", "nested": {"_speculative_tail_marker": "m2"}}]
    assert fingerprint_messages(tail_a) != fingerprint_messages(tail_b)
