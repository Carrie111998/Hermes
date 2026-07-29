"""Hermetic contract tests for the dormant physique-checkin Telegram seam."""

from __future__ import annotations
import asyncio
import json

import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock() -> None:
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    module = MagicMock()
    module.ext.ContextTypes.DEFAULT_TYPE = type(None)
    module.constants.ParseMode.MARKDOWN = "Markdown"
    module.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    module.constants.ParseMode.HTML = "HTML"
    module.constants.ChatType.PRIVATE = "private"
    module.constants.ChatType.GROUP = "group"
    module.constants.ChatType.SUPERGROUP = "supergroup"
    module.constants.ChatType.CHANNEL = "channel"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, module)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.platforms.physique_checkin import CallbackData, PhysiqueCheckinBridge, PhysiqueCheckinConfig, WizardPrompt, WizardReply
from gateway.platforms.physique_checkin_bindings import WizardBinding
from gateway.platforms.physique_checkin_prompts import build_wizard_prompt
from gateway.platforms.telegram import TelegramAdapter
from gateway.platforms.korean_humanizer import (
    AdaptiveGroundingInput,
    DailyGroundingInput,
)
from cron.physique_inline_card import launch_morning_card


@dataclass
class _FakeResult:
    session_id: str = "0123456789abcdef0123456789abcdef"
    version: int = 1
    step: str = "sleep_quality"
    message: str = "advanced"
    status: str = "advanced"


class _FakeService:
    def __init__(self) -> None:
        self.answers: list[tuple[str, int, str, str | None]] = []

    def start_morning(self, context, day):
        return _FakeResult(version=0, step="bodyweight")

    def start_workout(self, context, day):
        return _FakeResult(version=0, step="completion")

    def answer(self, context, session_id, version, action, value=None):
        self.answers.append((session_id, version, action, value))
        return _FakeResult()


def _config() -> PhysiqueCheckinConfig:
    parsed = PhysiqueCheckinConfig.from_extra({
        "physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "chat-1",
            "topic_id": "topic-1",
            "expires_seconds": 600,
        }
    })
    assert parsed is not None
    return parsed


def _bridge(tmp_path: Path) -> tuple[PhysiqueCheckinBridge, _FakeService]:
    service = _FakeService()
    return PhysiqueCheckinBridge(_config(), service=service, binding_path=tmp_path / "bindings.json"), service


