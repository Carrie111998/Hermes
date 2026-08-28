"""Canonical configuration and target identity for the outbound safety policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_POLICY_KEYS = ("outbound-message-gate", "outbound_message_gate")


def normalize_outbound_target(platform: str, chat_id: str | None = None) -> str:
    """Lowercase the platform while preserving the opaque recipient id."""
    if chat_id is None:
        raw = str(platform or "").strip()
        if ":" not in raw:
            raise ValueError("protected target must be '<platform>:<chat_id>'")
        platform, chat_id = raw.split(":", 1)
    platform_name = str(platform or "").strip().lower()
    recipient = str(chat_id or "").strip()
    if not platform_name or not recipient:
        raise ValueError("protected target must include platform and chat id")
    return f"{platform_name}:{recipient}"


def outbound_policy_settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return one validated policy settings mapping across both historical keys."""
    if config is None:
        try:
            from hermes_cli.config import load_config, read_user_config_raw

            # ``load_config`` intentionally degrades malformed YAML to ``{}``.
            # Validate the raw file first so a security policy cannot disappear
            # behind that general-purpose fail-open read behavior.
            read_user_config_raw()
            config = load_config()
        except Exception as exc:
            raise RuntimeError(f"outbound policy config unavailable: {exc}") from exc
    if not isinstance(config, Mapping):
        raise RuntimeError("outbound policy config unavailable: root is not a mapping")
    plugins = config.get("plugins") or {}
    entries = plugins.get("entries") if isinstance(plugins, Mapping) else None
    if entries is None:
        return {}
    if not isinstance(entries, Mapping):
        raise RuntimeError("outbound policy config unavailable: plugins.entries is not a mapping")
    candidates: list[dict[str, Any]] = []
    for key in _POLICY_KEYS:
        entry = entries.get(key)
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            raise RuntimeError(f"outbound policy config unavailable: {key} entry is not a mapping")
        settings = entry.get("settings", entry.get("config", {})) or {}
        if not isinstance(settings, Mapping):
            raise RuntimeError(f"outbound policy config unavailable: {key} settings is not a mapping")
        candidates.append(dict(settings))
    if len(candidates) > 1 and candidates[0] != candidates[1]:
        raise RuntimeError("outbound policy config unavailable: conflicting policy aliases")
    settings = candidates[0] if candidates else {}
    targets = settings.get("protected_targets", [])
    if not isinstance(targets, list):
        raise RuntimeError("outbound policy config unavailable: protected_targets is not a list")
    try:
        settings["protected_targets"] = [normalize_outbound_target(item) for item in targets]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"outbound policy config unavailable: {exc}") from exc
    return settings


def outbound_policy_required(platform: str, chat_id: str) -> bool:
    settings = outbound_policy_settings()
    return normalize_outbound_target(platform, chat_id) in set(
        settings.get("protected_targets", [])
    )
