"""Profile-gated configuration for multi-customer nutrition coaching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


JsonValue = object


@dataclass(frozen=True, slots=True)
class AdaptiveReviewOperator:
    """The exact Telegram triple used to authenticate review ingress."""

    user_id: str
    chat_id: str
    topic_id: int
    version: int

    def __post_init__(self) -> None:
        user_id = str(self.user_id or "").strip()
        chat_id = str(self.chat_id or "").strip()
        if not user_id or not chat_id or type(self.topic_id) is not int or self.topic_id != 59:
            raise ValueError("adaptive review operator address is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("adaptive review operator version is invalid")
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "chat_id", chat_id)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.user_id, self.chat_id, str(self.topic_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class NutritionCoachingConfig:
    registry_path: Path

    @classmethod
    def from_extra(cls, extra: JsonValue) -> NutritionCoachingConfig | None:
        if not isinstance(extra, dict):
            return None
        raw = extra.get("nutrition_coaching")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return None
        path = Path(str(raw.get("registry_path", "")))
        if not path.parts or path.is_absolute() or ".." in path.parts:
            return None
        return cls(path)


@dataclass(frozen=True, slots=True)
class AdaptiveNutritionConfig:
    enabled: bool
    operator_chat_id: str
    operator_topic_id: int
    delivery_enabled: bool
    operator_user_id: str = ""
    operator_version: int = 0
    review_operator: AdaptiveReviewOperator | None = None
    schedule_confirm_enabled: bool = False

    @classmethod
    def from_extra(cls, extra: JsonValue) -> "AdaptiveNutritionConfig | None":
        if not isinstance(extra, dict):
            return None
        raw = extra.get("adaptive_nutrition")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return None
        review = raw.get("review_operator")
        review_operator: AdaptiveReviewOperator | None = None
        if isinstance(review, dict):
            try:
                if "version" not in review:
                    return None
                review_operator = AdaptiveReviewOperator(
                    str(review.get("user_id", "")).strip(),
                    str(review.get("chat_id", "")).strip(),
                    int(review.get("topic_id")) if str(review.get("topic_id", "")).isdigit() else review.get("topic_id"),
                    review.get("version"),
                )
            except (TypeError, ValueError):
                return None
        # An enabled adaptive surface is never valid with only the historical
        # chat/topic pair. That shape cannot authenticate the review actor and
        # must fail closed rather than silently creating an unbound ingress.
        if review_operator is None:
            return None
        chat_id = str(raw.get("operator_chat_id", "")).strip()
        topic_id = raw.get("operator_topic_id")
        if chat_id and chat_id != review_operator.chat_id:
            return None
        if topic_id is not None:
            try:
                legacy_topic = int(topic_id)
            except (TypeError, ValueError):
                return None
            if legacy_topic != review_operator.topic_id:
                return None
        chat_id = review_operator.chat_id
        topic_id = review_operator.topic_id
        delivery = raw.get("delivery_enabled", False)
        schedule_confirm = raw.get("schedule_confirm_enabled", False)
        if (
            not chat_id
            or isinstance(topic_id, bool)
            or topic_id != 59
            or not isinstance(delivery, bool)
            or not isinstance(schedule_confirm, bool)
        ):
            return None
        return cls(
            True,
            chat_id,
            59,
            delivery,
            review_operator.user_id,
            review_operator.version,
            review_operator,
            schedule_confirm,
        )