def _callback(data: str, *, owner: str = "owner-1", chat: str = "chat-1", topic: str = "topic-1", message: str = "44"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat
    query.message.message_thread_id = topic
    query.message.message_id = message
    query.message.chat = SimpleNamespace(type="supergroup")
    query.from_user = SimpleNamespace(id=owner, first_name="Owner")
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


class TestPhysiqueCallbackGrammar:
    def test_callback_data_is_compact_and_contains_no_answer(self):
        callback = CallbackData("0123456789abcdef0123456789abcdef", "bodyweight", 123, "a0")
        encoded = callback.encode()

        assert len(encoded.encode("utf-8")) <= 64
        assert encoded == "pc1:0123456789abcdef0123456789abcdef:bodyweight:123:a0"
        assert CallbackData.parse(encoded) == callback
        assert CallbackData.parse("pc1:bad:bodyweight:0:a0") is None
        assert CallbackData.parse("pc1:" + "a" * 32 + ":bodyweight:0:70.2") is None

    def test_callback_data_rejects_oversize_or_personal_value_actions(self):
        assert CallbackData.parse("pc1:" + "a" * 32 + ":bodyweight:0:" + "a" * 21) is None
        assert CallbackData.parse("pc1:" + "a" * 32 + ":bodyweight:0:weight70") is None

    def test_customer_nutrition_steps_fit_the_opaque_callback_contract(self):
        encoded = CallbackData("a" * 32, "macros", 2, "a0").encode()

        assert CallbackData.parse(encoded) is not None
    @pytest.mark.parametrize(
        "step",
        (
            "Q-SLEEP-CAUSE",
            "Q-SLEEP-ADJUST",
            "Q-COND-SYMPTOM",
            "Q-COND-INTENSITY",
            "Q-PERF-REASON",
            "Q-PERF-NEXT",
        ),
    )
    def test_callback_grammar_accepts_only_canonical_adaptive_steps(self, step):
        encoded = CallbackData("a" * 32, step, 2, "a0").encode()
        assert CallbackData.parse(encoded) is not None
        assert CallbackData.parse(encoded.replace(step, "Q-UNKNOWN")) is None


class TestPhysiqueCheckinBridge:
    def test_finalized_snapshot_exports_branch_state_for_grounding(self, tmp_path):
        service = _FakeService()
        service._storage = MagicMock()
        service._storage.load.return_value = SimpleNamespace(
            owner_id="owner-1",
            topic_id="topic-1",
            finalized_event_id="event-1",
            safety_signals=(),
            safety_reasons=(),
            flow="nutrition_daily",
            kst_day="2026-07-20",
            answers={"bodyweight": "71.4"},
            branch="change",
            follow_up_ids=("Q-PERF-REASON", "Q-PERF-NEXT"),
        )
        bridge = PhysiqueCheckinBridge(
            _config(), service=service, binding_path=tmp_path / "bindings.json",
        )

        snapshot = bridge.finalized_coaching_snapshot("a" * 32)

        assert snapshot is not None
        assert snapshot["branch"] == "change"
        assert snapshot["detailed"] is True
        assert snapshot["follow_up_ids"] == ("Q-PERF-REASON", "Q-PERF-NEXT")

    def test_urgent_text_reaches_safety_transition_during_button_only_step(self, tmp_path):
        bridge, service = _bridge(tmp_path)
        launcher = bridge.open_launcher("morning", message_id="44")
        assert launcher.callback_data is not None
        bridge.handle_callback(launcher.callback_data, "owner-1", "chat-1", "topic-1", "44")
        binding = bridge._bindings["0123456789abcdef0123456789abcdef"]
        binding.step = "sleep_quality"
        binding.awaiting_text = False
        service.answer = MagicMock(
            return_value=_FakeResult(version=2, step="safety_ack", status="safety_stop", message="stop_and_escalate")
        )

        reply = bridge.handle_text(
            "흉통과 호흡 곤란이 있습니다",
            "owner-1",
            "chat-1",
            "topic-1",
        )

        assert reply is not None and reply.accepted is True
        assert reply.prompt is not None and "진료" in reply.prompt.text
        service.answer.assert_called_once()
        assert service.answer.call_args.args[3:] == ("value", "흉통과 호흡 곤란이 있습니다")

    @pytest.mark.parametrize("flow", ["morning", "workout", "nutrition_daily"])
    def test_all_fixed_coach_prompts_use_honorific_korean(self, flow):
        steps = (
            "bodyweight", "sleep_duration", "sleep_quality", "condition", "pain",
            "calories", "macros", "meals", "water", "digestion", "appetite_stress",
            "training_plan", "optional_note", "completion", "training_summary",
            "workout_quality", "summary", "edit_menu", "safety_ack",
        )
        banned = ("보내줘", "골라줘", "적어줘", "알려줘", "시작해줘", "확인해줘")

        for step in steps:
            prompt = build_wizard_prompt(step, False, lambda action: f"callback:{action}", flow=flow)
            assert not any(ending in prompt.text for ending in banned), prompt.text

    def test_customer_nutrition_edit_menu_uses_compact_korean_fields(self):
        # Given: the customer nutrition flow has reached its review screen.
        prompt = build_wizard_prompt("edit_menu", False, lambda action: f"callback:{action}", flow="nutrition_daily")

        # Then: all twelve fields are human-readable and fit the six two-column rows.
        assert tuple(label for label, _ in prompt.buttons) == (
            "체중", "칼로리", "탄단지", "식사", "수분", "수면 시간",
            "수면 질", "소화", "컨디션", "식욕·스트레스", "운동", "특이사항",
            "요약으로 돌아가기",
        )
        assert all(len(row) == 2 for row in prompt.button_rows[:-1])

    def test_summary_uses_three_compact_actions_and_a_flow_specific_two_column_edit_menu(self):
        # Given: a completed morning check-in waiting at its summary.
        callback = lambda action: f"callback:{action}"

        # When: the summary and its Edit tab are rendered.
        summary = build_wizard_prompt("summary", False, callback, flow="morning")
        edit_menu = build_wizard_prompt("edit_menu", False, callback, flow="morning")

        # Then: the summary is not an eleven-button field dump, and the menu
        # exposes only Korean morning fields in compact two-button rows.
        assert tuple(label for label, _ in summary.buttons) == ("저장", "수정", "임시저장")
        assert summary.button_rows == (summary.buttons,)
        assert edit_menu.text == "어느 항목을 수정할까요?"
        assert all("_" not in label for label, _ in edit_menu.buttons)
        assert tuple(label for label, _ in edit_menu.buttons) == (
            "체중", "수면 시간", "수면 질", "컨디션", "통증", "칼로리", "오늘 운동", "특이사항", "요약으로 돌아가기",
        )
        assert all(len(row) == 2 for row in edit_menu.button_rows[:-1])
        assert edit_menu.button_rows[-1][0][0] == "요약으로 돌아가기"

    def test_edit_tab_changes_the_same_binding_screen_and_field_callback_enters_correction(self, tmp_path):
        # Given: an exact owner/topic callback reaches a bound summary card.
        bridge, service = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        binding = bridge._bindings["0123456789abcdef0123456789abcdef"]
        binding.step = "summary"
        binding.version = 2
        binding.awaiting_text = False

        # When: the user opens Edit then selects the Korean bodyweight field.
        menu = bridge.handle_callback(
            CallbackData(binding.session_id, "summary", 2, "a1").encode(),
            "owner-1", "chat-1", "topic-1", "44",
        )
        field = bridge.handle_callback(
            CallbackData(binding.session_id, "edit_menu", 2, "e0").encode(),
            "owner-1", "chat-1", "topic-1", "44",
        )

        # Then: the display state rejects stale summary callbacks and the
        # field action is the existing immutable-domain edit transition.
        assert menu.accepted is True
        assert menu.prompt is not None and menu.prompt.text == "어느 항목을 수정할까요?"
        assert binding.step == "sleep_quality"
        assert field.accepted is True
        assert service.answers == [("0123456789abcdef0123456789abcdef", 2, "edit", "bodyweight")]
        stale_save = bridge.handle_callback(
            CallbackData(binding.session_id, "summary", 2, "a0").encode(),
            "owner-1", "chat-1", "topic-1", "44",
        )
        assert stale_save.accepted is False
    def test_disabled_config_is_a_true_noop(self):
        assert PhysiqueCheckinConfig.from_extra({}) is None
        assert PhysiqueCheckinConfig.from_extra({"physique_checkin": {"enabled": False}}) is None

    def test_optional_topic_allowlists_must_match_the_exact_wizard_target(self):
        assert PhysiqueCheckinConfig.from_extra({
            "physique_checkin": {
                "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
                "allowed_chats": ["other-chat"], "allowed_topics": ["topic-1"],
            }
        }) is None
        assert PhysiqueCheckinConfig.from_extra({
            "physique_checkin": {
                "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
                "allowed_chats": ["chat-1"], "allowed_topics": ["topic-1"], "require_mention": False,
            }
        }) is not None

    def test_start_binds_exact_owner_chat_topic_and_message(self, tmp_path):
        bridge, _ = _bridge(tmp_path)
        reply = bridge.open_launcher("morning", message_id="44")
        assert reply.handled is True
        assert reply.callback_data is not None

        result = bridge.handle_callback(reply.callback_data, "owner-1", "chat-1", "topic-1", "44")
        assert result.handled is True
        assert result.prompt is not None

        wrong_topic = bridge.handle_callback(reply.callback_data, "owner-1", "chat-1", "other-topic", "44")
        foreign_owner = bridge.handle_callback(reply.callback_data, "other", "chat-1", "topic-1", "44")
        wrong_message = bridge.handle_callback(reply.callback_data, "owner-1", "chat-1", "topic-1", "other-message")
        assert wrong_topic.handled is True and wrong_topic.accepted is False
        assert foreign_owner.handled is True and foreign_owner.accepted is False
        assert wrong_message.handled is True and wrong_message.accepted is False

    def test_recovery_command_boundary_requires_exact_owner_chat_and_topic(self, tmp_path):
        bridge, _ = _bridge(tmp_path)

        assert bridge.accepts_context("owner-1", "chat-1", "topic-1") is True
        assert bridge.accepts_context("other", "chat-1", "topic-1") is False
        assert bridge.accepts_context("owner-1", "chat-1", "other-topic") is False

    def test_replay_and_expiry_are_rejected_without_mutating_service(self, tmp_path):
        bridge, service = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        first = bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        assert first.accepted is True
        replay = bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        stale = bridge.handle_callback(
            "pc1:0123456789abcdef0123456789abcdef:bodyweight:1:a0",
            "owner-1", "chat-1", "topic-1", "44",
        )
        assert replay.accepted is False
        assert stale.accepted is False
        assert service.answers == []

        expired = bridge.open_launcher("morning", message_id="45", now_epoch=100)
        assert expired.callback_data is not None
        expired_result = bridge.handle_callback(expired.callback_data, "owner-1", "chat-1", "topic-1", "45", now_epoch=701)
        assert expired_result.accepted is False

    def test_typed_value_requires_exact_active_binding_then_bypasses_agent(self, tmp_path):
        bridge, service = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")

        accepted = bridge.handle_text("70.2", "owner-1", "chat-1", "topic-1")
        assert accepted.handled is True and accepted.accepted is True
        assert service.answers == [("0123456789abcdef0123456789abcdef", 0, "value", "70.2")]

        denied = bridge.handle_text("71.0", "foreign", "chat-1", "topic-1")
        wrong_topic = bridge.handle_text("71.0", "owner-1", "chat-1", "other-topic")
        assert denied.handled is True and denied.accepted is False
        assert wrong_topic is not None and wrong_topic.accepted is False
        assert len(service.answers) == 1

    def test_model_can_mark_missing_bodyweight_and_advance_without_creating_a_trend_value(self, tmp_path):
        profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
        if profile_source not in sys.path:
            sys.path.insert(0, profile_source)
        from checkin_cli.wizard import WizardService

        bridge = PhysiqueCheckinBridge(
            _config(), service=WizardService.for_standalone(tmp_path / "wizard-state"), binding_path=tmp_path / "bindings.json",
        )
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")

        # Given: the model chose the only valid missing-value action for bodyweight.
        reply = bridge.apply_model_action("skip", None)

        # Then: the draft moves forward and retains an explicit missing marker.
        assert reply.accepted is True
        assert reply.prompt is not None and "수면" in reply.prompt.text
        snapshot = bridge.active_checkin_snapshot()
        assert snapshot is not None
        assert snapshot["answers"]["bodyweight"] is None

    def test_model_can_rewrite_the_current_sleep_quality_prompt_without_advancing_the_draft(self, tmp_path):
        bridge, service = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        binding = bridge._bindings["0123456789abcdef0123456789abcdef"]
        binding.step = "sleep_quality"
        binding.version = 2
        binding.awaiting_text = False

        reply = bridge.apply_model_action("rewrite_prompt", None)

        assert reply.accepted is True
        assert reply.prompt is not None
        assert reply.prompt.text == "수면 질 점수 (1=매우 나쁨 · 3=보통 · 5=매우 좋음)를 골라주세요."
        assert len(reply.prompt.buttons) == 5
        assert bridge.active_prompt_message_id() == "44"
        assert service.answers == []

    def test_all_rendered_callbacks_are_opaque_and_telegram_sized(self, tmp_path):
        bridge, _ = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        binding = bridge._bindings["0123456789abcdef0123456789abcdef"]
        binding.step = "summary"
        binding.awaiting_text = False
        prompt = bridge._prompt(binding)

        assert prompt.buttons
        assert all(len(payload.encode("utf-8")) <= 64 for _, payload in prompt.buttons)
        assert all("70.2" not in payload and "2350" not in payload for _, payload in prompt.buttons)

    def test_typed_action_replaces_buttons_with_the_exact_expected_question(self, tmp_path):
        bridge, _ = _bridge(tmp_path)
        launch = bridge.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        binding = bridge._bindings["0123456789abcdef0123456789abcdef"]
        binding.step = "pain"
        binding.awaiting_text = False

        reply = bridge.handle_callback(
            "pc1:0123456789abcdef0123456789abcdef:pain:0:a1",
            "owner-1", "chat-1", "topic-1", "44",
        )

        assert reply.accepted is True
        assert reply.prompt is not None
        assert "위치" in reply.prompt.text and "강도" in reply.prompt.text and "언제부터" in reply.prompt.text
        assert reply.prompt.buttons == ()

    def test_fresh_bridge_rehydrates_persisted_active_draft_and_consumes_typed_value(self, tmp_path):
        profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
        if profile_source not in sys.path:
            sys.path.insert(0, profile_source)
        from checkin_cli.wizard import WizardService

        state_home = tmp_path / "wizard-state"
        binding_path = tmp_path / "bindings.json"
        first = PhysiqueCheckinBridge(_config(), service=WizardService.for_standalone(state_home), binding_path=binding_path)
        launch = first.open_launcher("morning", message_id="44")
        assert launch.callback_data is not None
        started = first.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "44")
        assert started.accepted is True

        restarted = PhysiqueCheckinBridge(_config(), service=WizardService.for_standalone(state_home), binding_path=binding_path)
        typed = restarted.handle_text("70.2", "owner-1", "chat-1", "topic-1")

        assert typed is not None and typed.accepted is True
        assert typed.prompt is not None and "수면" in typed.prompt.text
        denied = restarted.handle_text("70.5", "owner-1", "chat-1", "wrong-topic")
        assert denied is not None and denied.accepted is False


    def test_trainer_bridge_start_to_save_scopes_event_to_registered_customer(self, tmp_path):
        profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
        if profile_source not in sys.path:
            sys.path.insert(0, profile_source)
        from checkin_cli.wizard import WizardService

        service = WizardService.for_standalone(tmp_path / "wizard-state")
        bridge = PhysiqueCheckinBridge(
            _config(),
            service=service,
            binding_path=tmp_path / "bindings.json",
            customer_key="customer-1",
        )
        now = 4_000_000_000
        launch = bridge.open_trainer_launcher(message_id="99", now_epoch=now)

        assert launch.accepted is True and launch.callback_data is not None
        denied = bridge.handle_callback(launch.callback_data, "other", "chat-1", "topic-1", "99", now_epoch=now)
        assert denied.accepted is False
        started = bridge.handle_callback(launch.callback_data, "owner-1", "chat-1", "topic-1", "99", now_epoch=now)
        assert started.accepted is True
        assert started.prompt is not None
        assert tuple(label for label, _ in started.prompt.buttons) == ("완료", "부분 완료", "휴식·변경")

        session_id = next(iter(bridge._bindings))
        binding = bridge._bindings[session_id]
        done = bridge.handle_callback(
            CallbackData(session_id, "done", 0, "a0").encode(),
            "owner-1", "chat-1", "topic-1", "99", now_epoch=now,
        )
        assert done.accepted is True
        assert done.prompt is not None
        assert "운동 부위" in done.prompt.text
        assert "세트 수" in done.prompt.text
        summary = bridge.handle_text(
            "하체 중심 스쿼트 5세트, 런지 4세트, 총 55분",
            "owner-1", "chat-1", "topic-1", now_epoch=now,
        )
        assert summary is not None and summary.accepted is True
        for step, version, action in (
            ("performance", 2, "a4"),
            ("intensity", 3, "a1"),
            ("pain", 4, "a0"),
            ("operator_note", 5, "a0"),
            ("summary", 6, "a0"),
        ):
            reply = bridge.handle_callback(
                CallbackData(session_id, step, version, action).encode(),
                "owner-1", "chat-1", "topic-1", "99", now_epoch=now,
            )
            assert reply.accepted is True, (step, reply.notice)
            if step == "operator_note":
                assert reply.prompt is not None
                assert "하체 중심 스쿼트 5세트" in reply.prompt.text
                assert "수행도: 4 / 5" in reply.prompt.text
                assert "강도: 계획대로" in reply.prompt.text
                assert "통증: none" in reply.prompt.text
            expected_version = 7 if step == "summary" else version + 1
            assert binding.version == expected_version

        event = bridge.finalized_event(session_id)
        assert event is not None
        assert event.dedupe_key == "trainer-session:customer-1:" + event.occurred_at_kst[:10]
        assert event.provenance.source_ref == "pilot:customer-1:trainer_session_record"
