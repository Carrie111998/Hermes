from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from .models import (
    AuditEventType,
    ExecutionAuditEvent,
)


def _canonicalize(value: Any) -> Any:
    if is_dataclass(value):
        return _canonicalize(asdict(value))

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_canonicalize(item) for item in value]

    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def deterministic_identifier(
    prefix: str,
    *components: Any,
    length: int = 24,
) -> str:
    clean_prefix = prefix.strip().lower().replace("_", "-")
    if not clean_prefix:
        raise ValueError("prefix must not be empty")
    if length <= 0:
        raise ValueError("length must be positive")

    digest = hashlib.sha256(
        canonical_json(components).encode("utf-8")
    ).hexdigest()

    return f"{clean_prefix}-{digest[:length]}"


def build_audit_event(
    *,
    event_type: AuditEventType,
    occurred_at: str,
    message: str,
    source_references: tuple[str, ...] = (),
    evidence_references: tuple[str, ...] = (),
    identity_components: tuple[Any, ...] = (),
) -> ExecutionAuditEvent:
    event_id = deterministic_identifier(
        "execution-event",
        event_type,
        occurred_at,
        message,
        source_references,
        evidence_references,
        identity_components,
    )

    return ExecutionAuditEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        message=message,
        source_references=source_references,
        evidence_references=evidence_references,
    )


def audit_event_identity(
    event: ExecutionAuditEvent,
) -> str:
    return deterministic_identifier(
        "execution-event-identity",
        event,
    )
