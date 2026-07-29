"""Private, profile-gated bridge for the Telegram physique check-in wizard.

This module deliberately has no import-time dependency on the profile-local
wizard package.  A normal Hermes profile therefore cannot accidentally enable
or load the check-in implementation merely by importing the Telegram adapter.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, cast
from zoneinfo import ZoneInfo

from gateway.platforms.physique_checkin_bindings import BindingStore, WizardBinding
from gateway.platforms.physique_checkin_config import PhysiqueCheckinConfig
from gateway.platforms.physique_checkin_prompts import WizardPrompt, build_wizard_prompt


_SESSION_ID: Final[re.Pattern[str]] = re.compile(r"^[a-f0-9]{32}$")
_STEP: Final[re.Pattern[str]] = re.compile(
    r"^(?:launch|done|bodyweight|sleep_duration|sleep_quality|condition|pain|calories|macros|"
    r"meals|water|digestion|appetite_stress|training_plan|optional_note|summary|edit_menu|"
    r"completion|training_summary|workout_quality|performance|intensity|operator_note|safety_ack|"
    r"Q-SLEEP-CAUSE|Q-SLEEP-ADJUST|Q-COND-SYMPTOM|Q-COND-INTENSITY|Q-PERF-REASON|Q-PERF-NEXT)$"
)
_ACTION: Final[re.Pattern[str]] = re.compile(r"^(?:start|a[0-8]|e(?:[0-9]|1[0-2]))$")
_ADAPTIVE_STEPS: Final[frozenset[str]] = frozenset({
    "Q-SLEEP-CAUSE",
    "Q-SLEEP-ADJUST",
    "Q-COND-SYMPTOM",
    "Q-COND-INTENSITY",
    "Q-PERF-REASON",
    "Q-PERF-NEXT",
})
_ADAPTIVE_CHOICES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "Q-SLEEP-CAUSE": (("카페인", "caffeine"), ("야근", "overtime"), ("스트레스", "stress"), ("기타", "other")),
    "Q-SLEEP-ADJUST": (("예", "yes"), ("아니오", "no")),
    "Q-COND-SYMPTOM": (("피로", "fatigue"), ("근육통", "muscle_soreness"), ("감기 기운", "cold"), ("기타", "other")),
    "Q-COND-INTENSITY": (("유지", "maintain"), ("하향", "reduce"), ("휴식", "rest")),
    "Q-PERF-REASON": (("시간 부족", "time_shortage"), ("컨디션", "condition"), ("통증", "pain"), ("기타", "other")),
    "Q-PERF-NEXT": (("예", "yes"), ("아니오", "no")),
}
_ADAPTIVE_PROMPT_TEXT: Final[dict[str, str]] = {
    "Q-SLEEP-CAUSE": "수면이 짧거나 질이 낮았던 가장 가까운 이유를 골라주세요.",
    "Q-SLEEP-ADJUST": "오늘 수면을 위해 조정 가능한 행동을 받아들일까요.",
    "Q-COND-SYMPTOM": "오늘 컨디션 저하와 가장 가까운 상태를 골라주세요.",
    "Q-COND-INTENSITY": "오늘 훈련 강도를 어떻게 조정할까요.",
    "Q-PERF-REASON": "수행이 계획보다 낮았던 가장 가까운 이유를 골라주세요.",
    "Q-PERF-NEXT": "다음 세션에서 조정을 적용할까요.",
}
_URGENT_TEXT: Final[re.Pattern[str]] = re.compile(
    r"흉통|가슴\s*통증|실신|호흡\s*곤란|chest\s*pain",
    re.IGNORECASE,
)
_KST: Final[ZoneInfo] = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class CallbackData:
    """A Telegram-safe opaque callback address, never a health-data carrier."""

    session_id: str
    step: str
    version: int
    action: str

    def encode(self) -> str:
        value = f"pc1:{self.session_id}:{self.step}:{self.version}:{self.action}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("callback exceeds Telegram's 64-byte limit")
        return value

    @classmethod
    def parse(cls, value: str) -> CallbackData | None:
        parts = value.split(":")
        if len(parts) != 5 or parts[0] != "pc1":
            return None
        _, session_id, step, version_text, action = parts
        if not _SESSION_ID.fullmatch(session_id) or not _STEP.fullmatch(step) or not _ACTION.fullmatch(action):
            return None
        try:
            version = int(version_text)
        except ValueError:
            return None
        if version < 0 or version > 1_000_000:
            return None
        parsed = cls(session_id, step, version, action)
        try:
            parsed.encode()
        except ValueError:
            return None
        return parsed


@dataclass(frozen=True, slots=True)
class WizardReply:
    """Adapter result; deliberately excludes typed health/training content."""

    handled: bool
    accepted: bool
    notice: str
    prompt: WizardPrompt | None
    callback_data: str | None


class _WizardService(Protocol):
    def start_morning(self, context: object, kst_day: str) -> object: ...
    def start_workout(self, context: object, kst_day: str) -> object: ...
    def start_nutrition(self, context: object, kst_day: str) -> object: ...
    def start_nutrition_correction(self, context: object, kst_day: str) -> object: ...
    def start_trainer_session(self, context: object, kst_day: str) -> object: ...
    def start_schedule_reference(self, context: object, kst_day: str) -> object: ...
    def answer(self, context: object, session_id: str, expected_version: int, action: str, value: str | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class _WizardContext:
    """The profile wizard needs the auth boundary and optional customer scope."""

    owner_id: str
    topic_id: str
    customer_key: str | None = None


class PhysiqueCheckinBridge:
    """Validate exact Telegram boundaries before delegating to profile state."""

    def __init__(
        self,
        config: PhysiqueCheckinConfig,
        service: object | None = None,
        binding_path: Path | None = None,
        *,
        customer_key: str | None = None,
    ) -> None:
        self.config = config
        scope = customer_key.strip() if isinstance(customer_key, str) else ""
        self.customer_key = scope if 1 <= len(scope) <= 64 else None
        self._service = self._load_profile_service() if service is None else cast(_WizardService, service)
        self._binding_store = BindingStore(binding_path or self._default_binding_path())
        self._bindings, self._active_session_id = self._binding_store.load(
            config.owner_id, config.chat_id, config.topic_id, self._now(None),
        )

    def open_launcher(self, flow: str, *, message_id: str = "", now_epoch: int | None = None) -> WizardReply:
        """Create/resume a draft; caller binds the actual sent card message id."""
        if flow in {"trainer_session", "schedule_reference"} and self.customer_key is None:
            return WizardReply(True, False, "트레이너 기록 고객 범위를 확인할 수 없습니다.", None, None)
        now = self._now(now_epoch)
        context = self._context()
        day = datetime.now(_KST).date().isoformat()
        if flow == "morning":
            result = self._service.start_morning(context, day)
        elif flow == "nutrition_daily":
            result = self._service.start_nutrition(context, day)
        elif flow == "trainer_session":
            result = self._service.start_trainer_session(context, day)
        elif flow == "schedule_reference":
            result = self._service.start_schedule_reference(context, day)
        else:
            result = self._service.start_workout(context, day)
        session_id, version = self._result_identity(result)
        self._bindings[session_id] = WizardBinding(
            session_id, self.config.owner_id, self.config.chat_id, self.config.topic_id,
            "launch", version, str(message_id), now + self.config.expires_seconds,
        )
        self._persist()
        callback = CallbackData(session_id, "launch", version, "start").encode()
        return WizardReply(True, True, "", None, callback)

    def open_trainer_launcher(self, *, message_id: str = "", now_epoch: int | None = None) -> WizardReply:
        """Open the bounded trainer session entry card."""
        return self.open_launcher("trainer_session", message_id=message_id, now_epoch=now_epoch)
    def open_schedule_reference_launcher(
        self, *, message_id: str = "", now_epoch: int | None = None
    ) -> WizardReply:
        """Open the trainer-scoped schedule source-fact entry card."""
        return self.open_launcher("schedule_reference", message_id=message_id, now_epoch=now_epoch)

    def open_nutrition_correction(self, *, now_epoch: int | None = None) -> WizardReply:
        now = self._now(now_epoch)
        day = datetime.now(_KST).date().isoformat()
        result = self._service.start_nutrition_correction(self._context(), day)
        session_id, version = self._result_identity(result)
        if _SESSION_ID.fullmatch(session_id) is None:
            return WizardReply(True, False, "수정할 오늘 체크인이 없습니다.", None, None)
        step = str(getattr(result, "step", "bodyweight"))
        binding = WizardBinding(
            session_id, self.config.owner_id, self.config.chat_id, self.config.topic_id,
            step, version, "", now + self.config.expires_seconds,
            awaiting_text=self._expects_text(step),
        )
        self._bindings[session_id] = binding
        self._active_session_id = session_id
        self._persist()
        return WizardReply(True, True, "기존 기록은 보존하고 수정 기록을 새로 시작합니다.", self._prompt(binding), None)

    def open_trainer_correction(self, *, now_epoch: int | None = None) -> WizardReply:
        """Open an editable correction while preserving the saved trainer event."""
        starter = getattr(self._service, "start_trainer_session_correction", None)
        if not callable(starter):
            return WizardReply(True, False, "저장된 기록의 수정 기능을 사용할 수 없습니다.", None, None)
        now = self._now(now_epoch)
        day = datetime.now(_KST).date().isoformat()
        result = starter(self._context(), day)
        session_id, version = self._result_identity(result)
        if _SESSION_ID.fullmatch(session_id) is None:
            return WizardReply(True, False, "수정할 오늘 트레이너 기록이 없습니다.", None, None)
        step = str(getattr(result, "step", "summary"))
        binding = WizardBinding(
            session_id, self.config.owner_id, self.config.chat_id, self.config.topic_id,
            step, version, "", now + self.config.expires_seconds,
            awaiting_text=self._expects_text(step),
        )
        self._bindings[session_id] = binding
        self._active_session_id = session_id
        self._persist()
        return WizardReply(
            True,
            True,
            "기존 기록은 보존하고 수정본을 새로 만듭니다.",
            self._prompt(binding),
            None,
        )

    def active_is_finalized(self) -> bool:
        binding = self._bindings.get(self._active_session_id or "")
        storage = getattr(self._service, "_storage", None)
        session = storage.load(binding.session_id) if binding is not None and storage is not None else None
        return session is not None and getattr(session, "finalized_event_id", None) is not None

    def bind_launcher_message(self, session_id: str, message_id: str) -> bool:
        binding = self._bindings.get(session_id)
        if binding is None or not message_id:
            return False
        binding.message_id = str(message_id)
        self._persist()
        return True

    def bind_prompt_message(self, session_id: str, message_id: str) -> bool:
        return self.bind_launcher_message(session_id, message_id)

    def bind_active_prompt_message(self, message_id: str) -> bool:
        """Bind a follow-up typed-answer prompt to the same private session."""
        binding = self._bindings.get(self._active_session_id or "")
        if binding is None or not message_id:
            return False
        binding.message_id = str(message_id)
        self._persist()
        return True
    def has_binding(self, session_id: object) -> bool:
        """Return whether this bridge owns a live persisted wizard session."""
        if not isinstance(session_id, str):
            return False
        binding = self._bindings.get(session_id)
        return binding is not None and self._now(None) < binding.expires_at

    def has_active_binding(self) -> bool:
        """Return whether this bridge has a live session for typed continuation."""
        return self.has_binding(self._active_session_id)

    def active_prompt(self) -> WizardPrompt | None:
        """Return the current prompt for private-DM recovery without advancing state."""
        binding = self._bindings.get(self._active_session_id or "")
        if binding is None or self._now(None) >= binding.expires_at:
            return None
        return self._prompt(binding)

    def bind_active_message(self, message_id: str) -> None:
        """Move the active callback card to a newly rendered recovery message."""
        binding = self._bindings.get(self._active_session_id or "")
        if binding is None or not str(message_id).strip():
            return
        binding.message_id = str(message_id)
        self._persist()

    @property
    def binding_path(self) -> Path:
        """Expose the durable binding location for diagnostics and tests."""
        return self._binding_store._path

    def handle_callback(self, data: str, owner_id: str, chat_id: str, topic_id: str, message_id: str, *, now_epoch: int | None = None) -> WizardReply:
        parsed = CallbackData.parse(data)
        if parsed is None:
            return WizardReply(True, False, "체크인 버튼 형식이 올바르지 않습니다.", None, None)
        binding = self._bindings.get(parsed.session_id)
        if not self._matches(owner_id, chat_id, topic_id) or binding is None:
            return WizardReply(True, False, "이 체크인은 이 토픽의 소유자만 사용할 수 있습니다.", None, None)
        if self._now(now_epoch) >= binding.expires_at:
            return WizardReply(True, False, "체크인이 만료됐습니다. 새로 시작해 주세요.", None, None)
        if str(message_id) != binding.message_id or parsed.step != binding.step or parsed.version != binding.version or binding.awaiting_text:
            return WizardReply(True, False, "이전 버튼입니다. 현재 질문에서 이어가 주세요.", None, None)
        if parsed.step == "launch":
            if parsed.action != "start":
                return WizardReply(True, False, "시작 버튼만 사용할 수 있습니다.", None, None)
            binding.step = self._session_step(parsed.session_id)
            binding.awaiting_text = self._expects_text(binding.step)
            self._active_session_id = binding.session_id
            self._persist()
            return WizardReply(True, True, "", self._prompt(binding), None)
        if parsed.step == "summary" and parsed.action == "a1":
            binding.step = "edit_menu"
            binding.awaiting_text = False
            self._persist()
            return WizardReply(True, True, "", self._prompt(binding), None)
        if parsed.step == "summary" and parsed.action == "a2":
            return WizardReply(True, True, "임시저장했습니다.", self._prompt(binding), None)
        flow = self._session_flow(binding.session_id)
        back_action = "e12" if flow == "nutrition_daily" else "e5" if flow in {"trainer_session", "trainer"} else "e11"
        if parsed.step == "edit_menu" and parsed.action == back_action:
            binding.step = "summary"
            binding.awaiting_text = False
            self._persist()
            return WizardReply(True, True, "", self._prompt(binding), None)
        transition = self._callback_transition(binding.step, parsed.action, self._session_flow(binding.session_id))
        if transition is None:
            return WizardReply(True, False, "이 선택은 현재 질문에 맞지 않습니다.", None, None)
        action, value, awaiting_text = transition
        if awaiting_text:
            binding.awaiting_text = True
            self._active_session_id = binding.session_id
            self._persist()
            return WizardReply(True, True, "", self._prompt(binding), None)
        return self._advance(binding, action, value)

    def handle_text(self, text: str, owner_id: str, chat_id: str, topic_id: str, *, now_epoch: int | None = None) -> WizardReply | None:
        binding = self._bindings.get(self._active_session_id or "")
        if binding is None:
            return None
        if not self._matches(owner_id, chat_id, topic_id):
            return WizardReply(True, False, "이 체크인은 이 토픽의 소유자만 사용할 수 있습니다.", None, None)
        if self._now(now_epoch) >= binding.expires_at:
            return WizardReply(True, False, "체크인이 만료됐습니다. 새로 시작해 주세요.", None, None)
        if _URGENT_TEXT.search(text):
            binding.awaiting_text = False
            return self._advance(binding, "value", text)
        flow = self._session_flow(binding.session_id)
        if flow in {"trainer_session", "trainer"} and binding.step == "edit_menu":
            selected = self._advance(binding, "edit", "training_summary")
            if not selected.accepted:
                return selected
            return self._advance(binding, "value", text)
        if not (binding.awaiting_text or self._expects_text(binding.step)):
            return None
        binding.awaiting_text = False
        return self._advance(binding, "value", text)

    def active_checkin_snapshot(self) -> dict[str, object] | None:
        binding = self._bindings.get(self._active_session_id or "")
        storage = getattr(self._service, "_storage", None)
        session = storage.load(binding.session_id) if binding is not None and storage is not None else None
        if binding is None or session is None:
            return None
        answers = getattr(session, "answers", {})
        if not isinstance(answers, dict):
            return None
        return {
            "flow": str(getattr(session, "flow", "")),
            "kst_day": str(getattr(session, "kst_day", "")),
            "step": binding.step,
            "answers": {key: (value or None) for key, value in answers.items() if isinstance(key, str) and isinstance(value, str)},
        }

    def active_prompt_message_id(self) -> str | None:
        """Return the bound active card address without exposing check-in values."""
        binding = self._bindings.get(self._active_session_id or "")
        return binding.message_id if binding is not None else None

    def apply_model_action(self, action: str, value: str | None) -> WizardReply:
        binding = self._bindings.get(self._active_session_id or "")
        if binding is None:
            return WizardReply(True, False, "활성 체크인을 찾지 못했습니다.", None, None)
        if not self._is_allowed_model_action(binding.step, action, value):
            return WizardReply(True, False, "모델 해석을 적용하지 않았습니다.", self._prompt(binding), None)
        if action in {"stay", "clarify_current_step", "rewrite_prompt"}:
            binding.awaiting_text = self._expects_text(binding.step)
            self._persist()
            return WizardReply(True, True, "", self._prompt(binding), None)
        return self._advance(binding, action, value)

    def accepts_context(self, owner_id: str, chat_id: str, topic_id: str) -> bool:
        """Expose the exact non-sensitive boundary for manual card recovery."""
        return self._matches(owner_id, chat_id, topic_id)

    def finalized_coaching_snapshot(self, session_id: str) -> dict[str, object] | None:
        """Return one finalized, exact-bound local record for optional coaching."""
        return self._finalized_snapshot(session_id)

    def finalized_event(self, session_id: str) -> object | None:
        """Return the canonical finalized Event through the profile service."""
        if not _SESSION_ID.fullmatch(session_id):
            return None
        finalized = getattr(self._service, "finalized_event", None)
        if not callable(finalized):
            return None
        try:
            event = finalized(session_id)
        except (OSError, ValueError, TypeError):
            return None
        return event

    def append_event(self, event: object) -> object | None:
        """Append a validated canonical event without exposing storage internals."""
        append = getattr(self._service, "append_wizard_event", None)
        if callable(append):
            return append(event)
        events = getattr(self._service, "_events", None)
        append = getattr(events, "append_wizard_event", None)
        if callable(append):
            return append(event)
        return None

    def finalized_safety_snapshot(self, session_id: str) -> dict[str, object] | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        storage = getattr(self._service, "_storage", None)
        session = storage.load(session_id) if storage is not None else None
        if session is None or getattr(session, "finalized_event_id", None) is None:
            return None
        if (
            str(getattr(session, "owner_id", "")) != self.config.owner_id
            or str(getattr(session, "topic_id", "")) != self.config.topic_id
        ):
            return None
        event = self.finalized_event(session_id)
        safety = getattr(event, "safety", None) if event is not None else None
        signals = tuple(getattr(safety, "signals", ()) or ()) or tuple(
            getattr(session, "safety_signals", ()) or ()
        )
        reasons = tuple(getattr(safety, "reasons", ()) or ()) or tuple(
            getattr(session, "safety_reasons", ()) or ()
        )
        if not signals and not reasons:
            return None
        return {
            "kst_day": str(getattr(session, "kst_day", "")),
            "safety_held": True,
            "event": event,
        }

    @staticmethod
    def _snapshot_branch_state(session: object) -> dict[str, object]:
        branch = getattr(session, "branch", None)
        branch_value = getattr(branch, "value", branch)
        if branch_value not in {"normal", "anomaly", "change", "safety_hold"}:
            branch_value = None
        detailed = branch_value in {"anomaly", "change", "safety_hold"}
        follow_up_ids = tuple(
            item for item in (getattr(session, "follow_up_ids", ()) or ())
            if isinstance(item, str) and item in _ADAPTIVE_STEPS
        )
        return {
            "branch": branch_value,
            "detailed": detailed,
            "follow_up_ids": follow_up_ids,
        }

    def _finalized_snapshot(self, session_id: str) -> dict[str, object] | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        storage = getattr(self._service, "_storage", None)
        session = storage.load(session_id) if storage is not None else None
        if session is None or getattr(session, "finalized_event_id", None) is None:
            return None
        if (
            str(getattr(session, "owner_id", "")) != self.config.owner_id
            or str(getattr(session, "topic_id", "")) != self.config.topic_id
            or tuple(getattr(session, "safety_signals", ()) or ())
            or tuple(getattr(session, "safety_reasons", ()) or ())
        ):
            return None
        raw_answers = getattr(session, "answers", {})
        if not isinstance(raw_answers, dict):
            return None
        allowed = {
            "bodyweight", "sleep_duration", "sleep_quality", "condition", "pain",
            "calories", "training_plan", "optional_note", "completion",
            "training_summary", "workout_quality", "macros", "meals", "water",
            "digestion", "appetite_stress", "performance", "intensity", "operator_note",
        }
        branch_state = self._snapshot_branch_state(session)
        return {
            "flow": str(getattr(session, "flow", "")),
            "kst_day": str(getattr(session, "kst_day", "")),
            "answers": {
                key: str(value)[:4_000]
                for key, value in raw_answers.items()
                if key in allowed and isinstance(value, str)
            },
            **branch_state,
        }

    def finalized_trainer_snapshot(self, session_id: str) -> dict[str, object] | None:
        """Return a safe, finalized trainer record for owner-side projection."""
        snapshot = self._finalized_snapshot(session_id)
        if snapshot is None or snapshot.get("flow") not in {"trainer_session", "trainer"}:
            return None
        return snapshot

    def active_finalized_coaching_snapshot(self) -> dict[str, object] | None:
        """Return the currently bound saved check-in for an explicit feedback replay."""
        binding = self._bindings.get(self._active_session_id or "")
        return self.finalized_coaching_snapshot(binding.session_id) if binding is not None else None

    def latest_finalized_coaching_snapshot(self) -> dict[str, object] | None:
        """Return today's newest finalized check-in despite later unfinished launchers."""
        storage = getattr(self._service, "_storage", None)
        if storage is None:
            return None
        with storage.locked():
            session = storage.find_latest_finalized(
                self.config.owner_id,
                self.config.topic_id,
                datetime.now(_KST).date().isoformat(),
            )
        return self._coaching_snapshot(session)

    def _coaching_snapshot(self, session: object | None) -> dict[str, object] | None:
        if session is None or getattr(session, "finalized_event_id", None) is None:
            return None
        if (
            str(getattr(session, "owner_id", "")) != self.config.owner_id
            or str(getattr(session, "topic_id", "")) != self.config.topic_id
            or tuple(getattr(session, "safety_signals", ()) or ())
            or tuple(getattr(session, "safety_reasons", ()) or ())
        ):
            return None
        raw_answers = getattr(session, "answers", {})
        if not isinstance(raw_answers, dict):
            return None
        allowed = {
            "bodyweight", "sleep_duration", "sleep_quality", "condition",
            "pain", "calories", "training_plan", "optional_note",
            "completion", "training_summary", "workout_quality",
            "macros", "meals", "water", "digestion", "appetite_stress",
            "performance", "intensity", "operator_note",
        }
        answers = {
            key: str(value)[:4_000]
            for key, value in raw_answers.items()
            if key in allowed and isinstance(value, str)
        }
        branch_state = self._snapshot_branch_state(session)
        return {
            "session_id": str(getattr(session, "session_id", "")),
            "flow": str(getattr(session, "flow", "")),
            "kst_day": str(getattr(session, "kst_day", "")),
            "answers": answers,
            **branch_state,
        }

    def _advance(self, binding: WizardBinding, action: str, value: str | None) -> WizardReply:
        result = self._service.answer(self._context(), binding.session_id, binding.version, action, value)
        status = str(getattr(result, "status", ""))
        if status not in {"advanced", "WizardStatus.ADVANCED", "saved", "WizardStatus.SAVED", "safety_stop", "WizardStatus.SAFETY_STOP"}:
            return WizardReply(
                True,
                False,
                "입력을 확인하고 다시 시도해 주세요.",
                self._invalid_text_prompt(binding),
                None,
            )
        binding.version = int(getattr(result, "version"))
        binding.step = str(getattr(result, "step"))
        binding.awaiting_text = self._expects_text(binding.step)
        self._active_session_id = binding.session_id
        self._persist()
        if status in {"saved", "WizardStatus.SAVED"}:
            return WizardReply(
                True,
                True,
                "체크인을 저장했습니다.",
                WizardPrompt("✅ 오늘 체크인을 저장했습니다."),
                None,
            )
        return WizardReply(True, True, "", self._prompt(binding), None)

    def _prompt(self, binding: WizardBinding) -> WizardPrompt:
        if binding.step in _ADAPTIVE_STEPS:
            return self._adaptive_prompt(binding)
        flow = self._session_flow(binding.session_id)
        if flow == "schedule_reference":
            return self._schedule_reference_prompt(binding)
        if flow in {"trainer_session", "trainer"}:
            return self._trainer_prompt(binding)
        if flow == "nutrition_daily" and binding.step == "summary":
            return self._nutrition_summary_prompt(binding)
        return build_wizard_prompt(
            binding.step,
            binding.awaiting_text,
            lambda action: CallbackData(binding.session_id, binding.step, binding.version, action).encode(),
            flow=flow,
        )

    def _nutrition_summary_prompt(self, binding: WizardBinding) -> WizardPrompt:
        callback = lambda action: CallbackData(
            binding.session_id, binding.step, binding.version, action
        ).encode()
        session = self._service._storage.load(binding.session_id)
        answers = dict(getattr(session, "answers", {}) or {})
        digestion = {
            "bristol_1": "브리스톨 1 · 딱딱한 알갱이",
            "bristol_2": "브리스톨 2 · 울퉁불퉁한 소시지",
            "bristol_3": "브리스톨 3 · 갈라진 소시지",
            "bristol_4": "브리스톨 4 · 매끈하고 부드러움",
            "bristol_5": "브리스톨 5 · 부드러운 덩어리",
            "bristol_6": "브리스톨 6 · 묽고 풀어진 변",
            "bristol_7": "브리스톨 7 · 완전한 물변",
            "gas_bloating": "가스·복부 팽만",
        }.get(str(answers.get("digestion", "")), str(answers.get("digestion", "")))
        training = str(answers.get("training_summary", "") or "").strip()
        lines = [
            "입력한 내용을 확인해 주세요.",
            "",
            f"체중: {answers.get('bodyweight', '-')}kg",
            f"칼로리: {answers.get('calories', '-')}kcal",
            f"탄·단·지: {answers.get('macros', '-')}",
            f"식사: {answers.get('meals', '-')}",
            f"수분: {answers.get('water', '-')}L",
            f"수면: {answers.get('sleep_duration', '-')}시간 / 질 {answers.get('sleep_quality', '-')}/5",
            f"배변·소화: {digestion or '-'}",
            f"컨디션: {answers.get('condition', '-')}/5",
            f"식욕·스트레스: {answers.get('appetite_stress', '-')}",
            f"운동: {training if training else '트레이너 기록 사용'}",
        ]
        note = str(answers.get("optional_note", "") or "").strip()
        if note:
            lines.append(f"특이사항: {note}")
        buttons = (("저장", callback("a0")), ("수정", callback("a1")), ("임시저장", callback("a2")))
        return WizardPrompt("\n".join(lines), buttons, (buttons,))

    def _adaptive_prompt(self, binding: WizardBinding) -> WizardPrompt:
        callback = lambda action: CallbackData(
            binding.session_id, binding.step, binding.version, action
        ).encode()
        choices = _ADAPTIVE_CHOICES[binding.step]
        buttons = tuple(
            (label, callback(f"a{index}"))
            for index, (label, _) in enumerate(choices)
        )
        rows = tuple(buttons[index:index + 2] for index in range(0, len(buttons), 2))
        return WizardPrompt(_ADAPTIVE_PROMPT_TEXT[binding.step], buttons, rows)

    def _schedule_reference_prompt(self, binding: WizardBinding) -> WizardPrompt:
        callback = lambda action: CallbackData(
            binding.session_id, binding.step, binding.version, action
        ).encode()
        if binding.awaiting_text:
            labels = {
                "session_kst_date": "확인한 수업 날짜를 YYYY-MM-DD 형식으로 입력해 주세요.",
                "session_start_kst": "확인한 시작 시간을 HH:MM 형식으로 입력해 주세요.",
                "last_change_note": "최근 변경 사항 또는 확인 근거를 짧게 입력해 주세요.",
            }
            return WizardPrompt(labels.get(binding.step, "현재 항목을 입력해 주세요."))
        if binding.step in {"customer_confirmed", "trainer_confirmed"}:
            label = "고객" if binding.step == "customer_confirmed" else "트레이너"
            buttons = (("확인함", callback("a0")), ("확인되지 않음", callback("a1")))
            return WizardPrompt(f"{label}의 일정을 직접 확인했나요?", buttons, (buttons,))
        if binding.step == "summary":
            session = self._service._storage.load(binding.session_id)
            answers = dict(getattr(session, "answers", {}) or {})
            buttons = (("저장", callback("a0")),)
            return WizardPrompt(
                "아래 일정 원본 사실을 저장할까요?\n\n"
                f"날짜: {answers.get('session_kst_date', '-')}\n"
                f"시작: {answers.get('session_start_kst', '-')}\n"
                f"고객 확인: {answers.get('customer_confirmed', '-')}\n"
                f"트레이너 확인: {answers.get('trainer_confirmed', '-')}\n"
                f"변경 메모: {answers.get('last_change_note', '-')}",
                buttons,
                (buttons,),
            )
        return WizardPrompt("일정 확인을 이어가 주세요.")
    def _trainer_prompt(self, binding: WizardBinding) -> WizardPrompt:
        callback = lambda action: CallbackData(
            binding.session_id, binding.step, binding.version, action
        ).encode()
        if binding.awaiting_text:
            if binding.step == "training_summary":
                return WizardPrompt(
                    "수정할 운동 내용을 아래쪽 메시지 입력창에 편하게 적어주세요.\n"
                    "운동 부위, 운동 종목, 세트 수, 총 운동시간을 한 문장으로 보내면 됩니다.\n\n"
                    "예:\n하체 중심으로 스쿼트 5세트, 런지 4세트,\n총 운동시간은 55분입니다."
                )
            if binding.step == "pain":
                return WizardPrompt("통증 위치, 강도(0~10), 언제부터인지 적어주세요.")
            if binding.step == "operator_note":
                return WizardPrompt("운영 메모가 있으면 짧게 적어주세요.")
            return WizardPrompt("현재 질문에 대한 답을 짧게 입력해 주세요.")
        if binding.step == "done":
            buttons = (
                ("완료", callback("a0")),
                ("부분 완료", callback("a1")),
                ("휴식·변경", callback("a2")),
            )
            return WizardPrompt("이번 트레이너 세션 상태를 골라주세요.", buttons, (buttons,))
        if binding.step == "performance":
            return WizardPrompt(
                "운동 수행도 점수(1=매우 낮음 · 5=매우 좋음)를 골라주세요.",
                tuple((str(index), callback(f"a{index}")) for index in range(1, 6)),
            )
        if binding.step == "intensity":
            return WizardPrompt(
                "계획 대비 운동 강도를 골라주세요.",
                (("낮음", callback("a0")), ("계획대로", callback("a1")), ("높음", callback("a2"))),
            )
        if binding.step == "pain":
            return WizardPrompt(
                "통증·이상 신호가 있으면 알려주세요.",
                (("없음", callback("a0")), ("있음 · 입력", callback("a1"))),
            )
        if binding.step == "operator_note":
            return WizardPrompt(
                "운영 메모를 남길까요.",
                (("없음 · 계속", callback("a0")), ("입력", callback("a1"))),
            )
        if binding.step == "summary":
            session = self._service._storage.load(binding.session_id)
            answers = dict(getattr(session, "answers", {}) or {})
            intensity_label = {
                "below": "계획보다 낮음",
                "as_planned": "계획대로",
                "above": "계획보다 높음",
            }.get(str(answers.get("intensity", "")), str(answers.get("intensity", "")))
            pain = str(answers.get("pain", "") or "없음")
            note = str(answers.get("operator_note", "") or "")
            lines = [
                "아래 내용으로 트레이너 기록을 저장할까요?",
                "",
                f"운동 내용: {str(answers.get('training_summary', '') or '').strip()}",
                f"수행도: {str(answers.get('performance', '') or '-')} / 5",
                f"강도: {intensity_label or '-'}",
                f"통증: {pain}",
            ]
            if note and note != "skip":
                lines.append(f"운영 메모: {note}")
            buttons = (("저장", callback("a0")), ("수정", callback("a1")), ("임시저장", callback("a2")))
            return WizardPrompt("\n".join(lines), buttons, (buttons,))
        if binding.step == "edit_menu":
            fields = (("운동 내용", "training_summary"), ("수행도", "performance"), ("강도", "intensity"), ("통증", "pain"), ("운영 메모", "operator_note"))
            buttons = tuple((label, callback(f"e{index}")) for index, (label, _) in enumerate(fields))
            back = ("요약으로 돌아가기", callback("e5"))
            rows = tuple(buttons[index:index + 2] for index in range(0, len(buttons), 2)) + ((back,),)
            return WizardPrompt(
                "고칠 내용을 선택하세요.\n"
                "예: 운동 내용을 고치려면 아래 ‘운동 내용’을 누른 뒤 메시지 입력창에 새 내용을 보내세요.",
                buttons + (back,),
                rows,
            )
        if binding.step == "safety_ack":
            return WizardPrompt(
                "기록을 중단했습니다. 필요한 진료를 먼저 확인해 주세요.",
                (("확인", callback("a0")),),
            )
        return WizardPrompt("트레이너 기록을 이어가 주세요.")

    @staticmethod
    def _invalid_text_prompt(binding: WizardBinding) -> WizardPrompt:
        if binding.step == "bodyweight":
            return WizardPrompt(
                "오늘 체중 기록을 못 했구나. 체중을 비워 둔 채 저장하면 추이를 왜곡해서 아직 저장하지 않을게. "
                "나중에 측정한 숫자를 보내면 여기서 이어갈게."
            )
        labels = {
            "sleep_duration": "수면 시간",
            "calories": "어제 총칼로리",
        }
        label = labels.get(binding.step, "현재 항목")
        return WizardPrompt(f"{label}은(는) 숫자로 보내주세요. 현재 입력은 기록하지 않았습니다.")

    @staticmethod
    def _callback_transition(step: str, action: str, flow: str) -> tuple[str, str | None, bool] | None:
        if step in _ADAPTIVE_STEPS:
            choices = _ADAPTIVE_CHOICES[step]
            if action.startswith("a") and action[1:].isdigit():
                index = int(action[1:])
                if index < len(choices):
                    return "select", choices[index][1], False
            return None
        if flow == "schedule_reference" and step in {"customer_confirmed", "trainer_confirmed"}:
            confirmed = {"a0": "true", "a1": "false"}
            return ("select", confirmed[action], False) if action in confirmed else None
        if flow in {"trainer_session", "trainer"} and step == "done":
            done = {"a0": "complete", "a1": "partial", "a2": "rest_changed"}
            return ("select", done[action], False) if action in done else None
        if step in {"sleep_quality", "condition", "workout_quality"} and action in {"a1", "a2", "a3", "a4", "a5"}:
            return "select", action[1:], False
        if flow in {"trainer_session", "trainer"} and step == "performance" and action in {"a1", "a2", "a3", "a4", "a5"}:
            return "select", action[1:], False
        if flow in {"trainer_session", "trainer"} and step == "intensity":
            intensity = {"a0": "below", "a1": "as_planned", "a2": "above"}
            return ("select", intensity[action], False) if action in intensity else None
        if step == "pain":
            return ("select", "none", False) if action == "a0" else ("value", None, True) if action == "a1" else None
        if flow in {"trainer_session", "trainer"} and step == "operator_note":
            return ("select", "skip", False) if action == "a0" else ("value", None, True) if action == "a1" else None
        if step == "digestion":
            digestion = {
                "a0": "bristol_1", "a1": "bristol_2", "a2": "bristol_3",
                "a3": "bristol_4", "a4": "bristol_5", "a5": "bristol_6",
                "a6": "bristol_7", "a7": "gas_bloating",
            }
            if action in digestion:
                return "select", digestion[action], False
            return ("value", None, True) if action == "a8" else None
        if step == "training_plan":
            return ("select", "rest", False) if action == "a0" else ("select", "undecided", False) if action == "a1" else ("value", None, True) if action == "a2" else None
        if step == "optional_note":
            return ("select", "skip", False) if action == "a0" else ("value", None, True) if action == "a1" else None
        if step == "completion":
            return ("select", ("complete", "partial", "rest_changed")[int(action[1:])], False) if action in {"a0", "a1", "a2"} else None
        if flow == "nutrition_daily" and step == "training_summary":
            if action == "a0":
                return "select", "trainer_recorded", False
            if action == "a1":
                return "value", None, True
            return None
        if step == "summary" and action == "a0":
            return "save", None, False
        editable = {
            "morning": ("bodyweight", "sleep_duration", "sleep_quality", "condition", "pain", "calories", "training_plan", "optional_note"),
            "workout": ("completion", "training_summary", "workout_quality", "pain"),
            "nutrition_daily": (
                "bodyweight", "calories", "macros", "meals", "water", "sleep_duration",
                "sleep_quality", "digestion", "condition", "appetite_stress",
                "training_summary", "optional_note",
            ),
            "trainer_session": ("training_summary", "performance", "intensity", "pain", "operator_note"),
            "trainer": ("training_summary", "performance", "intensity", "pain", "operator_note"),
        }.get(flow, ())
        if step == "edit_menu" and action.startswith("e") and action[1:].isdigit():
            index = int(action[1:])
            if index < len(editable):
                return "edit", editable[index], False
        if step == "safety_ack" and action == "a0":
            return "acknowledge", None, False
        return None

    @staticmethod
    def _expects_text(step: str) -> bool:
        return step in {
            "bodyweight", "sleep_duration", "calories", "macros", "meals", "water",
            "appetite_stress", "training_summary", "session_kst_date", "session_start_kst",
            "last_change_note",
        }

    @staticmethod
    def _is_allowed_model_action(step: str, action: str, value: str | None) -> bool:
        if action in {"stay", "clarify_current_step", "rewrite_prompt"}:
            return value is None
        if step == "bodyweight":
            return (action == "skip" and value is None) or (action == "value" and value is not None)
        if step in {
            "sleep_duration", "calories", "macros", "meals", "water", "appetite_stress",
            "training_summary", "operator_note", "session_kst_date", "session_start_kst",
            "last_change_note",
        }:
            return action == "value" and value is not None
        if step in {"sleep_quality", "condition", "workout_quality", "completion", "done", "performance"}:
            return action == "select" and value is not None
        if step == "intensity":
            return action == "select" and value in {"below", "as_planned", "above"}
        if step == "pain":
            return (action == "select" and value == "none") or (action == "value" and value is not None)
        if step == "digestion":
            return (action == "select" and value == "normal") or (action == "value" and value is not None)
        if step == "training_plan":
            return (action == "select" and value in {"rest", "undecided"}) or (action == "value" and value is not None)
        if step == "optional_note":
            return (action == "select" and value == "skip") or (action == "value" and value is not None)
        return False

    def _matches(self, owner_id: str, chat_id: str, topic_id: str) -> bool:
        return (str(owner_id), str(chat_id), str(topic_id)) == (self.config.owner_id, self.config.chat_id, self.config.topic_id)

    def _persist(self) -> None:
        self._binding_store.save(self._bindings, self._active_session_id)

    @staticmethod
    def _now(value: int | None) -> int:
        return int(time.time()) if value is None else value

    def _context(self) -> object:
        return _WizardContext(
            owner_id=self.config.owner_id,
            topic_id=self.config.topic_id,
            customer_key=self.customer_key,
        )

    def _session_step(self, session_id: str) -> str:
        storage = getattr(self._service, "_storage", None)
        session = storage.load(session_id) if storage is not None else None
        return str(getattr(session, "step", "bodyweight"))

    def _session_flow(self, session_id: str) -> str:
        storage = getattr(self._service, "_storage", None)
        session = storage.load(session_id) if storage is not None else None
        return str(getattr(session, "flow", "morning"))

    @staticmethod
    def _result_identity(result: object) -> tuple[str, int]:
        return str(getattr(result, "session_id")), int(getattr(result, "version"))

    @staticmethod
    def _load_profile_service() -> _WizardService:
        from hermes_cli.config import get_hermes_home
        package_root = Path(get_hermes_home()) / "workspace" / "checkin_cli"
        if not package_root.is_dir():
            raise RuntimeError("physique check-in package is unavailable")
        package_text = str(package_root)
        if package_text not in sys.path:
            sys.path.insert(0, package_text)
        from checkin_cli.wizard import WizardService
        return cast(
            _WizardService,
            WizardService.for_standalone(Path(get_hermes_home()) / "data" / "wizard"),
        )

    @staticmethod
    def _default_binding_path() -> Path:
        from hermes_cli.config import get_hermes_home
        return Path(get_hermes_home()) / "data" / "wizard" / "telegram-bindings.json"
