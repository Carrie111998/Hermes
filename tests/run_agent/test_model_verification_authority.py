"""Completion authority belongs to the model, not host-side heuristics.

These tests intentionally use code-like filenames and test-like command text.
They are opaque model/tool data: neither may cause a host-authored
continuation, withheld response, or success/failure decision.  The isolated
worker boundary still validates structural receipt identity and generations.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_loop import _capture_isolated_worker_baseline
from agent.tool_runtime_effects import (
    bind_tool_runtime_effect,
    record_current_tool_runtime_effect,
)
from agent.turn_context import (
    _apply_runtime_effect,
    _persisted_conversation_root,
    _validate_runtime_effect_receipt,
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


def _tool_response(name: str, *, call_id: str, arguments: dict):
    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
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
    mutation_detection: str = "status",
    changed_paths=None,
    pending_paths=None,
    status: str = "passed",
):
    """Build the current wire receipt, including ignored legacy annotations."""

    return {
        "schema": "muncho.isolated-worker.proof-receipt.v1",
        "lease_id": canonical_lease_id(authority),
        "edit_generation": edit_generation,
        "verified_generation": edit_generation,
        "status": status,
        "mutation_detection": mutation_detection,
        "changed_paths": list(changed_paths or []),
        "pending_paths": list(pending_paths or []),
        "verification": {
            "canonical_command": "pytest tests/test_passwords.py",
            "kind": "test",
            "scope": "targeted",
            "status": status,
        },
        "applicability": "applicable",
        "project_root": "/workspace",
        "verify_commands_digest": "0" * 64,
        "material_fingerprint": "1" * 64,
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
            session_id="model-authority-test",
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


def _assert_model_completion(result, *, expected: str, api_calls: int) -> None:
    assert result["final_response"] == expected
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    assert result["failed"] is False
    assert result["api_calls"] == api_calls
    assert sum(message.get("role") == "user" for message in result["messages"]) == 1
    assert all(
        "RUNTIME BOUNDED PROOF GATE"
        not in str(message.get("content") or "")
        for message in result["messages"]
    )


def test_code_like_filename_cannot_withhold_or_continue_model_completion(agent):
    responses = iter(
        [
            _tool_response(
                "write_file",
                call_id="edit-1",
                arguments={
                    "path": "src/test_password_rotation.py",
                    "content": "SECRET = 'not-a-real-secret'\n",
                },
            ),
            _text_response("Model says the requested work is complete."),
        ]
    )
    requests = []

    def model_call(api_kwargs):
        requests.append(copy.deepcopy(api_kwargs["messages"]))
        return next(responses)

    def execute_tools(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        arguments = json.loads(tool_call.function.arguments)
        agent._turn_file_mutation_paths.add(arguments["path"])
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

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=AssertionError("host semantic gate must not run"),
        ),
        patch(
            "agent.verification_stop._is_non_code_path",
            side_effect=AssertionError("filename classifier must not run"),
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit and finish the implementation")

    _assert_model_completion(
        result,
        expected="Model says the requested work is complete.",
        api_calls=2,
    )
    assert len(requests) == 2
    assert all(
        "RUNTIME BOUNDED PROOF GATE"
        not in str(message.get("content") or "")
        for request in requests
        for message in request
    )


def test_test_like_terminal_prose_is_opaque_and_cannot_decide_workflow(agent):
    command = "pytest tests/test_passwords.py && python -m unittest"
    responses = iter(
        [
            _tool_response(
                "terminal",
                call_id="terminal-1",
                arguments={"command": command},
            ),
            _text_response("The model independently decides to stop here."),
        ]
    )

    def model_call(_api_kwargs):
        return next(responses)

    def execute_tools(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        arguments = json.loads(tool_call.function.arguments)
        authority = (
            getattr(agent, "_workspace_lease_authority", None)
            or agent.session_id
        )
        receipt = _worker_receipt(
            str(authority),
            edit_generation=4,
            status="passed",
        )
        with bind_tool_runtime_effect(
            tool_name="terminal",
            session_id=agent.session_id,
            turn_id=agent._current_turn_id,
            tool_call_id=tool_call.id,
        ):
            assert record_current_tool_runtime_effect({"receipt": receipt}) is True
        agent._record_file_mutation_result(
            "terminal",
            arguments,
            "all tests passed",
            False,
            tool_call_id=tool_call.id,
        )
        messages.append(
            {
                "role": "tool",
                "name": "terminal",
                "tool_call_id": tool_call.id,
                "content": "all tests passed",
            }
        )

    agent._interruptible_api_call = model_call
    agent._execute_tool_calls = execute_tools

    with (
        patch(
            "agent.verification_evidence._find_canonical_match",
            side_effect=AssertionError("command classifier must not run"),
        ),
        patch(
            "agent.verification_evidence.classify_verification_command",
            side_effect=AssertionError("command semantics must not run"),
        ),
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=AssertionError("host semantic gate must not run"),
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("run whatever checks you judge useful")

    _assert_model_completion(
        result,
        expected="The model independently decides to stop here.",
        api_calls=2,
    )
    stored = agent._turn_isolated_worker_proof_receipt
    assert stored["edit_generation"] == 4
    assert "status" not in stored
    assert "verification" not in stored


def test_structural_receipt_projection_drops_semantic_annotations():
    authority = "receipt-projection-root"
    receipt = _worker_receipt(
        authority,
        edit_generation=9,
        mutation_detection="changed",
        changed_paths=["/workspace/src/test_feature.py"],
        pending_paths=["/workspace/src/test_feature.py"],
        status="failed",
    )

    projected = _validate_runtime_effect_receipt(authority, receipt)

    assert projected == {
        "schema": "muncho.isolated-worker.proof-receipt.v1",
        "lease_id": canonical_lease_id(authority),
        "edit_generation": 9,
        "mutation_detection": "changed",
        "changed_paths": ["/workspace/src/test_feature.py"],
        "pending_paths": ["/workspace/src/test_feature.py"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "wrong.schema"),
        ("lease_id", "wrong-lease"),
        ("edit_generation", -1),
        ("edit_generation", "9"),
        ("mutation_detection", "tests-passed"),
        ("changed_paths", ["../outside.py"]),
        ("pending_paths", ["/workspace/../outside.py"]),
    ],
)
def test_structural_receipt_integrity_is_still_enforced(field, value):
    authority = "receipt-integrity-root"
    receipt = _worker_receipt(authority, edit_generation=9)
    receipt[field] = value

    with pytest.raises(ValueError, match="runtime_effect_.*receipt_invalid"):
        _validate_runtime_effect_receipt(authority, receipt)


def test_runtime_effect_generation_delta_seeds_current_turn_paths():
    authority = "runtime-effect-delta-root"
    target = "/workspace/src/test_changed.py"
    agent = SimpleNamespace(
        _workspace_lease_authority=authority,
        _turn_file_mutation_paths=set(),
        _turn_isolated_worker_proof_error=None,
    )
    receipt = _worker_receipt(
        authority,
        edit_generation=8,
        pending_paths=[target],
        status="stale",
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

    assert agent._turn_isolated_worker_baseline_generation == 8
    assert agent._turn_file_mutation_paths == {target}


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
            return_value=("model-authority-test",),
        ),
        patch("tools.terminal_tool.unregister_workspace_lease_authority") as unregister,
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
        "model-authority-test",
        owner_id=agent._workspace_lease_binding_owner_id,
    )


def test_baseline_capture_rejects_wrong_receipt_identity():
    authority = "baseline-identity-root"
    agent = SimpleNamespace(
        _isolated_worker_backend_selected=True,
        _turn_isolated_worker_baseline_attempted=False,
        _turn_isolated_worker_baseline_generation=None,
        _workspace_lease_authority=authority,
        session_id=authority,
        _turn_file_mutation_paths=set(),
    )
    receipt = _worker_receipt(authority, edit_generation=4)
    receipt["lease_id"] = "forged-lease"

    with patch(
        "tools.terminal_tool.isolated_worker_proof_status_for_authority",
        return_value=receipt,
    ):
        _capture_isolated_worker_baseline(agent)

    assert agent._turn_isolated_worker_baseline_generation is None
    assert agent._turn_file_mutation_paths == {"/workspace"}
    assert agent._turn_isolated_worker_proof_error.startswith(
        "isolated_worker_baseline_unavailable:ValueError"
    )


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

    assert _persisted_conversation_root(MissingChildRowDB(), "new-child") is None

    class PersistedChildDB:
        def get_session(self, _session_id):
            return {"id": "new-child"}

        def get_conversation_root(self, _session_id):
            return "root-parent"

    assert (
        _persisted_conversation_root(PersistedChildDB(), "new-child")
        == "root-parent"
    )