class TestPhysiqueTelegramAdapterIngress:
    @staticmethod
    def _adapter(extra):
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra=extra))
        adapter._bot = AsyncMock()
        adapter._app = MagicMock()
        return adapter

    def test_nutrition_private_dm_address_uses_registry_topic_zero(self):
        adapter = self._adapter({})
        message = SimpleNamespace(
            chat=SimpleNamespace(id=8693203710, type="private"),
            chat_id=8693203710,
            message_thread_id=None,
        )
        query = SimpleNamespace(from_user=SimpleNamespace(id=8693203710))

        address = adapter._nutrition_address(query, message)

        assert address.user_id == "8693203710"
        assert address.chat_id == "8693203710"
        assert address.topic_id == "0"

    @pytest.mark.asyncio
    async def test_completed_customer_checkin_sends_draft_request_only_to_operator_dm(self):
        adapter = self._adapter({
            "adaptive_nutrition": {
                "enabled": True,
                "operator_chat_id": "-1004290459350",
                "operator_topic_id": 59,
                "delivery_enabled": False,
                "review_operator": {
                    "user_id": "8693203710",
                    "chat_id": "-1004290459350",
                    "topic_id": 59,
                    "version": 1,
                },
            }
        })
        adapter._send_nutrition_topic = AsyncMock()
        owner = SimpleNamespace(user_id="8693203710", chat_id="8693203710", topic_id="0")
        trainer = SimpleNamespace(user_id="8693203710", chat_id="-1004290459350", topic_id="4")
        customer = SimpleNamespace(
            spec=SimpleNamespace(trainer=trainer),
        )
        coordinator = SimpleNamespace(owner=owner, customer=lambda _key: customer)
        adapter._get_nutrition_coaching = lambda: coordinator
        completion = SimpleNamespace(
            display_name="가상 고객",
            kst_day="2026-07-23",
            role="customer",
            safety_held=False,
            request_token="0123456789abcdef",
            customer_key="virtual_customer",
        )

        await adapter._render_nutrition_completion(completion)

        adapter._send_nutrition_topic.assert_awaited_once()
        call = adapter._send_nutrition_topic.await_args.kwargs
        assert call["chat_id"] == -1004290459350
        assert call["topic_id"] == "59"
        assert call["topic_id"] != "4"

    def test_dedicated_operator_topic_maps_to_canonical_owner_actor(self):
        adapter = self._adapter({
            "adaptive_nutrition": {
                "enabled": True,
                "operator_chat_id": "-1004290459350",
                "operator_topic_id": 59,
                "delivery_enabled": False,
                "review_operator": {
                    "user_id": "8693203710",
                    "chat_id": "-1004290459350",
                    "topic_id": 59,
                    "version": 1,
                },
            }
        })
        actual = SimpleNamespace(key=("8693203710", "-1004290459350", "59"))
        owner = SimpleNamespace(key=("8693203710", "8693203710", "0"))
        coordinator = SimpleNamespace(owner=owner)

        assert adapter._nutrition_operator_actor(actual, coordinator) is owner
        assert adapter._is_nutrition_operator_space(actual) is True
        wrong_topic = SimpleNamespace(key=("8693203710", "-1004290459350", "4"))
        assert adapter._nutrition_operator_actor(wrong_topic, coordinator) is None
        assert adapter._is_nutrition_operator_space(wrong_topic) is False

    def test_operator_draft_view_lists_every_final_checkin_field(self, tmp_path):
        answers = {
            "bodyweight": "69.1",
            "calories": "3000",
            "macros": "300 190 88",
            "meals": "세 끼",
            "water": "3",
            "sleep_duration": "6",
            "sleep_quality": "3",
            "digestion": "bristol_5",
            "condition": "5",
            "appetite_stress": "식욕 4 스트레스 2",
            "training_summary": "복싱 90분",
            "optional_note": "",
        }
        events = tmp_path / "wizard"
        events.mkdir()
        (events / "events.jsonl").write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in (
                    {
                        "event_id": "original",
                        "recorded_at_kst": "2026-07-23T10:00:00+09:00",
                        "check_in": {"water_liters": 3.0, "protein_g": 188},
                    },
                    {
                        "event_id": "correction-1",
                        "recorded_at_kst": "2026-07-23T10:15:00+09:00",
                        "supersedes": "original",
                        "check_in": {"water_liters": 5.0, "protein_g": 188},
                    },
                    {
                        "event_id": "correction-2",
                        "recorded_at_kst": "2026-07-23T10:30:00+09:00",
                        "supersedes": "correction-1",
                        "check_in": {"water_liters": 3.0, "protein_g": 190},
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        action = SimpleNamespace(
            status="created",
            text="고객에게 보낼 핵심 피드백입니다.",
            selection=SimpleNamespace(
                snapshot=SimpleNamespace(
                    answers=answers,
                    finalized_event_id="correction-2",
                    kst_day="2026-07-23",
                    macro_order="carbohydrate_protein_fat",
                ),
                customer=SimpleNamespace(
                    data_root=tmp_path,
                    spec=SimpleNamespace(
                        customer_key="client_001",
                        plan=SimpleNamespace(starts_on=date(2026, 7, 22)),
                    ),
                ),
            ),
        )

        rendered = TelegramAdapter._nutrition_draft_text(action)

        for expected in (
            "체중: 69.1",
            "칼로리: 3000",
            "탄수화물·단백질·지방(탄·단·지): 300 190 88",
            "식사(끼니별 Meal 기록): 세 끼",
            "수분: 3",
            "수면 시간: 6",
            "수면 질: 3",
            "소화·배변: 브리스톨 5형 · 가장자리가 뚜렷한 부드러운 덩어리",
            "컨디션: 5",
            "식욕·스트레스: 식욕 4 스트레스 2",
            "운동: 복싱 90분",
            "기타 메모: 미입력",
        ):
            assert expected in rendered
        assert "프로그램 진행일: D+2" in rendered
        assert "수정 이력: 총 3개 버전" in rendered
        assert "단백질: 188 → 190" in rendered
        assert "수분: 5.0 → 3.0" in rendered
        assert "수분: 3.0 → 5.0" in rendered

    @pytest.mark.asyncio
    async def test_natural_language_source_approval_consumes_the_latest_notified_candidate(self, monkeypatch):
        # Given: the exact owner is in the private topic with one notified public-source candidate.
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        candidate = SimpleNamespace(candidate_id="b" * 64, source=SimpleNamespace(value="naver"), title="탄수화물 배분")
        queue = MagicMock()
        queue.latest_notified_candidate.return_value = candidate
        queue.approve.return_value = SimpleNamespace(approved_count=1)
        monkeypatch.setattr(adapter, "_get_physique_source_review_queue", lambda: queue)
        bridge = MagicMock()
        bridge.accepts_context.return_value = True
        adapter._physique_checkin = bridge
        adapter._send_message_with_thread_fallback = AsyncMock()
        message = SimpleNamespace(
            text="이 네이버 블로그 자료 지식창고에 넣어줘", chat=SimpleNamespace(id="chat-1", type="supergroup"),
            from_user=SimpleNamespace(id="owner-1"), message_thread_id="topic-1",
        )

        # When: the owner explicitly asks to add the notified item to the knowledge base.
        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        # Then: no generic model turn runs and only that notified candidate is approved.
        queue.approve.assert_called_once()
        assert queue.approve.call_args.args[0] == candidate.candidate_id
        bridge.handle_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_today_feedback_replay_uses_saved_checkin_after_a_new_launcher_replaces_active_session(self, monkeypatch):
        # Given: a saved same-topic check-in whose legacy fields remain renderable.
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "-1001", "topic_id": "59", "coaching_feedback_enabled": True}})
        snapshot = {
            "flow": "morning",
            "kst_day": "2026-07-18",
            "answers": {"calories": "2200"},
            "legacy_revision": 1,
        }
        bridge = MagicMock()
        bridge.accepts_context.return_value = True
        bridge.active_finalized_coaching_snapshot.return_value = None
        bridge.latest_finalized_coaching_snapshot.return_value = snapshot
        adapter._physique_checkin = bridge
        adapter._humanize_korean_copy = AsyncMock(
            side_effect=lambda _surface, text, _grounding: text
        )
        adapter._send_message_with_thread_fallback = AsyncMock()
        message = SimpleNamespace(
            text="오늘 기준으로 내 체크인 피드백 다시 해봐", chat=SimpleNamespace(id="-1001", type="supergroup"),
            from_user=SimpleNamespace(id="owner-1"), message_thread_id="59",
        )

        # When: the owner asks again for today's check-in feedback.
        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        # Then: the saved snapshot remains renderable even when typed grounding is unavailable.
        adapter._humanize_korean_copy.assert_not_awaited()
        sent = adapter._send_message_with_thread_fallback.await_args.kwargs
        assert "오늘 체크인 완료" in sent["text"]
        bridge.handle_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_review_callback_requires_the_exact_owner_topic_and_approves_one_candidate(self, monkeypatch):
        # Given: a source-review card and a queue candidate in the private profile scope.
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        candidate = SimpleNamespace(candidate_id="a" * 64, source=SimpleNamespace(value="naver"), title="감량기 탄수화물 배분")
        queue = MagicMock()
        queue.candidate_by_prefix.return_value = candidate
        queue.approve.return_value = SimpleNamespace(approved_count=1)
        monkeypatch.setattr(adapter, "_get_physique_source_review_queue", lambda: queue)
        query = _callback("sr1:a:" + "a" * 12)

        # When: the exact owner presses the one-item approval button.
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

        # Then: the queue receives only that immutable candidate id and the card is finalized.
        queue.approve.assert_called_once()
        assert queue.approve.call_args.args[0] == candidate.candidate_id
        query.edit_message_text.assert_awaited_once()

        # And when: another member replays the same opaque callback in the topic.
        foreign = _callback("sr1:a:" + "a" * 12, owner="other")
        await adapter._handle_callback_query(SimpleNamespace(callback_query=foreign), None)

        # Then: no second approval can reach the queue.
        queue.approve.assert_called_once()

    def test_summary_keyboard_keeps_three_actions_in_one_telegram_row(self, monkeypatch):
        # Given: the compact summary prompt.
        prompt = build_wizard_prompt("summary", False, lambda action: f"callback:{action}", flow="morning")
        import gateway.platforms.telegram as telegram_module

        @dataclass(frozen=True)
        class _Button:
            text: str
            callback_data: str

        @dataclass(frozen=True)
        class _Markup:
            inline_keyboard: list[list[_Button]]

        monkeypatch.setattr(telegram_module, "InlineKeyboardButton", _Button)
        monkeypatch.setattr(telegram_module, "InlineKeyboardMarkup", _Markup)

        # When: the Telegram adapter builds its native inline keyboard.
        markup = TelegramAdapter._physique_markup(prompt)

        # Then: Telegram receives one row, not a vertically stacked field list.
        assert markup is not None
        assert [[button.text for button in row] for row in markup.inline_keyboard] == [["저장", "수정", "임시저장"]]

    @pytest.mark.asyncio
    async def test_disabled_pc1_callback_falls_through_existing_router(self):
        adapter = self._adapter({})
        query = _callback("pc1:0123456789abcdef0123456789abcdef:launch:0:start")
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
        # The existing router has no pc1 branch when the profile flag is absent.
        query.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_pc1_text_is_consumed_before_generic_aggregation(self, monkeypatch):
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        fake = MagicMock()
        fake.handle_text.return_value = WizardReply(True, True, "", None, None)
        adapter._physique_checkin = fake
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="70.2", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="owner-1"), message_thread_id="topic-1", is_topic_message=True,
        )
        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        fake.handle_text.assert_called_once()
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_anonymous_natural_language_wizard_text_uses_model_without_generic_ingress(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "chat-1",
            "topic_id": "topic-1",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = WizardReply(
            True,
            False,
            "오늘 체중을 못 쟀구나.",
            WizardPrompt("오늘 체중을 못 쟀구나. 나중에 숫자로 보내면 이어갈게."),
            None,
        )
        fake.active_checkin_snapshot.return_value = {
            "step": "bodyweight", "flow": "morning", "kst_day": "2026-07-18", "answers": {},
        }
        fake.apply_model_action.return_value = WizardReply(True, True, "", WizardPrompt("① 기상 직후 체중(kg)을 숫자로 보내줘. 예: 70.2"), None)
        adapter._physique_checkin = fake
        adapter._interpret_physique_active_turn = AsyncMock(return_value=("stay", None, "괜찮아. 지금은 체중 없이 진행할지 먼저 정해보자."))
        adapter._render_physique_text_prompt = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="기록 못했어 오늘",
            chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="telegram-fake-user"),
            sender_chat=SimpleNamespace(id="chat-1"),
            message_thread_id="topic-1",
            is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        fake.handle_text.assert_called_once_with("기록 못했어 오늘", "owner-1", "chat-1", "topic-1")
        fake.apply_model_action.assert_called_once_with("stay", None)
        rendered = adapter._render_physique_text_prompt.call_args.args[1]
        assert rendered.prompt is not None
        assert rendered.prompt.text.startswith("괜찮아. 지금은 체중 없이")
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_natural_language_turn_uses_model_action_instead_of_static_invalid_template(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = WizardReply(True, False, "입력을 확인하고 다시 시도해줘.", WizardPrompt("STATIC"), None)
        fake.active_checkin_snapshot.return_value = {
            "step": "bodyweight", "flow": "morning", "kst_day": "2026-07-18", "answers": {},
        }
        fake.apply_model_action.return_value = WizardReply(True, True, "", WizardPrompt("② 수면 시간을 숫자로 보내줘."), None)
        adapter._physique_checkin = fake
        adapter._interpret_physique_active_turn = AsyncMock(return_value=("skip", None, "좋아, 오늘 체중은 결측으로 남기고 수면부터 이어가자."))
        adapter._render_physique_text_prompt = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="N/A로 기록하고 다음으로 넘어가자", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="telegram-fake-user"), sender_chat=SimpleNamespace(id="chat-1"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter._interpret_physique_active_turn.assert_awaited_once_with(
            "N/A로 기록하고 다음으로 넘어가자", fake.active_checkin_snapshot.return_value,
        )
        fake.apply_model_action.assert_called_once_with("skip", None)
        rendered = adapter._render_physique_text_prompt.call_args.args[1]
        assert rendered.prompt is not None
        assert rendered.prompt.text.startswith("좋아, 오늘 체중은 결측으로")
        assert "수면" in rendered.prompt.text
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_anonymous_sleep_quality_feedback_rewrites_the_active_card_without_generic_ingress(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = None
        fake.active_checkin_snapshot.return_value = {
            "step": "sleep_quality", "flow": "morning", "kst_day": "2026-07-18", "answers": {"sleep_duration": "7"},
        }
        prompt = WizardPrompt(
            "수면 질 점수 (1=매우 나쁨 · 3=보통 · 5=매우 좋음)를 골라줘.",
            (("1", "pc1:0123456789abcdef0123456789abcdef:sleep_quality:2:a1"),),
        )
        fake.apply_model_action.return_value = WizardReply(True, True, "", prompt, None)
        adapter._physique_checkin = fake
        adapter._interpret_physique_active_turn = AsyncMock(return_value=("rewrite_prompt", None, "맞아, 수면의 질을 묻는 점수야."))
        adapter._render_physique_model_turn = AsyncMock()
        adapter._render_physique_conversation_reply = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="점수도 무슨 점수를 말하는건지 고칠 수 있냐?",
            chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="telegram-fake-user"), sender_chat=SimpleNamespace(id="chat-1"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter._interpret_physique_active_turn.assert_awaited_once_with(
            "점수도 무슨 점수를 말하는건지 고칠 수 있냐?", fake.active_checkin_snapshot.return_value,
        )
        fake.apply_model_action.assert_called_once_with("rewrite_prompt", None)
        adapter._render_physique_model_turn.assert_awaited_once_with(
            message, fake.apply_model_action.return_value, "맞아, 수면의 질을 묻는 점수야.", edit_current=True,
        )
        adapter._render_physique_conversation_reply.assert_not_awaited()
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_topic_conversation_uses_isolated_chos_coach_model_without_generic_ingress(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "100",
            "topic_id": "101",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = None
        fake.accepts_context.return_value = True
        fake.latest_finalized_coaching_snapshot.return_value = None
        adapter._physique_checkin = fake
        adapter._generate_physique_conversation_reply = AsyncMock(return_value="오늘은 무리하지 말고 회복부터 챙겨.")
        adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=77))
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="기록 못했어 오늘",
            chat=SimpleNamespace(id="100", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="telegram-fake-user"),
            sender_chat=SimpleNamespace(id="100"),
            message_thread_id="101",
            is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter._generate_physique_conversation_reply.assert_awaited_once_with("기록 못했어 오늘", None)
        sent = adapter._send_message_with_thread_fallback.call_args.kwargs
        assert sent["chat_id"] == 100
        assert sent["text"] == "오늘은 무리하지 말고 회복부터 챙겨."
        assert sent["message_thread_id"] == 101
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_anonymous_typed_value_uses_owner_only_for_the_exact_opted_in_topic(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "chat-1",
            "topic_id": "topic-1",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = WizardReply(True, True, "", None, None)
        adapter._physique_checkin = fake
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="70.2", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=None, sender_chat=SimpleNamespace(id="chat-1"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        fake.handle_text.assert_called_once_with("70.2", "owner-1", "chat-1", "topic-1")
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_anonymous_typed_value_uses_sender_chat_when_telegram_supplies_a_fake_user(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "chat-1",
            "topic_id": "topic-1",
            "allow_anonymous_sender_chat": True,
        }})
        fake = MagicMock()
        fake.handle_text.return_value = WizardReply(True, True, "", None, None)
        adapter._physique_checkin = fake
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="70.2", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="telegram-fake-user"), sender_chat=SimpleNamespace(id="chat-1"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        fake.handle_text.assert_called_once_with("70.2", "owner-1", "chat-1", "topic-1")
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_target_numeric_answer_reopens_checkin_instead_of_reaching_generic_agent(self):
        adapter = self._adapter({"physique_checkin": {
            "enabled": True,
            "owner_id": "owner-1",
            "chat_id": "chat-1",
            "topic_id": "topic-1",
        }})
        fake = MagicMock()
        fake.handle_text.return_value = None
        fake.accepts_context.return_value = True
        adapter._physique_checkin = fake
        adapter._bot.username = ""
        adapter._enqueue_text_event = MagicMock()
        adapter._should_process_message = MagicMock(return_value=False)
        adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
        adapter.send_inline_card = AsyncMock()
        message = SimpleNamespace(
            text="7", message_id=1,
            chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True, title="Test"),
            from_user=SimpleNamespace(id="owner-1", full_name="Owner"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter.send_inline_card.assert_awaited_once_with("physique-checkin-morning")
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_real_binding_numeric_answer_is_recovered_before_generic_ingress(self, tmp_path):
        """A restart after expiry must not leak the user's next scalar answer.

        This uses the real binding store and bridge, rather than a mocked
        ``handle_text`` result, to cover the exact failure that happened in
        the forum topic: load drops an expired active binding, then Telegram
        receives the delayed numeric answer.
        """
        binding_path = tmp_path / "bindings.json"
        original, service = _bridge(tmp_path)
        launch = original.open_launcher("morning", message_id="44", now_epoch=100)
        assert launch.callback_data is not None
        started = original.handle_callback(
            launch.callback_data, "owner-1", "chat-1", "topic-1", "44", now_epoch=100,
        )
        assert started.accepted is True

        # Loading at real current time discards the binding that expired at 700.
        expired_bridge = PhysiqueCheckinBridge(
            _config(), service=service, binding_path=binding_path,
        )
        assert expired_bridge._active_session_id is None

        adapter = self._adapter({"physique_checkin": {
            "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
        }})
        adapter._physique_checkin = expired_bridge
        adapter._bot.username = ""
        adapter._enqueue_text_event = MagicMock()
        adapter._should_process_message = MagicMock(return_value=False)
        adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
        adapter.send_inline_card = AsyncMock()
        message = SimpleNamespace(
            text="7", message_id=45,
            chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True, title="Test"),
            from_user=SimpleNamespace(id="owner-1", full_name="Owner"),
            message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(
            SimpleNamespace(message=message, effective_message=message, update_id=1), None,
        )

        adapter.send_inline_card.assert_awaited_once_with("physique-checkin-morning")
        adapter._enqueue_text_event.assert_not_called()
        assert service.answers == []

    @pytest.mark.asyncio
    async def test_owner_recovery_command_sends_one_topic_bound_card_before_generic_ingress(self):
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        fake = MagicMock()
        fake.accepts_context.return_value = True
        fake.handle_text.return_value = None
        adapter._physique_checkin = fake
        adapter.send_inline_card = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="체크인 시작", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="owner-1"), message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter.send_inline_card.assert_awaited_once_with("physique-checkin-morning")
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_foreign_or_wrong_topic_recovery_command_is_denied_before_generic_ingress(self):
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        fake = MagicMock()
        fake.accepts_context.return_value = False
        fake.handle_text.return_value = None
        adapter._physique_checkin = fake
        adapter.send_inline_card = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="체크인 시작", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="other"), message_thread_id="wrong-topic", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)

        adapter.send_inline_card.assert_not_awaited()
        adapter._enqueue_text_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_scheduled_launch_recovers_once_by_owner_command_without_automatic_retry(self, tmp_path):
        class FailingScheduledTransport:
            def is_inline_card_enabled(self, card: str) -> bool:
                return card == "physique-checkin-morning"

            def send_inline_card(self, card: str) -> bool:
                return False

        scheduled = launch_morning_card(
            tmp_path,
            FailingScheduledTransport(),
            datetime(2031, 2, 3, 8, 11, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        bridge = MagicMock()
        bridge.accepts_context.return_value = True
        adapter._physique_checkin = bridge
        adapter.send_inline_card = AsyncMock()
        adapter._enqueue_text_event = MagicMock()
        message = SimpleNamespace(
            text="체크인 시작", chat=SimpleNamespace(id="chat-1", type="supergroup", is_forum=True),
            from_user=SimpleNamespace(id="owner-1"), message_thread_id="topic-1", is_topic_message=True,
        )

        await adapter._handle_text_message(SimpleNamespace(message=message, effective_message=message, update_id=1), None)
        automatic_retry = launch_morning_card(
            tmp_path,
            FailingScheduledTransport(),
            datetime(2031, 2, 3, 8, 12, tzinfo=ZoneInfo("Asia/Seoul")),
        )

        assert scheduled.claimed is True and scheduled.sent is False
        adapter.send_inline_card.assert_awaited_once_with("physique-checkin-morning")
        assert automatic_retry.duplicate is True

    @pytest.mark.asyncio
    async def test_enabled_pc1_callback_is_consumed_before_existing_callbacks(self):
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        fake = MagicMock()
        fake.handle_callback.return_value = WizardReply(True, False, "denied", None, None)
        adapter._physique_checkin = fake
        query = _callback("pc1:0123456789abcdef0123456789abcdef:launch:0:start")

        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

        fake.handle_callback.assert_called_once()
        query.answer.assert_awaited_once_with(text="denied")

    @pytest.mark.asyncio
    async def test_typed_action_edits_the_same_topic_prompt_to_a_direct_question(self):
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1"}})
        fake = MagicMock()
        fake.handle_callback.return_value = WizardReply(True, True, "", WizardPrompt("통증 위치·강도·언제부터인지 적어줘."), None)
        adapter._physique_checkin = fake
        query = _callback("pc1:0123456789abcdef0123456789abcdef:pain:0:a1")

        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

        query.edit_message_text.assert_awaited_once()
        assert "위치" in query.edit_message_text.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_save_callback_records_once_and_replaces_summary_with_confirmation(self, tmp_path):
        """A real finalized wizard must visibly confirm instead of re-rendering Save."""
        profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
        if profile_source not in sys.path:
            sys.path.insert(0, profile_source)
        from checkin_cli.wizard import WizardService
        from checkin_cli.wizard_models import WizardContext

        # Given: a real profile draft at the summary step and a bound Telegram card.
        service = WizardService.for_standalone(tmp_path / "wizard-state")
        wizard_context = WizardContext("owner-1", "topic-1")
        started = service.start_morning(wizard_context, "2031-02-03")
        session_id, version = started.session_id, started.version
        for action, value in (
            ("value", "70.2"), ("value", "7"), ("select", "5"), ("select", "5"),
            ("select", "none"), ("value", "2400"), ("select", "rest"), ("select", "skip"),
        ):
            transitioned = service.answer(wizard_context, session_id, version, action, value)
            version = transitioned.version
        assert transitioned.step == "summary"
        bridge = PhysiqueCheckinBridge(_config(), service=service, binding_path=tmp_path / "bindings.json")
        bridge._bindings[session_id] = WizardBinding(
            session_id, "owner-1", "chat-1", "topic-1", "summary", version, "44", 4_000_000_000,
        )
        bridge._active_session_id = session_id
        bridge._persist()
        adapter = self._adapter({"physique_checkin": {
            "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
        }})
        adapter._physique_checkin = bridge
        adapter._enqueue_text_event = MagicMock()
        callback = CallbackData(session_id, "summary", version, "a0").encode()
        query = _callback(callback)

        # When: the user presses the exact Save callback through the adapter.
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

        # Then: one canonical event exists, generic ingress is untouched, and
        # the same Telegram card becomes a visible completion confirmation.
        assert len((tmp_path / "wizard-state" / "events.jsonl").read_text().splitlines()) == 1
        adapter._enqueue_text_event.assert_not_called()
        query.answer.assert_awaited_once_with(text="체크인을 저장했습니다.")
        query.edit_message_text.assert_awaited_once()
        assert query.edit_message_text.call_args.kwargs["text"] == (
            "오늘 체크인 완료\n\n"
            "- 체중: 70.2kg\n"
            "- 섭취: 2,400kcal\n"
            "- 탄수화물·단백질·지방: 기록 없음\n"
            "- 운동: 휴식\n"
            "- 수면: 7시간 · 수면 질 5/5\n"
            "- 컨디션: 5/5\n"
            "- 소화: 기록 없음\n\n"
            "일부 항목이 기록되지 않아 저장된 내용만 안내합니다.\n\n"
            "오늘 할 일\n\n"
            "- 다음 체크인에서 빠진 항목을 함께 기록해 주세요."
        )
        assert query.edit_message_text.call_args.kwargs["reply_markup"] is None
        replay = bridge.handle_callback(callback, "owner-1", "chat-1", "topic-1", "44")
        assert replay.accepted is False
        assert len((tmp_path / "wizard-state" / "events.jsonl").read_text().splitlines()) == 1

    @pytest.mark.asyncio
    async def test_saved_checkin_adds_profile_gated_model_feedback_without_generic_ingress(self, tmp_path):
        """Only an accepted finalized private check-in may ask the coach model."""
        profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
        if profile_source not in sys.path:
            sys.path.insert(0, profile_source)
        from checkin_cli.wizard import WizardService
        from checkin_cli.wizard_models import WizardContext

        service = WizardService.for_standalone(tmp_path / "wizard-state")
        wizard_context = WizardContext("owner-1", "topic-1")
        started = service.start_morning(wizard_context, "2031-02-03")
        session_id, version = started.session_id, started.version
        for action, value in (
            ("value", "70.2"), ("value", "7"), ("select", "5"), ("select", "5"),
            ("select", "none"), ("value", "2400"), ("select", "rest"), ("select", "skip"),
        ):
            transitioned = service.answer(wizard_context, session_id, version, action, value)
            version = transitioned.version
        bridge = PhysiqueCheckinBridge(_config(), service=service, binding_path=tmp_path / "bindings.json")
        bridge._bindings[session_id] = WizardBinding(
            session_id, "owner-1", "chat-1", "topic-1", "summary", version, "44", 4_000_000_000,
        )
        bridge._active_session_id = session_id
        bridge._persist()
        adapter = self._adapter({"physique_checkin": {
            "enabled": True, "owner_id": "owner-1", "chat_id": "chat-1", "topic_id": "topic-1",
            "coaching_feedback_enabled": True,
        }})
        adapter._physique_checkin = bridge
        adapter._enqueue_text_event = MagicMock()
        adapter._humanize_korean_copy = AsyncMock(
            side_effect=lambda _surface, text, _grounding: text.replace(
                "일부 항목이 기록되지 않아 저장된 내용만 안내합니다.",
                "오늘은 회복을 지키고, 내일도 같은 기준으로 확인하겠습니다.",
            )
        )
        callback = CallbackData(session_id, "summary", version, "a0").encode()
        query = _callback(callback)

        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

        adapter._enqueue_text_event.assert_not_called()
        adapter._humanize_korean_copy.assert_awaited_once()
        assert "오늘 체크인 완료" in query.edit_message_text.call_args.kwargs["text"]
        assert "오늘은 회복을 지키고" in query.edit_message_text.call_args.kwargs["text"]
        assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    def test_final_feedback_request_includes_profile_doctrine_and_verified_history(self, monkeypatch):
        # Given: the dedicated profile and a finalized, bounded check-in snapshot.
        import hermes_cli.config as config_module

        captured = MagicMock(return_value="근거 있는 피드백")
        monkeypatch.setattr(TelegramAdapter, "_request_physique_coach_completion", captured)
        monkeypatch.setattr(
            config_module,
            "get_hermes_home",
            lambda: Path("/home/cube/.hermes/profiles/physique-coach"),
        )

        # When: the post-save coaching request is built without Telegram delivery.
        feedback = TelegramAdapter._request_physique_coaching_feedback({
            "flow": "morning",
            "kst_day": "2026-07-18",
            "answers": {"bodyweight": "69.7", "sleep_duration": "7"},
        })

        # Then: the model receives public doctrine and observed-only baseline context.
        assert feedback == "근거 있는 피드백"
        system_prompt, user_content = captured.call_args.args
        assert "최코치 본인이라고 주장하지 마라" in system_prompt
        assert "원인부터 설명합니다" in user_content
        assert "D1–D20은 관찰되지 않아" in user_content
        assert "D+50 —" not in user_content

    @pytest.mark.asyncio
    async def test_launcher_shows_kst_date_and_korean_weekday_in_its_topic_card(self, monkeypatch):
        import gateway.platforms.telegram as telegram_module

        class _FixedDateTime:
            @staticmethod
            def now(tz):
                assert tz is timezone.utc
                return datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(telegram_module, "datetime", _FixedDateTime)
        adapter = self._adapter({"physique_checkin": {"enabled": True, "owner_id": "1", "chat_id": "100", "topic_id": "101"}})
        fake = MagicMock()
        fake.open_launcher.side_effect = (
            WizardReply(True, True, "", None, "pc1:0123456789abcdef0123456789abcdef:launch:0:start"),
            WizardReply(True, True, "", None, "pc1:fedcba9876543210fedcba9876543210:launch:0:start"),
        )
        adapter._physique_checkin = fake
        adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=77))
        adapter._thread_kwargs_for_send = MagicMock(return_value={"message_thread_id": 101})

        result = await adapter.send_physique_checkin_launcher()

        assert result.success is True
        kwargs = adapter._send_message_with_thread_fallback.call_args.kwargs
        assert kwargs["chat_id"] == 100
        assert kwargs["message_thread_id"] == 101
        assert kwargs["reply_markup"] is not None
        assert "2026년 7월 18일 (토)" in kwargs["text"]
        fake.bind_launcher_message.assert_any_call("0123456789abcdef0123456789abcdef", "77")
        fake.bind_launcher_message.assert_any_call("fedcba9876543210fedcba9876543210", "77")

    @pytest.mark.asyncio
    async def test_inline_card_capability_is_inert_when_profile_feature_is_disabled(self):
        adapter = self._adapter({})

        result = await adapter.send_inline_card("physique-checkin-morning")

        assert adapter.is_inline_card_enabled("physique-checkin-morning") is False
        assert result.success is False

