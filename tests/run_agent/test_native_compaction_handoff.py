"""Protected handoff integration tests for native Responses compaction."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _preflight_codex_input_items,
)
from agent.native_compaction import (
    PROTECTED_HANDOFF_METADATA_KEY,
    attach_protected_handoff,
    protected_handoff_boundary_fence,
    protected_handoff_evidence,
    protected_handoff_prompt,
    parse_protected_handoff,
)
from agent.turn_context import _attempt_native_protected_handoff
from agent.conversation_loop import run_native_protected_handoff_request


_LIST_FIELDS = (
    "decision_rationale",
    "agreed_sequence",
    "mechanism_limits",
    "requested_accounting",
    "protected_live_state",
    "verified_completed",
    "started_unverified",
    "verification_required",
    "blockers",
    "approvals",
    "prohibitions",
    "state_distinctions",
    "authoritative_corrections",
    "unrelated_work_to_ignore",
)


def _handoff(fence: str) -> str:
    value: dict[str, object] = {
        "boundary_fence": fence,
        "latest_user_instruction": "Complete the current task.",
        "immediate_resume_cursor": "Run focused verification.",
        "current_task": "Native compaction integration",
        "why_urgent": "The session is over the compression threshold.",
        "next_action": "Verify the committed checkpoint.",
    }
    value.update({field: [f"{field} evidence"] for field in _LIST_FIELDS})
    return json.dumps(value)


def _agent(**over):
    agent = SimpleNamespace(
        api_mode="codex_responses",
        _codex_reasoning_replay_enabled=True,
        model="gpt-5.6",
        base_url="https://api.openai.com/v1",
        provider="openai-api",
        codex_responses_native_compaction=True,
        compression_enabled=True,
        compression_checkpoint_required=False,
        codex_responses_compact_threshold=20_000,
        context_compressor=SimpleNamespace(threshold_tokens=30_000),
        capabilities={},
    )
    for key, value in over.items():
        setattr(agent, key, value)
    return agent


def _attached_checkpoint(fence: str, blob: str = "checkpoint"):
    return attach_protected_handoff(
        {"type": "compaction", "encrypted_content": blob, "_issuer_kind": "other"},
        canonical_handoff=_handoff(fence),
        boundary_fence=fence,
    )


def test_valid_handoff_commits_before_current_user_and_excludes_it(monkeypatch):
    import agent.conversation_loop as loop

    agent = _agent()
    prior = {"role": "user", "content": "Earlier decision."}
    current = {"role": "user", "content": "CURRENT USER MUST NOT BE SNAPSHOTTED"}
    messages = [prior, current]
    seen = {}

    def fake_request(_agent, snapshot, prompt):
        seen["snapshot"] = snapshot
        seen["prompt"] = prompt
        fence = protected_handoff_boundary_fence(snapshot)
        return _handoff(fence), [{"type": "compaction", "encrypted_content": "blob"}]

    monkeypatch.setattr(loop, "run_native_protected_handoff_request", fake_request)
    result = _attempt_native_protected_handoff(agent, messages, 1)

    assert result is not None
    committed, user_index = result
    assert seen["snapshot"] == [prior]
    assert current["content"] not in seen["prompt"]
    assert committed[user_index] is current
    carrier = committed[user_index - 1]
    assert carrier == {
        "role": "assistant",
        "content": "",
        "display_kind": "hidden",
        "codex_reasoning_items": [carrier["codex_reasoning_items"][0]],
    }
    assert PROTECTED_HANDOFF_METADATA_KEY in carrier["codex_reasoning_items"][0]


@pytest.mark.parametrize(
    "response",
    [
        None,
        ("not json", [{"type": "compaction", "encrypted_content": "blob"}]),
        ("{}", [{"type": "compaction", "encrypted_content": "blob"}]),
        (None, []),
    ],
)
def test_failed_handoff_is_non_committing_and_preserves_history(monkeypatch, response):
    import agent.conversation_loop as loop

    agent = _agent()
    messages = [{"role": "assistant", "content": "prior"}, {"role": "user", "content": "new"}]
    original = list(messages)
    monkeypatch.setattr(loop, "run_native_protected_handoff_request", lambda *_args: response)

    assert _attempt_native_protected_handoff(agent, messages, 1) is None
    assert messages == original
    assert messages[0] is original[0]
    assert messages[1] is original[1]


def test_gate_off_and_replay_disabled_create_no_synthetic_handoff(monkeypatch):
    import agent.conversation_loop as loop

    messages = [{"role": "assistant", "content": "prior"}, {"role": "user", "content": "new"}]
    called = False

    def fail_if_called(*_args):
        nonlocal called
        called = True
        raise AssertionError("isolated request must not run")

    monkeypatch.setattr(loop, "run_native_protected_handoff_request", fail_if_called)
    assert _attempt_native_protected_handoff(_agent(codex_responses_native_compaction=False), messages, 1) is None
    assert _attempt_native_protected_handoff(_agent(_codex_reasoning_replay_enabled=False), messages, 1) is None
    assert called is False
    assert len(messages) == 2


def test_stale_commit_fence_adds_no_handoff_carrier(monkeypatch):
    import agent.conversation_loop as loop

    agent = _agent()
    messages = [{"role": "assistant", "content": "prior"}, {"role": "user", "content": "new"}]

    def fake_request(_agent, snapshot, _prompt):
        # Simulate a concurrent redirect/repair after the snapshot but before
        # commit. The helper must not add a second synthetic record.
        messages.append({"role": "assistant", "content": "redirected"})
        fence = protected_handoff_boundary_fence(snapshot)
        return _handoff(fence), [{"type": "compaction", "encrypted_content": "blob"}]

    monkeypatch.setattr(loop, "run_native_protected_handoff_request", fake_request)
    assert _attempt_native_protected_handoff(agent, messages, 1) is None
    assert messages == [
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "redirected"},
    ]


def test_stale_commit_fence_detects_earlier_prefix_mutation(monkeypatch):
    import agent.conversation_loop as loop

    agent = _agent()
    messages = [
        {"role": "user", "content": "accepted scope"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "new"},
    ]

    def fake_request(_agent, snapshot, _prompt):
        fence = protected_handoff_boundary_fence(snapshot)
        messages[0]["content"] = "redirected scope"
        return _handoff(fence), [
            {"type": "compaction", "encrypted_content": "blob"}
        ]

    monkeypatch.setattr(loop, "run_native_protected_handoff_request", fake_request)
    assert _attempt_native_protected_handoff(agent, messages, 2) is None
    assert messages[0]["content"] == "redirected scope"


def test_handoff_parser_rejects_surrounding_prose_and_duplicate_keys():
    fence = protected_handoff_boundary_fence([
        {"role": "user", "content": "scope"}
    ])
    clean = _handoff(fence)

    with pytest.raises(ValueError, match="exactly one JSON object"):
        parse_protected_handoff(f"preface\n{clean}", fence)
    duplicate = clean[:-1] + f', "boundary_fence": {json.dumps(fence)}}}'
    with pytest.raises(ValueError, match="duplicate key"):
        parse_protected_handoff(duplicate, fence)


def test_isolated_request_uses_standard_transport_and_timeout_is_a_noop():
    seen = {}

    class Transport:
        def preflight_kwargs(self, kwargs, **_kwargs):
            seen["preflight"] = kwargs
            return kwargs

        def normalize_response(self, _response):
            return SimpleNamespace(
                content="{}",
                provider_data={"codex_reasoning_items": [{"type": "compaction", "encrypted_content": "blob"}]},
            )

    transport = Transport()
    agent = SimpleNamespace(
        api_mode="codex_responses",
        _codex_reasoning_replay_enabled=True,
        _cached_system_prompt="system",
        _build_api_kwargs=lambda messages, tools_for_api: seen.update(
            messages=messages, tools=tools_for_api
        ) or {"model": "gpt-5.6", "instructions": "system", "input": messages, "store": False,
              "context_management": [{"type": "compaction", "compact_threshold": 1}]},
        _get_transport=lambda: transport,
        _is_copilot_url=lambda: False,
        _is_codex_backend=lambda: False,
        _interruptible_api_call=lambda kwargs: seen.update(called=kwargs) or object(),
    )
    snapshot = [{"role": "user", "content": "prior"}]

    result = run_native_protected_handoff_request(agent, snapshot, "handoff prompt")

    assert result == ("{}", [{"type": "compaction", "encrypted_content": "blob"}])
    assert seen["tools"] == []
    assert seen["messages"][-1] == {"role": "user", "content": "handoff prompt"}
    assert seen["called"]["model"] == "gpt-5.6-luna"
    assert seen["called"]["reasoning"] == {"effort": "none", "summary": "auto"}
    assert snapshot == [{"role": "user", "content": "prior"}]

    agent._interruptible_api_call = lambda _kwargs: (_ for _ in ()).throw(TimeoutError("timeout"))
    assert run_native_protected_handoff_request(agent, snapshot, "handoff prompt") is None
    assert snapshot == [{"role": "user", "content": "prior"}]


def test_evidence_keeps_older_decision_and_newest_operational_tail_when_bounded():
    messages = [
        {"role": "assistant", "content": "accepted order: rules, recovery, then pilot"}
    ]
    messages.extend(
        {
            "role": "tool",
            "tool_name": "work",
            "content": f"operational-{index}-" + ("x" * 2_000),
        }
        for index in range(100)
    )

    evidence = protected_handoff_evidence(messages)

    assert "accepted order: rules, recovery, then pilot" in evidence
    assert "operational-99-" in evidence
    assert len(evidence) <= 100_000


def test_handoff_prompt_preserves_full_lifecycle_and_authority_contract():
    prompt = protected_handoff_prompt("7:fence", "direct evidence")

    assert "started, completed, failed, retried, stale, and uncommitted" in prompt
    assert "merged, deployed, running, and live" in prompt
    assert '\"decision_rationale\": [' in prompt
    assert '\"next_action\": \"...\"' in prompt
    assert "Later protected user messages override this handoff" in prompt


def test_wire_orders_checkpoint_handoff_then_true_tail_without_stale_retention():
    pre_user = {
        "role": "user",
        "content": "STALE CLAIM: activation already completed",
    }
    fence = protected_handoff_boundary_fence([pre_user])
    checkpoint = _attached_checkpoint(fence)
    history = [
        pre_user,
        {"role": "assistant", "content": "", "codex_reasoning_items": [checkpoint]},
        {"role": "user", "content": "tail user"},
    ]
    persisted_before = json.loads(json.dumps(history))

    items = _chat_messages_to_responses_input(
        history,
        current_issuer_kind="other",
        native_compaction_eligible=True,
    )

    assert items[0] == {"type": "compaction", "encrypted_content": "checkpoint"}
    assert items[1]["role"] == "user"
    assert items[1]["content"].startswith("PROTECTED PRE-COMPRESSION HANDOFF")
    assert items[2] == {"role": "user", "content": "tail user"}
    assert not any(
        "STALE CLAIM" in str(item.get("content", ""))
        for item in items
    )
    assert not any(item.get("role") == "assistant" and item.get("content") == "" for item in items)
    assert all(PROTECTED_HANDOFF_METADATA_KEY not in item for item in items)
    assert _preflight_codex_input_items(items)[0] == {
        "type": "compaction", "encrypted_content": "checkpoint"
    }
    # Request conversion is read-only over durable sidecar state.
    assert history == persisted_before
    assert PROTECTED_HANDOFF_METADATA_KEY in history[1]["codex_reasoning_items"][0]


def test_replay_drops_checkpoint_and_handoff_when_durable_prefix_changed():
    pre_user = {"role": "user", "content": "original scope"}
    fence = protected_handoff_boundary_fence([pre_user])
    history = [
        pre_user,
        {
            "role": "assistant",
            "content": "",
            "codex_reasoning_items": [_attached_checkpoint(fence)],
        },
        {"role": "user", "content": "tail user"},
    ]
    pre_user["content"] = "mutated scope"

    items = _chat_messages_to_responses_input(
        history,
        current_issuer_kind="other",
        native_compaction_eligible=True,
    )

    assert all(item.get("type") != "compaction" for item in items)
    assert all(
        not str(item.get("content", "")).startswith("PROTECTED")
        for item in items
    )
    assert {"role": "user", "content": "mutated scope"} in items


def test_newest_checkpoint_owns_the_only_replayed_handoff():
    old_user = {"role": "user", "content": "old"}
    old_fence = protected_handoff_boundary_fence([old_user])
    old_carrier = {
        "role": "assistant",
        "content": "old cp",
        "codex_reasoning_items": [_attached_checkpoint(old_fence, "old")],
    }
    mid_user = {"role": "user", "content": "mid"}
    new_fence = protected_handoff_boundary_fence(
        [old_user, old_carrier, mid_user]
    )
    history = [
        old_user,
        old_carrier,
        mid_user,
        {"role": "assistant", "content": "new cp", "codex_reasoning_items": [_attached_checkpoint(new_fence, "new")]},
        {"role": "user", "content": "tail"},
    ]

    items = _chat_messages_to_responses_input(history, native_compaction_eligible=True)

    assert [item.get("encrypted_content") for item in items if item.get("type") == "compaction"] == ["new"]
    handoffs = [item["content"] for item in items if item.get("role") == "user" and item["content"].startswith("PROTECTED")]
    assert len(handoffs) == 1
    assert "Native compaction integration" in handoffs[0]


def test_ineligible_wire_keeps_full_history_and_never_injects_handoff():
    pre_user = {"role": "user", "content": "retained user"}
    fence = protected_handoff_boundary_fence([pre_user])
    history = [
        pre_user,
        {"role": "assistant", "content": "checkpoint carrier", "codex_reasoning_items": [_attached_checkpoint(fence)]},
        {"role": "user", "content": "tail user"},
    ]

    items = _chat_messages_to_responses_input(history, native_compaction_eligible=False)

    assert all(item.get("type") != "compaction" for item in items)
    assert all(not str(item.get("content", "")).startswith("PROTECTED") for item in items)
    assert {"role": "assistant", "content": "checkpoint carrier"} in items


def test_resumed_sidecar_replays_only_newest_handoff_without_mutating_storage(tmp_path):
    """The existing reasoning sidecar survives a fresh SessionDB reconstruction."""
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("handoff", source="cli")
    db.append_message("handoff", "user", "old user")
    old_fence = protected_handoff_boundary_fence([{"role": "user", "content": "old user"}])
    db.append_message(
        "handoff", "assistant", "",
        codex_reasoning_items=[_attached_checkpoint(old_fence, "old")],
        display_kind="hidden",
    )
    db.append_message("handoff", "user", "mid user")
    durable_prefix, _display = db.get_resume_conversations("handoff")
    new_fence = protected_handoff_boundary_fence(durable_prefix)
    db.append_message(
        "handoff", "assistant", "",
        codex_reasoning_items=[_attached_checkpoint(new_fence, "new")],
        display_kind="hidden",
    )
    db.append_message("handoff", "user", "current user")
    db.close()

    reopened = SessionDB(path)
    history, _display = reopened.get_resume_conversations("handoff")
    before = json.loads(json.dumps(history))
    items = _chat_messages_to_responses_input(history, native_compaction_eligible=True)

    assert [item.get("encrypted_content") for item in items if item.get("type") == "compaction"] == ["new"]
    handoffs = [
        item["content"] for item in items
        if item.get("role") == "user" and str(item.get("content")).startswith("PROTECTED")
    ]
    assert len(handoffs) == 1
    assert history == before
    assert PROTECTED_HANDOFF_METADATA_KEY in history[-2]["codex_reasoning_items"][0]


def test_accepted_handoff_survives_real_agent_flush_and_resume(
    tmp_path, monkeypatch
):
    """Turn-start acceptance persists through Hermes's production DB flush."""
    import agent.conversation_loop as loop
    from hermes_state import SessionDB
    from run_agent import AIAgent

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    db = SessionDB(tmp_path / "state.db")
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        session_db=db,
        session_id="handoff-flush",
        skip_context_files=True,
        skip_memory=True,
    )
    agent._ensure_db_session()
    prior = {"role": "user", "content": "accepted earlier decision"}
    agent._flush_messages_to_session_db([prior], [])
    current = {"role": "user", "content": "current instruction"}
    messages = [prior, current]

    def fake_request(_agent, snapshot, _prompt):
        fence = protected_handoff_boundary_fence(snapshot)
        return _handoff(fence), [
            {"type": "compaction", "encrypted_content": "persisted-checkpoint"}
        ]

    monkeypatch.setattr(loop, "run_native_protected_handoff_request", fake_request)
    for name, value in {
        "api_mode": "codex_responses",
        "_codex_reasoning_replay_enabled": True,
        "model": "gpt-5.6-sol",
        "base_url": "https://api.openai.com/v1",
        "provider": "openai-codex",
        "codex_responses_native_compaction": True,
        "compression_enabled": True,
        "compression_checkpoint_required": False,
        "capabilities": {},
        "runtime_capabilities": {"native_compaction": True},
    }.items():
        setattr(agent, name, value)

    accepted = _attempt_native_protected_handoff(agent, messages, 1)
    assert accepted is not None
    committed, user_index = accepted
    setattr(agent, "_persist_user_message_idx", user_index)
    agent._flush_messages_to_session_db(committed, [prior])

    resumed, _display = db.get_resume_conversations("handoff-flush")
    assert resumed[-1]["content"] == "current instruction"
    carrier = resumed[-2]
    assert carrier["display_kind"] == "hidden"
    checkpoint = carrier["codex_reasoning_items"][0]
    assert checkpoint["encrypted_content"] == "persisted-checkpoint"
    assert PROTECTED_HANDOFF_METADATA_KEY in checkpoint

    wire = _chat_messages_to_responses_input(
        resumed,
        current_issuer_kind="openai_codex",
        native_compaction_eligible=True,
    )
    assert wire[0] == {
        "type": "compaction",
        "encrypted_content": "persisted-checkpoint",
    }
    assert wire[1]["content"].startswith("PROTECTED PRE-COMPRESSION HANDOFF")
    db.close()
