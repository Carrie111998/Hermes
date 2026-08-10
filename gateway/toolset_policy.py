"""Authoritative, fail-closed Telegram topic toolset policy."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Iterable


class ToolsetPolicyStatus(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED_VALID = "CONFIGURED_VALID"
    CONFIGURED_INVALID = "CONFIGURED_INVALID"
    RESOLUTION_ERROR = "RESOLUTION_ERROR"


@dataclass(frozen=True)
class EnabledToolsetsPolicy:
    status: ToolsetPolicyStatus
    toolsets: tuple[str, ...] = ()
    resolved_topic_id: str = ""
    fingerprint: str = "legacy"
    error: str | None = None


def _failure(status: ToolsetPolicyStatus, error: str) -> EnabledToolsetsPolicy:
    return EnabledToolsetsPolicy(
        status=status,
        error=error,
        fingerprint="invalid:" + sha256(error.encode()).hexdigest(),
    )


def _topic_extra(config: dict) -> dict:
    typed = ((config.get("platforms") or {}).get("telegram") or {}).get("extra") or {}
    legacy = (config.get("telegram") or {}).get("extra") or {}
    return typed if "group_topics" in typed else legacy


def resolve_enabled_toolsets_policy(
    source,
    config: dict | None,
    *,
    known_toolsets: Iterable[str] | None = None,
) -> EnabledToolsetsPolicy:
    try:
        platform = str(
            getattr(
                getattr(source, "platform", None),
                "value",
                getattr(source, "platform", ""),
            )
        ).lower()
        if (
            platform != "telegram"
            or not getattr(source, "chat_id", None)
            or not getattr(source, "thread_id", None)
        ):
            return EnabledToolsetsPolicy(ToolsetPolicyStatus.UNCONFIGURED)
        if not isinstance(config, dict):
            return _failure(
                ToolsetPolicyStatus.RESOLUTION_ERROR,
                "gateway configuration could not be read",
            )
        from gateway.platforms.base import resolve_group_topic

        topic = resolve_group_topic(
            _topic_extra(config), str(source.chat_id), str(source.thread_id)
        )
        if topic is None or "enabled_toolsets" not in topic:
            return EnabledToolsetsPolicy(ToolsetPolicyStatus.UNCONFIGURED)
        raw = topic["enabled_toolsets"]
        if not isinstance(raw, list):
            return _failure(
                ToolsetPolicyStatus.CONFIGURED_INVALID,
                "enabled_toolsets must be a list",
            )
        if not all(isinstance(item, str) and item and item.strip() == item for item in raw):
            return _failure(
                ToolsetPolicyStatus.CONFIGURED_INVALID,
                "enabled_toolsets entries must be non-empty canonical strings",
            )
        if len(set(raw)) != len(raw):
            return _failure(
                ToolsetPolicyStatus.CONFIGURED_INVALID,
                "enabled_toolsets contains duplicate canonical names",
            )
        from toolsets import get_toolset_names

        known = set(known_toolsets if known_toolsets is not None else get_toolset_names())
        unknown = sorted(set(raw) - known)
        if unknown:
            return _failure(
                ToolsetPolicyStatus.CONFIGURED_INVALID,
                "unknown enabled_toolsets: " + ", ".join(unknown),
            )
        disabled = set(((config.get("agent") or {}).get("disabled_toolsets") or []))
        conflicts = sorted(set(raw) & disabled)
        if conflicts:
            return _failure(
                ToolsetPolicyStatus.CONFIGURED_INVALID,
                "enabled_toolsets conflict with hard-disabled toolsets: "
                + ", ".join(conflicts),
            )
        canonical = str(topic.get("thread_id", ""))
        try:
            from tools.registry import registry

            generation = str(getattr(registry, "_generation", 0))
        except Exception:
            generation = "0"
        material = "|".join(
            [
                platform,
                str(source.chat_id),
                str(source.thread_id),
                canonical,
                *sorted(raw),
                *sorted(disabled),
                generation,
            ]
        )
        return EnabledToolsetsPolicy(
            ToolsetPolicyStatus.CONFIGURED_VALID,
            tuple(raw),
            canonical,
            sha256(material.encode()).hexdigest(),
        )
    except Exception as exc:
        return _failure(
            ToolsetPolicyStatus.RESOLUTION_ERROR,
            "could not resolve enabled_toolsets: " + str(exc),
        )
