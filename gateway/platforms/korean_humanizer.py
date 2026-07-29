"""Deterministic two-stage Korean coaching copy pipeline.

The model is allowed to select finite, code-owned semantic atoms only.  All
rendered Korean, facts, decisions, actions, timing, safety and delivery state
remain owned by this module (and the canonical caller).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date as _date, datetime as _datetime
import hashlib
import json
import re
from typing import Callable, Literal, Mapping


_INTERNAL_TOKENS = (
    "reason_code",
    "insufficient_data",
    "missing_data",
    "safety_hold",
    "provider_",
)
_FORBIDDEN_CLAIMS = (
    "진단합니다",
    "치료합니다",
    "완치",
    "약물을 복용",
    "의학적으로 확실",
)
_LOCKED_PREFIXES = (
    "오늘 체크인 완료",
    "오늘 할 일",
    "이번 주 ",
    "적응형 영양 검토",
    "권장안",
    "검토 필요",
    "현재 판단:",
    "이번 주 판단:",
    "상태:",
    "고객에게는 아직 전달되지 않았습니다.",
)
_NUMBER_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)*%?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|[\u200b-\u200f\u202a-\u202e\u2060-\u206f]")
_OPAQUE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})"
)
_MEMORY_ID_RE = re.compile(r"[a-z][a-z0-9_.:+-]{1,63}")
_SAFE_DECISION_RE = re.compile(
    r"(?:decision|review|hold|adjust)[_.-][a-z][a-z0-9_.-]{1,63}"
)
_IDENTITY_LEAK_RE = re.compile(
    r"customer|client|user|telegram|chat|phone|email|display[_ -]?name|account|uuid",
    re.IGNORECASE,
)
_SURFACES = frozenset({"daily", "weekly", "adaptive_operator"})
_SURFACE_CLUSTERS = {
    "daily": ("daily-checkin",),
    "weekly": ("weekly-summary",),
    "adaptive_operator": ("adaptive-proposal",),
}
_SURFACE_RISKS = ("medical", "unsafe_nutrition")
_ADAPTIVE_OPTIONAL_FIELDS = frozenset(
    {"current_mean_kg", "prior_mean_kg", "weekly_rate_percent"}
)
_ADAPTIVE_FIELD_NAMES = (
    "evaluation_day",
    "goal_mode",
    "goal_range",
    "current_mean_kg",
    "prior_mean_kg",
    "weekly_rate_percent",
    "decision",
    "reason_category_ids",
    "target_macros",
    "carb_category_targets",
    "safety_held",
    "approval_state",
    "delivery_state",
    "proposal_digest",
    "revision",
    "revision_binding_digest",
    "source_cluster_ids",
    "excluded_risk_ids",
)
_MEMORY_KEYS = frozenset(
    {
        "previous_comparison",
        "recent_committed_adjustment",
        "next_check_time",
        "evaluation_day",
        "goal_mode",
        "goal_range",
        "current_mean",
        "prior_mean",
        "weekly_rate",
        "decision",
        "safety_held",
        "approval_state",
        "delivery_state",
    }
)

# These are the only doctrine entries that may cross the grounding boundary.
# The profile copy uses the same identifiers and compact text verbatim.
_APPROVED_PRINCIPLE_TEXT: dict[str, str] = {
    "choi_01": "원인부터 설명합니다. 문제를 동작·관절·부하·회복 요소로 나누고, 조정 뒤 관찰할 결과까지 연결합니다.",
    "choi_02": "몸통 안정성과 부하 경로를 우선합니다. 목표 근육의 느낌 하나보다 자세, 관절 움직임, 보상 동작, 반복 재현성을 함께 봅니다.",
    "choi_03": "약점은 인접 움직임에서 찾되 진단하지 않습니다. 견갑·전거근·전완·발목 등 인접 패턴을 검토할 수 있지만, 보조운동은 선택적 수단입니다.",
    "choi_04": "작고 누적 가능한 수행을 중시합니다. 부하·반복·운동순서·속도는 실제 수행, 기술, 관절 내성, 회복을 보고 점진적으로 조정합니다.",
    "choi_05": "감량 전 훈련 수용능력을 중시합니다. 공격적 감량보다 훈련을 감당할 준비를 갖추고, 단기 절식보다 식사 이행·소화·훈련 수행·회복을 함께 봅니다.",
    "choi_06": "개인 반응을 기록으로 확인합니다. 한 번에 적은 수의 가설만 바꾸고 결과와 반례를 기록합니다.",
    "choi_07": "영상 모방보다 맥락과 개인 피드백을 중시합니다. 확인되지 않은 기전·성과·안전성을 권위 있게 단정하지 않습니다.",
    "nutrition_01": "감량·체중 판단은 추세와 맥락을 함께 봅니다. 체중 한 번의 숫자가 아니라 체중·체성분 추세, 훈련 수행, 회복, 에너지 가용성을 함께 봅니다.",
    "nutrition_02": "근비대는 훈련·영양·회복의 결합 결과입니다. 어느 하나가 나머지를 대체하지 않습니다.",
    "nutrition_03": "식단 조정은 하나씩, 관찰 뒤에 합니다. 현재 계획을 관찰하고, 필요할 때 한 변수만 작고 되돌릴 수 있게 바꾼 뒤 체중 추세·수행·회복을 확인합니다.",
    "nutrition_04": "음식 반응은 개인 기록으로 확인합니다. 특정 음식·감미료·식이 방식이 모두에게 맞거나 틀리다고 단정하지 않습니다.",
    "nutrition_05": "산화능력은 수행 보조 요인으로 제한합니다. 유산소·산화성 컨디셔닝은 반복 작업 내성과 운동능력을 도울 수 있지만, 모든 회복 문제의 근거가 아닙니다.",
    "nutrition_06": "소화 콘텐츠는 관찰 원칙으로만 사용합니다. TRP 채널·히스타민·FODMAP은 가능한 맥락일 뿐 자가진단 근거가 아닙니다.",
    "nutrition_07": "운동 후 탄수화물은 회복 일정에 맞춥니다. 실제 소모량과 다음 고강도 세션까지의 시간을 고려하며 특정 부위에 영양분이 선택적으로 배분된다고 단정하지 않습니다.",
    "nutrition_08": "운동 선택은 목표와 관절 내성에 맞춥니다. 관절 위치·가동범위·기구 설정은 부하를 바꿀 수 있지만 구조적 문제를 진단하거나 교정 효과를 보장하지 않습니다.",
    "nutrition_09": "저탄수 상황의 기초 대사는 처방 근거가 아닙니다. 포도당신생·케톤체 설명을 개인의 탄수화물 반응 진단이나 처방으로 확대하지 않습니다.",
    "nutrition_10": "수면과 회복은 여러 지표로 봅니다. 수면은 수행과 회복에 관련되지만 한 번의 수행을 단일한 회복 지표로 단정하지 않습니다.",
    "nutrition_11": "미량영양소와 보충제는 필요량·결핍·안전 경계를 기준으로 합니다. 확인되지 않은 고용량 보충제를 회복·소화·수행 향상책으로 일반화하지 않습니다.",
}
APPROVED_PRINCIPLE_IDS = frozenset(_APPROVED_PRINCIPLE_TEXT)


@dataclass(frozen=True, slots=True)
class ProseSlot:
    slot_id: str
    line_indexes: tuple[int, ...]
    canonical: str


@dataclass(frozen=True, slots=True)
class HumanizeDocument:
    surface: str
    canonical: str
    lines: tuple[str, ...]
    slots: tuple[ProseSlot, ...]


@dataclass(frozen=True, slots=True)
class CoachingGrounding:
    """The exact bounded profile export accepted by the coaching pipeline."""

    approved_principles: tuple[tuple[str, str], ...]
    verified_memory: tuple[tuple[str, str], ...]
    playbook_id: str
    playbook_version: str
    source_cluster_ids: tuple[str, ...] = ()
    excluded_risk_ids: tuple[str, ...] = ()
    decision_id: str = ""
    revision_binding_digest: str = ""


@dataclass(frozen=True, slots=True)
class CoachingPipelineReceipt:
    """Non-sensitive bounded receipt for one pipeline attempt."""

    surface: str
    outcome: Literal["canonical", "coached", "coached_and_polished"]
    principle_ids: tuple[str, ...]
    memory_keys: tuple[str, ...]
    playbook_id: str
    playbook_version: str
    coach_valid: bool
    polish_valid: bool
    output_sha256: str
    atom_ids: tuple[str, ...] = ()
    variant_ids: tuple[str, ...] = ()
    revision_binding_digest: str = ""
    canonical_sha256: str = ""
    source_cluster_ids: tuple[str, ...] = ()
    excluded_risk_ids: tuple[str, ...] = ()
    decision_id: str = ""


@dataclass(frozen=True, slots=True)
class DailyGroundingInput:
    """Closed projection of a finalized personal check-in snapshot."""

    facts: tuple[tuple[str, str], ...]
    verified_memory: tuple[tuple[str, str], ...]
    revision_binding_digest: str
    canonical_sha256: str = ""
    source_cluster_ids: tuple[str, ...] = ("daily-checkin",)
    excluded_risk_ids: tuple[str, ...] = ("medical", "unsafe_nutrition")
    customer_key: str | None = None

    @classmethod
    def from_finalized_snapshot(
        cls,
        snapshot: object,
        *,
        canonical: str | None = None,
    ) -> "DailyGroundingInput":
        if type(snapshot) is not dict:
            raise TypeError("finalized snapshot must be an exact mapping")
        allowed = {"flow", "kst_day", "answers", "branch", "detailed", "follow_up_ids"}
        if set(snapshot) - allowed:
            raise ValueError("finalized snapshot contains unsupported fields")
        answers = snapshot.get("answers")
        if type(answers) is not dict:
            raise TypeError("finalized snapshot answers are invalid")
        known = {
            "bodyweight", "sleep_duration", "sleep_quality", "condition", "pain",
            "digestion", "calories", "training_plan", "completion", "training_summary",
            "workout_quality", "macros", "performance", "intensity",
        }
        free_text = {"optional_note", "operator_note", "meals", "water", "appetite_stress"}
        if any(key not in known and key not in free_text for key in answers):
            raise ValueError("finalized snapshot answers contain unsupported fields")
        facts: list[tuple[str, str]] = []
        for key in sorted((known | {"water"}) & set(answers)):
            value = answers[key]
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                if key == "macros" and isinstance(value, Mapping):
                    compact = _compact_macro_mapping(value)
                    if compact:
                        facts.append((key, compact))
                    continue
                raise TypeError("finalized snapshot fact has the wrong type")
            compact = " ".join(str(value).split()).strip()
            if compact:
                facts.append((key, compact))
        flow = snapshot.get("flow", "")
        day = snapshot.get("kst_day", "")
        if flow is not None and not isinstance(flow, str):
            raise TypeError("finalized snapshot flow is invalid")
        if day is not None and not isinstance(day, str):
            raise TypeError("finalized snapshot day is invalid")
        if canonical is not None and type(canonical) is not str:
            raise TypeError("finalized snapshot canonical text is invalid")
        snapshot_projection = _binding_projection(snapshot)
        canonical_value = (
            canonical
            if canonical is not None
            else _binding_digest(snapshot_projection)
        )
        canonical_sha256 = _sha256(canonical_value)
        binding = _binding_digest(
            {
                "snapshot": snapshot_projection,
                "facts": facts,
                "canonical_sha256": canonical_sha256,
            }
        )
        return cls(tuple(facts), (), binding, canonical_sha256)


@dataclass(frozen=True, slots=True)
class WeeklyGroundingInput:
    """Closed projection of a code-built weekly report summary."""

    facts: tuple[tuple[str, str], ...]
    verified_memory: tuple[tuple[str, str], ...]
    revision_binding_digest: str
    customer_key: str | None = None
    source_cluster_ids: tuple[str, ...] = ("weekly-summary",)
    excluded_risk_ids: tuple[str, ...] = ("medical", "unsafe_nutrition")



    @classmethod
    def from_summary(
        cls,
        summary: object,
        *,
        customer_key: str | None = None,
        revision_binding_digest: str | None = None,
        prior_summary: object | None = None,
        plan: object | None = None,
        profile: object | None = None,
    ) -> "WeeklyGroundingInput":
        allowed = {
            "average_weight_kg", "prior_average_weight_kg", "weekly_change_percent",
            "weight_change_percent", "change_percent", "weekly_weight_change_percent",
            "checkin_rate_percent", "goal_range", "judgment", "decision", "starts_on",
            "ends_on", "evaluation_day", "prior_summary", "next_check_time",
        }
        if isinstance(summary, Mapping):
            if set(summary) - allowed:
                raise ValueError("weekly summary contains unsupported fields")
            getter = summary.get
        else:
            getter = lambda name, default=None: getattr(summary, name, default)
        facts: list[tuple[str, str]] = []
        aliases = (
            ("average_weight_kg", ("average_weight_kg",)),
            ("prior_average_weight_kg", ("prior_average_weight_kg",)),
            ("weekly_change_percent", ("weekly_change_percent", "weight_change_percent", "change_percent", "weekly_weight_change_percent")),
            ("checkin_rate_percent", ("checkin_rate_percent",)),
            ("goal_range", ("goal_range",)),
            ("judgment", ("judgment", "decision")),
            ("evaluation_day", ("evaluation_day", "ends_on")),
        )
        for key, names in aliases:
            value = next((getter(name) for name in names if getter(name) is not None), None)
            if value is None:
                continue
            if isinstance(value, (Mapping, list, tuple, set)) or isinstance(value, bool):
                raise TypeError("weekly summary fact has the wrong type")
            compact = " ".join(str(value).split()).strip()
            if compact:
                facts.append((key, compact))
        memory: list[tuple[str, str]] = []
        if len(facts) >= 2:
            memory.append(("previous_comparison", "주간 평균과 이전 평균을 비교함"))
        next_time = getter("next_check_time")
        if isinstance(next_time, str) and next_time.strip():
            memory.append(("next_check_time", " ".join(next_time.split())[:80]))
        binding = revision_binding_digest or _binding_digest(
            {
                "customer_key": customer_key or "",
                "summary": _binding_projection(summary),
                "prior_summary": _binding_projection(prior_summary),
                "plan": _binding_projection(plan),
                "profile": _binding_projection(profile),
                "facts": facts,
                "verified_memory": memory,
            }
        )
        if not _DIGEST_RE.fullmatch(binding):
            raise ValueError("weekly revision binding digest is invalid")
        return cls(tuple(facts), tuple(memory), binding, customer_key)
@dataclass(frozen=True, slots=True)
class AdaptiveGroundingInput:
    """Closed projection of an adaptive operator card's typed facts."""

    facts: tuple[tuple[str, str], ...]
    verified_memory: tuple[tuple[str, str], ...]
    revision_binding_digest: str
    customer_key: str | None = None
    source_cluster_ids: tuple[str, ...] = ("adaptive-proposal",)
    excluded_risk_ids: tuple[str, ...] = ("medical", "unsafe_nutrition")
    decision_id: str = ""

    @classmethod
    def from_card(cls, facts: object, *, customer_key: str | None = None) -> "AdaptiveGroundingInput":
        from dataclasses import fields

        try:
            from .nutrition_coaching import AdaptiveCoachingFacts
        except (ImportError, AttributeError) as exc:
            raise TypeError("adaptive card facts type is unavailable") from exc
        if customer_key is not None and type(customer_key) is not str:
            raise TypeError("adaptive customer key has the wrong type")
        if type(facts) is not AdaptiveCoachingFacts:
            raise TypeError("adaptive card facts must be an exact typed projection")
        expected_fields = _ADAPTIVE_FIELD_NAMES
        try:
            fact_fields = tuple(field.name for field in fields(AdaptiveCoachingFacts))
        except (TypeError, ValueError) as exc:
            raise TypeError("adaptive card facts must be an exact typed projection") from exc
        if fact_fields != expected_fields or not _valid_adaptive_fact_values(facts):
            raise ValueError("adaptive card facts contain invalid values")

        values: list[tuple[str, str]] = []
        for name in expected_fields:
            if name in {"proposal_digest", "revision_binding_digest", "source_cluster_ids", "excluded_risk_ids"}:
                continue
            value = getattr(facts, name)
            if value is None:
                if name in _ADAPTIVE_OPTIONAL_FIELDS:
                    continue
                raise TypeError("adaptive card fact has the wrong type")
            if type(value) is bool:
                text = "true" if value else "false"
            elif type(value) in {str, int, float}:
                text = str(value)
            elif type(value) is tuple:
                try:
                    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError, UnicodeEncodeError) as exc:
                    raise TypeError("adaptive card fact has the wrong type") from exc
            else:
                raise TypeError("adaptive card fact has the wrong type")
            values.append((name, text[:160]))

        binding = facts.revision_binding_digest
        return cls(
            tuple(values),
            (),
            binding,
            customer_key,
            facts.source_cluster_ids,
            facts.excluded_risk_ids,
            facts.decision,
        )


