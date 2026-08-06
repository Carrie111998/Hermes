"""FIX-009: Typed Envelopes fuer Hermes-Decision-Trace.

Drei Pydantic-Schemas, die in Telemetrie, Run-Trace, Audit-Reports und
Replay-Tools genutzt werden. Der Agent emittiert ``IntentEnvelope``
pro erkanntem Intent, ``DecisionRecord`` pro getroffener Genehmigungs-
oder Risk-Entscheidung, und ``ToolEnvelope`` pro Tool-Aufruf.

Schemata koennen ueber ``agent.telemetry_envelopes.emit_*`` oder direkt
instanziiert werden. Persistenz erfolgt ueber die bestehende
``run_journal``-Infrastruktur oder als JSONL nach
``~/.hermes/logs/envelopes.jsonl``.

Vor FIX-009 waren Entscheidungen als unstrukturierte ``dict``-Payloads
im Run-Journal abgelegt, was Replay, Audit und Replays von
Tool-Aufrufen verhindert hat. Mit den hier definierten Schemata ist
jeder Eintrag stark typisiert und validiert.

Version 2026-07-27.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _now_iso() -> str:
    """UTC ISO-8601 mit Mikrosekunden."""
    return datetime.now(tz=timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    """UUID4 mit Prefix fuer einfachere visuelle Identifikation."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class IntentEnvelope(BaseModel):
    """FIX-009: Reprasentiert einen erkannten Intent pro Hermes-Run.

    Wird vom Intent-Classifier emittiert (FIX-009 + FIX-013) und
    dient als Grundlage fuer ``DecisionRecord`` und Tool-Approval.
    """
    intent_id: str = Field(default_factory=lambda: _new_id("intent"))
    created_at: str = Field(default_factory=_now_iso)
    intent_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    detected_by: Literal["intent_classifier", "user_explicit", "plugin_hint", "fallback"] = "intent_classifier"
    rationale: str = Field(default="", max_length=2000)
    allowed_tools: List[str] = Field(default_factory=list)
    denied_tools: List[str] = Field(default_factory=list)
    cost_budget_usd: Optional[float] = Field(default=None, ge=0.0)
    token_budget: Optional[int] = Field(default=None, ge=0)
    duration_budget_s: Optional[int] = Field(default=None, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("intent_name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("intent_name must be non-empty")
        return v.strip().lower()


class DecisionRecord(BaseModel):
    """FIX-009: Jede Hermes-Approval-/Risk-/Planungs-Entscheidung.

    Wird von der Approval-Pipeline (``tools/approval.py``), der
    Subagent-Matrix (FIX-004), der Tirith-Schicht (FIX-002) und der
    Plugin-Conformance (FIX-001) emittiert. Das Schema erlaubt
    Replay-Audits: jeder Eintrag dokumentiert Eingabe, Begruendung,
    Actor, Result und Hashes.
    """
    decision_id: str = Field(default_factory=lambda: _new_id("dec"))
    created_at: str = Field(default_factory=_now_iso)
    intent_id: Optional[str] = Field(default=None, description="Reference zu IntentEnvelope.intent_id")
    actor: str = Field(..., description="Wer hat entschieden, z. B. user, subagent_matrix, tirith")
    decision_type: Literal["approval", "denial", "deny_unsafe_default", "risk_class", "policy_override"]
    rationale: str = Field(default="", max_length=2000)
    evidence_ids: List[str] = Field(default_factory=list, description="Source-IDs (z. B. plugin_name, tool_name)")
    risk_class: Optional[Literal["read_only", "workspace_write", "privileged", "external_write", "financial", "destructive", "regulated"]] = None
    result: Literal["allow", "deny", "ask", "quarantine", "fail_closed"]
    previous_decision_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolEnvelope(BaseModel):
    """FIX-009: Wrapper um jeden Tool-Aufruf.

    Wird vom Tool-Gateway emit um Tool-Eingabe, -Ausgabe, Dauer,
    Risk-Class und Tool-Manifest-Version festzuhalten. Dient als
    primäre Quelle fuer:
        * Tools-Replay
        * Audit-Reports (FIX-024)
        * Cost-Tracking
        * Tool-Manifest-Conformance
    """
    tool_call_id: str = Field(default_factory=lambda: _new_id("tc"))
    created_at: str = Field(default_factory=_now_iso)
    intent_id: Optional[str] = None
    tool_name: str
    side_effect_class: Literal[
        "read_only", "workspace_write", "privileged", "external_write", "financial", "destructive", "regulated"
    ] = "read_only"
    required_approval: Literal["never", "when_workspace", "when_external", "always"] = "never"
    timeout_ms: int = Field(default=10000, ge=0)
    retry: int = Field(default=0, ge=0, le=3)
    least_privilege_scope: Literal[
        "workspace_only", "workspace_inbox", "read_only", "full_system"
    ] = "read_only"
    arguments_hash: str = Field(..., description="SHA256 of canonicalised JSON args")
    arguments_preview: str = Field(default="", max_length=500, description="Truncated or redacted preview")
    result_summary: str = Field(default="", max_length=2000)
    result_hash: Optional[str] = Field(default=None, description="SHA256 of full result if captured")
    duration_ms: Optional[int] = Field(default=None, ge=0)
    approved_by: Optional[str] = None
    decision_id: Optional[str] = Field(default=None, description="Ref zu DecisionRecord.decision_id")
    manifest_version: str = Field(default="2026-07-27")
    manifest_sha256: Optional[str] = None
    error: Optional[str] = None
    # FIX-007: Evidence-Manifest - Source-ID -> Zitat/Beleg.
    # Erlaubt dem Tool-Gateway, fuer jeden Tool-Call die zugrunde
    # liegenden Quellen festzuhalten, damit Audit-Reports nicht nur
    # "Tool X wurde gerufen" wissen, sondern auch "auf Basis welcher
    # Quelle". Optional, weil alte Caller kein Manifest fuehren.
    evidence_manifest: Dict[str, str] = Field(
        default_factory=dict,
        description="FIX-007: source_id -> quote/beleg (z. B. 'doc.md#L12' -> 'sensitive command')",
    )

    @field_validator("tool_name")
    @classmethod
    def _toolname_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("tool_name must be non-empty")
        return v.strip()


# FIX-007 ----------------------------------------------------------------

def attach_evidence(
    envelope: ToolEnvelope, source_id: str, quote: str
) -> ToolEnvelope:
    """Fuegt einen Source-Beleg zum ``evidence_manifest`` einer Envelope hinzu.

    Quelle: in-place Mutation, damit der Aufrufer die Envelope danach
    ohne Neuzuweisung weiterreichen kann (z. B. an ``emit``). Wenn die
    ``source_id`` bereits existiert, wird der Eintrag ueberschrieben
    (Audit-Quellen sind unveraenderlich, latest-write-wins ist die
    explizite Konvention).

    Args:
        envelope: Die zu befuellende ``ToolEnvelope``.
        source_id: Stabile ID der Quelle (z. B. ``"release/tool-manifest.yaml#L42"``).
        quote: Kurzer Beleg-String (max. 500 Zeichen).

    Returns:
        Die gleiche ``envelope`` (fuer Chaining).
    """
    if not source_id or not source_id.strip():
        raise ValueError("source_id must be non-empty")
    if not quote:
        raise ValueError("quote must be non-empty")
    # Quotes hart kappen, damit das Manifest nicht aufgeblasen wird.
    if len(quote) > 500:
        quote = quote[:497] + "..."
    envelope.evidence_manifest[source_id.strip()] = quote
    return envelope


# Public API ------------------------------------------------------------

__all__ = [
    "IntentEnvelope",
    "DecisionRecord",
    "ToolEnvelope",
    "now_iso",
    "new_id",
    "emit",
    "attach_evidence",
]


def now_iso() -> str:
    """Re-export fuer externe Caller."""
    return _now_iso()


def new_id(prefix: str) -> str:
    """Re-export."""
    return _new_id(prefix)


def emit(envelope: BaseModel, path: str | None = None) -> None:
    """Schreibt eine Envelope als JSONL-Zeile.

    Wenn ``path`` None ist, wird ``~/.hermes/logs/envelopes.jsonl``
    verwendet. Persistenz ist non-blocking: ein fehlgeschlagener
    Write loggt eine Warnung, raise-t aber nicht.
    """
    import json
    import logging
    import os
    target = path or os.path.expanduser("~/.hermes/logs/envelopes.jsonl")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(envelope.model_dump_json() + "\n")
    except Exception as exc:  # pragma: no cover - non-fatal
        logging.getLogger("hermes.telemetry_envelopes").warning(
            "FIX-009 emit failed for %s: %s", target, exc
        )