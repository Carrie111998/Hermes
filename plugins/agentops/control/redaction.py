"""Redact untrusted observation data before it becomes evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from plugins.agentops.control.observer_models import RawSignal, Signal, stable_signal_id


_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|cookie|password|secret|authorization|credential|session)", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._-]{8,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{8,}\b|"
    r"(?:api[_-]?key|token|cookie|session|secret|authorization)\s*[=:]\s*[^\s;]{4,})",
    re.I,
)
_REDACTED = "[REDACTED]"


class RedactionError(ValueError):
    """Raised without including the untrusted input that violated the gate."""


@dataclass(frozen=True)
class RedactionPolicy:
    version: int = 1
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
    return _SENSITIVE_VALUE.sub(policy.replacement, value)


def redact_value(value: Any, policy: RedactionPolicy = DEFAULT_POLICY) -> Any:
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


def contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SENSITIVE_VALUE.search(value))
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
    payload = redact_value(signal.payload, policy)
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
