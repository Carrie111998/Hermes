"""The provider-neutral context compilation boundary.

The compiler accounts for the entire request once: instructions, canonical
history, current input, selected tool schemas, fixed adapter overhead, and
output reserve. It returns a typed failure before provider invocation when the
mandatory envelope or truthful history projection cannot fit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from agent.models_dev import ModelCapabilities
from agent.session_contracts import (
    CanonicalSessionEvent,
    CompilationMessage,
    CompiledTurn,
    ContextCompilationFailure,
    ContextCompilationFailureReason,
    ContextCompilationResult,
    ContextComponentUsage,
    ContextReceipt,
    ModelInvocation,
    SessionSnapshot,
    ToolCatalogSnapshot,
    TurnCommand,
)


TokenCounter = Callable[[Any], int]

CONTEXT_EVENT_IDS_KEY = "_hermes_context_event_ids"
CONTEXT_REQUIRED_KEY = "_hermes_context_required"


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


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def resolve_runtime_capabilities(agent: Any) -> ModelCapabilities:
    """Resolve the active model's capabilities at invocation time.

    Fallback/model switching mutates ``agent.provider`` and ``agent.model``.
    Looking these values up for every outer-loop compilation ensures a
    fallback is recompiled against its own capacity instead of inheriting the
    primary model's budget. The runtime compressor remains authoritative when
    it has already resolved a more specific context window.
    """
    from agent.models_dev import get_model_capabilities

    provider = str(getattr(agent, "provider", "") or "")
    model = str(getattr(agent, "model", "") or "")
    discovered = get_model_capabilities(provider, model, allow_network=False)
    if discovered is None:
        discovered = ModelCapabilities(
            supports_tools=True,
            supports_vision=True,
            supports_reasoning=True,
            capacity_source="hermes_runtime_fallback",
        )

    compressor = getattr(agent, "context_compressor", None)
    runtime_context = _positive_int(getattr(compressor, "context_length", None))
    runtime_output = _positive_int(getattr(agent, "max_tokens", None))
    context_window = runtime_context or discovered.context_window
    max_output_tokens = min(
        runtime_output or discovered.max_output_tokens,
        context_window,
    )
    fixed_overhead = getattr(agent, "_model_adapter_fixed_overhead_tokens", None)
    if type(fixed_overhead) is not int or fixed_overhead < 0:
        fixed_overhead = discovered.fixed_input_overhead_tokens
    return replace(
        discovered,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        fixed_input_overhead_tokens=fixed_overhead,
        capacity_source=(
            "hermes_runtime"
            if runtime_context is not None
            else discovered.capacity_source
        ),
    )


def _projection_event_id(index: int, message: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_stable_json(message)).hexdigest()[:16]
    return f"projection:{index}:{digest}"


def compile_prepared_context(
    *,
    session_id: str,
    session_revision: int,
    turn_id: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    capabilities: ModelCapabilities,
    model: str,
    provider: str,
    token_counter: TokenCounter = conservative_json_token_count,
) -> ContextCompilationResult:
    """Compile a fully prepared, provider-neutral Hermes request projection.

    Callers attach source IDs and required markers before request-only context
    transforms. If a legacy transform drops every marker, the system message
    and the latest user-to-tail suffix are treated as required; deterministic
    projection IDs keep the migration path observable until every source is a
    canonical SessionSnapshot event.
    """
    prepared = [dict(message) for message in messages]
    marked_required = any(
        bool(message.get(CONTEXT_REQUIRED_KEY))
        and message.get("role") != "system"
        for message in prepared
    )
    fallback_required_from = None
    if not marked_required:
        for index in range(len(prepared) - 1, -1, -1):
            if prepared[index].get("role") == "user":
                fallback_required_from = index
                break

    items = []
    for index, raw in enumerate(prepared):
        raw_ids = raw.pop(CONTEXT_EVENT_IDS_KEY, ())
        marked = bool(raw.pop(CONTEXT_REQUIRED_KEY, False))
        if isinstance(raw_ids, str):
            event_ids = (raw_ids,)
        elif isinstance(raw_ids, (list, tuple)):
            event_ids = tuple(
                value for value in raw_ids if isinstance(value, str) and value
            )
        else:
            event_ids = ()
        if not event_ids and raw.get("role") != "system":
            event_ids = (_projection_event_id(index, raw),)
        required = marked or raw.get("role") == "system"
        if fallback_required_from is not None and index >= fallback_required_from:
            required = True
        items.append(
            CompilationMessage(
                message=raw,
                source_event_ids=event_ids,
                required=required,
            )
        )

    tool_tuple = tuple(dict(tool) for tool in tools)
    catalog_version = "sha256:" + hashlib.sha256(_stable_json(tool_tuple)).hexdigest()
    return compile_context(
        invocation=ModelInvocation(
            session_id=session_id,
            session_revision=session_revision,
            turn_id=turn_id,
            messages=tuple(items),
        ),
        tool_catalog=ToolCatalogSnapshot(version=catalog_version, tools=tool_tuple),
        capabilities=capabilities,
        model=model,
        provider=provider,
        token_counter=token_counter,
    )


def _failure(
    reason: ContextCompilationFailureReason,
    invocation: ModelInvocation,
    *,
    capacity_tokens: int,
    required_tokens: int,
) -> ContextCompilationResult:
    return ContextCompilationResult(
        failure=ContextCompilationFailure(
            reason=reason,
            session_id=invocation.session_id,
            session_revision=invocation.session_revision,
            turn_id=invocation.turn_id,
            capacity_tokens=capacity_tokens,
            required_tokens=required_tokens,
        )
    )


def _context_units(
    messages: Sequence[CompilationMessage],
) -> list[tuple[CompilationMessage, ...]]:
    """Group optional context into complete conversational turns.

    A user event begins a unit containing the assistant/tool exchange that
    follows it. Selecting only the assistant half of a prior turn would be a
    semantically corrupt history projection even when the wire format accepts
    it. System messages remain standalone request instructions.
    """
    units: list[tuple[CompilationMessage, ...]] = []
    current: list[CompilationMessage] = []
    for item in messages:
        role = item.message.get("role")
        if role == "system":
            if current:
                units.append(tuple(current))
                current = []
            units.append((item,))
            continue
        if role == "user" and current:
            units.append(tuple(current))
            current = []
        current.append(item)
    if current:
        units.append(tuple(current))
    return units


def _source_event_ids(items: Sequence[CompilationMessage]) -> tuple[str, ...]:
    return _ordered_unique(
        event_id
        for item in items
        for event_id in item.source_event_ids
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _contains_image_content(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, Mapping)
        and part.get("type") in {"image", "image_url", "input_image"}
        for part in content
    )


def compile_context(
    *,
    invocation: ModelInvocation,
    tool_catalog: ToolCatalogSnapshot,
    capabilities: ModelCapabilities,
    model: str,
    provider: str,
    token_counter: TokenCounter = conservative_json_token_count,
) -> ContextCompilationResult:
    """Compile one model invocation from canonical/projection candidates.

    Required messages include request-only instructions and every event in the
    active turn. Optional history is retained as a contiguous newest suffix,
    with assistant tool calls and their results selected atomically. This
    shape works for both the first call of a user turn and later calls whose
    current tail ends in tool results.
    """
    capacity = capabilities.context_window
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        return _failure(
            ContextCompilationFailureReason.CAPACITY_UNKNOWN,
            invocation,
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
            invocation,
            capacity_tokens=capacity,
            required_tokens=0,
        )
    if not invocation.messages or not any(item.required for item in invocation.messages):
        return _failure(
            ContextCompilationFailureReason.INVALID_CURRENT_TURN,
            invocation,
            capacity_tokens=capacity,
            required_tokens=0,
        )
    if tool_catalog.tools and not capabilities.supports_tools:
        return _failure(
            ContextCompilationFailureReason.UNSUPPORTED_REQUIRED_CONTENT,
            invocation,
            capacity_tokens=capacity,
            required_tokens=0,
        )
    if any(
        item.message.get("role") not in capabilities.supported_roles
        or (_contains_image_content(item.message) and not capabilities.supports_vision)
        for item in invocation.messages
        if item.required
    ):
        return _failure(
            ContextCompilationFailureReason.UNSUPPORTED_REQUIRED_CONTENT,
            invocation,
            capacity_tokens=capacity,
            required_tokens=0,
        )

    output_reserve = min(output_limit, capacity)
    required_items = tuple(item for item in invocation.messages if item.required)
    optional_items = tuple(item for item in invocation.messages if not item.required)
    instructions_tokens = sum(
        token_counter(item.message)
        for item in required_items
        if item.message.get("role") == "system"
    )
    current_tokens = sum(
        token_counter(item.message)
        for item in required_items
        if item.message.get("role") != "system"
    )
    tool_tokens = token_counter(tool_catalog.tools) if tool_catalog.tools else 0
    mandatory = (
        instructions_tokens
        + current_tokens
        + tool_tokens
        + fixed_overhead
        + output_reserve
    )
    if mandatory > capacity:
        return _failure(
            ContextCompilationFailureReason.MANDATORY_ENVELOPE_EXCEEDS_CAPACITY,
            invocation,
            capacity_tokens=capacity,
            required_tokens=mandatory,
        )

    remaining = capacity - mandatory
    units = _context_units(optional_items)
    selected_reversed: list[tuple[tuple[CompilationMessage, ...], int]] = []
    omitted_units: list[tuple[CompilationMessage, ...]] = []
    selecting = True
    for unit in reversed(units):
        unit_tokens = sum(token_counter(item.message) for item in unit)
        if selecting and unit_tokens <= remaining:
            selected_reversed.append((unit, unit_tokens))
            remaining -= unit_tokens
        else:
            selecting = False
            omitted_units.append(unit)

    selected = list(reversed(selected_reversed))
    history_source_ids = _source_event_ids(optional_items)
    if history_source_ids and not selected:
        newest_unit_tokens = sum(token_counter(item.message) for item in units[-1])
        return _failure(
            ContextCompilationFailureReason.HISTORY_CANNOT_FIT_WITHOUT_CHECKPOINT,
            invocation,
            capacity_tokens=capacity,
            required_tokens=mandatory + newest_unit_tokens,
        )

    selected_items = tuple(item for unit, _tokens in selected for item in unit)
    omitted_items = tuple(
        item
        for unit in reversed(omitted_units)
        for item in unit
    )
    selected_ids = set(_source_event_ids(selected_items))
    required_ids = set(_source_event_ids(required_items))
    retained_id_set = selected_ids | required_ids
    retained_event_ids = _ordered_unique(
        event_id
        for item in invocation.messages
        for event_id in item.source_event_ids
        if event_id in retained_id_set
    )
    omitted_id_set = set(_source_event_ids(omitted_items))
    omitted_event_ids = _ordered_unique(
        event_id
        for item in invocation.messages
        for event_id in item.source_event_ids
        if event_id in omitted_id_set
    )
    history_tokens = sum(tokens for _unit, tokens in selected)
    selected_item_ids = {id(item) for item in selected_items}
    compiled_items = tuple(
        item
        for item in invocation.messages
        if item.required or id(item) in selected_item_ids
    )
    messages = tuple(dict(item.message) for item in compiled_items)
    usage = ContextComponentUsage(
        instructions_tokens=instructions_tokens,
        history_tokens=history_tokens,
        current_input_tokens=current_tokens,
        tool_tokens=tool_tokens,
        fixed_overhead_tokens=fixed_overhead,
        output_reserve_tokens=output_reserve,
    )
    all_source_ids = _source_event_ids(invocation.messages)
    receipt = ContextReceipt(
        source_revision=invocation.session_revision,
        source_event_count=len(all_source_ids),
        retained_event_ids=retained_event_ids,
        omitted_event_ids=omitted_event_ids,
        tool_catalog_version=tool_catalog.version,
        selected_tool_count=len(tool_catalog.tools),
        usage=usage,
        estimator=getattr(token_counter, "__name__", type(token_counter).__name__),
    )
    fingerprint_payload = {
        "session_id": invocation.session_id,
        "revision": invocation.session_revision,
        "turn_id": invocation.turn_id,
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
            session_id=invocation.session_id,
            session_revision=invocation.session_revision,
            turn_id=invocation.turn_id,
            model=model,
            provider=provider,
            messages=messages,
            tools=tool_catalog.tools,
            capabilities=capabilities,
            receipt=receipt,
            context_fingerprint=fingerprint,
        )
    )


def compile_turn(
    *,
    snapshot: SessionSnapshot,
    command: TurnCommand,
    instructions: Sequence[Mapping[str, Any]],
    tool_catalog: ToolCatalogSnapshot,
    capabilities: ModelCapabilities,
    model: str,
    provider: str,
    current_event_id: str | None = None,
    token_counter: TokenCounter = conservative_json_token_count,
) -> ContextCompilationResult:
    """Compatibility wrapper for a user-turn append plus prior snapshot."""
    if command.session_id != snapshot.session_id:
        raise ValueError("command and snapshot session_id must match")
    if command.expected_revision != snapshot.revision:
        raise ValueError("command expected_revision must match snapshot revision")
    invocation = ModelInvocation(
        session_id=snapshot.session_id,
        session_revision=snapshot.revision,
        turn_id=command.turn_id,
        messages=(
            tuple(
                CompilationMessage(message=instruction, required=True)
                for instruction in instructions
            )
            + tuple(
                CompilationMessage(
                    message=event.message,
                    source_event_ids=(event.event_id,),
                )
                for event in snapshot.events
            )
            + (
                CompilationMessage(
                    message=command.user_event,
                    source_event_ids=(current_event_id,) if current_event_id else (),
                    required=True,
                ),
            )
        ),
    )
    return compile_context(
        invocation=invocation,
        tool_catalog=tool_catalog,
        capabilities=capabilities,
        model=model,
        provider=provider,
        token_counter=token_counter,
    )
