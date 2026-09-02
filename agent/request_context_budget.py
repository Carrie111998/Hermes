"""Request-local context budgets and safe token-based history selection.

This module deliberately does not mutate the durable transcript.  It computes
what may be sent for one provider request and returns a selected history copy.
Durable summarization remains owned by ``ContextCompressor``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence


BudgetConfidence = Literal["exact", "tokenizer", "rough"]
_VALID_CONFIDENCE = frozenset({"exact", "tokenizer", "rough"})


@dataclass(frozen=True)
class RequestContextBudget:
    """Token allocation for one provider request.

    ``history_budget_tokens`` is derived after reserving output capacity and
    fixed request components.  It is never negative: an oversized system/tool
    payload leaves zero room for history and the caller can retain only pinned
    active-turn messages.
    """

    context_window_tokens: int
    reserved_output_tokens: int
    system_prompt_tokens: int
    tool_schema_tokens: int
    confidence: BudgetConfidence

    def __post_init__(self) -> None:
        for name in (
            "context_window_tokens",
            "reserved_output_tokens",
            "system_prompt_tokens",
            "tool_schema_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.confidence not in _VALID_CONFIDENCE:
            raise ValueError("confidence must be one of: exact, tokenizer, rough")

    @property
    def safe_input_budget_tokens(self) -> int:
        """Maximum prompt input after reserving output capacity."""
        return max(0, self.context_window_tokens - self.reserved_output_tokens)

    @property
    def fixed_input_tokens(self) -> int:
        """Input tokens consumed outside conversation history."""
        return self.system_prompt_tokens + self.tool_schema_tokens

    @property
    def history_budget_tokens(self) -> int:
        """Pure history allowance after all fixed request components."""
        return max(0, self.safe_input_budget_tokens - self.fixed_input_tokens)


@dataclass(frozen=True)
class TokenBudgetedHistorySelection:
    """Request-only result of history-tail selection."""

    messages: list[dict[str, Any]]
    history_budget_tokens: int
    selected_tokens: int
    pinned_tokens: int
    omitted_message_count: int


def build_request_context_budget(
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
    system_prompt_tokens: int,
    tool_schema_tokens: int,
    confidence: BudgetConfidence = "rough",
) -> RequestContextBudget:
    """Build a budget from already-measured request components."""
    return RequestContextBudget(
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        system_prompt_tokens=system_prompt_tokens,
        tool_schema_tokens=tool_schema_tokens,
        confidence=confidence,
    )


def _rough_message_tokens(message: Mapping[str, Any]) -> int:
    """Conservative local fallback for selection when no tokenizer is available."""
    try:
        serialized = json.dumps(message, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = str(message)
    return max(1, (len(serialized) + 3) // 4)


def _latest_user_start(messages: Sequence[Mapping[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    # A malformed/resumed transcript with no user message is kept intact.  The
    # standard request sanitizer remains responsible for repairing it.
    return 0


def _previous_group_start(messages: Sequence[Mapping[str, Any]], end: int) -> int:
    """Return the start of the immediately preceding complete conversation turn.

    A turn starts at a user message and includes every following assistant/tool
    activity until the next user message. This keeps ordinary assistant replies
    coherent with the request that prompted them, and also makes every
    ``assistant(tool_calls) + tool results`` block indivisible. Malformed
    history with no preceding user is kept as one conservative group for the
    standard request sanitizer to repair later.
    """
    for start in range(end - 1, -1, -1):
        if messages[start].get("role") == "user":
            return start
    return 0


def select_token_budgeted_history_tail(
    messages: Sequence[Mapping[str, Any]],
    *,
    history_budget_tokens: int,
    estimate_message_tokens: Callable[[Mapping[str, Any]], int] = _rough_message_tokens,
) -> TokenBudgetedHistorySelection:
    """Select the newest safe history tail without mutating ``messages``.

    The latest user turn and every following message are pinned even if they
    alone exceed budget.  Earlier groups are added only when the whole group
    fits, so oversized old tool output is omitted first and can use the existing
    spillover/preview path rather than breaking tool-call/result pairing.
    """
    if (
        isinstance(history_budget_tokens, bool)
        or not isinstance(history_budget_tokens, int)
        or history_budget_tokens < 0
    ):
        raise ValueError("history_budget_tokens must be a non-negative integer")
    copied = [dict(message) for message in messages]
    if not copied:
        return TokenBudgetedHistorySelection([], history_budget_tokens, 0, 0, 0)

    pinned_start = _latest_user_start(copied)
    pinned_tokens = sum(max(0, int(estimate_message_tokens(message))) for message in copied[pinned_start:])
    selected_start = pinned_start
    selected_tokens = pinned_tokens

    while selected_start > 0:
        group_start = _previous_group_start(copied, selected_start)
        group_tokens = sum(
            max(0, int(estimate_message_tokens(message)))
            for message in copied[group_start:selected_start]
        )
        if selected_tokens + group_tokens > history_budget_tokens:
            break
        selected_start = group_start
        selected_tokens += group_tokens

    return TokenBudgetedHistorySelection(
        messages=copied[selected_start:],
        history_budget_tokens=history_budget_tokens,
        selected_tokens=selected_tokens,
        pinned_tokens=pinned_tokens,
        omitted_message_count=selected_start,
    )


def select_request_context_window(
    request_messages: Sequence[Mapping[str, Any]],
    *,
    fixed_prefix_count: int,
    budget: RequestContextBudget,
    estimate_message_tokens: Callable[[Mapping[str, Any]], int] = _rough_message_tokens,
) -> TokenBudgetedHistorySelection:
    """Select a request-only history tail while preserving fixed prompt prefix.

    ``fixed_prefix_count`` is for system and ephemeral prefill messages that
    are not durable conversation history.  They stay verbatim; only the
    remaining history consumes ``budget.history_budget_tokens``.
    """
    if fixed_prefix_count < 0 or fixed_prefix_count > len(request_messages):
        raise ValueError("fixed_prefix_count must identify a request prefix")
    prefix = [dict(message) for message in request_messages[:fixed_prefix_count]]
    tail = select_token_budgeted_history_tail(
        request_messages[fixed_prefix_count:],
        history_budget_tokens=budget.history_budget_tokens,
        estimate_message_tokens=estimate_message_tokens,
    )
    return TokenBudgetedHistorySelection(
        messages=prefix + tail.messages,
        history_budget_tokens=tail.history_budget_tokens,
        selected_tokens=tail.selected_tokens,
        pinned_tokens=tail.pinned_tokens,
        omitted_message_count=tail.omitted_message_count,
    )
