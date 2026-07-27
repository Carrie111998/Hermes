"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import os
from typing import Any


_FALLBACK_ENTRY_KEYS = {
    "provider",
    "model",
    "base_url",
    "api_key",
    "key_env",
    "api_key_env",
    "api_mode",
    "transport",
    "context_length",
    "max_output_tokens",
    "max_tokens",
    "extra_body",
    "extra_headers",
    "timeout",
    "request_timeout_seconds",
    "model_transition_policy",
}


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        return os.getenv(key_env, "").strip() or None
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_fallback_entry(entry: Any) -> dict[str, Any] | None:
    """Validate one fallback route and discard unsupported route-level keys.

    Provider-specific request fields belong under ``extra_body``.  Keeping a
    small allowlist here prevents an arbitrary config key from becoming an SDK
    constructor or request kwarg when the route is activated.
    """
    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not provider or not model:
        return None

    normalized = {key: entry[key] for key in _FALLBACK_ENTRY_KEYS if key in entry}
    normalized["provider"] = provider
    normalized["model"] = model

    base_url = _normalized_base_url(entry.get("base_url"))
    if base_url:
        normalized["base_url"] = base_url
    else:
        normalized.pop("base_url", None)

    for key in ("api_key", "key_env", "api_key_env"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
        else:
            normalized.pop(key, None)

    api_mode = entry.get("api_mode") or entry.get("transport")
    if api_mode is not None:
        from hermes_cli.runtime_provider import _parse_api_mode

        parsed_api_mode = _parse_api_mode(api_mode)
    else:
        parsed_api_mode = None
    if parsed_api_mode:
        normalized["api_mode"] = parsed_api_mode
    else:
        normalized.pop("api_mode", None)
    normalized.pop("transport", None)

    for key in ("context_length", "max_output_tokens", "max_tokens"):
        if key in normalized:
            parsed = _positive_int(normalized[key])
            if parsed is None:
                normalized.pop(key, None)
            else:
                normalized[key] = parsed

    for key in ("timeout", "request_timeout_seconds"):
        if key in normalized:
            parsed = _positive_float(normalized[key])
            if parsed is None:
                normalized.pop(key, None)
            else:
                normalized[key] = parsed

    if "extra_body" in normalized and not isinstance(normalized["extra_body"], dict):
        normalized.pop("extra_body", None)
    elif "extra_body" in normalized:
        normalized["extra_body"] = dict(normalized["extra_body"])

    if "extra_headers" in normalized:
        from hermes_cli.config import normalize_extra_headers

        route_headers = normalize_extra_headers(normalized["extra_headers"])
        if route_headers:
            normalized["extra_headers"] = route_headers
        else:
            normalized.pop("extra_headers", None)

    policy = str(normalized.get("model_transition_policy") or "").strip().lower()
    if policy == "sequential":
        normalized["model_transition_policy"] = policy
    else:
        normalized.pop("model_transition_policy", None)

    return normalized


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        normalized = normalize_fallback_entry(entry)
        if normalized is not None:
            entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain


def resolve_fallback_runtime(entry: dict[str, Any]) -> dict[str, Any]:
    """Resolve one fallback through the canonical runtime-provider pipeline.

    Named custom-provider defaults are retained, then explicit route fields
    override them.  The returned dict is the complete target runtime used by
    the main agent and auxiliary callers.
    """
    normalized = normalize_fallback_entry(entry)
    if normalized is None:
        raise ValueError("fallback entry requires non-empty provider and model")

    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    explicit_api_key = resolve_entry_api_key(normalized)
    try:
        runtime = dict(
            resolve_runtime_provider(
                requested=normalized["provider"],
                explicit_api_key=explicit_api_key,
                explicit_base_url=normalized.get("base_url"),
                target_model=normalized["model"],
            )
        )
    except AuthError:
        # Preserve legacy fallback routing when the central resolver cannot
        # obtain credentials up front. The downstream client router performs
        # its own provider-specific auth lookup and returns no client when that
        # also fails. Validation and secret-scope errors are not caught here.
        runtime = {
            "provider": normalized["provider"],
            "requested_provider": normalized["provider"],
            "model": normalized["model"],
        }
        if normalized.get("base_url"):
            runtime["base_url"] = normalized["base_url"]
        if explicit_api_key:
            runtime["api_key"] = explicit_api_key
        if normalized.get("api_mode"):
            runtime["api_mode"] = normalized["api_mode"]
    runtime["model"] = normalized["model"]
    if normalized.get("api_mode"):
        runtime["api_mode"] = normalized["api_mode"]

    for key in ("context_length", "model_transition_policy"):
        if key in normalized:
            runtime[key] = normalized[key]

    route_max = normalized.get("max_output_tokens") or normalized.get("max_tokens")
    if route_max is not None:
        runtime["max_output_tokens"] = route_max

    route_timeout = normalized.get("request_timeout_seconds")
    if route_timeout is None:
        route_timeout = normalized.get("timeout")
    if route_timeout is not None:
        runtime["request_timeout_seconds"] = route_timeout

    route_body = normalized.get("extra_body")
    if isinstance(route_body, dict):
        overrides = runtime.get("request_overrides")
        overrides = dict(overrides) if isinstance(overrides, dict) else {}
        inherited_body = overrides.get("extra_body")
        merged_body = dict(inherited_body) if isinstance(inherited_body, dict) else {}
        merged_body.update(route_body)
        overrides["extra_body"] = merged_body
        runtime["request_overrides"] = overrides

    if isinstance(normalized.get("extra_headers"), dict):
        inherited_headers = runtime.get("extra_headers")
        merged_headers = dict(inherited_headers) if isinstance(inherited_headers, dict) else {}
        merged_headers.update(normalized["extra_headers"])
        runtime["extra_headers"] = merged_headers

    return runtime
