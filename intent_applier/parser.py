"""Parse `*_INTENT_*.json` messages from the tracker mailbox inbox.

Spec: docs/superpowers/specs/2026-04-25-tracker-intent-applier-design.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_INTENT_TYPES = ("APPROVAL_INTENT", "STATE_TRANSITION_INTENT")


@dataclass(frozen=True)
class IntentMessage:
    """Parsed view of a tracker intent message file."""
    message_id: str
    idempotency_key: str
    intent_type: str               # "APPROVAL_INTENT" | "STATE_TRANSITION_INTENT"
    job_id: str
    requested_stage: str
    actor_id: str
    source: str
    notes: str
    metadata: dict[str, Any]
    path: Path                     # original file path on disk


class IntentParseError(Exception):
    """Raised when an intent file cannot be parsed into an IntentMessage."""


def parse_intent_file(path: Path) -> IntentMessage:
    """Read and parse an intent JSON file. Raises IntentParseError on any issue."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntentParseError(f"could not read intent file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"intent file {path} contains invalid JSON: {exc}") from exc

    def require(d: dict, key: str, ctx: str = "") -> Any:
        if key not in d:
            ctx_str = f" in {ctx}" if ctx else ""
            raise IntentParseError(f"intent file {path} missing required field '{key}'{ctx_str}")
        return d[key]

    message_id = require(data, "message_id")
    idempotency_key = require(data, "idempotency_key")
    intent_type = require(data, "type")
    if intent_type not in VALID_INTENT_TYPES:
        raise IntentParseError(
            f"intent file {path} has unknown intent_type {intent_type!r}; "
            f"expected one of {VALID_INTENT_TYPES}"
        )
    job_id = require(data, "job_id")
    payload = require(data, "payload")
    if not isinstance(payload, dict):
        raise IntentParseError(f"intent file {path} payload must be a dict, got {type(payload).__name__}")

    requested_stage = require(payload, "requested_stage", ctx="payload")
    actor_id = payload.get("actor_id") or "main"
    source = payload.get("source") or "operator_api"
    notes = payload.get("notes") or ""
    metadata = payload.get("metadata") or {}

    return IntentMessage(
        message_id=message_id,
        idempotency_key=idempotency_key,
        intent_type=intent_type,
        job_id=job_id,
        requested_stage=requested_stage,
        actor_id=actor_id,
        source=source,
        notes=notes,
        metadata=metadata if isinstance(metadata, dict) else {},
        path=path,
    )
