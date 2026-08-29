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


# A vendor prefix is not a security boundary.  Sink text uses a conservative
# token policy: mixed alphanumeric values and long opaque words are treated as
# reusable material even when they have no recognizable vendor prefix.  This is
# intentionally stricter than model-facing output handling.
_OPAQUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z][A-Za-z0-9_-]{7,}"
    r"(?![A-Za-z0-9])"
)
_URL_QUERY_VALUE_RE = re.compile(r"([?&][^\s&#=]+=)([^\s&#]*)")
_MAX_SAFE_NESTING = 32
_UNSUPPORTED = "«redacted-unsupported-value»"
_CIRCULAR = "«redacted-circular-value»"
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


def _safe_type_name(value: Any) -> str:
    """Return a type label without invoking user-defined conversion hooks."""
    try:
        name = type(value).__name__
    except Exception:
        return "object"
    return name if isinstance(name, str) and name else "object"


def sanitize_exception_for_sink(error: BaseException) -> str:
    """Return safe exception diagnostics without exposing exception text.

    Exception messages can contain tool output, credentials, URLs, or request
    bodies, and logging's ``exc_info`` would reintroduce that text even when
    the log message itself was sanitized. Preserve only stable diagnostic
    classes and bounded numeric status fields so operators retain useful
    failure classification without receiving attacker-controlled bytes.
    """
    classes: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(classes) < 8:
        seen.add(id(current))
        classes.append(_safe_type_name(current))
        try:
            cause = current.__cause__
            if cause is None and not current.__suppress_context__:
                cause = current.__context__
        except Exception:
            cause = None
        current = cause

    if not classes:
        classes.append(_safe_type_name(error))
    diagnostics = ["exception=" + " <- ".join(classes)]
    for attr in ("errno", "status_code", "code"):
        try:
            value = getattr(error, attr, None)
        except Exception:
            value = None
        if isinstance(value, int) and not isinstance(value, bool):
            diagnostics.append(f"{attr}={value}")
    return " ".join(diagnostics)


def _safe_key(value: Any) -> str:
    """Convert a mapping key without allowing hostile ``__str__`` to escape."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        try:
            return str(value)
        except Exception:
            pass
    return f"<{_safe_type_name(value)}>"


def _safe_value(
    value: Any,
    *,
    key: str | None = None,
    seen: set[int] | None = None,
    depth: int = 0,
) -> Any:
    """Build a JSON-compatible value without calling hostile repr/str hooks."""
    if key is not None and _SECRET_KEY_RE.search(key):
        return _REDACTED
    if depth > _MAX_SAFE_NESTING:
        return _UNSUPPORTED
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return _UNSUPPORTED
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _UNSUPPORTED

    if seen is None:
        seen = set()
    value_id = id(value)
    if isinstance(value, Mapping):
        if value_id in seen:
            return _CIRCULAR
        seen.add(value_id)
        try:
            items = list(value.items())
        except Exception:
            seen.discard(value_id)
            return _UNSUPPORTED
        result = {}
        try:
            for raw_key, raw_value in items:
                safe_key = _safe_key(raw_key)
                result[safe_key] = _safe_value(
                    raw_value, key=safe_key, seen=seen, depth=depth + 1
                )
        except Exception:
            return _UNSUPPORTED
        finally:
            seen.discard(value_id)
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        if value_id in seen:
            return _CIRCULAR
        seen.add(value_id)
        try:
            return [
                _safe_value(item, seen=seen, depth=depth + 1)
                for item in value
            ]
        except Exception:
            return _UNSUPPORTED
        finally:
            seen.discard(value_id)

    return _UNSUPPORTED


def _as_text(value: Any) -> str:
    """Serialize a pre-sanitized value without invoking arbitrary repr code."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except Exception:
        return _UNSUPPORTED


def _json_default(value: Any) -> str:
    """Compatibility hook that never invokes an object's repr implementation."""
    return _UNSUPPORTED


def _mask_opaque_tokens(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.casefold() in {
            "redacted-secret",
            "redacted-unsupported-value",
            "redacted-circular-value",
        }:
            return token
        # Mixed tokens, separator-bearing credential ids, and long opaque
        # words are treated as reusable.  Avoid masking ordinary repeated
        # output such as a large ``xxxxx...`` body.
        diverse = len(set(token.casefold())) >= 4
        if (
            any(char.isdigit() for char in token)
            or ("_" in token or "-" in token) and len(token) >= 16 and diverse
            or len(token) >= 24 and diverse
        ):
            return _REDACTED
        return token

    return _OPAQUE_TOKEN_RE.sub(replace, text)


def _mask_structured_secret_values(value: Any, *, key: str | None = None) -> Any:
    """Recursively produce a total, JSON-compatible sink representation."""
    return _safe_value(value, key=key)


def _sanitize_text_for_sink(text: str) -> str:
    """Apply the textual sink policy after values are structurally bounded."""
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
    # Query values are untrusted tool output.  Preserve the URL shape while
    # removing state, signed values, and unknown credential parameters instead
    # of maintaining an unsafe allowlist of parameter names.
    text = _URL_QUERY_VALUE_RE.sub(
        lambda match: match.group(1) + (_REDACTED if match.group(2) else ""),
        text,
    )
    try:
        scope = current_secret_scope() or {}
    except Exception:
        scope = {}
    secrets = {secret for secret in scope.values() if isinstance(secret, str)}
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    text = _KNOWN_TOKEN_RE.sub(_REDACTED, text)
    text = _OPAQUE_MARKER_RE.sub(_REDACTED, text)
    return _mask_opaque_tokens(text)


def sanitize_tool_result_projection_for_sink(content: Any) -> Any:
    """Return a JSON-compatible structured projection safe for extension sinks.

    Unlike :func:`sanitize_tool_result_for_sink`, this preserves mapping and
    sequence shape for callbacks that need structured arguments.  Every string
    leaf and key still crosses the same textual redaction boundary, while
    unsupported, cyclic, non-finite, and excessively nested values are replaced
    by inert markers before a plugin/UI callback can observe them.
    """
    value = _mask_structured_secret_values(content)
    if isinstance(value, str):
        return _sanitize_text_for_sink(value)
    if isinstance(value, dict):
        return {
            _sanitize_text_for_sink(key) if isinstance(key, str) else key:
            sanitize_tool_result_projection_for_sink(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_tool_result_projection_for_sink(item) for item in value]
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
    return _sanitize_text_for_sink(_as_text(structured))