class TestNutritionCopySurfaces:
    def test_daily_copy_keeps_the_approved_sections_and_spacing(self):
        snapshot = {
            "answers": {
                "bodyweight": "78.6",
                "calories": "2675",
                "macros": "탄수화물 335g · 단백질 160g · 지방 75g",
                "training_summary": "웨이트 70분 · RPE 7",
                "sleep_duration": "7.3",
                "sleep_quality": "4",
                "condition": "4",
                "digestion": "normal",
                "water": "2.8",
            }
        }
        feedback = (
            "오늘은 운동일 목표에 가깝게 섭취했고 단백질도 충분했습니다.\n"
            "체중 증가 속도도 현재 린매스업 목표 범위 안에 있습니다."
        )

        assert TelegramAdapter._nutrition_daily_text(snapshot, feedback) == (
            "오늘 체크인 완료\n\n"
            "- 체중: 78.6kg\n"
            "- 섭취: 2,675kcal\n"
            "- 탄수화물 335g · 단백질 160g · 지방 75g\n"
            "- 운동: 웨이트 70분 · RPE 7\n"
            "- 수면: 7.3시간 · 수면 질 4/5\n"
            "- 컨디션: 4/5\n"
            "- 소화: 정상\n\n"
            "오늘은 운동일 목표에 가깝게 섭취했고 단백질도 충분했습니다.\n"
            "체중 증가 속도도 현재 린매스업 목표 범위 안에 있습니다.\n\n"
            "오늘 할 일\n\n"
            "- 현재 식사량 유지\n"
            "- 수분 2.8L 이상 유지\n"
            "- 내일 아침 같은 조건으로 체중 측정"
        )

    def test_daily_copy_fails_closed_when_facts_or_model_are_missing(self):
        assert TelegramAdapter._nutrition_daily_text({"answers": {}}) == (
            "오늘 체크인 완료\n\n"
            "- 체중: 기록 없음\n"
            "- 섭취: 기록 없음\n"
            "- 탄수화물·단백질·지방: 기록 없음\n"
            "- 운동: 기록 없음\n"
            "- 수면: 기록 없음\n"
            "- 컨디션: 기록 없음\n"
            "- 소화: 기록 없음\n\n"
            "일부 항목이 기록되지 않아 저장된 내용만 안내합니다.\n\n"
            "오늘 할 일\n\n"
            "- 다음 체크인에서 빠진 항목을 함께 기록해 주세요."
        )

    def test_weekly_copy_renders_averages_rate_judgment_and_actions(self):
        summary = SimpleNamespace(
            average_weight_kg=78.47,
            prior_average_weight_kg=78.34,
            weekly_change_percent=0.16,
            checkin_rate_percent=100,
            goal_range="+0.10~+0.25%",
            judgment="유지",
            actions=(
                "기준 섭취량: 2,600kcal 유지",
                "단백질: 하루 160g 유지",
                "운동일에는 탄수화물을 운동 전후에 우선 배치",
                "다음 주에도 같은 조건으로 체중 추세 확인",
            ),
            interpretation=(
                "체중은 린매스업 목표 범위 안에서 안정적으로 증가했습니다.",
                "현재 속도라면 불필요한 체지방 증가 위험은 높지 않습니다.",
            ),
            rationale="급격한 증량이나 정체가 없어 이번 주에는 칼로리를 변경하지 않습니다.",
        )

        assert TelegramAdapter._nutrition_report_text("고객", summary) == (
            "이번 주 린매스업 리포트\n\n"
            "- 최근 7일 평균 체중: 78.47kg\n"
            "- 이전 7일 평균 체중: 78.34kg\n"
            "- 주간 변화: +0.16%\n"
            "- 체크인율: 100%\n"
            "- 목표 범위: +0.10~+0.25%\n\n"
            "체중은 린매스업 목표 범위 안에서 안정적으로 증가했습니다.\n"
            "현재 속도라면 불필요한 체지방 증가 위험은 높지 않습니다.\n\n"
            "이번 주 판단: 유지\n\n"
            "- 기준 섭취량: 2,600kcal 유지\n"
            "- 단백질: 하루 160g 유지\n"
            "- 운동일에는 탄수화물을 운동 전후에 우선 배치\n"
            "- 다음 주에도 같은 조건으로 체중 추세 확인\n\n"
            "급격한 증량이나 정체가 없어 이번 주에는 칼로리를 변경하지 않습니다."
        )

    def test_weekly_copy_fails_closed_when_no_report_data_exists(self):
        assert TelegramAdapter._nutrition_report_text("고객", SimpleNamespace()) == (
            "이번 주 린매스업 리포트\n\n"
            "- 최근 7일 평균 체중: 기록 없음\n"
            "- 이전 7일 평균 체중: 기록 없음\n"
            "- 주간 변화: 기록 없음\n"
            "- 목표 범위: 기록 없음\n\n"
            "최근 7일 평균과 이전 7일 평균을 확인할 수 없어 추세를 판단하지 않습니다.\n"
            "기록이 보완된 뒤 유지·조정 여부를 다시 확인합니다.\n\n"
            "이번 주 판단: 기록 보완\n\n"
            "- 다음 체크인에서 체중과 섭취 기록을 보완합니다.\n\n"
            "기록이 충분하지 않아 이번 주 판단을 확정하지 않습니다."
        )


