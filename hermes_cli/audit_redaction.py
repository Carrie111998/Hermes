"""Deterministic redaction for durable governance evidence."""

from __future__ import annotations

import re
from typing import Any, Mapping


_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "access_key", "client_secret", "credential",
    "credentials", "password", "passphrase", "private_key", "secret",
    "seed_phrase", "signing_secret", "token", "webhook_secret", "pan", "cvv",
})
_SENSITIVE_TEXT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?key|client[_-]?secret|token|secret|"
    r"password|private[_-]?key|authorization)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,\s}\]]+)"
)


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(f"_{suffix}") for suffix in _SENSITIVE_KEYS
    )


def sanitize(value: Any) -> Any:
    """Return JSON-safe evidence with credential-like fields redacted."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _sensitive_key(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def sanitize_text(value: str) -> str:
    """Redact credential-like key/value fragments in malformed text."""
    return _SENSITIVE_TEXT.sub(r"\1[REDACTED]", value)