def _valid_adaptive_fact_values(facts: object) -> bool:
    try:
        from dataclasses import fields
        from .nutrition_coaching import AdaptiveCoachingFacts
    except (ImportError, AttributeError):
        return False
    if type(facts) is not AdaptiveCoachingFacts:
        return False
    try:
        if tuple(field.name for field in fields(AdaptiveCoachingFacts)) != _ADAPTIVE_FIELD_NAMES:
            return False
        if not _valid_iso_date(facts.evaluation_day):
            return False
        if type(facts.goal_mode) is not str or facts.goal_mode not in {
            "lean_mass_gain", "fat_loss", "maintenance", "unknown",
        }:
            return False
        scalar = re.compile(r"[+-]?\d+(?:\.\d+)?")
        goal_range = facts.goal_range
        if (
            type(goal_range) is not tuple
            or len(goal_range) != 2
            or any(
                type(value) is not str or (value and not scalar.fullmatch(value))
                for value in goal_range
            )
        ):
            return False
        for name in _ADAPTIVE_OPTIONAL_FIELDS:
            value = getattr(facts, name)
            if value is not None and (
                type(value) is not str or not scalar.fullmatch(value)
            ):
                return False
        if not _valid_safe_decision_id(facts.decision, allow_known=True):
            return False
        reasons = facts.reason_category_ids
        if (
            type(reasons) is not tuple
            or len(reasons) > 8
            or len(set(reasons)) != len(reasons)
            or any(
                type(value) is not str
                or not _OPAQUE_ID_RE.fullmatch(value)
                or _IDENTITY_LEAK_RE.search(value)
                for value in reasons
            )
        ):
            return False
        if type(facts.safety_held) is not bool:
            return False
        if type(facts.approval_state) is not str or facts.approval_state not in {
            "pending", "approved", "held",
        }:
            return False
        if type(facts.delivery_state) is not str or facts.delivery_state not in {
            "disabled", "enabled", "revoked", "not_delivered",
            "sent_audited", "delivery_unknown",
        }:
            return False
        if (
            type(facts.proposal_digest) is not str
            or not _DIGEST_RE.fullmatch(facts.proposal_digest)
            or type(facts.revision_binding_digest) is not str
            or not _DIGEST_RE.fullmatch(facts.revision_binding_digest)
            or type(facts.revision) is not int
            or facts.revision < 1
        ):
            return False
        if not _valid_macro_pairs(facts.target_macros):
            return False
        cycles = facts.carb_category_targets
        if type(cycles) is not tuple or len(cycles) > 7:
            return False
        categories: set[str] = set()
        for item in cycles:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] not in {"high", "medium", "low"}
                or item[0] in categories
                or not _valid_macro_pairs(item[1])
            ):
                return False
            categories.add(item[0])
        return (
            type(facts.source_cluster_ids) is tuple
            and facts.source_cluster_ids == ("adaptive-proposal",)
            and type(facts.excluded_risk_ids) is tuple
            and facts.excluded_risk_ids == ("medical", "unsafe_nutrition")
            and all(type(value) is str for value in facts.source_cluster_ids)
            and all(type(value) is str for value in facts.excluded_risk_ids)
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _valid_macro_pairs(value: object) -> bool:
    allowed = {"calories", "calories_kcal", "carbs_g", "protein_g", "fat_g"}
    if type(value) is not tuple or len(value) > 5:
        return False
    keys: set[str] = set()
    for item in value:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or item[0] not in allowed
            or item[0] in keys
            or type(item[1]) is not int
            or item[1] < 0
            or item[1] > 10_000
        ):
            return False
        keys.add(item[0])
    return True


