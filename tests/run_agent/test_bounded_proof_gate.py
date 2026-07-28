"""End-to-end coverage for the one-shot engineering proof gate.

The gate replaces the old synthetic assistant/user verification loop with a
request-only instruction on the existing user-message copy.  These tests pin
the important behavior contracts: one continuation, no premature delivery or
persistence, strict role alternation, and a truthful failed/partial receipt
when proof still has not landed.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_loop import (
    _bounded_proof_gate_instruction,
    _capture_isolated_worker_baseline,
)
from agent.turn_context import (
    _apply_runtime_effect,
    _persisted_conversation_root,
)
from gateway.isolated_worker import canonical_lease_id
from run_agent import AIAgent


def _text_response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="test/model",
        usage=None,
    )


def _tool_response(name: str, *, call_id: str):
    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments="{}"),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call]),
                finish_reason="tool_calls",
            )
        ],
        model="test/model",
        usage=None,
    )


def _runtime_effect(authority: str, baseline):
    return {
        "schema": "hermes.runtime-effect.v1",
        "kind": "isolated_workspace_may_have_changed.v1",
        "workspace_lease_authority": authority,
        "baseline_edit_generation": baseline,
    }


def _worker_receipt(
    authority: str,
    *,
    edit_generation: int,
    verified_generation: int,
    status: str,
    pending_paths=None,
):
    return {
        "schema": "muncho.isolated-worker.proof-receipt.v1",
        "lease_id": canonical_lease_id(authority),
        "edit_generation": edit_generation,
        "verified_generation": verified_generation,
        "status": status,
        "pending_paths": list(pending_paths or []),
        "applicability": "applicable",
    }


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="bounded-proof-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=8,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._disable_streaming = True
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    instance.valid_tool_names = ["write_file", "terminal", "read_file"]
    return instance


def _run_scripted_turn(
    agent,
    responses,
    *,
    changed_path,
    proof_state,
):
    requests = []
    streamed = []
    response_iter = iter(responses)

    def model_call(api_kwargs):
        requests.append(copy.deepcopy(api_kwargs["messages"]))
        response = next(response_iter)
        content = response.choices[0].message.content
        if content and agent.stream_delta_callback is not None:
            agent.stream_delta_callback(content)
        return response

    def execute_tools(assistant_message, messages, *_args):
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            if name == "write_file":
                agent._turn_file_mutation_paths.add(str(changed_path))
                proof_state["passed"] = False
            elif name == "terminal":
                proof_state["passed"] = True
            messages.append(
                {
                    "role": "tool",
                    "name": name,
                    "tool_call_id": tool_call.id,
                    "content": json.dumps({"success": True, "tool": name}),
                }
            )

    agent._interruptible_api_call = model_call
    agent._execute_tool_calls = execute_tools
    agent.stream_delta_callback = streamed.append

    def proof_instruction(**_kwargs):
        return None if proof_state["passed"] else "Run the focused test now."

    with (
        patch("agent.verification_stop.verify_on_stop_enabled", return_value=True),
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=proof_instruction,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit the implementation and verify it")

    return result, requests, streamed


def _assert_no_synthetic_user_or_candidate(result, *candidates):
    messages = result["messages"]
    assert sum(message.get("role") == "user" for message in messages) == 1
    assert messages[0]["content"] == "edit the implementation and verify it"
    assert all(
        not any(
            key in message
            for key in (
                "_verification_stop_synthetic",
                "_pre_verify_synthetic",
            )
        )
        for message in messages
    )
    persisted_text = "\n".join(
        str(message.get("content") or "") for message in messages
    )
    for candidate in candidates:
        assert candidate not in persisted_text
    roles = [message["role"] for message in messages]
    assert all(left != right for left, right in zip(roles, roles[1:]))


def test_unverified_second_stop_is_failed_receipt_not_success(
    agent, tmp_path
):
    changed = tmp_path / "project" / "changed.py"
    proof_state = {"passed": False}
    result, requests, streamed = _run_scripted_turn(
        agent,
        [
            _tool_response("write_file", call_id="edit-1"),
            _text_response("Premature: everything is done."),
            _text_response("Still done, no tests needed."),
        ],
        changed_path=changed,
        proof_state=proof_state,
    )

    assert result["api_calls"] == 3
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["partial"] is True
    assert result["error"] == "verification_evidence_missing"
    assert result["turn_exit_reason"] == "bounded_proof_gate_unverified"
    assert "PARTIAL / NOT VERIFIED" in result["final_response"]
    assert str(changed) in result["final_response"]
    assert result["final_response"].count(str(changed)) == 1
    assert [chunk for chunk in streamed if chunk is not None] == []
    _assert_no_synthetic_user_or_candidate(
        result,
        "Premature: everything is done.",
        "Still done, no tests needed.",
    )

    # The one continuation reuses the existing user role on the wire.  Its
    # request-only suffix never enters the stored transcript.
    continuation_users = [
        message
        for message in requests[2]
        if message.get("role") == "user"
    ]
    assert len(continuation_users) == 1
    assert "RUNTIME BOUNDED PROOF GATE" in continuation_users[0]["content"]
    assert "RUNTIME BOUNDED PROOF GATE" not in result["messages"][0]["content"]


def test_continuation_tool_can_land_proof_then_close_normally(
    agent, tmp_path
):
    changed = tmp_path / "project" / "changed.py"
    proof_state = {"passed": False}
    result, requests, streamed = _run_scripted_turn(
        agent,
        [
            _tool_response("write_file", call_id="edit-1"),
            _text_response("Premature completion."),
            _tool_response("terminal", call_id="verify-1"),
            _text_response("Verified final report."),
        ],
        changed_path=changed,
        proof_state=proof_state,
    )

    assert proof_state["passed"] is True
    assert result["api_calls"] == 4
    assert result["completed"] is True
    assert result["failed"] is False
    assert result["partial"] is False
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["final_response"] == "Verified final report."
    assert [chunk for chunk in streamed if chunk is not None] == [
        "Verified final report."
    ]
    _assert_no_synthetic_user_or_candidate(result, "Premature completion.")
    assert "RUNTIME BOUNDED PROOF GATE" in next(
        message["content"]
        for message in requests[2]
        if message.get("role") == "user"
    )
    assert "RUNTIME BOUNDED PROOF GATE" not in next(
        message["content"]
        for message in requests[3]
        if message.get("role") == "user"
    )


def test_later_edit_after_passing_proof_fails_without_second_continuation(
    agent, tmp_path
):
    changed = tmp_path / "project" / "changed.py"
    proof_state = {"passed": False}
    result, requests, streamed = _run_scripted_turn(
        agent,
        [
            _tool_response("write_file", call_id="edit-1"),
            _text_response("Premature completion."),
            _tool_response("terminal", call_id="verify-1"),
            _tool_response("write_file", call_id="edit-2"),
        ],
        changed_path=changed,
        proof_state=proof_state,
    )

    assert proof_state["passed"] is False
    assert len(requests) == 4
    assert result["api_calls"] == 4
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["partial"] is True
    assert result["error"] == "verification_evidence_missing"
    assert result["turn_exit_reason"] == "bounded_proof_gate_unverified"
    assert "PARTIAL / NOT VERIFIED" in result["final_response"]
    assert [chunk for chunk in streamed if chunk is not None] == []
    _assert_no_synthetic_user_or_candidate(result, "Premature completion.")


def test_unrelated_tool_does_not_open_another_continuation(
    agent, tmp_path
):
    changed = tmp_path / "project" / "changed.py"
    proof_state = {"passed": False}
    result, requests, _streamed = _run_scripted_turn(
        agent,
        [
            _tool_response("write_file", call_id="edit-1"),
            _text_response("Premature completion."),
            _tool_response("read_file", call_id="wander-1"),
        ],
        changed_path=changed,
        proof_state=proof_state,
    )

    assert len(requests) == 3
    assert result["api_calls"] == 3
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["partial"] is True
    assert result["error"] == "verification_evidence_missing"
    assert result["turn_exit_reason"] == "bounded_proof_gate_unverified"
    assert "PARTIAL / NOT VERIFIED" in result["final_response"]
    _assert_no_synthetic_user_or_candidate(result, "Premature completion.")


def test_disabled_policy_preserves_model_authored_completion(
    agent, tmp_path
):
    changed = tmp_path / "project" / "changed.py"
    requests = []
    streamed = []
    responses = iter(
        [
            _tool_response("write_file", call_id="edit-1"),
            _text_response("Model-authored completion."),
        ]
    )

    def model_call(api_kwargs):
        requests.append(copy.deepcopy(api_kwargs["messages"]))
        response = next(responses)
        content = response.choices[0].message.content
        if content and agent.stream_delta_callback is not None:
            agent.stream_delta_callback(content)
        return response

    def execute_tools(assistant_message, messages, *_args):
        agent._turn_file_mutation_paths.add(str(changed))
        tool_call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": "write_file",
                "tool_call_id": tool_call.id,
                "content": '{"success":true}',
            }
        )

    agent._interruptible_api_call = model_call
    agent._execute_tool_calls = execute_tools
    agent.stream_delta_callback = streamed.append

    with (
        patch("agent.verification_stop.verify_on_stop_enabled", return_value=False),
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            return_value="must not be used",
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit the implementation and verify it")

    assert len(requests) == 2
    assert result["completed"] is True
    assert result["failed"] is False
    assert result["final_response"] == "Model-authored completion."
    assert [chunk for chunk in streamed if chunk is not None] == [
        "Model-authored completion."
    ]
    assert all(
        "RUNTIME BOUNDED PROOF GATE"
        not in str(message.get("content") or "")
        for request in requests
        for message in request
    )


def test_enabled_gate_treats_ledger_failure_as_missing_proof(agent):
    agent._turn_file_mutation_paths = {"/project/changed.py"}

    with (
        patch("agent.verification_stop.verify_on_stop_enabled", return_value=True),
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=RuntimeError("ledger unavailable"),
        ),
    ):
        instruction = _bounded_proof_gate_instruction(agent)

    assert instruction is not None
    assert "could not read fresh passing verification evidence" in instruction


def test_runtime_effect_generation_delta_seeds_current_turn_paths():
    authority = "runtime-effect-delta-root"
    agent = SimpleNamespace(
        _workspace_lease_authority=authority,
        _turn_file_mutation_paths=set(),
        _turn_isolated_worker_proof_error=None,
    )
    receipt = _worker_receipt(
        authority,
        edit_generation=8,
        verified_generation=7,
        status="stale",
        pending_paths=["/workspace/src/changed.py"],
    )

    with patch(
        "tools.terminal_tool.isolated_worker_proof_status_for_authority",
        return_value=receipt,
    ):
        _apply_runtime_effect(
            agent,
            _runtime_effect(authority, 7),
            isolated_worker_selected=True,
        )

    assert agent._turn_isolated_worker_runtime_effect_active is True
    assert agent._turn_isolated_worker_baseline_generation == 8
    assert agent._turn_file_mutation_paths == {
        "/workspace/src/changed.py"
    }


def test_runtime_effect_equal_generation_is_read_only():
    authority = "runtime-effect-read-only-root"
    agent = SimpleNamespace(
        _workspace_lease_authority=authority,
        _turn_file_mutation_paths=set(),
        _turn_isolated_worker_proof_error=None,
    )
    receipt = _worker_receipt(
        authority,
        edit_generation=4,
        verified_generation=4,
        status="passed",
    )

    with patch(
        "tools.terminal_tool.isolated_worker_proof_status_for_authority",
        return_value=receipt,
    ):
        _apply_runtime_effect(
            agent,
            _runtime_effect(authority, 4),
            isolated_worker_selected=True,
        )

    assert agent._turn_file_mutation_paths == set()
    assert agent._turn_isolated_worker_proof_error is None


def test_runtime_effect_authority_mismatch_is_hard_failure():
    agent = SimpleNamespace(
        _workspace_lease_authority="actual-conversation-root",
        _turn_file_mutation_paths=set(),
    )

    with pytest.raises(
        RuntimeError,
        match="runtime_effect_workspace_authority_mismatch",
    ):
        _apply_runtime_effect(
            agent,
            _runtime_effect("forged-other-root", 1),
            isolated_worker_selected=True,
        )


def test_null_runtime_effect_baseline_requires_new_passing_proof():
    """An old already-passing receipt cannot erase dispatch uncertainty."""
    authority = "runtime-effect-null-baseline-root"
    agent = SimpleNamespace(
        _workspace_lease_authority=authority,
        _turn_file_mutation_paths=set(),
        _turn_runtime_effect_fresh_proof_observed=False,
        _isolated_worker_backend_selected=True,
    )
    receipt = _worker_receipt(
        authority,
        edit_generation=5,
        verified_generation=5,
        status="passed",
    )

    with (
        patch(
            "tools.terminal_tool.isolated_worker_proof_status_for_authority",
            return_value=receipt,
        ),
        patch(
            "agent.verification_stop.verify_on_stop_enabled",
            return_value=False,
        ),
    ):
        _apply_runtime_effect(
            agent,
            _runtime_effect(authority, None),
            isolated_worker_selected=True,
        )
        instruction = _bounded_proof_gate_instruction(agent)
        assert instruction is not None
        assert agent._turn_isolated_worker_proof_error == (
            "runtime_effect_baseline_unavailable"
        )

        # A host-observed terminal verification in the completion turn makes
        # the same live passed receipt fresh enough to resolve uncertainty.
        agent._turn_runtime_effect_fresh_proof_observed = True
        assert _bounded_proof_gate_instruction(agent) is None

    assert agent._turn_isolated_worker_proof_error is None


def test_runtime_effect_generation_rollback_cannot_be_recovered():
    authority = "runtime-effect-rollback-root"
    agent = SimpleNamespace(
        _workspace_lease_authority=authority,
        _turn_file_mutation_paths=set(),
        _turn_runtime_effect_fresh_proof_observed=True,
        _isolated_worker_backend_selected=True,
    )
    receipt = _worker_receipt(
        authority,
        edit_generation=5,
        verified_generation=5,
        status="passed",
    )

    with (
        patch(
            "tools.terminal_tool.isolated_worker_proof_status_for_authority",
            return_value=receipt,
        ),
        patch(
            "agent.verification_stop.verify_on_stop_enabled",
            return_value=False,
        ),
    ):
        _apply_runtime_effect(
            agent,
            _runtime_effect(authority, 6),
            isolated_worker_selected=True,
        )
        instruction = _bounded_proof_gate_instruction(agent)

    assert instruction is not None
    assert agent._turn_isolated_worker_proof_error == (
        "runtime_effect_generation_rollback"
    )


def test_isolated_worker_no_tool_turn_makes_zero_worker_calls(agent):
    agent._interruptible_api_call = lambda _kwargs: _text_response("plain answer")
    status_calls = []

    def unexpected_status(_authority):
        status_calls.append(_authority)
        raise AssertionError("no-tool turn must not contact worker")

    with (
        patch(
            "tools.terminal_tool.isolated_worker_backend_selected",
            return_value=True,
        ),
        patch(
            "tools.terminal_tool.register_workspace_lease_authorities",
            return_value=("bounded-proof-test",),
        ),
        patch(
            "tools.terminal_tool.unregister_workspace_lease_authority",
        ) as unregister,
        patch(
            "tools.terminal_tool.isolated_worker_proof_status_for_authority",
            side_effect=unexpected_status,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("answer without tools")

    assert result["completed"] is True
    assert result["final_response"] == "plain answer"
    assert status_calls == []
    unregister.assert_called_once_with(
        agent._current_task_id,
        "bounded-proof-test",
        owner_id=agent._workspace_lease_binding_owner_id,
    )


def test_hidden_mutator_generation_delta_is_caught_from_lazy_baseline(agent):
    agent._isolated_worker_backend_selected = True
    agent._workspace_lease_authority = "bounded-proof-test"
    agent._turn_isolated_worker_baseline_attempted = False
    agent._turn_isolated_worker_baseline_generation = None
    agent._turn_isolated_worker_proof_receipt = None
    agent._turn_isolated_worker_proof_error = None
    agent._turn_file_mutation_paths = set()
    receipts = iter(
        [
            {
                "edit_generation": 4,
                "verified_generation": 4,
                "status": "passed",
                "pending_paths": [],
                "applicability": "applicable",
            },
            {
                "schema": "muncho.isolated-worker.proof-receipt.v1",
                "lease_id": canonical_lease_id("bounded-proof-test"),
                "edit_generation": 5,
                "verified_generation": 4,
                "status": "stale",
                "pending_paths": ["/workspace/app.py"],
                "applicability": "applicable",
            },
        ]
    )

    def status(_authority):
        return next(receipts)

    with (
        patch(
            "tools.terminal_tool.isolated_worker_proof_status_for_authority",
            side_effect=status,
        ),
        patch("agent.verification_stop.verify_on_stop_enabled", return_value=True),
    ):
        _capture_isolated_worker_baseline(agent)
        instruction = _bounded_proof_gate_instruction(agent)

    assert agent._turn_isolated_worker_baseline_generation == 4
    assert "/workspace/app.py" in agent._turn_file_mutation_paths
    assert instruction is not None
    assert "authoritative proof epoch is not fresh" in instruction


def test_user_message_provenance_is_forwarded_by_agent_wrapper(agent):
    marker = {"kind": "trusted-runtime", "event_id": "evt-1"}
    expected = {
        "final_response": "ok",
        "messages": [],
        "completed": True,
    }
    with patch(
        "agent.conversation_loop.run_conversation",
        return_value=expected,
    ) as inner:
        assert (
            agent.run_conversation(
                "trusted continuation",
                user_message_provenance=marker,
            )
            is expected
        )

    assert inner.call_args.kwargs["user_message_provenance"] == marker


def test_child_first_turn_does_not_treat_missing_row_as_lineage_root() -> None:
    class MissingChildRowDB:
        def get_session(self, _session_id):
            return None

        def get_conversation_root(self, _session_id):
            raise AssertionError("lineage reader must not run before row exists")

    assert (
        _persisted_conversation_root(MissingChildRowDB(), "new-child")
        is None
    )

    class PersistedChildDB:
        def get_session(self, _session_id):
            return {"id": "new-child"}

        def get_conversation_root(self, _session_id):
            return "root-parent"

    assert (
        _persisted_conversation_root(PersistedChildDB(), "new-child")
        == "root-parent"
    )