def _schedule_checkin_cli():
    profile_source = "/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli"
    if profile_source not in sys.path:
        sys.path.insert(0, profile_source)
    import checkin_cli

    return checkin_cli


class _ScheduleCoordinator:
    def __init__(self, profile_root: Path):
        self.profile_root = profile_root
        self.owner = SimpleNamespace(
            user_id="coach",
            chat_id="owner-chat",
            topic_id="owner-topic",
        )
        customer_address = SimpleNamespace(
            user_id="client",
            chat_id="customer-chat",
            topic_id="customer-topic",
        )
        spec = SimpleNamespace(
            customer_key="client_001",
            display_name="고객 001",
            telegram=customer_address,
            plan=SimpleNamespace(starts_on=date(2026, 7, 1)),
            profile=SimpleNamespace(),
        )
        self._customer = SimpleNamespace(
            spec=spec,
            data_root=profile_root / "customer",
        )
        self._customer.data_root.joinpath("wizard").mkdir(parents=True)
        self.registry = SimpleNamespace(
            owner=self.owner,
            customers=(self._customer,),
        )

    def refresh_live_registry(self):
        return True

    def customer(self, customer_key):
        return self._customer if customer_key == "client_001" else None

    def customer_start_callback(self, customer_key):
        return f"cs1:{customer_key}"

    def customer_transport_allowed(self, customer_key, destination, *, kst_date):
        return customer_key == "client_001" and destination is self._customer.spec.telegram


