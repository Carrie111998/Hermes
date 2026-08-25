"""Shared helpers for copying a session's non-secret model runtime.

Session forks cross several frontends (CLI, TUI, ACP, gateway, and the HTTP
API).  Keeping the allowlist here prevents those paths from copying API keys
while preserving the route that owns the copied conversation history.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


NON_SECRET_SESSION_RUNTIME_KEYS = (
    "model",
    "requested_provider",
    "provider",
    "base_url",
    "api_mode",
    "responses_transport",
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _model_config(source: Mapping[str, Any]) -> dict[str, Any]:
    raw = source.get("model_config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return _mapping(raw)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def copy_non_secret_session_runtime(
    source: Any,
    existing: Mapping[str, Any] | None = None,
    *,
    include_model: bool = True,
    clear_missing: bool = False,
) -> dict[str, Any]:
    """Merge a runtime snapshot from *source* into model/session metadata.

    ``source`` may be an agent-like object, a runtime mapping, or a persisted
    session row.  Persisted rows may carry route fields at model_config's top
    level or under ``gateway_runtime``.  Only the allowlisted, non-secret
    fields are copied; callers can safely seed ``existing`` with reasoning,
    service-tier, or lineage metadata.

    ``requested_provider`` and the resolved ``provider`` are retained
    separately.  Newer builders can honor the requested identity (important
    for named custom providers), while older readers continue to consume the
    compatible ``provider`` field.
    """

    result = dict(existing or {})
    direct = _mapping(source)
    config = _model_config(direct) if direct else {}
    gateway_runtime = _mapping(config.get("gateway_runtime"))

    def value(key: str) -> str:
        candidates: list[Any]
        if direct:
            candidates = (direct.get(key), gateway_runtime.get(key), config.get(key))
            if key == "provider":
                candidates += (direct.get("billing_provider"),)
            elif key == "base_url":
                candidates += (direct.get("billing_base_url"),)
        else:
            candidates = [getattr(source, key, None)]
        for candidate in candidates:
            if cleaned := _clean_text(candidate):
                return cleaned
        return ""

    keys = NON_SECRET_SESSION_RUNTIME_KEYS
    if not include_model:
        keys = tuple(key for key in keys if key != "model")
    for key in keys:
        if cleaned := value(key):
            result[key] = cleaned
        elif clear_missing and key != "model":
            result.pop(key, None)

    return result
