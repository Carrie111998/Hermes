"""Tests for the execution-fidelity trace store, postcondition readback,
claim-evidence enforcement, and the memory-write security gate.
"""

import pytest

from agent import trajectory
from agent.trajectory import ToolTrace, get_trace, store_trace
from tools.memory_gate import check_memory_write, tag_provenance
from tools.mcp_tool import classify_action, record_postcondition
from agent.conversation_loop import reconcile_claims


@pytest.fixture(autouse=True)
def _isolate_traces():
    """T7: traces must not leak across tests via the process-global store."""
    trajectory.reset_turn_traces()
    trajectory._TRACES.clear()
    yield
    trajectory.reset_turn_traces()
    trajectory._TRACES.clear()


# ── Task B1: memory gate ────────────────────────────────────────────────────

def test_memory_gate_blocks_untrusted():
    assert check_memory_write("x", "web_untrusted") is False
    assert check_memory_write("x", "tool_untrusted") is False


def test_memory_gate_allows_trusted():
    assert check_memory_write("x", "user_instruction") is True
    assert check_memory_write("x", "system_policy") is True
    assert check_memory_write("x", "repo_trusted") is True


def test_memory_gate_fail_closed_unverified_empty_none():
    """T1: unknown / empty / missing provenance must not write."""
    assert check_memory_write("x", "memory_unverified") is False
    assert check_memory_write("x", "") is False
    assert check_memory_write("x", None) is False


def test_tag_provenance_passthrough_and_unknown():
    """T2: known labels pass through; unknowns coerce to memory_unverified."""
    assert tag_provenance("user_instruction") == "user_instruction"
    assert tag_provenance("web_untrusted") == "web_untrusted"
    assert tag_provenance("not_a_real_label") == "memory_unverified"
    assert tag_provenance("") == "memory_unverified"
    assert tag_provenance(None) == "memory_unverified"


# ── Task A1: trace store ────────────────────────────────────────────────────

def test_trace_store_roundtrip():
    oid = trajectory.new_observation_id()
    trace = ToolTrace(
        observation_id=oid,
        tool_name="write_file",
        normalized_args={"path": "/tmp/a"},
        raw_response_hash=trajectory.hash_response("ok"),
        transport_status=200,
        postcondition_status="pending",
        action_class="REVERSIBLE_WRITE",
    )
    store_trace(trace)
    assert get_trace(oid) is trace
    assert get_trace("missing") is None


def test_reset_turn_traces_clears_current_turn():
    """T6: store_trace then reset_turn_traces leaves the turn list empty."""
    store_trace(ToolTrace(
        observation_id=trajectory.new_observation_id(),
        tool_name="write_file",
        normalized_args={},
        raw_response_hash="h",
        transport_status=0,
        postcondition_status="unknown",
        action_class="REVERSIBLE_WRITE",
    ))
    assert trajectory.current_turn_traces()
    trajectory.reset_turn_traces()
    assert trajectory.current_turn_traces() == []


def test_trace_store_evicts_oldest_past_cap():
    cap = trajectory._MAX_TRACES
    first_id = None
    last_id = None
    for i in range(cap + 1):
        oid = f"obs-{i}"
        if i == 0:
            first_id = oid
        last_id = oid
        store_trace(ToolTrace(
            observation_id=oid,
            tool_name="t",
            normalized_args={},
            raw_response_hash="h",
            transport_status=0,
            postcondition_status="unknown",
            action_class="REVERSIBLE_WRITE",
        ))
    assert get_trace(first_id) is None
    assert get_trace(last_id) is not None
    assert len(trajectory._TRACES) == cap


# ── Task A2: postcondition readback ─────────────────────────────────────────

def test_write_without_readback_is_unknown_never_succeeded():
    """A WRITE tool with no read-variant / verification endpoint must land as
    'unknown' — never default to 'succeeded'."""
    trace = record_postcondition(
        tool_name="create_issue",
        normalized_args={"title": "x"},
        response='{"result": "ok"}',
        transport_status=200,
    )
    assert trace.action_class in ("REVERSIBLE_WRITE", "IRREVERSIBLE_WRITE")
    assert trace.postcondition_status == "unknown"
    assert trace.postcondition_status != "succeeded"
    # persisted in the store
    assert get_trace(trace.observation_id) is trace


def test_read_op_is_skipped_not_readback():
    """A READ-classified tool call skips readback entirely."""
    trace = record_postcondition(
        tool_name="get_file",
        normalized_args={"path": "/tmp/a"},
        response='{"result": "contents"}',
        transport_status=200,
    )
    assert trace.action_class == "READ"
    assert trace.postcondition_status == "skipped"


