"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import os
from typing import Any


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


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def _parse_invocation_fallbacks(overrides: Any) -> list[dict[str, Any]] | None:
    """Parse repeatable ``PROVIDER/MODEL`` invocation overrides.

    ``None`` means no CLI override and preserves the profile chain. Any supplied
    list replaces the primary agent's configured fallback chain exactly (and is
    inherited by subagents); malformed or duplicate routes fail closed so
    automation cannot silently run with a different fallback order.
    """

    if overrides is None:
        return None
    if isinstance(overrides, str):
        values = [overrides]
    elif isinstance(overrides, (list, tuple)):
        values = list(overrides)
    else:
        raise ValueError("fallback overrides must be PROVIDER/MODEL strings")

    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in values:
        value = str(raw or "").strip()
        provider, separator, model = value.partition("/")
        provider = provider.strip()
        model = model.strip()
        if not separator or not provider or not model:
            raise ValueError(
                f"invalid fallback override {value!r}; expected PROVIDER/MODEL"
            )
        entry = {"provider": provider, "model": model}
        identity = _entry_identity(entry)
        if identity in seen:
            raise ValueError(f"duplicate fallback override: {provider}/{model}")
        seen.add(identity)
        chain.append(entry)
    return chain


def get_fallback_chain(
    config: dict[str, Any] | None,
    invocation_overrides: Any = None,
) -> list[dict[str, Any]]:
    """Return the effective fallback chain.

    Repeatable invocation overrides replace the primary agent's configured
    fallback chain in exact order, and inherited subagents see the same chain.
    Without overrides, ``fallback_providers`` remains the primary source of
    truth and keeps its order; legacy ``fallback_model`` entries are appended
    unless they target an existing provider/model/base_url route. Returned
    entries are fresh dict copies.
    """

    override_chain = _parse_invocation_fallbacks(invocation_overrides)
    if override_chain is not None:
        return override_chain

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
