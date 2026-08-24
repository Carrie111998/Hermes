"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

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

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
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


def get_configured_default_route(
    config: dict[str, Any] | None,
    *,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the configured primary route in fallback-entry form."""

    config = config or {}
    runtime = runtime or {}
    model_cfg = config.get("model", {})
    if isinstance(model_cfg, str):
        model = model_cfg.strip()
        model_route: dict[str, Any] = {}
    elif isinstance(model_cfg, dict):
        raw_default = model_cfg.get("default") or model_cfg.get("model")
        if isinstance(raw_default, dict):
            model_route = raw_default
            model = str(
                raw_default.get("model")
                or raw_default.get("name")
                or raw_default.get("default")
                or ""
            ).strip()
        else:
            model_route = {}
            model = str(raw_default or "").strip()
    else:
        return None

    runtime_model = str(runtime.get("model") or "").strip()
    if runtime_model:
        # Gateway auth recovery returns the selected fallback model alongside
        # its runtime. That provider describes the fallback route, not a
        # provider-less configured default. This remains true when both routes
        # use the same model ID, so combining them would invent a route that
        # was never configured.
        runtime = {}

    provider = str(
        model_route.get("provider")
        or (model_cfg.get("provider") if isinstance(model_cfg, dict) else "")
        or runtime.get("requested_provider")
        or runtime.get("provider")
        or ""
    ).strip()
    if not model or not provider:
        return None

    route: dict[str, Any] = {"provider": provider, "model": model}
    base_url = _normalized_base_url(
        model_route.get("base_url")
        or (model_cfg.get("base_url") if isinstance(model_cfg, dict) else "")
        or runtime.get("base_url")
    )
    if base_url:
        route["base_url"] = base_url
    api_mode = str(
        model_route.get("api_mode")
        or (model_cfg.get("api_mode") if isinstance(model_cfg, dict) else "")
        or runtime.get("api_mode")
        or ""
    ).strip()
    if api_mode:
        route["api_mode"] = api_mode
    return route


def compose_fallback_chain(
    chain: Any,
    *,
    primary: dict[str, Any] | None,
    configured_default: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Seat the configured default behind an overridden primary.

    Routes are de-duplicated by provider/model/base URL. The active primary is
    omitted, while a distinct configured default precedes configured fallbacks.
    """

    primary_entries = _iter_fallback_entries(primary)
    primary_identity = _entry_identity(primary_entries[0]) if primary_entries else None
    candidates = [*_iter_fallback_entries(configured_default), *_iter_fallback_entries(chain)]
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in candidates:
        identity = _entry_identity(entry)
        if identity == primary_identity or identity in seen:
            continue
        seen.add(identity)
        result.append(entry)
    return result


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