def _valid_iso_date(value: object) -> bool:
    if type(value) is not str or _ISO_DATE_RE.fullmatch(value) is None:
        return False
    try:
        _date.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _valid_iso_datetime(value: object) -> bool:
    if type(value) is not str or _ISO_DATETIME_RE.fullmatch(value) is None:
        return False
    try:
        parsed = _datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_safe_decision_id(value: object, *, allow_known: bool = False) -> bool:
    if type(value) is not str or not value or len(value) > 64:
        return False
    if _IDENTITY_LEAK_RE.search(value) or _OPAQUE_ID_RE.fullmatch(value) is None:
        return False
    if _SAFE_DECISION_RE.fullmatch(value):
        return True
    return allow_known and value in {
        "observe",
        "maintain",
        "calorie_adjustment_candidate",
        "macro_redistribution_candidate",
        "human_review",
    }


def _valid_memory_value(key: str, value: str) -> bool:
    if key == "previous_comparison":
        return value == "주간 평균과 이전 평균을 비교함"
    if key == "recent_committed_adjustment":
        return _MEMORY_ID_RE.fullmatch(value) is not None
    if key == "next_check_time":
        return _valid_iso_datetime(value)
    if key == "evaluation_day":
        return _valid_iso_date(value)
    if key == "goal_mode":
        return value in {"lean_mass_gain", "fat_loss", "maintenance", "unknown"}
    if key in {"goal_range", "current_mean", "prior_mean", "weekly_rate"}:
        return re.fullmatch(
            r"[+-]?\d+(?:\.\d+)?(?:~[+-]?\d+(?:\.\d+)?)?%?(?:kg)?",
            value,
        ) is not None
    if key == "decision":
        return _valid_safe_decision_id(value, allow_known=True)
    if key == "safety_held":
        return value in {"true", "false"}
    if key == "approval_state":
        return value in {"pending", "approved", "held"}
    if key == "delivery_state":
        return value in {
            "disabled",
            "enabled",
            "revoked",
            "not_delivered",
            "sent_audited",
            "delivery_unknown",
        }
    return False


