from __future__ import annotations

from agent.context_compiler import (
    compile_context,
    compile_turn,
    conservative_json_token_count,
)
from agent.models_dev import ModelCapabilities
from agent.session_contracts import (
    CanonicalSessionEvent,
    CompilationMessage,
    ContextCompilationFailureReason,
    ModelInvocation,
    SessionSnapshot,
    ToolCatalogSnapshot,
    TurnCommand,
)


def _command(session_id: str = "session-1", revision: int = 2) -> TurnCommand:
    return TurnCommand(
        session_id=session_id,
        turn_id="turn-2",
        idempotency_key="desktop-input-2",
        expected_revision=revision,
        user_event={"role": "user", "content": "What was the code?"},
    )


def _tool(index: int) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"office_tool_{index}",
            "description": "Synthetic office tool. " * 24,
            "parameters": {
                "type": "object",
                "properties": {
                    f"field_{part}": {
                        "type": "string",
                        "description": "Synthetic field for deterministic budget coverage.",
                    }
                    for part in range(7)
                },
            },
        },
    }


def _event(event_id: str, sequence: int, role: str, content: str, **extra) -> CanonicalSessionEvent:
    return CanonicalSessionEvent(
        event_id=event_id,
        sequence=sequence,
        message={"role": role, "content": content, **extra},
    )


def test_office_manager_scale_tools_retain_dependent_history() -> None:
    snapshot = SessionSnapshot(
        session_id="session-1",
        revision=2,
        events=(
            _event("event-1", 1, "user", "Remember the code is OLIVE-42."),
            _event("event-2", 2, "assistant", "I will remember OLIVE-42."),
        ),
    )
    catalog = ToolCatalogSnapshot(
        version="office-manager-v1",
        tools=tuple(_tool(index) for index in range(165)),
    )

    result = compile_turn(
        snapshot=snapshot,
        command=_command(),
        instructions=({"role": "system", "content": "Use office policy."},),
        tool_catalog=catalog,
        capabilities=ModelCapabilities(
            context_window=256_000,
            max_output_tokens=16_000,
            capacity_source="test",
        ),
        model="model-b",
        provider="future-provider",
    )

    assert result.ok
    assert result.compiled is not None
    assert result.compiled.session_id == "session-1"
    assert result.compiled.receipt.selected_tool_count == 165
    assert result.compiled.receipt.retained_event_ids == ("event-1", "event-2")
    assert "OLIVE-42" in str(result.compiled.messages)
    assert result.compiled.receipt.usage.total_reserved_tokens <= 256_000


def test_model_switch_recompiles_same_hermes_revision_without_provider_state() -> None:
    snapshot = SessionSnapshot(
        session_id="session-1",
        revision=2,
        events=(
            _event("event-1", 1, "user", "Remember OLIVE-42."),
            _event("event-2", 2, "assistant", "Stored."),
        ),
    )
    common = dict(
        snapshot=snapshot,
        command=_command(),
        instructions=(),
        tool_catalog=ToolCatalogSnapshot(version="none"),
    )

    first = compile_turn(
        **common,
        capabilities=ModelCapabilities(context_window=128_000, max_output_tokens=8_000),
        model="model-a",
        provider="provider-a",
    )
    second = compile_turn(
        **common,
        capabilities=ModelCapabilities(context_window=256_000, max_output_tokens=16_000),
        model="model-b",
        provider="provider-b",
    )

    assert first.ok and second.ok
    assert first.compiled is not None and second.compiled is not None
    assert first.compiled.session_id == second.compiled.session_id == snapshot.session_id
    assert first.compiled.session_revision == second.compiled.session_revision == snapshot.revision
    assert first.compiled.messages == second.compiled.messages
    assert first.compiled.context_fingerprint != second.compiled.context_fingerprint


def test_mandatory_large_tool_envelope_fails_before_dispatch() -> None:
    catalog = ToolCatalogSnapshot(
        version="too-large",
        tools=tuple(_tool(index) for index in range(165)),
    )
    invoked = False

    result = compile_turn(
        snapshot=SessionSnapshot(session_id="session-1", revision=0, events=()),
        command=_command(revision=0),
        instructions=({"role": "system", "content": "Use office policy."},),
        tool_catalog=catalog,
        capabilities=ModelCapabilities(
            context_window=24_000,
            max_output_tokens=8_000,
            capacity_source="test",
        ),
        model="tiny-model",
        provider="future-provider",
    )
    if result.compiled is not None:
        invoked = True

    assert not invoked
    assert result.failure is not None
    assert result.failure.reason is ContextCompilationFailureReason.MANDATORY_ENVELOPE_EXCEEDS_CAPACITY
    assert result.failure.required_tokens > result.failure.capacity_tokens


