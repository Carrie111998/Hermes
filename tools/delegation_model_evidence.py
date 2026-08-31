"""Sanitized requested/resolved/actual model evidence for delegation."""

from __future__ import annotations

from typing import Any, Dict, Optional

_UNAVAILABLE = "not-reported"
_MAX_IDENTIFIER_CHARS = 256
_MAX_DIAGNOSTIC_CHARS = 1000
_SELECTION_SOURCES = {
    "task",
    "top_level",
    "delegation_config",
    "parent",
    "legacy-record",
}


def sanitize_model_identifier(value: Any) -> str:
    """Return a bounded identifier that cannot carry a credential token."""
    if not isinstance(value, str) or not value.strip():
        return _UNAVAILABLE
    normalized = " ".join(value.split())[:_MAX_IDENTIFIER_CHARS]
    try:
        from agent.redact import redact_sensitive_text

        redacted = redact_sensitive_text(normalized, force=True)
    except Exception:
        return "redacted"
    if redacted != normalized:
        return "redacted"
    return normalized


def sanitize_diagnostic(value: Any) -> str:
    """Force-redact and bound free-text evidence diagnostics."""
    text = str(value or "")[:_MAX_DIAGNOSTIC_CHARS]
    try:
        from agent.redact import redact_sensitive_text

        return (redact_sensitive_text(text, force=True) or "")[:_MAX_DIAGNOSTIC_CHARS]
    except Exception:
        return "[diagnostic withheld: redaction unavailable]"


def make_model_evidence(
    *,
    requested_provider: Optional[str],
    requested_model: Optional[str],
    resolved_provider: Optional[str],
    resolved_model: Optional[str],
    selection_source: str,
) -> Dict[str, Any]:
    """Build the dispatch record before any provider response exists."""
    source = str(selection_source or "parent").strip()[:64] or "parent"
    if source not in _SELECTION_SOURCES:
        source = "unknown"
    return {
        "selection_source": source,
        "requested": {
            "provider": sanitize_model_identifier(requested_provider),
            "model": sanitize_model_identifier(requested_model),
        },
        "resolved": {
            "provider": sanitize_model_identifier(resolved_provider),
            "model": sanitize_model_identifier(resolved_model),
        },
        "actual": {"provider": _UNAVAILABLE, "model": _UNAVAILABLE},
    }


def sanitize_model_evidence(evidence: Any) -> Dict[str, Any]:
    """Copy an evidence record through the persistence-boundary sanitizer."""
    raw = evidence if isinstance(evidence, dict) else {}
    requested = raw.get("requested") if isinstance(raw.get("requested"), dict) else {}
    resolved = raw.get("resolved") if isinstance(raw.get("resolved"), dict) else {}
    actual = raw.get("actual") if isinstance(raw.get("actual"), dict) else {}
    clean = make_model_evidence(
        requested_provider=requested.get("provider"),
        requested_model=requested.get("model"),
        resolved_provider=resolved.get("provider"),
        resolved_model=resolved.get("model"),
        selection_source=str(raw.get("selection_source") or "parent"),
    )
    clean["actual"] = {
        "provider": sanitize_model_identifier(actual.get("provider")),
        "model": sanitize_model_identifier(actual.get("model")),
    }
    _refresh_substitution(clean)
    return clean


def _explicit_response_value(response: Any, *keys: str) -> Optional[str]:
    """Read only provider-reported fields; never consult the configured route."""
    containers = [response]
    if not isinstance(response, dict):
        for attr in ("model_extra", "metadata"):
            extra = getattr(response, attr, None)
            if isinstance(extra, dict):
                containers.append(extra)
    else:
        for key in ("model_extra", "metadata"):
            extra = response.get(key)
            if isinstance(extra, dict):
                containers.append(extra)

    for container in containers:
        for key in keys:
            value = (
                container.get(key)
                if isinstance(container, dict)
                else getattr(container, key, None)
            )
            if isinstance(value, str) and value.strip():
                return value
    return None


def _refresh_substitution(evidence: Dict[str, Any]) -> None:
    resolved = evidence.get("resolved") or {}
    actual = evidence.get("actual") or {}
    substitution: Dict[str, Dict[str, str]] = {}
    for key in ("provider", "model"):
        before = resolved.get(key)
        after = actual.get(key)
        if (
            before not in (None, _UNAVAILABLE, "redacted")
            and after not in (None, _UNAVAILABLE, "redacted")
            and before != after
        ):
            substitution[key] = {"resolved": before, "actual": after}
    if substitution:
        evidence["substitution"] = substitution
    else:
        evidence.pop("substitution", None)


def record_actual_response(evidence: Dict[str, Any], response: Any) -> None:
    """Merge explicit runtime reporting into one evidence record in place."""
    if not isinstance(evidence, dict):
        return
    actual = evidence.setdefault(
        "actual", {"provider": _UNAVAILABLE, "model": _UNAVAILABLE}
    )
    provider = _explicit_response_value(response, "provider", "provider_name")
    model = _explicit_response_value(response, "model")
    if provider is not None:
        actual["provider"] = sanitize_model_identifier(provider)
    if model is not None:
        actual["model"] = sanitize_model_identifier(model)
    _refresh_substitution(evidence)


def format_model_evidence(evidence: Any) -> str:
    """Render evidence without ambiguous question-mark placeholders."""
    if not isinstance(evidence, dict):
        evidence = make_model_evidence(
            requested_provider=None,
            requested_model=None,
            resolved_provider=None,
            resolved_model=None,
            selection_source="parent",
        )
    requested = evidence.get("requested") or {}
    resolved = evidence.get("resolved") or {}
    actual = evidence.get("actual") or {}

    def pair(values: Dict[str, Any]) -> str:
        provider = sanitize_model_identifier(values.get("provider"))
        model = sanitize_model_identifier(values.get("model"))
        return f"{provider}/{model}"

    rendered = (
        f"Requested: {pair(requested)}; "
        f"Resolved: {pair(resolved)}; "
        f"Actual: {pair(actual)}; "
        f"Source: {str(evidence.get('selection_source') or 'parent')[:64]}"
    )
    substitution = evidence.get("substitution")
    if isinstance(substitution, dict) and substitution:
        parts = []
        for key in ("provider", "model"):
            change = substitution.get(key)
            if isinstance(change, dict):
                parts.append(
                    f"{key} {sanitize_model_identifier(change.get('resolved'))}"
                    f" -> {sanitize_model_identifier(change.get('actual'))}"
                )
        if parts:
            rendered += "; Substitution: " + ", ".join(parts)
    return rendered
