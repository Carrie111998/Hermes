"""Deterministic, bounded model-context projection for durable missions.

This module is a derived view only. P1 owns mission progression and P2 owns
action execution status; the compiler never writes either authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from agent.action_commit import ActionStatus
from agent.durable_mission import MissionCheckpoint, validate_checkpoint

CONTEXT_BUDGET_INSUFFICIENT = "CONTEXT_BUDGET_INSUFFICIENT"
_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|authorization|credential|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
_DEFAULT_CONTEXT_TOKENS = 32_000
_DEFAULT_HEADROOM_RATIO = 0.20
_MAX_RECENT_MESSAGES = 24
_MAX_WARM_ITEMS = 16
_MAX_FIELD_CHARS = 4096


class ContextCompilerError(RuntimeError):
    """Base compiler error."""


class ContextBudgetInsufficientError(ContextCompilerError):
    """Required HOT durable state cannot fit the configured budget."""


@dataclass(frozen=True)
class ContextMetrics:
    raw_transcript_tokens: int = 0
    compiled_context_tokens: int = 0
    hot_state_tokens: int = 0
    warm_state_tokens: int = 0
    recent_conversation_tokens: int = 0
    reserved_headroom: int = 0
    compression_count: int = 0
    compression_distance_turns: int | None = None


@dataclass(frozen=True)
class CompiledContext:
    machine_context: str
    messages: list[dict[str, Any]]
    metrics: ContextMetrics
    llm_calls: int = 0


def _estimate_tokens(value: Any) -> int:
    """Stable conservative estimate used for selection, not billing."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return max(1, (len(text) + 3) // 4)


def _safe_value(value: Any, key: str | None = None) -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v, str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            if re.search(r"(?:token|secret|password|authorization|api[_-]?key)\s*=", value, re.IGNORECASE):
                return "[REDACTED]"
            return value[:_MAX_FIELD_CHARS]
        return value
    return str(value)[:_MAX_FIELD_CHARS]