def test_classify_action_heuristic():
    assert classify_action("read_file") == "READ"
    assert classify_action("list_dirs") == "READ"
    assert classify_action("delete_file") == "IRREVERSIBLE_WRITE"
    assert classify_action("write_file") == "REVERSIBLE_WRITE"
    assert classify_action("draft_email") == "DRAFT"


def test_classify_action_adversarial_destructive_not_draft():
    """T3: destructive / mutating MCP names must not skip readback as DRAFT."""
    assert classify_action("trash_message") not in ("READ", "DRAFT")
    assert classify_action("mark_message_spam") != "DRAFT"
    assert classify_action("label_thread") != "DRAFT"


def test_classify_unknown_head_is_reversible_write():
    """F1: unknown head-verb defaults to a conservative write, not DRAFT."""
    assert classify_action("frobnicate_widget") == "REVERSIBLE_WRITE"


def test_classify_send_is_reversible_not_irreversible():
    """send stays WRITE-only; IRREVERSIBLE wins for overlapping verbs like remove."""
    assert classify_action("send_email") == "REVERSIBLE_WRITE"
    assert classify_action("remove_item") == "IRREVERSIBLE_WRITE"


def test_record_postcondition_confirmed_write_branches(monkeypatch):
    """T5: a real reader can land succeeded or failed; never vacuously succeeded."""

    def ok_reader(_args):
        return {"ok": True}

    def empty_reader(_args):
        return None

    monkeypatch.setattr("tools.mcp_tool._read_variant", lambda _name: ok_reader)
    succeeded = record_postcondition(
        tool_name="create_issue",
        normalized_args={"title": "x"},
        response='{"result": "ok"}',
        transport_status=0,
    )
    assert succeeded.postcondition_status == "succeeded"
    assert succeeded.action_class == "REVERSIBLE_WRITE"

    monkeypatch.setattr("tools.mcp_tool._read_variant", lambda _name: empty_reader)
    failed = record_postcondition(
        tool_name="create_issue",
        normalized_args={"title": "x"},
        response='{"result": "ok"}',
        transport_status=0,
    )
    assert failed.postcondition_status == "failed"


def test_record_postcondition_forced_failed_is_reachable():
    """F6: error-path traces can stamp postcondition_status='failed'."""
    trace = record_postcondition(
        tool_name="create_issue",
        normalized_args={},
        response='{"error": "unreachable"}',
        transport_status=0,
        postcondition_status="failed",
    )
    assert trace.postcondition_status == "failed"
    assert get_trace(trace.observation_id) is trace


# ── Task A3: claim-evidence enforcement ─────────────────────────────────────

def test_claim_without_trace_flagged_unsupported():
    """A response claiming a completed action with no matching ToolTrace is
    flagged 'unsupported'."""
    text = "I've successfully created the file for you."
    adjusted, findings = reconcile_claims(text, traces=[])
    assert any(f["status"] == "unsupported" for f in findings)
    # log-only default: original text is preserved, no user-facing advisory
    assert adjusted == text
    assert "could not be confirmed" not in adjusted


def test_claim_matched_to_unknown_trace_is_flagged():
    """A done-claim matching a trace whose postcondition is 'unknown' is
    flagged (not treated as confirmed)."""
    tr = ToolTrace(
        observation_id=trajectory.new_observation_id(),
        tool_name="create_issue",
        normalized_args={},
        raw_response_hash="h",
        transport_status=200,
        postcondition_status="unknown",
        action_class="IRREVERSIBLE_WRITE",
    )
    text = "Done — I've created the issue."
    adjusted, findings = reconcile_claims(text, traces=[tr])
    assert any(f["status"] in ("unconfirmed", "unsupported") for f in findings)
    assert adjusted == text


def test_false_positive_claim_text_unchanged():
    """T4: bare past-tense verbs in ordinary prose are not done-claims."""
    text = "The upstream library removed support in v3."
    adjusted, findings = reconcile_claims(text, traces=[])
    assert findings == []
    assert adjusted == text


def test_advisory_appended_only_when_flag_on(monkeypatch):
    from agent import conversation_loop as cl

    monkeypatch.setattr(cl, "_APPEND_ADVISORY", True)
    text = "I've created the file."
    adjusted, findings = reconcile_claims(text, traces=[])
    assert findings
    assert "could not be confirmed" in adjusted
    assert text in adjusted


def test_advisory_skipped_on_unclosed_fence(monkeypatch):
    """F8: never splice the advisory into an open code fence."""
    from agent import conversation_loop as cl

    monkeypatch.setattr(cl, "_APPEND_ADVISORY", True)
    text = "I've created the file.\n```\ncode"
    adjusted, findings = reconcile_claims(text, traces=[])
    assert findings
    assert adjusted == text
