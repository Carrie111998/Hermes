"""Redact untrusted observation data before it becomes evidence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from plugins.agentops.control.observer_models import RawSignal, Signal, stable_signal_id, thaw_value


_SENSITIVE_NAME = r"(?:api[_-]?key|token|cookie|password|secret|authorization|credential|session)"
_SENSITIVE_KEY = re.compile(_SENSITIVE_NAME, re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._-]{1,}\b|\bgh[pousr]_[A-Za-z0-9]{8,}\b|\b(?:Authorization|Cookie)\s*[:=]\s*[^\s,;}]+)",
    re.I,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?P<prefix>(?:(?:\"|')?{_SENSITIVE_NAME}(?:\"|')?)\s*[:=]\s*)"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,;}]+)",
    re.I,
)
_REDACTED = "[REDACTED]"


class RedactionError(ValueError):
    """Raised without including the untrusted input that violated the gate."""


@dataclass(frozen=True)
class RedactionPolicy:
    version: int = 2
    replacement: str = _REDACTED

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("invalid redaction policy")
        if not isinstance(self.replacement, str) or not self.replacement:
            raise ValueError("invalid redaction replacement")


DEFAULT_POLICY = RedactionPolicy()


def redact_text(value: str, policy: RedactionPolicy = DEFAULT_POLICY) -> str:
    if not isinstance(value, str):
        raise RedactionError("invalid text value")
    # Assignment redaction runs first so ``Authorization: Bearer token`` does
    # not leave the token tail behind after the Bearer marker is replaced.
    value = _SENSITIVE_ASSIGNMENT.sub(lambda match: match.group("prefix") + policy.replacement, value)
    return _SENSITIVE_VALUE.sub(policy.replacement, value)


def redact_value(value: Any, policy: RedactionPolicy = DEFAULT_POLICY) -> Any:
    """Recursively remove secret fields and values from JSON-compatible data."""
    if isinstance(value, str):
        return redact_text(value, policy)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise RedactionError("invalid mapping key")
            safe_key = "redacted_field" if _SENSITIVE_KEY.search(key) else key
            safe_child = policy.replacement if _SENSITIVE_KEY.search(key) else redact_value(child, policy)
            if safe_key in result and result[safe_key] != safe_child:
                safe_key = f"{safe_key}_{len(result)}"
            result[safe_key] = safe_child
        return result
    if isinstance(value, (list, tuple)):
        return [redact_value(child, policy) for child in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return policy.replacement


def redact_log_line(line: str, policy: RedactionPolicy = DEFAULT_POLICY) -> dict[str, Any]:
    """Prefer a recursively redacted JSON record, otherwise return safe text."""
    if not isinstance(line, str):
        raise RedactionError("invalid log line")
    try:
        decoded = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return {"message": redact_text(line, policy)}
    if isinstance(decoded, (Mapping, list, tuple)):
        return {"record": redact_value(decoded, policy)}
    return {"message": redact_text(line, policy)}


def contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        assignment = _SENSITIVE_ASSIGNMENT.search(value)
        assignment_has_secret = assignment is not None and assignment.group("value").strip("\"'") != _REDACTED
        return bool(_SENSITIVE_VALUE.search(value) or assignment_has_secret)
    if isinstance(value, Mapping):
        return any(
            (not isinstance(key, str)) or _SENSITIVE_KEY.search(key) is not None or contains_secret(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(child) for child in value)
    return False


def redact_signal(signal: RawSignal, policy: RedactionPolicy = DEFAULT_POLICY) -> Signal:
    if not isinstance(signal, RawSignal):
        raise RedactionError("invalid raw signal")
    payload = redact_value(thaw_value(signal.payload), policy)
    if contains_secret(payload):
        raise RedactionError("redaction gate failed")
    return Signal(
        signal_id=stable_signal_id(
            target_id=signal.target_id,
            collector=signal.collector,
            signal_type=signal.signal_type,
            payload=payload,
        ),
        target_id=signal.target_id,
        collector=signal.collector,
        signal_type=signal.signal_type,
        observed_at=signal.observed_at,
        payload=payload,
        severity=signal.severity,
        redaction_version=policy.version,
    )


def verify_redacted_signal(signal: Signal) -> None:
    """Final gate called before an observer-store or Bridge accepts evidence."""
    if not isinstance(signal, Signal) or contains_secret(signal.to_dict()):
        raise RedactionError("redaction gate failed")
