"""The provider-neutral context compilation boundary.

The compiler accounts for the entire request once: instructions, canonical
history, current input, selected tool schemas, fixed adapter overhead, and
output reserve. It returns a typed failure before provider invocation when the
mandatory envelope or truthful history projection cannot fit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

from agent.models_dev import ModelCapabilities
from agent.session_contracts import (
    CanonicalSessionEvent,
    CompiledTurn,
    ContextCompilationFailure,
    ContextCompilationFailureReason,
    ContextCompilationResult,
    ContextComponentUsage,
    ContextReceipt,
    SessionSnapshot,
    ToolCatalogSnapshot,
    TurnCommand,
)


TokenCounter = Callable[[Any], int]


def conservative_json_token_count(value: Any) -> int:
    """Conservative dependency-free estimate for unknown tokenizers."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    # One token per three UTF-8 bytes is intentionally more conservative than
    # the common chars/4 estimate, especially for Hebrew and schema-heavy JSON.
    return (len(encoded) + 2) // 3


def _stable_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _failure(
    reason: ContextCompilationFailureReason,
    snapshot: SessionSnapshot,
    command: TurnCommand,
    *,
    capacity_tokens: int,
    required_tokens: int,
) -> ContextCompilationResult:
    return ContextCompilationResult(
        failure=ContextCompilationFailure(
            reason=reason,
            session_id=snapshot.session_id,
            session_revision=snapshot.revision,
            turn_id=command.turn_id,
            capacity_tokens=capacity_tokens,
            required_tokens=required_tokens,
        )
    )


def _history_units(
    events: Sequence[CanonicalSessionEvent],
) -> list[tuple[CanonicalSessionEvent, ...]]:
    """Group tool-call assistants with their following tool-result events."""
    units: list[tuple[CanonicalSessionEvent, ...]] = []
    index = 0
    while index < len(events):
        event = events[index]
        message = event.message
        if message.get("role") == "assistant" and message.get("tool_calls"):
            grouped = [event]
            index += 1
            while index < len(events) and events[index].message.get("role") == "tool":
                grouped.append(events[index])
                index += 1
            units.append(tuple(grouped))
            continue
        units.append((event,))
        index += 1
    return units