def _schedule_adapter(coordinator, provider):
    adapter = object.__new__(TelegramAdapter)
    adapter._bot = SimpleNamespace()
    adapter._get_nutrition_coaching = lambda: coordinator
    adapter._thread_kwargs_for_send = lambda _chat, topic, _metadata: {
        "message_thread_id": topic,
    }
    adapter._send_message_strict_topic = provider
    adapter._customer_checkin_keyboard_topics = {
        ("customer-chat", "customer-topic"),
    }
    adapter._physique_markup = lambda prompt: prompt
    return adapter


def _due_schedule_tasks(monkeypatch, checkin_cli, *tasks):
    monkeypatch.setattr(
        checkin_cli,
        "build_due_customer_tasks",
        lambda _registry, _now: tuple(tasks),
    )


def _schedule_rows(profile_root: Path):
    path = profile_root / "data" / "scheduled-deliveries.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
def _approved_reminder_config(*, response_window_ends_at="2026-07-21T08:30:00+09:00"):
    return PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "nutrition_coaching": {
                "missing_checkin_reminder": {
                    "operator_approval": "topic-59-approved-reminder-evidence-v1",
                    "response_window_ends_at": response_window_ends_at,
                }
            }
        },
    )


class _ReminderDualCoach:
    def __init__(self, checkin_cli, profile_root):
        self._checkin_cli = checkin_cli
        self._profile_root = profile_root
        self.reservations = []
        self.review_rows = []
        self.after_first_reservation = None
        self.reject_second_reservation = False
        self.response_after_sending = False
        self.response_before_reservation = False
        self.canonical_transaction = SimpleNamespace(
            read_snapshot=lambda: SimpleNamespace(events=()),
            current_terminal_morning_response=self._current_terminal_morning_response,
            authorize_missing_morning_reminder=self._authorize_missing_morning_reminder,
        )

    def _authorize_missing_morning_reminder(self, _day, authorize):
        if self.response_before_reservation:
            return None
        return authorize(0, "0" * 64)

    def _current_terminal_morning_response(self, _day):
        if not self.response_after_sending:
            return None
        if _schedule_rows(self._profile_root)[-1:][0].get("state") != "sending":
            return None
        return SimpleNamespace(occurred_at_kst="2026-07-21T08:16:00+09:00")

    def reserve_missing_checkin_reminder(
        self,
        missing_window,
        destination,
        *,
        registry_digest,
        config_digest,
        operator_approval,
        canonical_sequence=None,
        canonical_digest=None,
    ):
        reservation = {
            "missing_window": missing_window,
            "destination": destination,
            "registry_digest": registry_digest,
            "config_digest": config_digest,
            "operator_approval": operator_approval,
            "canonical_sequence": canonical_sequence,
            "canonical_digest": canonical_digest,
        }
        self.reservations.append(reservation)
        if len(self.reservations) == 1 and self.after_first_reservation:
            self.after_first_reservation()
        schedule = __import__(
            "checkin_cli.customer_schedule",
            fromlist=["reserve_missing_checkin_reminder"],
        )
        receipt = schedule.reserve_missing_checkin_reminder(
            self._profile_root,
            "client_001",
            missing_window,
            destination,
            registry_digest=registry_digest,
            config_digest=config_digest,
            operator_approval=operator_approval,
            canonical_sequence=canonical_sequence,
            canonical_digest=canonical_digest,
        )
        if self.reject_second_reservation:
            raise RuntimeError("policy pin expired")
        reservation["reservation_id"] = receipt.reservation_id
        return receipt

    def reminder_review_candidate(self, reminder, **_kwargs):
        row = {
            "reservation_id": reminder.reservation_id,
            "state": reminder.state,
        }
        if row not in self.review_rows:
            self.review_rows.append(row)
        return row


def _reminder_adapter(monkeypatch, tmp_path, *, provider):
    checkin_cli = _schedule_checkin_cli()
    task = checkin_cli.CustomerScheduleTask("client_001", "reminder", date(2026, 7, 21))
    _due_schedule_tasks(monkeypatch, checkin_cli, task)
    coordinator = _ScheduleCoordinator(tmp_path)
    adapter = _schedule_adapter(coordinator, provider)
    adapter.config = _approved_reminder_config()
    dual_coach = _ReminderDualCoach(checkin_cli, tmp_path)
    coaching = __import__(
        "checkin_cli.customer_coaching",
        fromlist=["RegisteredCustomerDualCoachCoordinator"],
    )
    monkeypatch.setattr(
        coaching,
        "RegisteredCustomerDualCoachCoordinator",
        lambda _customer: dual_coach,
    )
    return adapter, coordinator, dual_coach, task