def _reference_label(reference: Mapping[str, Any] | None) -> str:
    if not reference:
        return "NONE"
    safe = _safe_value(reference)
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ContextCompiler:
    """Compile P1/P2 state and bounded conversational context deterministically."""

    def __init__(
        self,
        *,
        token_budget: int = _DEFAULT_CONTEXT_TOKENS,
        reserved_headroom: int | None = None,
        recent_message_limit: int = _MAX_RECENT_MESSAGES,
    ) -> None:
        if token_budget <= 0:
            raise ValueError("token_budget must be positive")
        self.token_budget = int(token_budget)
        self.reserved_headroom = (
            int(reserved_headroom)
            if reserved_headroom is not None
            else max(256, int(self.token_budget * _DEFAULT_HEADROOM_RATIO))
        )
        self.recent_message_limit = max(1, int(recent_message_limit))

    def _hot_projection(self, checkpoint: MissionCheckpoint, actions: Sequence[Any]) -> str:
        checkpoint = validate_checkpoint(checkpoint)
        lines = [
            "[DURABLE MISSION CONTEXT]",
            f"MISSION_ID: {checkpoint.mission_id}",
            f"OBJECTIVE: {checkpoint.objective}",
            f"STATUS: {checkpoint.status}",
            f"PHASE: {checkpoint.phase}",
            f"CURRENT_BLOCKER: {checkpoint.blocker or 'NONE'}",
            f"BLOCKING_UNKNOWN: {checkpoint.blocking_unknown or 'NONE'}",
            f"NEXT_ACTION: {checkpoint.next_action or 'NONE'}",
            f"COMPLETED_STEPS: {', '.join(checkpoint.completed_steps) or 'NONE'}",
            f"PENDING_STEPS: {', '.join(checkpoint.pending_steps) or 'NONE'}",
            f"FORBIDDEN_RETRIES: {', '.join(checkpoint.forbidden_retries) or 'NONE'}",
            "ACTION_LEDGER:",
        ]
        for item in sorted(actions, key=lambda action: str(getattr(action, "action_id", ""))):
            status = getattr(item, "status", "")
            status = getattr(status, "value", status)
            replay = getattr(item, "replay_class", "")
            replay = getattr(replay, "value", replay)
            unresolved = status in {ActionStatus.RUNNING.value, ActionStatus.UNKNOWN_OUTCOME.value, ActionStatus.VERIFY_REQUIRED.value}
            lines.append(
                f"- ACTION_ID: {getattr(item, 'action_id', '')} "
                f"TOOL: {getattr(item, 'tool_name', '')} ACTION_STATUS: {status} "
                f"REPLAY_CLASS: {replay} VERIFY_REQUIRED: {'true' if unresolved else 'false'}"
            )
        if not actions:
            lines.append("- NONE")
        lines.extend([
            "EXTERNAL_BINDINGS:",
            f"- CANONICAL_REPO: {checkpoint.canonical_repo or 'NONE'}",
            f"- REPO_OBSERVED_HEAD: {checkpoint.repo_observed_head or 'NONE'}",
            f"- CODEGRAPH_PROJECT: {checkpoint.codegraph_project or 'NONE'}",
            f"- CODEGRAPH_FINGERPRINT: {checkpoint.codegraph_fingerprint or 'NONE'}",
            f"- APPROVAL_REFERENCE: {_reference_label(checkpoint.approval_reference)}",
            f"- SAFETY_REFERENCE: {_reference_label(checkpoint.safety_reference)}",
            f"- FINANCIAL_REFERENCE: {_reference_label(checkpoint.financial_reference)}",
            f"- CONVERGENCE_REFERENCE: {_reference_label(checkpoint.convergence_reference)}",
            "CONSTRAINTS:",
            "- External authorities remain authoritative; references grant no authority.",
            "- P1 checkpoint owns mission progression.",
            "- P2 action ledger owns execution status.",
            "AUTHORITATIVE_RULE: Durable mission/action state overrides conversation memory. Continue from NEXT_ACTION unless blocked.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _warm_projection(evidence: Iterable[Any]) -> str:
        rows = []
        for item in list(evidence)[:_MAX_WARM_ITEMS]:
            if isinstance(item, Mapping):
                safe = _safe_value({k: item.get(k) for k in ("ref", "source", "timestamp", "summary") if item.get(k) is not None})
                if safe:
                    rows.append(json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            elif item:
                rows.append(str(_safe_value(str(item)))[:_MAX_FIELD_CHARS])
        return "[WARM EVIDENCE REFERENCES]\n" + "\n".join(f"- {row}" for row in rows) if rows else ""

    def _select_recent(self, messages: Sequence[Mapping[str, Any]], budget: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        used = 0
        for message in reversed(list(messages)):
            if len(selected) >= self.recent_message_limit:
                break
            candidate = dict(message)
            cost = _estimate_tokens(candidate)
            if used + cost > budget:
                continue
            selected.append(candidate)
            used += cost
        return list(reversed(selected))

    def compile(
        self,
        *,
        checkpoint: MissionCheckpoint | None = None,
        actions: Sequence[Any] = (),
        evidence: Sequence[Any] = (),
        additional_context: str = "",
        messages: Sequence[Mapping[str, Any]] = (),
        token_budget: int | None = None,
        reserved_headroom: int | None = None,
        model_context_window: int | None = None,
        compression_count: int = 0,
        compression_distance_turns: int | None = None,
    ) -> CompiledContext:
        budget = int(model_context_window or token_budget or self.token_budget)
        headroom = int(self.reserved_headroom if reserved_headroom is None else reserved_headroom)
        available = budget - headroom
        raw_tokens = sum(_estimate_tokens(message) for message in messages)
        if checkpoint is None:
            return CompiledContext(
                machine_context="",
                messages=[dict(message) for message in messages],
                metrics=ContextMetrics(raw_transcript_tokens=raw_tokens, reserved_headroom=headroom),
            )
        if available <= 0:
            raise ContextBudgetInsufficientError(CONTEXT_BUDGET_INSUFFICIENT)

        hot = self._hot_projection(checkpoint, actions)
        hot_tokens = _estimate_tokens(hot)
        if hot_tokens > available:
            raise ContextBudgetInsufficientError(CONTEXT_BUDGET_INSUFFICIENT)

        remaining = available - hot_tokens
        warm_items = list(evidence)
        if additional_context:
            warm_items.append({"ref": "plugin-context", "source": "pre_llm_call", "summary": additional_context})
        warm = self._warm_projection(warm_items)
        warm_tokens = _estimate_tokens(warm) if warm else 0
        if warm and warm_tokens <= remaining:
            remaining -= warm_tokens
        else:
            warm = ""
            warm_tokens = 0
        recent = self._select_recent(messages, remaining)
        recent_tokens = sum(_estimate_tokens(message) for message in recent)
        context = hot + (f"\n\n{warm}" if warm else "")
        compiled_tokens = hot_tokens + warm_tokens + recent_tokens
        metrics = ContextMetrics(
            raw_transcript_tokens=raw_tokens,
            compiled_context_tokens=compiled_tokens,
            hot_state_tokens=hot_tokens,
            warm_state_tokens=warm_tokens,
            recent_conversation_tokens=recent_tokens,
            reserved_headroom=headroom,
            compression_count=int(compression_count),
            compression_distance_turns=compression_distance_turns,
        )
        return CompiledContext(machine_context=context, messages=recent, metrics=metrics)