def _valid_verified_memory(value: object) -> bool:
    if type(value) is not tuple or len(value) > 8:
        return False
    seen: set[str] = set()
    for item in value:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or item[0] not in _MEMORY_KEYS
            or item[0] in seen
            or type(item[1]) is not str
        ):
            return False
        key, text = item
        if (
            not text
            or text != text.strip()
            or len(text) > 80
            or _CONTROL_RE.search(text)
            or _IDENTITY_LEAK_RE.search(text)
            or not _valid_memory_value(key, text)
        ):
            return False
        seen.add(key)
    return True


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _valid_surface(surface: object) -> bool:
    return type(surface) is str and surface in _SURFACES


def _valid_canonical(canonical: object) -> bool:
    if type(canonical) is not str:
        return False
    try:
        canonical.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True

def prepare_document(surface: str, canonical: str) -> HumanizeDocument:
    """Select only prose paragraphs; facts, actions, headings, and state stay locked."""
    if not isinstance(surface, str) or not isinstance(canonical, str):
        return HumanizeDocument(str(surface), str(canonical), tuple(str(canonical).splitlines()), ())
    lines = tuple(canonical.splitlines())
    indexes: list[tuple[int, ...]] = []
    if surface == "daily":
        indexes.extend(_between(lines, _last_fact_line(lines), _line_index(lines, "오늘 할 일")))
    elif surface == "weekly":
        judgment = _line_index_prefix(lines, "이번 주 판단:")
        indexes.extend(_between(lines, _last_fact_line(lines), judgment))
        if judgment is not None:
            indexes.extend(_paragraphs_after_actions(lines, judgment))
    elif surface == "adaptive_operator":
        review = _line_index(lines, "검토 필요")
        if review is not None:
            indexes.extend(_between(lines, review, _line_index(lines, "고객에게는 아직 전달되지 않았습니다.")))

    slots: list[ProseSlot] = []
    for ordinal, group in enumerate(indexes):
        text = "\n".join(lines[index] for index in group).strip()
        if text and _is_editable_text(text):
            slots.append(ProseSlot(f"slot_{ordinal}", group, text))
    return HumanizeDocument(surface, canonical, lines, tuple(slots))