def test_existing_history_never_silently_compiles_to_empty() -> None:
    snapshot = SessionSnapshot(
        session_id="session-1",
        revision=2,
        events=(_event("event-1", 1, "user", "x" * 9_000),),
    )
    result = compile_turn(
        snapshot=snapshot,
        command=_command(),
        instructions=(),
        tool_catalog=ToolCatalogSnapshot(version="none"),
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=1_000),
        model="small-model",
        provider="future-provider",
    )

    assert result.compiled is None
    assert result.failure is not None
    assert result.failure.reason is ContextCompilationFailureReason.HISTORY_CANNOT_FIT_WITHOUT_CHECKPOINT


def test_tool_call_and_results_are_selected_as_one_atomic_history_unit() -> None:
    events = (
        _event("event-old", 1, "user", "x" * 15_000),
        _event("event-old-answer", 2, "assistant", "Old answer."),
        _event("event-prompt", 3, "user", "Look up the new result."),
        _event(
            "event-call",
            4,
            "assistant",
            "",
            tool_calls=[{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        ),
        _event("event-result", 5, "tool", "result", tool_call_id="call-1"),
        _event("event-answer", 6, "assistant", "The result was accepted."),
    )
    result = compile_turn(
        snapshot=SessionSnapshot(session_id="session-1", revision=2, events=events),
        command=_command(),
        instructions=(),
        tool_catalog=ToolCatalogSnapshot(version="none"),
        capabilities=ModelCapabilities(context_window=4_500, max_output_tokens=1_000),
        model="model-b",
        provider="provider-b",
    )

    assert result.ok
    assert result.compiled is not None
    assert result.compiled.receipt.retained_event_ids == (
        "event-prompt",
        "event-call",
        "event-result",
        "event-answer",
    )
    assert result.compiled.receipt.omitted_event_ids == (
        "event-old",
        "event-old-answer",
    )


def test_hebrew_estimator_counts_utf8_bytes_conservatively() -> None:
    hebrew = {"role": "user", "content": "שלום עולם"}
    ascii_text = {"role": "user", "content": "hello world"}
    assert conservative_json_token_count(hebrew) > conservative_json_token_count(ascii_text)


def test_tool_loop_invocation_keeps_entire_active_turn_mandatory() -> None:
    invocation = ModelInvocation(
        session_id="session-1",
        session_revision=8,
        turn_id="turn-4",
        messages=(
            CompilationMessage(
                message={"role": "system", "content": "Use tools safely."},
                required=True,
            ),
            CompilationMessage(
                message={"role": "user", "content": "old history" * 2_000},
                source_event_ids=("event-old",),
            ),
            CompilationMessage(
                message={"role": "assistant", "content": "Old answer."},
                source_event_ids=("event-old-answer",),
            ),
            CompilationMessage(
                message={"role": "user", "content": "Recent question."},
                source_event_ids=("event-recent-user",),
            ),
            CompilationMessage(
                message={"role": "assistant", "content": "Recent checkpoint."},
                source_event_ids=("event-recent",),
            ),
            CompilationMessage(
                message={"role": "user", "content": "Look up the order."},
                source_event_ids=("event-user",),
                required=True,
            ),
            CompilationMessage(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }],
                },
                source_event_ids=("event-call",),
                required=True,
            ),
            CompilationMessage(
                message={
                    "role": "tool",
                    "content": "Order 42 is ready.",
                    "tool_call_id": "call-1",
                },
                source_event_ids=("event-result",),
                required=True,
            ),
        ),
    )

    result = compile_context(
        invocation=invocation,
        tool_catalog=ToolCatalogSnapshot(version="tools-v1"),
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=1_000),
        model="model-b",
        provider="provider-b",
    )

    assert result.compiled is not None
    assert result.compiled.receipt.omitted_event_ids == (
        "event-old",
        "event-old-answer",
    )
    assert result.compiled.receipt.retained_event_ids == (
        "event-recent-user",
        "event-recent",
        "event-user",
        "event-call",
        "event-result",
    )
    assert [message["role"] for message in result.compiled.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]


def test_active_tool_result_no_fit_fails_before_provider_invocation() -> None:
    invocation = ModelInvocation(
        session_id="session-1",
        session_revision=3,
        turn_id="turn-2",
        messages=(
            CompilationMessage(
                message={"role": "user", "content": "Run the report."},
                source_event_ids=("event-user",),
                required=True,
            ),
            CompilationMessage(
                message={"role": "tool", "content": "x" * 20_000},
                source_event_ids=("event-result",),
                required=True,
            ),
        ),
    )

    result = compile_context(
        invocation=invocation,
        tool_catalog=ToolCatalogSnapshot(version="none"),
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=1_000),
        model="small-model",
        provider="provider-b",
    )

    assert result.compiled is None
    assert result.failure is not None
    assert result.failure.reason is (
        ContextCompilationFailureReason.MANDATORY_ENVELOPE_EXCEEDS_CAPACITY
    )