def compile_turn(
    *,
    snapshot: SessionSnapshot,
    command: TurnCommand,
    instructions: Sequence[Mapping[str, Any]],
    tool_catalog: ToolCatalogSnapshot,
    capabilities: ModelCapabilities,
    model: str,
    provider: str,
    token_counter: TokenCounter = conservative_json_token_count,
) -> ContextCompilationResult:
    """Compile a canonical session snapshot for one model adapter.

    History is retained newest-first within the remaining budget and emitted
    in canonical order. Until checkpoint inputs are part of this contract, a
    session with prior events that cannot retain even one event fails loudly;
    it never degenerates into a current-message-only provider call.
    """
    if command.session_id != snapshot.session_id:
        raise ValueError("command and snapshot session_id must match")
    if command.expected_revision != snapshot.revision:
        raise ValueError("command expected_revision must match snapshot revision")
    if command.user_event.get("role") != "user":
        return _failure(
            ContextCompilationFailureReason.INVALID_CURRENT_TURN,
            snapshot,
            command,
            capacity_tokens=max(0, capabilities.context_window),
            required_tokens=0,
        )

    capacity = capabilities.context_window
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        return _failure(
            ContextCompilationFailureReason.CAPACITY_UNKNOWN,
            snapshot,
            command,
            capacity_tokens=0,
            required_tokens=0,
        )

    output_limit = capabilities.max_output_tokens
    fixed_overhead = capabilities.fixed_input_overhead_tokens
    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or output_limit <= 0
        or not isinstance(fixed_overhead, int)
        or isinstance(fixed_overhead, bool)
        or fixed_overhead < 0
    ):
        return _failure(
            ContextCompilationFailureReason.CAPACITY_UNKNOWN,
            snapshot,
            command,
            capacity_tokens=capacity,
            required_tokens=0,
        )
    if tool_catalog.tools and not capabilities.supports_tools:
        return _failure(
            ContextCompilationFailureReason.UNSUPPORTED_REQUIRED_CONTENT,
            snapshot,
            command,
            capacity_tokens=capacity,
            required_tokens=0,
        )

    output_reserve = min(output_limit, capacity)
    instructions_tokens = token_counter(tuple(instructions)) if instructions else 0
    current_tokens = token_counter(command.user_event)
    tool_tokens = token_counter(tool_catalog.tools) if tool_catalog.tools else 0
    fixed_tokens = fixed_overhead
    mandatory = (
        instructions_tokens
        + current_tokens
        + tool_tokens
        + fixed_tokens
        + output_reserve
    )
    if mandatory > capacity:
        return _failure(
            ContextCompilationFailureReason.MANDATORY_ENVELOPE_EXCEEDS_CAPACITY,
            snapshot,
            command,
            capacity_tokens=capacity,
            required_tokens=mandatory,
        )

    remaining = capacity - mandatory
    units = _history_units(snapshot.events)
    selected_reversed: list[tuple[tuple[CanonicalSessionEvent, ...], int]] = []
    omitted_units: list[tuple[CanonicalSessionEvent, ...]] = []
    selecting = True
    for unit in reversed(units):
        unit_tokens = sum(token_counter(event.message) for event in unit)
        if selecting and unit_tokens <= remaining:
            selected_reversed.append((unit, unit_tokens))
            remaining -= unit_tokens
        else:
            # Keep a contiguous suffix. Once one unit does not fit, older
            # messages cannot jump over that omission into the request.
            selecting = False
            omitted_units.append(unit)

    selected = list(reversed(selected_reversed))
    if snapshot.events and not selected:
        newest_unit_tokens = sum(
            token_counter(event.message) for event in units[-1]
        )
        return _failure(
            ContextCompilationFailureReason.HISTORY_CANNOT_FIT_WITHOUT_CHECKPOINT,
            snapshot,
            command,
            capacity_tokens=capacity,
            required_tokens=mandatory + newest_unit_tokens,
        )

    retained_events = tuple(event for unit, _tokens in selected for event in unit)
    omitted_events = tuple(
        sorted(
            (event for unit in omitted_units for event in unit),
            key=lambda event: event.sequence,
        )
    )
    retained_event_ids = tuple(event.event_id for event in retained_events)
    omitted_event_ids = tuple(event.event_id for event in omitted_events)
    history_tokens = sum(tokens for _unit, tokens in selected)
    messages = (
        tuple(instructions)
        + tuple(event.message for event in retained_events)
        + (command.user_event,)
    )
    usage = ContextComponentUsage(
        instructions_tokens=instructions_tokens,
        history_tokens=history_tokens,
        current_input_tokens=current_tokens,
        tool_tokens=tool_tokens,
        fixed_overhead_tokens=fixed_tokens,
        output_reserve_tokens=output_reserve,
    )
    receipt = ContextReceipt(
        source_revision=snapshot.revision,
        source_event_count=len(snapshot.events),
        retained_event_ids=retained_event_ids,
        omitted_event_ids=omitted_event_ids,
        tool_catalog_version=tool_catalog.version,
        selected_tool_count=len(tool_catalog.tools),
        usage=usage,
        estimator=getattr(token_counter, "__name__", type(token_counter).__name__),
    )
    fingerprint_payload = {
        "session_id": snapshot.session_id,
        "revision": snapshot.revision,
        "turn_id": command.turn_id,
        "model": model,
        "provider": provider,
        "messages": messages,
        "tools": tool_catalog.tools,
        "receipt": {
            "retained": retained_event_ids,
            "omitted": omitted_event_ids,
            "catalog": tool_catalog.version,
        },
    }
    fingerprint = hashlib.sha256(_stable_json(fingerprint_payload)).hexdigest()
    return ContextCompilationResult(
        compiled=CompiledTurn(
            session_id=snapshot.session_id,
            session_revision=snapshot.revision,
            turn_id=command.turn_id,
            model=model,
            provider=provider,
            messages=messages,
            tools=tool_catalog.tools,
            capabilities=capabilities,
            receipt=receipt,
            context_fingerprint=fingerprint,
        )
    )