def build_request(document: HumanizeDocument) -> tuple[str, str]:
    """Build the legacy one-stage prose request kept for compatibility."""
    system = (
        "당신은 한국어 코칭 문구의 문체 편집자입니다. 사실이나 판단을 추가하지 말고, "
        "주어진 문장의 의미만 자연스러운 존댓말로 다듬으세요. 번역투, 기계적인 보고서체, "
        "같은 종결어미 반복을 줄이되 숫자·날짜·결정·행동·안전 문구는 바꾸지 마세요. "
        "반드시 JSON 객체 하나만 반환하세요."
    )
    payload = {
        "schema_version": "dualcoach-korean-humanizer-v1",
        "surface": document.surface,
        "slots": [{"id": slot.slot_id, "text": slot.canonical} for slot in document.slots],
        "response_schema": {"slots": [{"id": "slot id", "text": "윤문한 한국어"}]},
    }
    return system, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_coaching_request(
    surface: str,
    canonical: str,
    grounding: CoachingGrounding,
) -> tuple[str, str]:
    """Build the finite-ID stage-one request from an exact grounding export."""
    if (
        not _valid_surface(surface)
        or not _valid_canonical(canonical)
        or not _valid_grounding(grounding, surface)
    ):
        raise ValueError("bounded coaching grounding is unavailable")
    document = prepare_document(surface, canonical)
    if not document.slots:
        raise ValueError("bounded coaching grounding is unavailable")
    playbook = _PLAYBOOKS.get(grounding.playbook_id)
    if playbook is None:
        raise ValueError("coaching playbook is unavailable")
    offered = _offered_atoms(document, playbook)
    system = (
        "당신은 승인된 한국어 코칭 초안의 의미 선택기입니다. 자유 문장, 숫자, 행동, "
        "시점, 안전, 승인 또는 전달 상태를 만들지 마세요. 각 slot의 id와 hard_identity를 "
        "응답에 한 글자도 바꾸지 말고 반드시 그대로 복사하세요. 그 뒤 제공된 "
        "principle_ids와 atom_ids만 순서대로 선택해 JSON 객체 하나만 반환하세요."
    )
    payload = {
        "schema_version": "dualcoach-finite-coaching-v1",
        "stage": "coach",
        "surface": surface,
        "playbook": {"id": grounding.playbook_id, "version": grounding.playbook_version},
        "approved_principles": [
            {"id": principle_id, "text": text}
            for principle_id, text in grounding.approved_principles
        ],
        "verified_memory": [
            {"key": key, "value": value} for key, value in grounding.verified_memory
        ],
        "slots": [
            {
                "id": slot.slot_id,
                "hard_identity": _hard_identity(slot),
                "offered_principle_ids": [item[0] for item in grounding.approved_principles],
                "offered_atom_ids": list(offered.get(slot.slot_id, ())),
            }
            for slot in document.slots
        ],
        "response_schema": {
            "slots": [
                {
                    "id": slot.slot_id,
                    "hard_identity": _hard_identity(slot),
                    "principle_ids": ["approved_id"],
                    "atom_ids": ["offered_id"],
                }
                for slot in document.slots
            ]
        },
    }
    return system, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def apply_response(document: HumanizeDocument, response: object) -> str:
    """Apply a legacy prose response only when every deterministic invariant passes."""
    if not document.slots or not isinstance(response, str) or len(response.encode("utf-8")) > 4_096:
        return document.canonical
    try:
        parsed = json.loads(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        return document.canonical
    if not isinstance(parsed, dict) or set(parsed) != {"slots"} or not isinstance(parsed["slots"], list):
        return document.canonical
    expected_ids = [slot.slot_id for slot in document.slots]
    received: dict[str, str] = {}
    for item in parsed["slots"]:
        if not isinstance(item, dict) or set(item) != {"id", "text"}:
            return document.canonical
        slot_id, text = item.get("id"), item.get("text")
        if not isinstance(slot_id, str) or not isinstance(text, str) or slot_id in received:
            return document.canonical
        received[slot_id] = text.strip()
    if list(received) != expected_ids:
        return document.canonical

    allowed_numbers = Counter(_NUMBER_RE.findall(document.canonical))
    validated: list[tuple[ProseSlot, list[str]]] = []
    for slot in document.slots:
        candidate = received[slot.slot_id]
        if not _valid_candidate(slot.canonical, candidate, allowed_numbers, document.canonical):
            return document.canonical
        validated.append((slot, candidate.splitlines()))

    rendered = list(document.lines)
    for slot, replacement in reversed(validated):
        first, last = slot.line_indexes[0], slot.line_indexes[-1]
        rendered[first : last + 1] = replacement
    result = "\n".join(rendered)
    return result if _locked_lines_preserved(document, result) else document.canonical


def coach_and_polish(
    surface: str,
    canonical: str,
    grounding: CoachingGrounding,
    request: Callable[[str, str], object],
    *,
    processing_allowed: Callable[[], bool] | None = None,
    revision_binding_digest: str | None = None,
) -> tuple[str, CoachingPipelineReceipt]:
    """Run finite coaching stages only for an exact, externally bound grounding."""
    valid_surface = _valid_surface(surface)
    valid_canonical = _valid_canonical(canonical)
    valid_grounding = valid_surface and valid_canonical and _valid_grounding(grounding, surface)
    valid_binding = (
        valid_grounding
        and type(revision_binding_digest) is str
        and _valid_digest(revision_binding_digest)
        and revision_binding_digest == grounding.revision_binding_digest
    )
    valid_request = callable(request)
    valid_processing_gate = processing_allowed is None or callable(processing_allowed)
    if not (
        valid_surface
        and valid_canonical
        and valid_grounding
        and valid_binding
        and valid_request
        and valid_processing_gate
    ):
        return canonical, _receipt(
            surface,
            canonical,
            grounding,
            outcome="canonical",
            coach_valid=False,
            polish_valid=False,
        )

    document = prepare_document(surface, canonical)
    canonical_digest = _sha256(canonical)
    binding = revision_binding_digest
    base_receipt = _receipt(
        surface,
        canonical,
        grounding,
        outcome="canonical",
        coach_valid=False,
        polish_valid=False,
        revision_binding_digest=binding,
        canonical_sha256=canonical_digest,
    )
    if (
        not document.slots
        or (processing_allowed is not None and not _gate(processing_allowed))
    ):
        return canonical, base_receipt

    try:
        system, user = build_coaching_request(surface, canonical, grounding)
        stage_one_response = request(system, user)
    except Exception:
        return canonical, base_receipt
    selections = _parse_stage_one(document, grounding, stage_one_response)
    if selections is None:
        return canonical, base_receipt
    coaching = _render_coaching(document, selections)
    if coaching is None:
        return canonical, base_receipt
    atom_ids = tuple(atom for _, _, atoms in selections for atom in atoms)
    principle_ids = tuple(principle for _, principles, _ in selections for principle in principles)
    if processing_allowed is not None and not _gate(processing_allowed):
        return canonical, base_receipt

    try:
        polish_system, polish_user = _build_polish_request(document, coaching, grounding, selections)
        stage_two_response = request(polish_system, polish_user)
    except Exception:
        if processing_allowed is not None and not _gate(processing_allowed):
            return canonical, base_receipt
        receipt = _receipt(
            surface,
            coaching,
            grounding,
            outcome="coached",
            coach_valid=True,
            polish_valid=False,
            atom_ids=atom_ids,
            principle_ids=principle_ids,
            revision_binding_digest=binding,
            canonical_sha256=canonical_digest,
        )
        return coaching, receipt
    variants = _parse_stage_two(document, grounding, stage_two_response)
    polished = _render_polished(document, variants, coaching) if variants is not None else coaching
    polish_valid = variants is not None and polished is not None
    result = polished if polish_valid and polished is not None else coaching
    if processing_allowed is not None and not _gate(processing_allowed):
        return canonical, base_receipt
    receipt = _receipt(
        surface,
        result,
        grounding,
        outcome="coached_and_polished" if polish_valid else "coached",
        coach_valid=True,
        polish_valid=polish_valid,
        atom_ids=atom_ids,
        variant_ids=tuple(variants or ()),
        principle_ids=principle_ids,
        revision_binding_digest=binding,
        canonical_sha256=canonical_digest,
    )
    return result, receipt


def humanize(
    surface: str,
    canonical: str,
    request: Callable[[str, str], str | None],
) -> str:
    """Compatibility wrapper for the original one-call prose editor."""
    document = prepare_document(surface, canonical)
    if not document.slots:
        return canonical
    system, user = build_request(document)
    try:
        response = request(system, user)
    except Exception:
        return canonical
    return apply_response(document, response)


def _build_polish_request(
    document: HumanizeDocument,
    coaching: str,
    grounding: CoachingGrounding,
    selections: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...],
) -> tuple[str, str]:
    playbook = _PLAYBOOKS[grounding.playbook_id]
    variants = [
        {
            "id": slot_id,
            "draft": _slot_text_from_rendered(document, coaching, slot_id),
            "offered_variant_ids": list(playbook["variants"].get(slot_id, ())),
        }
        for slot_id, _principles, _atoms in selections
    ]
    system = (
        "당신은 승인된 한국어 코칭 초안의 표현 선택기입니다. 초안의 사실과 의미를 "
        "바꾸지 말고 제공된 variant_id 하나씩만 선택해 JSON 객체 하나만 반환하세요."
    )
    payload = {
        "schema_version": "dualcoach-finite-polish-v1",
        "stage": "polish",
        "surface": document.surface,
        "locked_constraints": {
            "number_tokens": list(_NUMBER_RE.findall(document.canonical)),
            "locked_line_digest": _sha256("\n".join(_locked_line_values(document))),
        },
        "slots": variants,
        "response_schema": {"slots": [{"id": "slot_0", "variant_id": "offered_variant_id"}]},
    }
    return system, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_stage_one(
    document: HumanizeDocument,
    grounding: CoachingGrounding,
    response: object,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] | None:
    parsed = _parse_json_object(response)
    expected = [slot.slot_id for slot in document.slots]
    if (
        parsed is None
        or set(parsed) != {"slots"}
        or not isinstance(parsed["slots"], list)
        or len(parsed["slots"]) != len(expected)
    ):
        return None
    offered = _offered_atoms(document, _PLAYBOOKS[grounding.playbook_id])
    received: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for item, slot_id in zip(parsed["slots"], expected):
        if not isinstance(item, Mapping):
            return None
        if set(item) != {"id", "hard_identity", "principle_ids", "atom_ids"}:
            return None
        if item.get("id") != slot_id:
            return None
        supplied_hard = item.get("hard_identity")
        if supplied_hard != _hard_identity(next(slot for slot in document.slots if slot.slot_id == slot_id)):
            return None
        principles = item.get("principle_ids")
        atoms = item.get("atom_ids")
        if not isinstance(principles, list) or not isinstance(atoms, list):
            return None
        if any(not isinstance(value, str) for value in (*principles, *atoms)):
            return None
        principle_tuple = tuple(principles)
        atom_tuple = tuple(atoms)
        allowed_principles = {identifier for identifier, _ in grounding.approved_principles}
        if (
            not principle_tuple
            or len(set(principle_tuple)) != len(principle_tuple)
            or any(identifier not in allowed_principles for identifier in principle_tuple)
            or len(set(atom_tuple)) != len(atom_tuple)
            or not atom_tuple
            or any(identifier not in offered.get(slot_id, ()) for identifier in atom_tuple)
            or tuple(identifier for identifier in offered.get(slot_id, ()) if identifier in atom_tuple) != atom_tuple
        ):
            return None
        received.append((slot_id, principle_tuple, atom_tuple))
    if len(received) != len(expected) or [item[0] for item in received] != expected:
        return None
    return tuple(received)


