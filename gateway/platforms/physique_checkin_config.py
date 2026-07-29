"""Validated exact-address configuration for the private physique wizard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PhysiqueCheckinConfig:
    """Explicit security boundary required before the wizard can exist."""

    owner_id: str
    chat_id: str
    topic_id: str
    expires_seconds: int
    allow_anonymous_sender_chat: bool
    coaching_feedback_enabled: bool

    @classmethod
    def from_extra(cls, extra: object) -> PhysiqueCheckinConfig | None:
        """Parse the profile feature flag only when every supplied boundary agrees."""
        if not isinstance(extra, dict):
            return None
        raw = extra.get("physique_checkin")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return None
        owner = str(raw.get("owner_id", "")).strip()
        chat = str(raw.get("chat_id", "")).strip()
        topic = str(raw.get("topic_id", "")).strip()
        if not owner or not chat or not topic:
            return None
        if raw.get("allowed_chats") is not None and raw["allowed_chats"] != [chat]:
            return None
        if raw.get("allowed_topics") is not None and raw["allowed_topics"] != [topic]:
            return None
        if raw.get("require_mention", False) is not False:
            return None
        try:
            expiry = int(raw.get("expires_seconds", 1_800))
        except (TypeError, ValueError):
            return None
        if not 60 <= expiry <= 86_400:
            return None
        return cls(
            owner,
            chat,
            topic,
            expiry,
            raw.get("allow_anonymous_sender_chat") is True,
            raw.get("coaching_feedback_enabled") is True,
        )
