"""Authoritative, fail-closed Telegram topic skill policy."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Iterable


class SkillPolicyStatus(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED_VALID = "CONFIGURED_VALID"
    CONFIGURED_INVALID = "CONFIGURED_INVALID"
    RESOLUTION_ERROR = "RESOLUTION_ERROR"


@dataclass(frozen=True)
class EnabledSkillsPolicy:
    status: SkillPolicyStatus
    identities: tuple[str, ...] = ()
    fingerprint: str = "legacy"
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.status is not SkillPolicyStatus.UNCONFIGURED

    def permits(self, identity: str) -> bool:
        return (
            self.status is SkillPolicyStatus.CONFIGURED_VALID
            and identity in self.identities
        )


def canonical_skill_identity(value: object) -> str:
    value = str(value or "").strip()
    if not value or "/" in value or ":" in value or "\\" in value:
        raise ValueError(f"unsupported enabled_skills identity: {value!r}")
    from agent.skill_utils import normalize_skill_lookup_name

    identity = normalize_skill_lookup_name(value)
    if not identity or identity != value:
        raise ValueError(
            f"enabled_skills must use canonical frontmatter names: {value!r}"
        )
    return identity


def _failure(status: SkillPolicyStatus, error: str) -> EnabledSkillsPolicy:
    return EnabledSkillsPolicy(
        status=status,
        error=error,
        fingerprint="invalid:" + sha256(error.encode()).hexdigest(),
    )


def _topic_extra(config: dict) -> dict:
    typed = ((config.get("platforms") or {}).get("telegram") or {}).get("extra") or {}
    legacy = (config.get("telegram") or {}).get("extra") or {}
    return typed if "group_topics" in typed else legacy


def resolve_enabled_skills_policy(
    source,
    config: dict | None,
    *,
    skill_names: Iterable[str] | None = None,
) -> EnabledSkillsPolicy:
    try:
        platform = getattr(
            getattr(source, "platform", None), "value", getattr(source, "platform", "")
        )
        if (
            str(platform).lower() != "telegram"
            or not getattr(source, "chat_id", None)
            or not getattr(source, "thread_id", None)
        ):
            return EnabledSkillsPolicy(SkillPolicyStatus.UNCONFIGURED)
        if not isinstance(config, dict):
            return _failure(
                SkillPolicyStatus.RESOLUTION_ERROR,
                "gateway configuration could not be read",
            )
        from gateway.platforms.base import resolve_group_topic

        topic = resolve_group_topic(
            _topic_extra(config), str(source.chat_id), str(source.thread_id)
        )
        if topic is None or "enabled_skills" not in topic:
            return EnabledSkillsPolicy(SkillPolicyStatus.UNCONFIGURED)
        raw = topic.get("enabled_skills")
        if not isinstance(raw, list) or not raw:
            return _failure(
                SkillPolicyStatus.CONFIGURED_INVALID,
                "enabled_skills must be a non-empty list",
            )
        if not all(isinstance(item, str) for item in raw):
            return _failure(
                SkillPolicyStatus.CONFIGURED_INVALID,
                "enabled_skills entries must be strings",
            )
        identities = [canonical_skill_identity(item) for item in raw]
        if len(set(identities)) != len(identities):
            return _failure(
                SkillPolicyStatus.CONFIGURED_INVALID,
                "enabled_skills contains duplicate canonical identities",
            )
        if skill_names is not None:
            known = {canonical_skill_identity(name) for name in skill_names}
            unknown = sorted(set(identities) - known)
            if unknown:
                return _failure(
                    SkillPolicyStatus.CONFIGURED_INVALID,
                    "unknown enabled_skills: " + ", ".join(unknown),
                )
        canonical = str(topic.get("thread_id", ""))
        material = "|".join([canonical, *sorted(identities), *sorted(skill_names or ())])
        return EnabledSkillsPolicy(
            SkillPolicyStatus.CONFIGURED_VALID,
            tuple(identities),
            sha256(material.encode()).hexdigest(),
        )
    except Exception as exc:
        return _failure(
            SkillPolicyStatus.RESOLUTION_ERROR,
            f"could not resolve enabled_skills: {exc}",
        )