def _parse_stage_two(document: HumanizeDocument, grounding: CoachingGrounding, response: object) -> tuple[str, ...] | None:
    parsed = _parse_json_object(response)
    expected = [slot.slot_id for slot in document.slots]
    if (
        parsed is None
        or set(parsed) != {"slots"}
        or not isinstance(parsed["slots"], list)
        or len(parsed["slots"]) != len(expected)
    ):
        return None
    variants_by_slot = _PLAYBOOKS[grounding.playbook_id]["variants"]
    received: list[str] = []
    for item, slot_id in zip(parsed["slots"], expected):
        if not isinstance(item, Mapping) or set(item) != {"id", "variant_id"}:
            return None
        if item.get("id") != slot_id or not isinstance(item.get("variant_id"), str):
            return None
        variant = item["variant_id"]
        if variant not in variants_by_slot.get(slot_id, ()):
            return None
        received.append(variant)
    if len(received) != len(expected):
        return None
    return tuple(received)


def _render_coaching(
    document: HumanizeDocument,
    selections: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...],
) -> str | None:
    replacements: dict[str, str] = {}
    for slot_id, _principles, atom_ids in selections:
        texts = [_ATOM_TEXT.get(atom_id, "") for atom_id in atom_ids]
        text = " ".join(item for item in texts if item).strip()
        if not text:
            return None
        slot = next((candidate for candidate in document.slots if candidate.slot_id == slot_id), None)
        if slot is None or not _valid_candidate(
            slot.canonical,
            text,
            Counter(_NUMBER_RE.findall(slot.canonical)),
            document.canonical,
        ):
            return None
        replacements[slot_id] = text
    return _render_slots(document, replacements)


def _render_polished(document: HumanizeDocument, variants: tuple[str, ...], coaching: str) -> str | None:
    replacements = {
        slot.slot_id: _VARIANT_TEXT.get(variant, "")
        for slot, variant in zip(document.slots, variants)
    }
    if any(not value for value in replacements.values()):
        return None
    result = _render_slots(document, replacements)
    if result is None:
        return None
    # Ensure the final expression is validated against the same locked rules.
    for slot in document.slots:
        replacement = replacements[slot.slot_id]
        if not _valid_candidate(slot.canonical, replacement, Counter(_NUMBER_RE.findall(document.canonical)), document.canonical):
            return None
    return result


def _render_slots(document: HumanizeDocument, replacements: Mapping[str, str]) -> str | None:
    rendered = list(document.lines)
    for slot in reversed(document.slots):
        replacement = replacements.get(slot.slot_id)
        if not isinstance(replacement, str):
            return None
        lines = replacement.splitlines()
        if not lines:
            return None
        first, last = slot.line_indexes[0], slot.line_indexes[-1]
        rendered[first : last + 1] = lines
    result = "\n".join(rendered)
    if not _locked_lines_preserved(document, result):
        return None
    return result


