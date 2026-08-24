"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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


def _normalize_aux_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize raw ``auxiliary.<task>.fallback_chain`` entries.

    The per-task chain format mirrors the top-level one, but the aux
    resolvers historically accepted looser shapes. Normalize to the exact
    contract ``agent_init`` expects for ``fallback_model`` list entries
    (non-empty string ``provider`` + non-empty ``model``, optional
    ``base_url`` / ``api_mode`` / ``key_env``), dropping invalid entries
    instead of failing the whole chain.
    """
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
        # Parity with the call_llm fallback path (_resolve_fallback_entry,
        # auxiliary_client.py): entries may declare the wire format as
        # ``transport``, but the conversation-loop failover consumer
        # (_try_activate_fallback) reads only ``api_mode`` — alias it so
        # both paths honor either spelling.
        if not str(normalized.get("api_mode") or "").strip():
            transport = str(entry.get("transport") or "").strip()
            if transport:
                normalized["api_mode"] = transport
        entries.append(normalized)
    return entries


def _dedupe_aux_by_backend_identity(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop later entries that resolve to an earlier backend identity."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            entry["provider"].lower(),
            entry["model"].lower(),
            str(entry.get("base_url") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def resolve_aux_task_fallback_chain(
    task: str,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Effective fallback chain for an ``AIAgent``-fork auxiliary review task.

    Review forks spawned by background_review (#93592) and the curator pass
    (#78371) run through ``run_conversation()``, whose failover consults only
    the constructor-provided chain. This builds it: per-task
    ``auxiliary.<task>.fallback_chain`` entries first, then the top-level
    chain (:func:`get_fallback_chain`) as the last-resort safety net —
    mirroring how ``call_llm``-based aux tasks layer their chains.
    Duplicates across both sources are dropped by backend identity
    (provider+model+base_url). Returns fresh dicts; safe against a shared
    cached config object.

    Plugin-registered task default layering (``_get_auxiliary_task_config``
    in agent/auxiliary_client.py) is intentionally not applied: both
    consumers are built-in tasks, and importing the auxiliary client from
    this low-level module would add a heavy import edge for no behavioral
    change.
    """
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("aux fallback-chain read failed (%s); empty chain", exc)
            return []

    aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    task_cfg = aux.get(task, {}) if isinstance(aux.get(task), dict) else {}
    chain: list[dict[str, Any]] = []
    chain.extend(_normalize_aux_fallback_entries(task_cfg.get("fallback_chain")))
    try:
        chain.extend(get_fallback_chain(cfg))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("top-level fallback chain read failed (%s)", exc)

    return _dedupe_aux_by_backend_identity(chain)
