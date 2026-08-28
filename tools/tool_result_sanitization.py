"""Safe serialization and redaction for non-model tool-result sinks.

This boundary is intentionally stricter than model-facing result handling.  It
accepts the result shapes that tools commonly return, converts them to a
bounded textual representation, and applies structural plus exact-value
redaction before the value can be logged, persisted, or sent to a hook.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from agent.redact import redact_sensitive_text
from agent.secret_scope import current_secret_scope


# A vendor prefix is not a security boundary.  This catches opaque, credential-
# shaped values used by integrations and tests while leaving ordinary prose
# alone.  Keyed values are handled structurally below even when short.
_OPAQUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9_-]{16,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9_-]*[0-9])"
    r"(?=[A-Za-z0-9_-]*[A-Z])"
    r"[A-Za-z][A-Za-z0-9_-]{15,}"
    r"(?![A-Za-z0-9])"
)
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"auth(?:entication)?|secret|password|passwd|credential|token|private[_-]?key)",
    re.IGNORECASE,
)
_REDACTED = "«redacted-secret»"
_OPAQUE_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])opaque[-_][A-Za-z0-9_-]{6,}(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk[-_]|ghp_|github_pat_|xox[baprs]-|pplx-|AIza)[^\s\"'<>]+"
)


def _as_text(value: Any) -> str:
    """Serialize result values without invoking arbitrary object repr code."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        return str(value)
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
        except (TypeError, ValueError, RecursionError):
            return repr(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            return json.dumps(list(value), ensure_ascii=False, sort_keys=True, default=_json_default)
        except (TypeError, ValueError, RecursionError):
            return repr(value)
    return str(value)


def _json_default(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return repr(value)


def _mask_opaque_tokens(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.casefold().startswith("opaque") or len(token) >= 24:
            return _REDACTED
        return token

    return _OPAQUE_TOKEN_RE.sub(replace, text)


def _mask_structured_secret_values(value: Any, *, key: str | None = None) -> Any:
    """Recursively replace secret-keyed leaves before JSON serialization."""
    if key is not None and _SECRET_KEY_RE.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(k): _mask_structured_secret_values(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_mask_structured_secret_values(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def sanitize_tool_result_for_sink(content: Any) -> str:
    """Return a textual, non-reusable representation safe for every sink.

    The function is total for ordinary Python result values.  JSON/config
    secret fields are masked recursively, exact active redaction patterns are
    applied with URL credentials enabled, and opaque credential-shaped values
    are removed without depending on a vendor prefix.  This function must be
    called at each sink boundary; it is not used to alter model-facing raw
    context.
    """
    structured = _mask_structured_secret_values(content)
    text = _as_text(structured)
    text = redact_sensitive_text(
        text,
        force=True,
        redact_url_credentials=True,
    )
    text = redact_sensitive_text(
        text,
        force=True,
        file_read=True,
        redact_url_credentials=True,
    )
    # The opaque pass is deliberately after the standard redactor: standard
    # redaction can expose a previously nested value when it rewrites syntax.
    scope = current_secret_scope() or {}
    secrets = {secret for secret in scope.values() if isinstance(secret, str)}
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    text = _KNOWN_TOKEN_RE.sub(_REDACTED, text)
    text = _OPAQUE_MARKER_RE.sub(_REDACTED, text)
    return _mask_opaque_tokens(text)
