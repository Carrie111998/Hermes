"""Optional bounded inference routed through the active Hermes session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import EvidenceRecord, MemoryEvent, MemoryRecord
from .security import assert_safe_value, scan_for_secrets


RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked_ids": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ranked_ids", "uncertainties"],
}

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "memory_type": {"type": "string"},
            "scope": {"type": "string"},
            "confidence": {"type": "number"},
            "verification": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "source_session_id": {"type": "string"},
            "task_id": {"type": "string"},
            "project_id": {"type": "string"},
            "mission_id": {"type": "string"},
            "agent_id": {"type": "string"},
        }}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["candidates", "uncertainties"],
}


@dataclass(frozen=True)
class InferenceResult:
    status: str
    parsed: dict[str, Any] | None = None
    deferred: bool = False
    reason: str = ""


class MemoryInference:
    def __init__(self, llm):
        self._llm = llm

    def _complete(self, *, instructions: str, input_text: str, schema: dict, purpose: str) -> InferenceResult:
        if self._llm is None:
            return InferenceResult("deferred", deferred=True, reason="no active session llm")
        if scan_for_secrets(input_text).matches or scan_for_secrets(instructions).matches:
            return InferenceResult("deferred", deferred=True, reason="secret in inference input")
        try:
            result = self._llm.complete_structured(
                instructions=instructions,
                input=[{"type": "text", "text": input_text[:4000]}],
                json_schema=schema,
                json_mode=True,
                temperature=0.0,
                max_tokens=256,
                purpose=purpose,
                fallback_policy="same_provider_only",
            )
        except Exception as exc:
            return InferenceResult("deferred", deferred=True, reason=type(exc).__name__)
        parsed = result.parsed if isinstance(result.parsed, dict) else None
        if parsed is None:
            return InferenceResult("deferred", deferred=True, reason="invalid structured output")
        return InferenceResult("complete", parsed=parsed)

    def rerank(self, query: str, candidates: list[MemoryRecord]) -> InferenceResult:
        input_text = "query: " + query + "\n" + "\n".join(
            f"{record.memory_id}: {record.content[:300]}" for record in candidates[:12]
        )
        return self._complete(
            instructions="Rank the supplied memory IDs for the query. Do not invent IDs.",
            input_text=input_text,
            schema=RERANK_SCHEMA,
            purpose="memory-duo.rerank",
        )

    def extract_candidates(self, event: MemoryEvent) -> InferenceResult:
        result = self._complete(
            instructions="Extract only durable memory candidates from this event; never extract credentials.",
            input_text=event.content[:4000],
            schema=_CANDIDATE_SCHEMA,
            purpose="memory-duo.extract-candidates",
        )
        return self._scan_candidate_output(result)

    def consolidate(self, events: list[MemoryEvent], evidence: list[EvidenceRecord]) -> InferenceResult:
        text = "events:\n" + "\n".join(event.content[:300] for event in events[:12])
        text += "\nevidence:\n" + "\n".join(item.content[:300] for item in evidence[:12])
        result = self._complete(
            instructions="Consolidate only supported durable candidates; preserve uncertainty and conflicts.",
            input_text=text,
            schema=_CANDIDATE_SCHEMA,
            purpose="memory-duo.consolidate",
        )
        return self._scan_candidate_output(result)

    @staticmethod
    def _scan_candidate_output(result: InferenceResult) -> InferenceResult:
        if result.parsed is None:
            return result
        for candidate in result.parsed.get("candidates", []):
            content = str(candidate.get("content", "")) if isinstance(candidate, dict) else ""
            if scan_for_secrets(content).matches:
                return InferenceResult("deferred", deferred=True, reason="secret in inference output")
            try:
                assert_safe_value(candidate)
            except ValueError:
                return InferenceResult("deferred", deferred=True, reason="secret in inference output")
        return result