def _parse_json_object(response: object) -> dict[str, object] | None:
    if not isinstance(response, str) or not response:
        return None
    try:
        if len(response.encode("utf-8")) > 1_024:
            return None
        parsed = json.loads(response)
    except (TypeError, ValueError, UnicodeEncodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _valid_grounding(grounding: object, surface: str | None = None) -> bool:
    if type(grounding) is not CoachingGrounding:
        return False
    if surface is not None and not _valid_surface(surface):
        return False
    try:
        playbook_id = grounding.playbook_id
        playbook_version = grounding.playbook_version
        binding = grounding.revision_binding_digest
        if type(playbook_id) is not str or type(playbook_version) is not str:
            return False
        playbook = _PLAYBOOKS.get(playbook_id)
        if (
            playbook is None
            or playbook_version != playbook["version"]
            or not _valid_digest(binding)
            or (surface is not None and playbook["surface"] != surface)
        ):
            return False

        principles = grounding.approved_principles
        if type(principles) is not tuple or not 3 <= len(principles) <= 5:
            return False
        seen: set[str] = set()
        for item in principles:
            if type(item) is not tuple or len(item) != 2:
                return False
            identifier, text = item
            if (
                type(identifier) is not str
                or _OPAQUE_ID_RE.fullmatch(identifier) is None
                or identifier in seen
                or identifier not in APPROVED_PRINCIPLE_IDS
                or type(text) is not str
                or text != _APPROVED_PRINCIPLE_TEXT[identifier]
            ):
                return False
            seen.add(identifier)

        if not _valid_verified_memory(grounding.verified_memory):
            return False

        clusters = grounding.source_cluster_ids
        risks = grounding.excluded_risk_ids
        if (
            type(clusters) is not tuple
            or type(risks) is not tuple
            or not 1 <= len(clusters) <= 8
            or not 1 <= len(risks) <= 8
            or any(
                type(value) is not str
                or _OPAQUE_ID_RE.fullmatch(value) is None
                for value in (*clusters, *risks)
            )
            or len(set(clusters)) != len(clusters)
            or len(set(risks)) != len(risks)
            or risks != _SURFACE_RISKS
        ):
            return False
        if surface is not None:
            if clusters != _SURFACE_CLUSTERS[surface]:
                return False
        elif clusters not in tuple(_SURFACE_CLUSTERS.values()):
            return False

        decision_id = grounding.decision_id
        if type(decision_id) is not str:
            return False
        if decision_id and not _valid_safe_decision_id(decision_id, allow_known=True):
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def _offered_atoms(document: HumanizeDocument, playbook: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    by_slot = playbook.get("atoms", {})
    return {slot.slot_id: tuple(by_slot.get(slot.slot_id, ())) for slot in document.slots}


def _hard_identity(slot: ProseSlot) -> str:
    return _sha256(f"{slot.slot_id}\0{slot.canonical}")[:16]


def _slot_text_from_rendered(document: HumanizeDocument, rendered: str, slot_id: str) -> str:
    slot = next(slot for slot in document.slots if slot.slot_id == slot_id)
    lines = rendered.splitlines()
    return "\n".join(lines[slot.line_indexes[0] : slot.line_indexes[-1] + 1]).strip()


def _receipt(
    surface: str,
    output: str,
    grounding: CoachingGrounding,
    *,
    outcome: Literal["canonical", "coached", "coached_and_polished"],
    coach_valid: bool,
    polish_valid: bool,
    atom_ids: tuple[str, ...] = (),
    variant_ids: tuple[str, ...] = (),
    principle_ids: tuple[str, ...] | None = None,
    revision_binding_digest: str = "",
    canonical_sha256: str = "",
) -> CoachingPipelineReceipt:
    valid = (
        _valid_surface(surface)
        and _valid_canonical(output)
        and _valid_grounding(grounding, surface)
        and _valid_digest(revision_binding_digest)
        and _valid_digest(canonical_sha256)
    )
    safe_surface = surface if _valid_surface(surface) else ""
    safe_outcome = outcome if type(outcome) is str and outcome in {"canonical", "coached", "coached_and_polished"} else "canonical"
    if not valid:
        return CoachingPipelineReceipt(
            surface=safe_surface,
            outcome="canonical",
            principle_ids=(),
            memory_keys=(),
            playbook_id="",
            playbook_version="",
            coach_valid=False,
            polish_valid=False,
            output_sha256="",
            atom_ids=(),
            variant_ids=(),
            revision_binding_digest="",
            canonical_sha256="",
            source_cluster_ids=(),
            excluded_risk_ids=(),
            decision_id="",
        )
    selected = _unique(principle_ids if principle_ids is not None else (
        tuple(identifier for identifier, _ in grounding.approved_principles)
    ))
    memory_keys = _unique(tuple(key for key, _ in grounding.verified_memory))
    safe_atoms = _unique(atom_ids)
    safe_variants = _unique(variant_ids)
    return CoachingPipelineReceipt(
        surface=safe_surface,
        outcome=safe_outcome,
        principle_ids=selected,
        memory_keys=memory_keys,
        playbook_id=grounding.playbook_id,
        playbook_version=grounding.playbook_version,
        coach_valid=coach_valid is True,
        polish_valid=polish_valid is True,
        output_sha256=_sha256(output),
        atom_ids=safe_atoms,
        variant_ids=safe_variants,
        revision_binding_digest=revision_binding_digest if _valid_digest(revision_binding_digest) else "",
        canonical_sha256=canonical_sha256 if _valid_digest(canonical_sha256) else "",
        source_cluster_ids=grounding.source_cluster_ids,
        excluded_risk_ids=grounding.excluded_risk_ids,
        decision_id=grounding.decision_id,
    )
def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or any(
        type(value) is not str or _OPAQUE_ID_RE.fullmatch(value) is None
        for value in values
    ):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
def _gate(callback: Callable[[], bool]) -> bool:
    try:
        return callback() is True
    except Exception:
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding_projection(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _binding_projection(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_binding_projection(item) for item in value]
    if isinstance(value, set):
        return sorted((_binding_projection(item) for item in value), key=repr)
    if is_dataclass(value):
        return _binding_projection(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        return _binding_projection(dumped)
    if isinstance(value, (_date, _datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _binding_digest(value: object) -> str:
    try:
        encoded = json.dumps(_binding_projection(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(value)
    return _sha256(encoded)


def _compact_macro_mapping(value: Mapping[object, object]) -> str:
    pairs: list[str] = []
    for key in ("carbohydrate", "protein", "fat", "탄수화물", "단백질", "지방"):
        raw = value.get(key)
        if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            pairs.append(f"{key}:{raw}")
    return " · ".join(pairs)


def _locked_line_values(document: HumanizeDocument) -> list[str]:
    editable = {index for slot in document.slots for index in slot.line_indexes}
    return [line for index, line in enumerate(document.lines) if index not in editable and line.strip()]


def _line_index(lines: tuple[str, ...], value: str) -> int | None:
    return next((i for i, line in enumerate(lines) if line == value), None)


def _line_index_prefix(lines: tuple[str, ...], value: str) -> int | None:
    return next((i for i, line in enumerate(lines) if line.startswith(value)), None)


def _last_fact_line(lines: tuple[str, ...]) -> int | None:
    indexes = [i for i, line in enumerate(lines) if line.startswith("- ")]
    if not indexes:
        return None
    first_block = [indexes[0]]
    for index in indexes[1:]:
        if index == first_block[-1] + 1:
            first_block.append(index)
        else:
            break
    return first_block[-1]


def _between(lines: tuple[str, ...], start: int | None, end: int | None) -> list[tuple[int, ...]]:
    if start is None or end is None or start >= end:
        return []
    groups: list[tuple[int, ...]] = []
    current: list[int] = []
    for index in range(start + 1, end):
        if lines[index].strip():
            current.append(index)
        elif current:
            groups.append(tuple(current))
            current = []
    if current:
        groups.append(tuple(current))
    return groups


def _paragraphs_after_actions(lines: tuple[str, ...], judgment: int) -> list[tuple[int, ...]]:
    index = judgment + 1
    while index < len(lines) and not lines[index].startswith("- "):
        index += 1
    while index < len(lines) and (not lines[index].strip() or lines[index].startswith("- ")):
        index += 1
    if index >= len(lines):
        return []
    return [tuple(i for i in range(index, len(lines)) if lines[i].strip())]


def _is_editable_text(text: str) -> bool:
    return not any(line.startswith(_LOCKED_PREFIXES) or line.startswith("- ") for line in text.splitlines())


def _valid_candidate(
    canonical: str,
    candidate: str,
    allowed_numbers: Counter[str],
    full_canonical: str | None = None,
) -> bool:
    if not candidate or len(candidate) > 600 or len(candidate.splitlines()) > 2:
        return False
    if _CONTROL_RE.search(candidate) or not re.search(r"[가-힣]", candidate):
        return False
    lowered = candidate.casefold()
    if any(token in lowered for token in _INTERNAL_TOKENS):
        return False
    if any(claim in candidate for claim in _FORBIDDEN_CLAIMS):
        return False
    if Counter(_NUMBER_RE.findall(candidate)) != Counter(_NUMBER_RE.findall(canonical)):
        # Editable prose normally has no numbers.  If it does, preserve them exactly.
        return False
    semantic_tokens = (
        "해야", "하세요", "권장", "조정", "다음 주", "내일", "즉시", "안전", "통증",
        "진료", "의료", "보류", "승인", "전달", "고객",
    )
    reference = full_canonical or canonical
    for token in semantic_tokens:
        if token in candidate and token not in reference:
            return False
    if any(line.lstrip().startswith(("- ", "* ", "#", "```")) for line in candidate.splitlines()):
        return False
    return len(candidate) <= max(120, int(len(canonical) * 2.5))


def _locked_lines_preserved(document: HumanizeDocument, rendered: str) -> bool:
    if not isinstance(rendered, str):
        return False
    editable = {index for slot in document.slots for index in slot.line_indexes}
    locked = [line for index, line in enumerate(document.lines) if index not in editable and line.strip()]
    rendered_lines = [line for line in rendered.splitlines() if line.strip()]
    # Compare the locked subsequence, not merely set membership: headings, facts,
    # actions and delivery state must remain byte-identical and ordered.
    cursor = 0
    for line in rendered_lines:
        if cursor < len(locked) and line == locked[cursor]:
            cursor += 1
    return cursor == len(locked)


# Finite, code-owned semantic vocabulary.  These strings are soft explanatory
# clauses only; no action, timing, safety, approval, delivery or heading is here.
_ATOM_TEXT: dict[str, str] = {
    "daily_observation": "오늘 기록된 내용을 기준으로 현재 상태를 차분히 확인했어요.",
    "daily_context": "기록된 범위 안에서 무리하게 결론 내리지 않고 살펴볼게요.",
    "daily_followup": "다음 기록에서도 같은 기준으로 변화를 확인해 볼게요.",
    "weekly_observation": "최근 기록은 이전 기간과 함께 놓고 보면 흐름을 읽을 수 있어요.",
    "weekly_comparison": "한 번의 수치보다 확인된 추세와 맥락을 먼저 보겠습니다.",
    "weekly_reason": "현재 자료에서 보이는 흐름과 아직 모르는 부분을 나누어 보겠습니다.",
    "weekly_followup": "추가 기록이 쌓인 뒤 같은 기준으로 다시 살펴볼게요.",
    "adaptive_observation": "현재 카드의 확정된 자료에서 확인되는 부분을 먼저 살펴볼게요.",
    "adaptive_verification": "수정 전후 기록이 겹치지 않는지 확정된 자료를 기준으로 확인하겠습니다.",
}
_VARIANT_TEXT: dict[str, str] = {
    "daily_v1": "오늘 기록을 기준으로 현재 상태를 차분히 확인했어요.",
    "daily_v2": "현재 기록에서 확인되는 내용부터 차분히 살펴볼게요.",
    "weekly_v1": "최근 기록을 이전 기간과 함께 보며 흐름을 확인했어요.",
    "weekly_v2": "한 번의 수치보다 확인된 추세와 맥락을 먼저 살펴보겠습니다.",
    "weekly_reason_v1": "현재 자료에서 보이는 흐름과 아직 모르는 부분을 나누어 보겠습니다.",
    "weekly_reason_v2": "확인된 내용과 더 지켜볼 부분을 구분해 두겠습니다.",
    "adaptive_v1": "확정된 자료에서 확인되는 부분부터 차분히 살펴볼게요.",
    "adaptive_v2": "현재 카드의 확인된 근거를 기준으로 먼저 살펴보겠습니다.",
}
_PLAYBOOKS: dict[str, dict[str, object]] = {
    "daily_checkin_v1": {
        "surface": "daily",
        "version": "1",
        "atoms": {"slot_0": ("daily_observation", "daily_context", "daily_followup")},
        "variants": {"slot_0": ("daily_v1", "daily_v2")},
    },
    "weekly_report_v1": {
        "surface": "weekly",
        "version": "1",
        "atoms": {
            "slot_0": ("weekly_observation", "weekly_comparison"),
            "slot_1": ("weekly_reason", "weekly_followup"),
        },
        "variants": {
            "slot_0": ("weekly_v1", "weekly_v2"),
            "slot_1": ("weekly_reason_v1", "weekly_reason_v2"),
        },
    },
    "adaptive_nutrition_v1": {
        "surface": "adaptive_operator",
        "version": "1",
        "atoms": {"slot_0": ("adaptive_observation", "adaptive_verification")},
        "variants": {"slot_0": ("adaptive_v1", "adaptive_v2")},
    },
}