class TestNutritionScheduleDelivery:
    @pytest.mark.asyncio
    async def test_tick_success_persists_audited_receipt(self, tmp_path, monkeypatch):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(return_value=SimpleNamespace(message_id="message-1"))
        adapter = _schedule_adapter(coordinator, provider)
        adapter._ensure_customer_checkin_keyboard = AsyncMock()

        result = await adapter._send_nutrition_coaching_tick(
            datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        )

        assert result.success is True
        assert provider.await_count == 1
        adapter._ensure_customer_checkin_keyboard.assert_not_awaited()
        assert [row["state"] for row in _schedule_rows(tmp_path)] == [
            "prepared",
            "sending",
            "delivered",
            "sent_audited",
        ]

    @pytest.mark.asyncio
    async def test_provider_timeout_is_unknown_and_never_retried(
        self, tmp_path, monkeypatch
    ):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(side_effect=asyncio.TimeoutError())
        adapter = _schedule_adapter(coordinator, provider)
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is False and second.success is False
        assert provider.await_count == 1
        assert _schedule_rows(tmp_path)[-1]["state"] == "unknown"

    @pytest.mark.asyncio
    async def test_delivered_receipt_restarts_into_audit_without_provider(
        self, tmp_path, monkeypatch
    ):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(return_value=SimpleNamespace(message_id="message-1"))
        adapter = _schedule_adapter(coordinator, provider)
        real_audit = checkin_cli.mark_customer_task_sent_audited
        monkeypatch.setattr(
            checkin_cli,
            "mark_customer_task_sent_audited",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit")),
        )
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        assert first.success is False
        assert _schedule_rows(tmp_path)[-1]["state"] == "delivered"

        monkeypatch.setattr(checkin_cli, "mark_customer_task_sent_audited", real_audit)
        restarted = _schedule_adapter(
            coordinator, AsyncMock(return_value=SimpleNamespace(message_id="message-2"))
        )
        second = await restarted._send_nutrition_coaching_tick(now)

        assert second.success is True
        assert provider.await_count == 1
        assert restarted._send_message_strict_topic.await_count == 0
        assert _schedule_rows(tmp_path)[-1]["state"] == "sent_audited"

    @pytest.mark.asyncio
    async def test_duplicate_tick_has_one_provider_delivery(self, tmp_path, monkeypatch):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(return_value=SimpleNamespace(message_id="message-1"))
        adapter = _schedule_adapter(coordinator, provider)
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is True and second.success is True
        assert provider.await_count == 1
    @pytest.mark.asyncio
    async def test_concurrent_ticks_share_one_durable_provider_authority(
        self, tmp_path, monkeypatch
    ):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send_once(**_kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id="message-concurrent")

        provider = AsyncMock(side_effect=send_once)
        adapter = _schedule_adapter(coordinator, provider)
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first_task = asyncio.create_task(adapter._send_nutrition_coaching_tick(now))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second_task = asyncio.create_task(adapter._send_nutrition_coaching_tick(now))
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert first.success is True
        assert second.success is False
        assert provider.await_count == 1
        assert _schedule_rows(tmp_path)[-1]["state"] == "sent_audited"
    @pytest.mark.asyncio
    async def test_orphan_tombstone_recovery_is_terminal_and_never_calls_provider(
        self, tmp_path, monkeypatch
    ):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(return_value=SimpleNamespace(message_id="must-not-send"))
        adapter = _schedule_adapter(coordinator, provider)
        claim = (
            tmp_path
            / "data"
            / "customer-schedule-claims"
            / task.customer_key
            / task.kst_day.isoformat()
            / f"{task.kind}.claim"
        )
        claim.parent.mkdir(parents=True)
        claim.write_bytes(b"scheduled-delivery-tombstone-v1\nrecovery-only\n")
        claim.chmod(0o600)
        before = claim.read_bytes()

        result = await adapter._send_nutrition_coaching_tick(
            datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        )

        assert result.success is False
        assert provider.await_count == 0
        fence = json.loads(
            (tmp_path / "data" / "scheduled-deliveries-fence.json").read_text()
        )
        assert fence["state"] == "recovery_required"
        assert checkin_cli.initialize_schedule_delivery_fence(tmp_path).state == "ready"
        assert claim.read_bytes() == before
        assert _schedule_rows(tmp_path)[-1]["state"] == "unknown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fence", ["preparing", "corrupt"])
    async def test_invalid_cutover_fence_sends_nothing(
        self, tmp_path, monkeypatch, fence
    ):
        checkin_cli = _schedule_checkin_cli()
        task = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 21)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, task)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(return_value=SimpleNamespace(message_id="message-1"))
        adapter = _schedule_adapter(coordinator, provider)
        if fence == "preparing":
            checkin_cli.prepare_schedule_delivery_cutover(tmp_path)
        else:
            fence_path = tmp_path / "data" / "scheduled-deliveries-fence.json"
            fence_path.parent.mkdir(parents=True)
            fence_path.write_text("{not-json}\n")

        result = await adapter._send_nutrition_coaching_tick(
            datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        )

        assert result.success is False
        assert provider.await_count == 0

    @pytest.mark.asyncio
    async def test_daily_customer_and_weekly_owner_destinations_are_pinned(
        self, tmp_path, monkeypatch
    ):
        checkin_cli = _schedule_checkin_cli()
        daily = checkin_cli.CustomerScheduleTask(
            "client_001", "daily", date(2026, 7, 20)
        )
        weekly = checkin_cli.CustomerScheduleTask(
            "client_001", "weekly", date(2026, 7, 20)
        )
        _due_schedule_tasks(monkeypatch, checkin_cli, daily, weekly)
        coordinator = _ScheduleCoordinator(tmp_path)
        provider = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id="daily-message"),
                SimpleNamespace(message_id="weekly-message"),
            ]
        )
        adapter = _schedule_adapter(coordinator, provider)
        adapter._humanize_korean_copy = AsyncMock(return_value="변경된 주간 문구")
        adapter._coaching_processing_allowed = MagicMock(return_value=False)
        reporting = __import__(
            "checkin_cli.customer_reporting", fromlist=["build_customer_weekly_summary"]
        )
        monkeypatch.setattr(
            reporting,
            "build_customer_weekly_summary",
            lambda *_args, **_kwargs: SimpleNamespace(
                starts_on="2026-07-13",
                ends_on="2026-07-19",
                eligible_weekdays=(),
                checkin_dates=(),
                checkin_rate_percent=0,
                trends=(),
                keep_behaviors=(),
                change_behaviors=(),
                next_decision="다음 주 결정",
            ),
        )

        result = await adapter._send_nutrition_coaching_tick(
            datetime(2026, 7, 20, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        )

        assert result.success is True
        assert provider.await_count == 2
        calls = provider.await_args_list
        assert calls[0].kwargs["chat_id"] == "customer-chat"
        assert calls[0].kwargs["message_thread_id"] == "customer-topic"
        assert "오늘 체크인을 시작하거나 이어갈 수 있습니다." in calls[0].kwargs["text"]
        assert calls[1].kwargs["chat_id"] == "owner-chat"
        assert calls[1].kwargs["message_thread_id"] == "owner-topic"
        assert calls[1].kwargs["text"].startswith("이번 주 린매스업 리포트")
        assert calls[1].kwargs["text"] != "변경된 주간 문구"
        adapter._coaching_processing_allowed.assert_called_with(
            "weekly",
            ANY,
        )
    @pytest.mark.asyncio
    async def test_dual_coach_reminder_is_static_pinned_and_exactly_once(
        self, tmp_path, monkeypatch
    ):
        provider = AsyncMock(return_value=SimpleNamespace(message_id="reminder-1"))
        adapter, _coordinator, dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        adapter._humanize_korean_copy = AsyncMock()
        adapter._coaching_processing_allowed = MagicMock()
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is True and second.success is True
        assert provider.await_count == 1
        assert provider.await_args.kwargs == {
            "chat_id": "customer-chat",
            "message_thread_id": "customer-topic",
            "text": "체크인이 확인되지 않았습니다. 오늘 아침 체크인을 제출해 주세요.",
        }
        adapter._humanize_korean_copy.assert_not_awaited()
        adapter._coaching_processing_allowed.assert_not_called()
        assert dual_coach.reservations[0]["operator_approval"] == (
            "topic-59-approved-reminder-evidence-v1"
        )
        assert len(dual_coach.reservations) == 2
        assert [row["state"] for row in _schedule_rows(tmp_path)] == [
            "prepared",
            "sending",
            "delivered",
            "sent_audited",
        ]

    @pytest.mark.asyncio
    async def test_terminal_response_after_sending_abandons_without_provider_or_review(
        self, tmp_path, monkeypatch
    ):
        provider = AsyncMock(return_value=SimpleNamespace(message_id="must-not-send"))
        adapter, coordinator, dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        dual_coach.response_after_sending = True
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)

        restarted_provider = AsyncMock(return_value=SimpleNamespace(message_id="retry"))
        restarted_dual_coach = _ReminderDualCoach(_schedule_checkin_cli(), tmp_path)
        restarted_dual_coach.response_before_reservation = True
        coaching = __import__(
            "checkin_cli.customer_coaching",
            fromlist=["RegisteredCustomerDualCoachCoordinator"],
        )
        monkeypatch.setattr(
            coaching,
            "RegisteredCustomerDualCoachCoordinator",
            lambda _customer: restarted_dual_coach,
        )
        restarted = _schedule_adapter(coordinator, restarted_provider)
        restarted.config = _approved_reminder_config()
        second = await restarted._send_nutrition_coaching_tick(now)

        assert first.success is True and second.success is True
        assert provider.await_count == restarted_provider.await_count == 0
        assert dual_coach.review_rows == restarted_dual_coach.review_rows == []
        rows = _schedule_rows(tmp_path)
        assert [row["state"] for row in rows] == ["prepared", "sending", "abandoned"]
        assert rows[-1]["reason"] == "terminal_morning_response_before_provider"
        assert all(row["provider_receipt"] is None for row in rows)
        assert all(row["message_id"] is None for row in rows)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "authority",
        ["registration", "destination", "config"],
    )
    async def test_dual_coach_reminder_stale_authority_never_calls_provider(
        self, tmp_path, monkeypatch, authority
    ):
        provider = AsyncMock(return_value=SimpleNamespace(message_id="must-not-send"))
        adapter, coordinator, dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        if authority == "registration":
            refreshes = iter((True, False))
            coordinator.refresh_live_registry = lambda: next(refreshes)
        elif authority == "destination":
            replacement = SimpleNamespace(
                user_id="client",
                chat_id="replacement-chat",
                topic_id="replacement-topic",
            )
            dual_coach.after_first_reservation = lambda: setattr(
                coordinator._customer.spec, "telegram", replacement
            )
        elif authority == "config":
            dual_coach.after_first_reservation = lambda: adapter.config.extra[
                "nutrition_coaching"
            ].update({"authority_revision": "stale"})
        else:
            dual_coach.reject_second_reservation = True

        result = await adapter._send_nutrition_coaching_tick(
            datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        )

        assert result.success is False
        assert provider.await_count == 0
        assert _schedule_rows(tmp_path)[-1]["state"] == "unknown"

    @pytest.mark.asyncio
    async def test_dual_coach_reminder_provider_unknown_is_never_retried(
        self, tmp_path, monkeypatch
    ):
        provider = AsyncMock(side_effect=asyncio.TimeoutError())
        adapter, _coordinator, _dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is False and second.success is False
        assert provider.await_count == 1
        assert _schedule_rows(tmp_path)[-1]["state"] == "unknown"
    @pytest.mark.asyncio
    async def test_dual_coach_reminder_explicit_no_send_rejection_is_replaceable(
        self, tmp_path, monkeypatch
    ):
        from gateway.platforms.telegram import ReminderNoSendRejection

        provider = AsyncMock(return_value=ReminderNoSendRejection("topic_closed_before_send"))
        adapter, _coordinator, _dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is False and second.success is False
        assert provider.await_count == 1
        assert [row["state"] for row in _schedule_rows(tmp_path)][-1] == "known_failure"

    @pytest.mark.asyncio
    async def test_dual_coach_reminder_ambiguous_rejection_remains_unknown(
        self, tmp_path, monkeypatch
    ):
        provider = AsyncMock(return_value=SimpleNamespace(success=False))
        adapter, _coordinator, _dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is False and second.success is False
        assert provider.await_count == 1
        assert [row["state"] for row in _schedule_rows(tmp_path)][-1] == "unknown"

    @pytest.mark.asyncio
    async def test_dual_coach_reminder_expired_audited_delivery_creates_one_review(
        self, tmp_path, monkeypatch
    ):
        provider = AsyncMock(return_value=SimpleNamespace(message_id="reminder-1"))
        adapter, _coordinator, dual_coach, _task = _reminder_adapter(
            monkeypatch, tmp_path, provider=provider
        )
        before_expiry = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        after_expiry = datetime(2026, 7, 21, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))

        sent = await adapter._send_nutrition_coaching_tick(before_expiry)
        first_review = await adapter._send_nutrition_coaching_tick(after_expiry)
        duplicate_review = await adapter._send_nutrition_coaching_tick(after_expiry)

        assert sent.success is True
        assert first_review.success is True and duplicate_review.success is True
        assert provider.await_count == 1
        assert dual_coach.review_rows == [{
            "reservation_id": dual_coach.reservations[-1]["reservation_id"],
            "state": "sent_audited",
        }]


def test_coaching_stage_uses_resolved_provider_token_parameter(monkeypatch):
    from agent import auxiliary_client
    from hermes_cli import config as hermes_config

    captured = {}
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: (
                    captured.update(kwargs)
                    or SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content='{"slots":[]}'))]
                    )
                )
            )
        )
    )
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"model": {"provider": "openai", "default": "gpt-5.6"}},
    )
    monkeypatch.setattr(
        auxiliary_client,
        "resolve_provider_client",
        lambda _provider, _model: (client, "gpt-5.6"),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "auxiliary_max_tokens_param",
        lambda value, *, model=None: {"max_completion_tokens": value},
    )

    result = TelegramAdapter._request_physique_coaching_stage(
        "system",
        "user",
        max_output_tokens=256,
    )

    assert result == '{"slots":[]}'
    assert captured["max_completion_tokens"] == 256
    assert "max_output_tokens" not in captured
    assert "max_tokens" not in captured

def _strict_daily_input() -> DailyGroundingInput:
    return DailyGroundingInput.from_finalized_snapshot(
        {
            "flow": "morning",
            "kst_day": "2026-07-21",
            "answers": {"bodyweight": "80.0"},
        },
        canonical="오늘 체크인 완료",
    )

def _strict_adaptive_input() -> AdaptiveGroundingInput:
    return AdaptiveGroundingInput(
        (
            ("evaluation_day", "2026-07-21"),
            ("goal_mode", "maintenance"),
            ("goal_range", "[\"\",\"\"]"),
            ("decision", "observe"),
            ("reason_category_ids", "[]"),
            ("target_macros", "[]"),
            ("carb_category_targets", "[]"),
            ("safety_held", "false"),
            ("approval_state", "pending"),
            ("delivery_state", "not_delivered"),
        ),
        (),
        "a" * 64,
        "client_001",
        ("adaptive-proposal",),
        ("medical", "unsafe_nutrition"),
        "observe",
    )



class TestStrictCoachingClosure:
    def test_malformed_grounding_containers_are_canonical_with_empty_binding(self):
        adapter = object.__new__(TelegramAdapter)
        valid = _strict_daily_input()
        malformed = replace(valid, excluded_risk_ids=["medical", "unsafe_nutrition"])
        assert TelegramAdapter._valid_grounding_input("daily", valid) is True
        assert TelegramAdapter._valid_grounding_input("daily", malformed) is False
        receipt = TelegramAdapter._canonical_coaching_receipt(
            "daily",
            "오늘 체크인 완료",
            malformed,
        )
        assert receipt.revision_binding_digest == ""

    def test_revision_drift_before_stage_one_skips_grounding_and_model(self):
        adapter = object.__new__(TelegramAdapter)
        adapter._physique_checkin_config = SimpleNamespace(coaching_feedback_enabled=True)
        adapter._coaching_processing_allowed = MagicMock(return_value=True)
        adapter._coaching_grounding_for_input = MagicMock()
        adapter._request_physique_coaching_stage = MagicMock()
        frozen = _strict_daily_input()
        drifted = replace(frozen, revision_binding_digest="b" * 64)
        adapter._coaching_current_input_supplier = ("daily", lambda: drifted)

        canonical = "오늘 체크인 완료"
        assert asyncio.run(
            adapter._humanize_korean_copy("daily", canonical, frozen)
        ) == canonical
        adapter._coaching_grounding_for_input.assert_not_called()
        adapter._request_physique_coaching_stage.assert_not_called()

    def test_revision_drift_between_stages_discards_model_output(self, monkeypatch):
        adapter = object.__new__(TelegramAdapter)
        adapter._physique_checkin_config = SimpleNamespace(coaching_feedback_enabled=True)
        adapter._coaching_processing_allowed = MagicMock(return_value=True)
        adapter._coaching_grounding_for_input = MagicMock(return_value=object())
        frozen = _strict_daily_input()
        current = [frozen]
        adapter._coaching_current_input_supplier = ("daily", lambda: current[0])
        calls = []

        def fake_pipeline(
            surface,
            canonical,
            grounding,
            request,
            *,
            processing_allowed,
            revision_binding_digest,
        ):
            assert processing_allowed() is True
            calls.append("stage-one")
            current[0] = replace(frozen, revision_binding_digest="c" * 64)
            assert processing_allowed() is False
            return canonical, TelegramAdapter._canonical_coaching_receipt(
                surface,
                canonical,
                grounding,
            )

        monkeypatch.setattr(
            "gateway.platforms.telegram.coach_and_polish",
            fake_pipeline,
        )
        canonical = "오늘 체크인 완료"
        assert asyncio.run(
            adapter._humanize_korean_copy("daily", canonical, frozen)
        ) == canonical
        assert calls == ["stage-one"]

    def test_adaptive_payload_requires_exact_proposal_revision_and_digest(self):
        adapter = object.__new__(TelegramAdapter)
        adaptive = MagicMock()
        adaptive._proposal_for_digest.return_value = None
        nutrition = SimpleNamespace(
            adaptive_nutrition_coordinator=MagicMock(return_value=adaptive)
        )
        facts_resolver = MagicMock()
        adapter._nutrition_coaching = nutrition
        adapter._adaptive_operator_service = SimpleNamespace(
            coaching_facts_for_current_card=facts_resolver,
        )
        invalid = adapter._adaptive_grounding_input(
            {"customer_key": "client_001", "proposal_digest": "FORGED", "revision": 1}
        )
        assert invalid is None
        facts_resolver.assert_not_called()
        stale = adapter._adaptive_grounding_input(
            {
                "customer_key": "client_001",
                "proposal_digest": "a" * 64,
                "revision": 1,
            }
        )
        assert stale is None
        facts_resolver.assert_not_called()
    def test_daily_publication_rechecks_revision_before_edit(self):
        adapter = object.__new__(TelegramAdapter)
        adapter._physique_checkin_config = SimpleNamespace(coaching_feedback_enabled=True)
        snapshot = {
            "flow": "morning",
            "kst_day": "2026-07-21",
            "answers": {"bodyweight": "80.0"},
        }
        drifted = {
            **snapshot,
            "answers": {"bodyweight": "81.0"},
        }
        callback = CallbackData(
            "0123456789abcdef0123456789abcdef",
            "done",
            1,
            "start",
        )
        bridge = SimpleNamespace(
            finalized_coaching_snapshot=MagicMock(side_effect=[snapshot, drifted]),
        )
        adapter._get_physique_checkin = lambda: bridge
        adapter._humanize_korean_copy = AsyncMock(return_value="코칭 문구")
        adapter._coaching_processing_allowed = MagicMock(return_value=True)
        adapter._physique_markup = lambda prompt: prompt
        query = _callback(callback.encode())
        reply = WizardReply(
            True,
            True,
            "체크인을 저장했습니다.",
            WizardPrompt("완료", ()),
            callback.encode(),
        )

        asyncio.run(adapter._render_physique_callback_prompt(query, reply))

        query.edit_message_text.assert_not_awaited()

    def test_adaptive_publication_rechecks_revision_before_reservation(self):
        adapter = object.__new__(TelegramAdapter)
        adapter._coaching_processing_allowed = MagicMock(return_value=True)
        frozen = _strict_adaptive_input()
        stale = replace(frozen, revision_binding_digest="d" * 64)
        payload = {
            "status": "card",
            "text": "적응형 초안",
            "customer_key": "client_001",
            "proposal_digest": "a" * 64,
            "revision": 1,
            "buttons": [],
        }
        service = SimpleNamespace(
            handle_callback=MagicMock(return_value=dict(payload)),
            mark_publish_pending=MagicMock(),
        )
        adapter._adaptive_operator_service = service
        adapter._nutrition_address = lambda *_args: SimpleNamespace()
        adapter._adaptive_grounding_input = MagicMock(side_effect=[frozen, stale])
        adapter._humanize_korean_copy = AsyncMock(return_value="적응형 초안")
        query = _callback("nc2:token:approve")
        query.edit_message_text = AsyncMock()

        asyncio.run(
            adapter._handle_adaptive_review_callback(
                query,
                "nc2:token:approve",
                query.message,
            )
        )

        service.mark_publish_pending.assert_not_called()
        query.edit_message_text.assert_not_awaited()
        assert query.answer.await_count >= 1
def test_dual_coach_review_recovery_is_topic_bound_and_deduplicated() -> None:
    adapter = object.__new__(TelegramAdapter)
    address = SimpleNamespace(user_id="reviewer", chat_id="review-chat", topic_id="59")
    card = SimpleNamespace(card_id="dual-coach-review:client_001:risk-1", text="운영자 검토 전용")
    adapter._nutrition_operator_address = lambda: address
    adapter._nutrition_coaching = object()
    adapter._dual_coach_review_service = SimpleNamespace(
        accepts=lambda actual: actual is address,
        cards=lambda: (card,),
    )
    adapter._send_message_strict_topic = AsyncMock()

    asyncio.run(adapter._recover_dual_coach_review_cards())
    asyncio.run(adapter._recover_dual_coach_review_cards())

    assert adapter._send_message_strict_topic.await_count == 1
    assert adapter._send_message_strict_topic.await_args.kwargs == {
        "chat_id": "review-chat",
        "message_thread_id": "59",
        "text": "운영자 검토 전용",
    }
    assert not hasattr(adapter, "_customer_transport")
def test_topic_59_dual_coach_drain_coexists_with_adaptive_operator() -> None:
    adapter = object.__new__(TelegramAdapter)
    address = SimpleNamespace(user_id="reviewer", chat_id="review-chat", topic_id="59")
    address.key = ("reviewer", "review-chat", "59")
    adapter._nutrition_operator_address = lambda: address
    adapter._nutrition_address = lambda *_args: address
    adapter._nutrition_coaching = object()
    adapter._dual_coach_review_service = SimpleNamespace(
        accepts=lambda actual: actual is address,
        cards=lambda: (),
    )
    adaptive = SimpleNamespace(
        accepts=lambda actual: actual is address,
        handle_text=MagicMock(return_value={"status": "accepted", "text": "적응형 검토 확인"}),
    )
    adapter._adaptive_operator_service = adaptive
    message = SimpleNamespace(
        chat=SimpleNamespace(id="review-chat"),
        text="적응형 영양 검토",
        message_id="message-1",
        message_thread_id=59,
        reply_to_message=None,
        reply_text=AsyncMock(),
    )

    assert asyncio.run(adapter._reserve_adaptive_review_update(SimpleNamespace(), message)) is True

    adaptive.handle_text.assert_called_once()
    assert message.reply_text.await_count == 2
    assert "메뉴를 열려면" in message.reply_text.await_args_list[0].args[0]
    message.reply_text.assert_awaited_with("적응형 검토 확인", reply_markup=None)
def test_dual_coach_review_runtime_drain_requires_topic_59_and_persists_receipt() -> None:
    card = SimpleNamespace(
        card_id="dual-coach-review:client_001:reminder-1",
        text="운영자 검토 전용",
    )
    claimed: set[str] = set()
    receipts: list[tuple[str, str]] = []

    service = SimpleNamespace(
        accepts=lambda _address: True,
        cards=lambda: (card,),
        claim_publication=lambda value: (
            False if value.card_id in claimed else (claimed.add(value.card_id) or True)
        ),
        record_publication=lambda value, receipt: receipts.append((value.card_id, receipt)),
        publication_receipt=lambda result: str(result.message_id),
    )

    def adapter(topic_id: str):
        value = object.__new__(TelegramAdapter)
        value._nutrition_operator_address = lambda: SimpleNamespace(
            user_id="reviewer", chat_id="review-chat", topic_id=topic_id
        )
        value._nutrition_coaching = object()
        value._dual_coach_review_service = service
        value._send_message_strict_topic = AsyncMock(
            return_value=SimpleNamespace(message_id="operator-101")
        )
        return value

    first = adapter("59")
    asyncio.run(first._drain_dual_coach_review_cards())
    restarted = adapter("59")
    asyncio.run(restarted._drain_dual_coach_review_cards())
    wrong_topic = adapter("58")
    asyncio.run(wrong_topic._drain_dual_coach_review_cards())

    assert receipts == [(card.card_id, "operator-101")]
    assert first._send_message_strict_topic.await_args.kwargs == {
        "chat_id": "review-chat",
        "message_thread_id": "59",
        "text": "운영자 검토 전용",
    }
    restarted._send_message_strict_topic.assert_not_awaited()
    wrong_topic._send_message_strict_topic.assert_not_awaited()
