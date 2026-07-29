from __future__ import annotations

import hashlib
import importlib
import importlib.util
import http.client
import asyncio
import json
import sys
import pytest
from datetime import date, datetime
from pathlib import Path
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo


PROFILE_PACKAGE = Path("/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli")
if str(PROFILE_PACKAGE) not in sys.path:
    sys.path.insert(0, str(PROFILE_PACKAGE))

from checkin_cli import load_customer_registry
from checkin_cli.customer_admin import activate_customer, disable_customer, set_customer_ai_consent
from checkin_cli.customer_coaching import AiProcessingConsent
_PROFILE_WIZARD_DOMAIN_TEST = PROFILE_PACKAGE / "tests" / "test_wizard_domain.py"
from gateway.platforms.korean_humanizer import WeeklyGroundingInput
from gateway.platforms.telegram import TelegramAdapter


def _profile_ac21_safety_cases():
    spec = importlib.util.spec_from_file_location(
        "_physique_coach_wizard_domain_fixtures",
        _PROFILE_WIZARD_DOMAIN_TEST,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"profile safety fixture table is unavailable: {_PROFILE_WIZARD_DOMAIN_TEST}")
    fixture_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture_module
    spec.loader.exec_module(fixture_module)
    return fixture_module.AC21_SAFETY_CASES


AC21_SAFETY_CASES = _profile_ac21_safety_cases()


def _registry(
    tmp_path: Path,
    *,
    owner_chat_id: str = "control",
    include_disabled_second: bool = False,
):
    weeks = [
        {"week": week, "calories_kcal": 2300, "protein_g": 150, "meal_structure": ["아침", "점심", "저녁"]}
        for week in range(1, 13)
    ]
    payload = {
        "version": 1,
        "owner": {"user_id": "coach", "chat_id": owner_chat_id, "topic_id": "owner"},
        "customers": [{
            "customer_key": "client_001",
            "display_name": "고객 001",
            "enabled": True,
            "telegram": {"user_id": "client", "chat_id": "customer-chat", "topic_id": "customer-topic"},
            "trainer": {"user_id": "trainer", "chat_id": "trainer-chat", "topic_id": "trainer-topic"},
            "ai_processing_consent": {
                "granted": True,
                "recorded_on": "2026-07-01",
                "notice_version": "privacy-v1",
            },
            "schedule": {"daily_time": "08:00", "weekly_weekday": 0, "monthly_day": 1},
            "plan": {"starts_on": "2026-07-01", "focus": "nutrition_90_training_10", "weeks": weeks},
        }],
    }
    if include_disabled_second:
        payload["customers"].append(
            {
                "customer_key": "client_002",
                "display_name": "고객 002",
                "enabled": False,
                "telegram": {
                    "user_id": "disabled-client",
                    "chat_id": "disabled-chat",
                    "topic_id": "disabled-topic",
                },
                "schedule": {"daily_time": "08:30", "weekly_weekday": 1, "monthly_day": 2},
                "plan": {"starts_on": "2026-07-01", "focus": "nutrition_90_training_10", "weeks": weeks},
            }
        )
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_customer_registry(path, tmp_path)
def _profile_registry(
    tmp_path: Path,
    *,
    enabled: bool = False,
    committed: bool = False,
) -> tuple[Path, Path, Path]:
    profile_root = tmp_path / "profile"
    registry_path = profile_root / "customers" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    weeks = [
        {
            "week": week,
            "calories_kcal": 2300,
            "protein_g": 150,
            "meal_structure": ["아침", "점심", "저녁"],
        }
        for week in range(1, 13)
    ]
    payload = {
        "version": 1,
        "owner": {"user_id": "coach", "chat_id": "owner-chat", "topic_id": "owner-topic"},
        "customers": [
            {
                "customer_key": "client_001",
                "display_name": "고객 001",
                "enabled": enabled,
                "telegram": {
                    "user_id": "client",
                    "chat_id": "customer-chat",
                    "topic_id": "customer-topic",
                },
                "trainer": {
                    "user_id": "trainer",
                    "chat_id": "trainer-chat",
                    "topic_id": "trainer-topic",
                },
                "ai_processing_consent": {
                    "granted": True,
                    "recorded_on": "2026-07-01",
                    "notice_version": "privacy-v1",
                },
                "schedule": {"daily_time": "08:00", "weekly_weekday": 0, "monthly_day": 1},
                "plan": {
                    "starts_on": "2026-07-01",
                    "focus": "nutrition_90_training_10",
                    "weeks": weeks,
                },
            }
        ],
    }
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    data_root = profile_root / "data" / "customers" / "client_001"
    data_root.mkdir(parents=True)
    checklist_path = tmp_path / "activation-checklist.json"
    checklist_path.write_text(
        json.dumps(
            {
                "checklist": {
                    "token_rotated": True,
                    "missend_test_passed": True,
                    "provider_terms_checked": {
                        "checked": True,
                        "version": "privacy-v1",
                    },
                    "withdrawal_deletion_doc": True,
                    "retention_backup_doc": True,
                    "manual_fallback_doc": True,
                }
            }
        ),
        encoding="utf-8",
    )
    if committed:
        activate_customer(
            profile_root,
            data_root,
            "client_001",
            checklist_path,
            kst_date=date(2026, 7, 1),
        )
    return profile_root, registry_path, data_root



def _module():
    try:
        return importlib.import_module("gateway.platforms.nutrition_coaching")
    except ModuleNotFoundError:
        return None
def _nutrition_adapter(profile_root: Path, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.telegram import TelegramAdapter

    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: profile_root)
    return TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "nutrition_coaching": {
                    "enabled": True,
                    # The profile loader, rather than this hint, owns canonical resolution.
                    "registry_path": "registry.json",
                }
            },
        )
    )


def test_nutrition_startup_rejects_manual_enable_without_committed_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_root, _, _ = _profile_registry(tmp_path, enabled=True)
    adapter = _nutrition_adapter(profile_root, monkeypatch)

    assert adapter._get_nutrition_coaching() is None
    assert "CustomerAdminError" in (adapter._nutrition_coaching_error or "")


def test_nutrition_startup_accepts_committed_receipt_and_canonical_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_root, registry_path, _ = _profile_registry(tmp_path, committed=True)
    adapter = _nutrition_adapter(profile_root, monkeypatch)

    coordinator = adapter._get_nutrition_coaching()

    assert coordinator is not None
    assert coordinator._registry_path == registry_path.resolve()


def test_nutrition_live_reload_rejects_manual_enable_without_committed_receipt(
    tmp_path: Path,
) -> None:
    module = _module()
    assert module is not None
    profile_root, registry_path, _ = _profile_registry(tmp_path, enabled=True)
    coordinator = module.NutritionCoachingCoordinator(
        profile_root,
        load_customer_registry(registry_path, profile_root),
        registry_path=registry_path,
    )

    assert coordinator.refresh_live_registry() is False
    assert coordinator._live_registry_error == "customer registry reload failed: CustomerAdminError"


def test_nutrition_live_reload_keeps_disable_and_revoke_live(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    profile_root, registry_path, _ = _profile_registry(tmp_path, committed=True)
    coordinator = module.NutritionCoachingCoordinator(
        profile_root,
        load_customer_registry(registry_path, profile_root),
        registry_path=registry_path,
    )

    assert coordinator.refresh_live_registry() is True
    disable_customer(registry_path, "client_001")
    assert coordinator.refresh_live_registry() is True
    assert coordinator.customer("client_001") is None

    set_customer_ai_consent(
        registry_path,
        "client_001",
        AiProcessingConsent(granted=False),
    )
    assert coordinator.refresh_live_registry() is True


def test_disabled_customer_is_not_routable(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    registry = _registry(tmp_path, include_disabled_second=True)
    coordinator = module.NutritionCoachingCoordinator(tmp_path, registry)

    disabled = module.IncomingAddress("disabled-client", "disabled-chat", "disabled-topic")

    assert coordinator.resolve(disabled) is None
    assert coordinator.customer("client_002") is None


def test_customer_route_requires_exact_submitter_chat_and_topic(tmp_path: Path) -> None:
    # Given: one enabled customer and a separate owner control space.
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))

    # When/Then: only the customer's exact tuple resolves.
    exact = module.IncomingAddress("client", "customer-chat", "customer-topic")
    assert coordinator.resolve(exact) is not None
    assert coordinator.resolve(module.IncomingAddress("coach", "customer-chat", "customer-topic")) is None
    assert coordinator.resolve(module.IncomingAddress("client", "customer-chat", "wrong")) is None


def test_customer_space_is_reserved_even_for_an_unregistered_sender(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))

    assert coordinator.owns_space("customer-chat", "customer-topic") is True
    assert coordinator.resolve(module.IncomingAddress("intruder", "customer-chat", "customer-topic")) is None
    assert coordinator.owns_space("customer-chat", "another-topic") is False


def test_saved_customer_checkin_creates_owner_only_draft_request(tmp_path: Path) -> None:
    # Given: a customer starts and answers a morning check-in in the exact topic.
    module = _module()
    assert module is not None
    registry = _registry(tmp_path)
    coordinator = module.NutritionCoachingCoordinator(tmp_path, registry)
    address = module.IncomingAddress("client", "customer-chat", "customer-topic")
    opening = coordinator.open_launcher("client_001")
    assert opening.callback_data is not None
    coordinator.bind_launcher("client_001", opening.callback_data, "44")
    transition = coordinator.handle_callback(module.CallbackInput(opening.callback_data, address, "44"))
    assert transition.reply.accepted
    bridge = coordinator.resolve(address).bridge
    answers = (
        "70", "2300", "150 280 65", "계획대로 3식", "2.5", "7", "4",
        "normal", "4", "식욕 3/5, 스트레스 2/5", "하체 70분", "skip",
    )
    actions = (
        "value", "value", "value", "value", "value", "value", "select",
        "select", "select", "value", "value", "select",
    )
    for action, value in zip(actions, answers, strict=True):
        reply = bridge.apply_model_action(action, value)
        assert reply.accepted
    assert reply.prompt is not None
    summary = next(callback for label, callback in reply.prompt.buttons if label == "저장")

    # When: the customer saves the finalized record.
    completed = coordinator.handle_callback(module.CallbackInput(summary, address, "44"))

    # Then: one opaque request exists and only the exact owner can resolve it.
    assert completed.completion is not None
    token = completed.completion.request_token
    owner = module.IncomingAddress("coach", "control", "owner")
    draft = coordinator.resolve_draft(token, owner)
    assert draft is not None
    assert draft.snapshot.flow == "nutrition_daily"
    assert draft.snapshot.answers["macros"] == "150 280 65"
    assert coordinator.resolve_draft(token, address) is None

    correction = coordinator.handle_text(address, "오늘 체크인 수정")
    assert correction.reply.accepted is True
    assert correction.reply.prompt is not None
    assert "체중" in correction.reply.prompt.text


def test_urgent_customer_note_notifies_owner_without_draft_token(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    address = module.IncomingAddress("client", "customer-chat", "customer-topic")
    opening = coordinator.open_launcher("client_001")
    assert opening.callback_data is not None
    coordinator.bind_launcher("client_001", opening.callback_data, "44")
    coordinator.handle_callback(module.CallbackInput(opening.callback_data, address, "44"))
    bridge = coordinator.resolve(address).bridge
    for action, value in (
        ("value", "70"), ("value", "2300"), ("value", "150 280 65"),
        ("value", "계획대로 3식"), ("value", "2.5"), ("value", "7"),
        ("select", "4"), ("select", "normal"), ("select", "4"),
        ("value", "식욕 3/5, 스트레스 2/5"), ("value", "하체 70분"),
    ):
        reply = bridge.apply_model_action(action, value)
        assert reply.accepted
    stopped = bridge.apply_model_action("value", "흉통과 호흡 곤란이 있습니다")
    assert stopped.prompt is not None
    acknowledgement = stopped.prompt.buttons[0][1]

    completed = coordinator.handle_callback(module.CallbackInput(acknowledgement, address, "44"))

    assert completed.completion is not None
    assert completed.completion.safety_held is True
    assert completed.completion.request_token is None


def _drive_gateway_safety_case(tmp_path: Path, case: dict[str, object]):
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(
        tmp_path,
        _registry(tmp_path, owner_chat_id="-100"),
    )
    trainer_flow = case["source_flow"] == "trainer_session"

    if trainer_flow:
        address = module.IncomingAddress("trainer", "trainer-chat", "trainer-topic")
        opening = coordinator.open_trainer_launcher("client_001")
        assert opening.callback_data is not None
        assert coordinator.bind_trainer_launcher("client_001", opening.callback_data, "44")
        transition = coordinator.handle_trainer_callback(
            module.CallbackInput(opening.callback_data, address, "44")
        )
        resolved = coordinator.resolve_trainer(address)
        assert resolved is not None and resolved.trainer_bridge is not None
        bridge = resolved.trainer_bridge
    else:
        address = module.IncomingAddress("client", "customer-chat", "customer-topic")
        resolved = coordinator.resolve(address)
        assert resolved is not None
        bridge = resolved.bridge
        flow = getattr(case["flow"], "value", case["flow"])
        opening = (
            coordinator.open_launcher("client_001")
            if flow == "nutrition_daily"
            else bridge.open_launcher(str(flow))
        )
        assert opening.callback_data is not None
        assert coordinator.bind_launcher("client_001", opening.callback_data, "44")
        transition = coordinator.handle_callback(
            module.CallbackInput(opening.callback_data, address, "44")
        )

    assert transition.reply.accepted
    for action, value in case["prefix"]:
        reply = bridge.apply_model_action(str(action), str(value))
        assert reply.accepted, (case["fixture_id"], reply)

    stopped = bridge.apply_model_action(str(case["action"]), str(case["raw"]))
    assert stopped.accepted
    assert stopped.prompt is not None
    assert stopped.prompt.buttons

    acknowledgement = stopped.prompt.buttons[0][1]
    callback = module.CallbackData.parse(acknowledgement)
    assert callback is not None
    callback_input = module.CallbackInput(acknowledgement, address, "44")
    completed = (
        coordinator.handle_trainer_callback(callback_input)
        if trainer_flow
        else coordinator.handle_callback(callback_input)
    )
    assert completed.reply.accepted
    assert completed.completion is not None

    event = bridge.finalized_event(callback.session_id)
    assert event is not None
    return coordinator, bridge, completed.completion, event, callback.session_id


@pytest.mark.parametrize("case", AC21_SAFETY_CASES)
def test_gateway_ac21_safety_matrix(tmp_path: Path, case: dict[str, object]) -> None:
    (
        coordinator,
        bridge,
        completion,
        event,
        session_id,
    ) = _drive_gateway_safety_case(tmp_path, case)

    safety = getattr(event, "safety", None)
    assert safety is not None
    assert safety.coaching_held is True
    assert len(safety.reasons) == 1
    reason = safety.reasons[0]
    assert reason.class_name == case["class_name"]
    assert reason.rule_id.value == case["rule_id"]
    assert reason.source_flow.value == case["source_flow"]
    assert reason.matched_field.value == case["matched_field"]
    assert reason.excerpt == case["raw"]
    assert len(reason.excerpt) <= 160

    assert completion.safety_held is True
    assert completion.request_token is None
    assert completion.role == (
        "trainer" if case["source_flow"] == "trainer_session" else "customer"
    )
    assert completion.hold_reasons
    bounded_reason = completion.hold_reasons[0]
    assert 0 < len(bounded_reason) <= 240
    assert completion.referral_guidance
    assert len(completion.referral_guidance) <= 500

    async def _render_owner_notice() -> AsyncMock:
        from gateway.platforms.telegram import TelegramAdapter

        adapter = object.__new__(TelegramAdapter)
        adapter._bot = SimpleNamespace()
        adapter._get_nutrition_coaching = lambda: coordinator
        adapter._thread_kwargs_for_send = lambda _chat, topic, _metadata: {
            "message_thread_id": topic,
        }
        adapter._send_message_strict_topic = AsyncMock(
            return_value=SimpleNamespace(message_id=501)
        )
        await adapter._render_nutrition_completion(completion)
        return adapter._send_message_strict_topic

    sends = asyncio.run(_render_owner_notice())
    assert sends.await_count == 1
    call = sends.await_args
    assert call is not None
    assert call.kwargs["chat_id"] == int(coordinator.owner.chat_id)
    assert bounded_reason in call.kwargs["text"]
    assert " ".join(completion.referral_guidance.split()) in call.kwargs["text"]
    assert case["referral_marker"] in call.kwargs["text"]
    assert "진료 안내:" in call.kwargs["text"]
    assert "reply_markup" not in call.kwargs
    assert "초안" not in call.kwargs["text"]
    assert "request_token" not in call.kwargs["text"]

    events = coordinator.event_source("client_001")._read_events()
    assert [item.event_type.value for item in events] == ["safety_audit"]
    assert not any(
        token in bounded_reason
        for token in ("draft", "request_token", "send_token")
    )
    assert bridge.finalized_coaching_snapshot(session_id) is None
    assert all(
        str(sent.kwargs["chat_id"]) != str(coordinator.customer("client_001").spec.telegram.chat_id)
        for sent in sends.await_args_list
    )
def test_safety_completion_is_sent_to_owner_without_draft_button() -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        adapter = object.__new__(TelegramAdapter)
        adapter._bot = SimpleNamespace()
        adapter._get_nutrition_coaching = lambda: SimpleNamespace(
            owner=SimpleNamespace(chat_id="-100", topic_id="73")
        )
        adapter._thread_kwargs_for_send = lambda *_args: {"message_thread_id": 73}
        adapter._send_message_strict_topic = AsyncMock()
        completion = SimpleNamespace(
            safety_held=True, request_token=None, display_name="고객 001", kst_day="2026-07-19"
        )

        await adapter._render_nutrition_completion(completion)

        call = adapter._send_message_strict_topic.await_args
        assert call is not None
        assert "안전 신호로 코칭이 보류" in call.kwargs["text"]
        assert "reply_markup" not in call.kwargs

    asyncio.run(_run())


def test_due_tick_sends_one_customer_launcher_and_claims_the_day(tmp_path: Path) -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        module = _module()
        assert module is not None
        coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))

        def get_coordinator():
            return coordinator

        def thread_kwargs(_chat, topic, _metadata):
            return {"message_thread_id": topic}

        adapter = object.__new__(TelegramAdapter)
        adapter._bot = SimpleNamespace()
        adapter._get_nutrition_coaching = get_coordinator
        adapter._send_message_strict_topic = AsyncMock(return_value=SimpleNamespace(message_id=81))
        adapter._thread_kwargs_for_send = thread_kwargs
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is True and second.success is True
        calls = adapter._send_message_strict_topic.await_args_list
        assert len(calls) == 1
        call = calls[0]
        assert call.kwargs["chat_id"] == "customer-chat"
        assert "2026년 7월 21일" in call.kwargs["text"]
        assert call.kwargs["reply_markup"] is not None

    asyncio.run(_run())


def test_due_tick_sends_weekly_summary_only_to_owner_during_pilot(tmp_path: Path) -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        module = _module()
        assert module is not None
        coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))

        def get_coordinator():
            return coordinator

        def thread_kwargs(_chat, topic, _metadata):
            return {"message_thread_id": topic}

        adapter = object.__new__(TelegramAdapter)
        adapter._bot = SimpleNamespace()
        adapter._get_nutrition_coaching = get_coordinator
        adapter._thread_kwargs_for_send = thread_kwargs
        adapter._send_message_strict_topic = AsyncMock(return_value=SimpleNamespace(message_id=82))
        now = datetime(2026, 7, 6, 8, 5, tzinfo=ZoneInfo("Asia/Seoul"))

        result = await adapter._send_nutrition_coaching_tick(now)

        assert result.success is True
        calls = adapter._send_message_strict_topic.await_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["chat_id"] == "customer-chat"
        assert calls[1].kwargs["chat_id"] == "control"
        assert calls[1].kwargs["text"] == (
            "이번 주 린매스업 리포트\n\n"
            "- 최근 7일 평균 체중: 기록 없음\n"
            "- 이전 7일 평균 체중: 기록 없음\n"
            "- 주간 변화: 기록 없음\n"
            "- 체크인율: 0%\n"
            "- 목표 범위: 기록 없음\n\n"
            "최근 7일 평균과 이전 7일 평균을 확인할 수 없어 추세를 판단하지 않습니다.\n"
            "기록이 보완된 뒤 유지·조정 여부를 다시 확인합니다.\n\n"
            "이번 주 판단: 기록 보완\n\n"
            "- 체크인 누락 줄이기\n\n"
            "기록이 충분하지 않아 이번 주 판단을 확정하지 않습니다."
        )
        assert "reply_markup" not in calls[1].kwargs

    asyncio.run(_run())


def test_coaching_id_command_reports_only_the_callers_current_address() -> None:
    async def _run() -> None:
        from gateway.config import PlatformConfig
        from gateway.platforms.telegram import TelegramAdapter

        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra={}))
        adapter._should_process_message = lambda *_args, **_kwargs: False
        message = SimpleNamespace(
            text="/coachingid", message_thread_id=73,
            chat=SimpleNamespace(id=-100123, type="supergroup"),
            from_user=SimpleNamespace(id=456), reply_text=AsyncMock(),
        )

        await adapter._handle_command(
            SimpleNamespace(message=message, effective_message=message, update_id=1), None,
        )

        message.reply_text.assert_awaited_once_with(
            "고객 등록 주소입니다.\nuser_id=456\nchat_id=-100123\ntopic_id=73"
        )

    asyncio.run(_run())
def _draft_coordinator(tmp_path: Path):
    module = _module()
    assert module is not None
    events: list[object] = []

    class _Bridge:
        def __init__(self) -> None:
            self.snapshot = {
                "flow": "nutrition_daily",
                "kst_day": "2026-07-19",
                "answers": {"calories": "2300"},
            }
            self._events = events

        def finalized_coaching_snapshot(self, session_id: str):
            return dict(self.snapshot)

        def finalized_safety_snapshot(self, session_id: str):
            if self.snapshot.get("safety_signals") or self.snapshot.get("safety_reasons"):
                return {"safety_held": True}
            return None

        def append_event(self, event: object):
            events.append(event)
            return SimpleNamespace(event_id=getattr(event, "event_id", "event"))

    telegram = SimpleNamespace(
        user_id="client",
        chat_id="customer-chat",
        topic_id="customer-topic",
    )
    spec = SimpleNamespace(
        ai_processing_consent=SimpleNamespace(
            granted=True,
            recorded_on="2026-07-01",
            notice_version="privacy-v1",
        ),
        customer_key="client_001",
        enabled=True,
        display_name="고객 001",
        telegram=telegram,
        plan=SimpleNamespace(starts_on=date(2026, 7, 1)),
    )
    customer = SimpleNamespace(spec=spec, data_root=tmp_path / "customer")
    bridge = _Bridge()
    resolved = SimpleNamespace(customer=customer, bridge=bridge, trainer_bridge=None)
    registry = SimpleNamespace(
        owner=SimpleNamespace(
            key=("coach", "control", "owner"),
            space_key=("control", "owner"),
        ),
        customers=(customer,),
    )
    coordinator = object.__new__(module.NutritionCoachingCoordinator)
    coordinator._profile_root = tmp_path
    coordinator._registry = registry
    coordinator._requests_path = tmp_path / "data" / "owner-actions" / "draft-requests.json"
    coordinator._drafts_path = tmp_path / "data" / "owner-actions" / "drafts.json"
    coordinator._routes = {}
    coordinator._trainer_routes = {}
    coordinator._by_key = {"client_001": resolved}
    coordinator._spaces = set()
    coordinator._save_request("draft-001", "client_001", "session-001")
    return coordinator, module.IncomingAddress("coach", "control", "owner"), events
def _durable_draft_coordinator(tmp_path: Path):
    coordinator, owner, events = _draft_coordinator(tmp_path)
    coordinator._deliveries_path = tmp_path / "data" / "owner-actions" / "draft-deliveries.json"
    coordinator._outbox_path = coordinator._deliveries_path
    return coordinator, owner, events


def _approved_durable_draft(tmp_path: Path):
    coordinator, owner, events = _durable_draft_coordinator(tmp_path)
    assert coordinator.create_draft("draft-001", owner, "승인된 초안입니다.").accepted
    assert coordinator.approve_draft("draft-001", owner).accepted
    return coordinator, owner, events
def _live_registry_draft_coordinator(tmp_path: Path):
    module = _module()
    assert module is not None
    registry_path = tmp_path / "registry.json"
    registry = _registry(tmp_path)
    coordinator = module.NutritionCoachingCoordinator(
        tmp_path,
        registry,
        registry_path=registry_path,
    )
    resolved = coordinator._by_key["client_001"]
    events: list[object] = []
    snapshot = {
        "flow": "nutrition_daily",
        "kst_day": "2026-07-19",
        "answers": {"calories": "2300"},
    }
    resolved.bridge.finalized_coaching_snapshot = lambda _session_id: dict(snapshot)
    resolved.bridge.finalized_safety_snapshot = lambda _session_id: None
    resolved.bridge.append_event = lambda event: (
        events.append(event) or SimpleNamespace(event_id=getattr(event, "event_id", "event"))
    )
    owner = module.IncomingAddress("coach", "control", "owner")
    coordinator._save_request("draft-001", "client_001", "session-001")
    coordinator._deliveries_path = tmp_path / "data" / "owner-actions" / "draft-deliveries.json"
    coordinator._outbox_path = coordinator._deliveries_path
    return coordinator, owner, registry_path, events


def _mutate_live_registry(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload["customers"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_owner_draft_lifecycle_is_canonical_and_preapproval_send_is_rejected(tmp_path: Path) -> None:
    coordinator, owner, events = _draft_coordinator(tmp_path)

    created = coordinator.create_draft("draft-001", owner, "초안입니다.")
    assert created.accepted and created.status == "created"
    assert coordinator.prepare_send_draft("draft-001", owner).error == "draft_not_approved"

    edited = coordinator.edit_draft("draft-001", owner, "수정한 초안입니다.")
    approved = coordinator.approve_draft("draft-001", owner)
    sent = coordinator.send_draft("draft-001", owner)

    assert edited.accepted and approved.accepted and sent.accepted
    assert [event.event_type.value for event in events] == [
        "draft_created", "draft_edited", "draft_approved", "draft_sent",
    ]
    assert [event.draft.actor.value for event in events] == ["ai", "richard", "richard", "richard"]


def test_corrupt_draft_ledger_blocks_create_without_overwriting_the_file(tmp_path: Path) -> None:
    coordinator, owner, events = _draft_coordinator(tmp_path)
    path = coordinator._drafts_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    result = coordinator.create_draft("draft-001", owner, "초안입니다.")

    assert result.accepted is False
    assert result.error == "draft_ledger_corrupt"
    assert path.read_text(encoding="utf-8") == "{not-json"
    assert events == []


def test_trainer_route_is_exactly_isolated_from_customer_and_reserved_spaces(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    trainer = module.IncomingAddress("trainer", "trainer-chat", "trainer-topic")

    assert coordinator.resolve_trainer(trainer) is not None
    assert coordinator.resolve(trainer) is None
    assert coordinator.resolve_trainer(
        module.IncomingAddress("intruder", "trainer-chat", "trainer-topic")
    ) is None
    assert coordinator.owns_space("trainer-chat", "trainer-topic") is True


def test_private_trainer_menu_uses_live_assignment_and_opaque_customer_callback(
    tmp_path: Path,
) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    trainer_dm = module.IncomingAddress("trainer", "trainer", "0")

    menu = coordinator.trainer_private_menu("trainer")

    assert tuple(label for label, _ in menu.buttons) == ("고객 001",)
    callback = menu.buttons[0][1]
    assert callback.startswith("pt1:")
    assert "client_001" not in callback
    assert len(callback.encode("utf-8")) <= 64
    assert coordinator.trainer_private_menu("intruder").buttons == ()
    resolved = coordinator.resolve_trainer_private_selection(trainer_dm, callback)
    assert resolved is not None
    assert resolved.customer.spec.customer_key == "client_001"

    opened = coordinator.open_trainer_private_launcher(trainer_dm, callback)
    assert opened is not None
    selected, opening = opened
    assert selected.customer.spec.customer_key == "client_001"
    assert opening.callback_data is not None
    callback_input = module.CallbackInput(opening.callback_data, trainer_dm, "44")
    assert selected.trainer_dm_bridge is not None
    parsed = module.CallbackData.parse(opening.callback_data)
    assert parsed is not None
    assert selected.trainer_dm_bridge.bind_launcher_message(parsed.session_id, "44")
    started = coordinator.handle_trainer_private_callback(callback_input)
    assert started.reply.accepted is True
    assert coordinator.trainer_private_active(trainer_dm) is selected


def test_private_trainer_selection_rejects_group_and_wrong_trainer(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    callback = coordinator.trainer_private_menu("trainer").buttons[0][1]

    assert coordinator.resolve_trainer_private_selection(
        module.IncomingAddress("trainer", "trainer-chat", "trainer-topic"),
        callback,
    ) is None
    assert coordinator.resolve_trainer_private_selection(
        module.IncomingAddress("intruder", "intruder", "0"),
        callback,
    ) is None


@pytest.mark.parametrize(
    "command",
    (
        "오늘 PT 기록",
        "기록 시작",
        "트레이너 기록 시작",
        "트레이너기록시작",
        "운동 기록 시작",
        "PT 기록 시작",
        "트레이너 세션 시작",
        "고객 001 기록 시작",
        "client_001 기록 시작",
    ),
)
def test_trainer_launcher_accepts_human_friendly_commands_and_shows_customer_date(
    tmp_path: Path,
    command: str,
) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    trainer = module.IncomingAddress("trainer", "trainer-chat", "trainer-topic")

    transition = coordinator.handle_trainer_text(trainer, command)

    assert transition.reply.accepted is True
    assert transition.reply.callback_data is not None
    assert "고객 001" in transition.reply.notice
    today = module._current_kst_date()
    assert f"{today.year}년 {today.month}월 {today.day}일" in transition.reply.notice

def test_registry_trainer_bridge_opens_scoped_trainer_session(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))

    resolved = coordinator.trainer_for("client_001")
    assert resolved is not None
    assert resolved.trainer_bridge is not None
    assert resolved.trainer_bridge.customer_key == "client_001"

    opening = coordinator.open_trainer_launcher("client_001")
    assert opening.accepted is True
    assert opening.callback_data is not None

def test_delivery_intent_is_pending_before_transport_and_retry_never_resends(tmp_path: Path) -> None:
    coordinator, owner, _ = _approved_durable_draft(tmp_path)
    sends = 0

    first = coordinator.prepare_delivery("draft-001", owner)
    assert first.accepted and first.status == "pending"
    assert first.transport_required is True
    ledger = json.loads(coordinator._deliveries_path.read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "pending"

    if first.transport_required:
        sends += 1
    retry = coordinator.prepare_delivery("draft-001", owner)
    assert retry.accepted and retry.status == "pending"
    assert retry.transport_required is False
    if retry.transport_required:
        sends += 1
    assert sends == 1


def test_delivered_receipt_retries_audit_without_duplicate_transport(tmp_path: Path) -> None:
    coordinator, owner, events = _approved_durable_draft(tmp_path)
    prepared = coordinator.prepare_delivery("draft-001", owner)
    assert prepared.transport_required is True
    sends = 1

    delivered = coordinator.mark_delivered("draft-001", owner, "telegram-101")
    assert delivered.accepted and delivered.status == "delivered"

    original_append = coordinator._append_draft_event
    coordinator._append_draft_event = lambda *_args, **_kwargs: False
    failed = coordinator.mark_sent_audited("draft-001", owner)
    assert failed.accepted is False
    key = "draft-001:" + coordinator._draft_revision("승인된 초안입니다.")
    assert json.loads(coordinator._deliveries_path.read_text(encoding="utf-8"))[key]["status"] == "delivered"

    coordinator._append_draft_event = original_append
    retried = coordinator.prepare_delivery("draft-001", owner)
    assert retried.status == "delivered"
    audited = coordinator.mark_sent_audited("draft-001", owner)
    assert audited.accepted and audited.status == "sent_audited"
    assert len([event for event in events if event.event_type.value == "draft_sent"]) == 1
    assert sends == 1
    again = coordinator.prepare_delivery("draft-001", owner)
    assert again.accepted and again.status == "sent_audited"


def test_pending_delivery_requires_explicit_receipt_reconciliation(tmp_path: Path) -> None:
    coordinator, owner, events = _approved_durable_draft(tmp_path)
    prepared = coordinator.prepare_delivery("draft-001", owner)
    assert prepared.status == "pending"
    pending = coordinator.reconcile_delivery("draft-001", owner)
    assert pending.accepted is False
    assert pending.error == "delivery_reconciliation_required"
    reconciled = coordinator.reconcile_delivery("draft-001", owner, "telegram-202")
    assert reconciled.accepted and reconciled.status == "sent_audited"
    assert len([event for event in events if event.event_type.value == "draft_sent"]) == 1


def test_corrupt_delivery_ledger_fails_closed_without_transport(tmp_path: Path) -> None:
    coordinator, owner, _ = _approved_durable_draft(tmp_path)
    path = coordinator._deliveries_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken": {"status": "delivered"}}', encoding="utf-8")

    result = coordinator.prepare_delivery("draft-001", owner)

    assert result.accepted is False
    assert result.error == "draft_ledger_corrupt"
    assert path.read_text(encoding="utf-8") == '{"broken": {"status": "delivered"}}'


def test_approved_draft_cannot_be_edited_without_reapproval(tmp_path: Path) -> None:
    coordinator, owner, events = _approved_durable_draft(tmp_path)

    result = coordinator.edit_draft("draft-001", owner, "몰래 바꾼 초안입니다.")

    assert result.accepted is False
    assert result.error == "draft_not_editable"
    assert [event.event_type.value for event in events] == ["draft_created", "draft_approved"]
def test_sent_audit_write_failure_leaves_receipt_for_retry_without_resend(tmp_path: Path) -> None:
    coordinator, owner, events = _approved_durable_draft(tmp_path)
    assert coordinator.prepare_delivery("draft-001", owner).transport_required is True
    assert coordinator.mark_delivered("draft-001", owner, "telegram-303").accepted

    real_write = coordinator._write_json_private

    def fail_draft_index(path: Path, payload: object) -> None:
        if path == coordinator._drafts_path:
            raise OSError("simulated crash after transport")
        real_write(path, payload)

    coordinator._write_json_private = fail_draft_index
    failed = coordinator.mark_sent_audited("draft-001", owner)
    assert failed.accepted is False
    assert failed.status == "delivered"
    coordinator._write_json_private = real_write

    retry = coordinator.prepare_delivery("draft-001", owner)
    assert retry.status == "sent_audited"
    audited = coordinator.mark_sent_audited("draft-001", owner)
    assert audited.accepted
    assert len([event for event in events if event.event_type.value == "draft_sent"]) == 1
    sent_event = next(event for event in events if event.event_type.value == "draft_sent")
    assert getattr(sent_event.draft, "approved_message_id", None) == "telegram-303"


def test_corrupt_request_ledger_blocks_delivery_preparation(tmp_path: Path) -> None:
    coordinator, owner, _ = _approved_durable_draft(tmp_path)
    coordinator._requests_path.write_text("[]", encoding="utf-8")

    result = coordinator.prepare_delivery("draft-001", owner)

    assert result.accepted is False
    assert result.error == "draft_ledger_corrupt"


def test_production_console_factory_wires_canonical_lifecycle_and_receipt_transport(
    tmp_path: Path,
) -> None:
    module = _module()
    assert module is not None
    registry = _registry(tmp_path)
    coordinator = module.NutritionCoachingCoordinator(tmp_path, registry)
    address = module.IncomingAddress("client", "customer-chat", "customer-topic")
    opening = coordinator.open_launcher("client_001")
    assert opening.callback_data is not None
    coordinator.bind_launcher("client_001", opening.callback_data, "44")
    coordinator.handle_callback(module.CallbackInput(opening.callback_data, address, "44"))
    bridge = coordinator.resolve(address).bridge
    answers = (
        "70", "2300", "150 280 65", "계획대로 3식", "2.5", "7", "4",
        "normal", "4", "식욕 3/5, 스트레스 2/5", "하체 70분", "skip",
    )
    actions = (
        "value", "value", "value", "value", "value", "value", "select",
        "select", "select", "value", "value", "select",
    )
    for action, value in zip(actions, answers, strict=True):
        reply = bridge.apply_model_action(action, value)
        assert reply.accepted
    assert reply.prompt is not None
    save_callback = next(callback for label, callback in reply.prompt.buttons if label == "저장")
    completed = coordinator.handle_callback(module.CallbackInput(save_callback, address, "44"))
    assert completed.completion is not None
    draft_id = completed.completion.request_token
    assert draft_id is not None

    owner = coordinator.owner
    assert coordinator.create_draft(draft_id, owner, "초안입니다.").accepted

    class _TelegramAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def _thread_kwargs_for_send(
            self,
            _chat_id: str,
            topic_id: str,
            _metadata: dict[str, object],
        ) -> dict[str, object]:
            return {"message_thread_id": topic_id}

        def _send_message_strict_topic(
            self,
            *,
            chat_id: str,
            text: str,
            message_thread_id: str,
        ) -> object:
            self.calls.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "message_thread_id": message_thread_id,
                }
            )
            return SimpleNamespace(message_id="telegram-501")

    adapter = _TelegramAdapter()
    transport = module.TelegramCustomerTransport(adapter, coordinator)
    token_path = tmp_path / "runtime" / "operator.token"
    token_path.parent.mkdir()
    token_path.write_text("rotated-test-token\n", encoding="utf-8")
    token_path.chmod(0o600)
    server = module.create_nutrition_operator_console_server(
        coordinator,
        token_path=token_path,
        customer_transport=transport,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(
        method: str,
        target: str,
        *,
        token: str | None = None,
        body: object | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=3)
        headers = {} if token is None else {"X-Operator-Token": token}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, target, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload.decode("utf-8")) if payload else {}

    try:
        assert request("GET", "/")[0] == 401
        assert request("GET", "/", token="wrong")[0] == 401
        status, evidence = request(
            "GET",
            f"/evidence/client_001/{draft_id}",
            token="rotated-test-token",
        )
        assert status == 200
        assert evidence["evidence"]["state"] == "created"

        status, _ = request(
            "POST",
            f"/draft/client_001/{draft_id}/edit",
            token="rotated-test-token",
            body={"text": "수정한 초안입니다."},
        )
        assert status == 200
        assert request(
            "POST",
            f"/draft/client_001/{draft_id}/approve",
            token="rotated-test-token",
            body={},
        )[0] == 200
        assert request(
            "POST",
            f"/draft/client_001/{draft_id}/send",
            token="rotated-test-token",
            body={},
        )[0] == 200
        assert len(adapter.calls) == 1
        assert adapter.calls[0]["chat_id"] == "customer-chat"
        assert adapter.calls[0]["text"] == "수정한 초안입니다."
        status, evidence = request(
            "GET",
            f"/evidence/client_001/{draft_id}",
            token="rotated-test-token",
        )
        assert status == 200
        assert evidence["evidence"]["state"] == "sent"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_consent_revocation_is_rechecked_at_draft_lifecycle_boundaries(tmp_path: Path) -> None:
    create_coordinator, owner, _ = _draft_coordinator(tmp_path / "create")
    create_coordinator._by_key["client_001"].customer.spec.ai_processing_consent.granted = False
    assert create_coordinator.create_draft("draft-001", owner, "초안").accepted is False

    approve_coordinator, approve_owner, _ = _draft_coordinator(tmp_path / "approve")
    assert approve_coordinator.create_draft("draft-001", approve_owner, "초안").accepted
    approve_coordinator._by_key["client_001"].customer.spec.ai_processing_consent.granted = False
    assert approve_coordinator.approve_draft("draft-001", approve_owner).accepted is False

    reserve_coordinator, reserve_owner, _ = _approved_durable_draft(tmp_path / "reserve")
    reserve_coordinator._by_key["client_001"].customer.spec.ai_processing_consent.granted = False
    assert reserve_coordinator.prepare_delivery("draft-001", reserve_owner).accepted is False

    reconcile_coordinator, reconcile_owner, _ = _approved_durable_draft(tmp_path / "reconcile")
    assert reconcile_coordinator.prepare_delivery("draft-001", reconcile_owner).transport_required
    reconcile_coordinator._by_key["client_001"].customer.spec.ai_processing_consent.granted = False
    assert reconcile_coordinator.mark_delivered("draft-001", reconcile_owner, "telegram-revoked").accepted is False
    audited_coordinator, audited_owner, _ = _approved_durable_draft(tmp_path / "audit")
    assert audited_coordinator.prepare_delivery("draft-001", audited_owner).transport_required
    assert audited_coordinator.mark_delivered("draft-001", audited_owner, "telegram-audit").accepted
    audited_coordinator._by_key["client_001"].customer.spec.ai_processing_consent.granted = False
    assert audited_coordinator.mark_sent_audited("draft-001", audited_owner).accepted is False


def test_safety_hold_on_existing_draft_blocks_delivery_reservation(tmp_path: Path) -> None:
    coordinator, owner, _ = _approved_durable_draft(tmp_path)
    bridge = coordinator._by_key["client_001"].bridge
    bridge.snapshot["safety_signals"] = ("pain",)

    result = coordinator.prepare_delivery("draft-001", owner)

    assert result.accepted is False
    assert result.error == "customer_safety_hold"


def test_approved_revision_and_canonical_evidence_are_required(tmp_path: Path) -> None:
    missing_coordinator, missing_owner, _ = _approved_durable_draft(tmp_path / "missing")
    drafts_path = missing_coordinator._drafts_path
    drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
    drafts["draft-001"].pop("approved_revision")
    drafts_path.write_text(json.dumps(drafts), encoding="utf-8")
    missing = missing_coordinator.prepare_delivery("draft-001", missing_owner)
    assert missing.accepted is False
    assert missing.error == "draft_approval_revision_missing"

    tampered_coordinator, tampered_owner, _ = _approved_durable_draft(tmp_path / "tampered")
    drafts_path = tampered_coordinator._drafts_path
    drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
    drafts["draft-001"]["approved_revision"] = "0" * 64
    drafts_path.write_text(json.dumps(drafts), encoding="utf-8")
    tampered = tampered_coordinator.prepare_delivery("draft-001", tampered_owner)
    assert tampered.accepted is False
    assert tampered.error == "draft_approval_revision_mismatch"

    evidence_coordinator, evidence_owner, events = _approved_durable_draft(tmp_path / "evidence")
    events.clear()
    missing_evidence = evidence_coordinator.prepare_delivery("draft-001", evidence_owner)
    assert missing_evidence.accepted is False
    assert missing_evidence.error == "draft_approval_evidence_missing"


def test_concurrent_delivery_reservation_allows_only_one_transport(tmp_path: Path) -> None:
    coordinator, owner, _ = _approved_durable_draft(tmp_path)

    transports: list[str] = []

    def reserve_and_transport(_: int):
        action = coordinator.prepare_delivery("draft-001", owner)
        if action.transport_required:
            transports.append("telegram")
        return action

    with ThreadPoolExecutor(max_workers=8) as executor:
        actions = list(executor.map(reserve_and_transport, range(8)))

    assert sum(action.transport_required for action in actions) == 1
    assert len(transports) == 1
    assert all(action.accepted for action in actions)
    ledger = json.loads(coordinator._deliveries_path.read_text(encoding="utf-8"))
    assert list(ledger.values())[0]["status"] == "pending"
@pytest.mark.parametrize(
    ("text", "accepted"),
    (
        ("a" * 4096, True),
        ("a" * 4097, False),
        ("😀" * 2048, True),
        ("😀" * 2048 + "a", False),
    ),
)
def test_telegram_utf16_limit_is_checked_before_outbox_reservation(
    tmp_path: Path,
    text: str,
    accepted: bool,
) -> None:
    coordinator, owner, _ = _durable_draft_coordinator(tmp_path)

    created = coordinator.create_draft("draft-001", owner, text)

    assert created.accepted is accepted
    if not accepted:
        assert not coordinator._drafts_path.exists()
        assert not coordinator._deliveries_path.exists()
        return

    assert coordinator.approve_draft("draft-001", owner).accepted
    prepared = coordinator.prepare_delivery("draft-001", owner)
    assert prepared.accepted is True
    assert prepared.transport_required is True


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(
            lambda coordinator: setattr(
                coordinator._by_key["client_001"].customer.spec.ai_processing_consent,
                "granted",
                False,
            ),
            id="consent-revoked",
        ),
        pytest.param(
            lambda coordinator: coordinator._by_key["client_001"].bridge.snapshot.__setitem__(
                "safety_signals",
                ("pain",),
            ),
            id="safety-held",
        ),
    ),
)
def test_console_transport_revalidates_after_reservation(
    tmp_path: Path,
    mutate,
) -> None:
    from checkin_cli.operator_console import CoordinatorLifecycleAdapter

    coordinator, owner, _ = _approved_durable_draft(tmp_path)
    calls: list[tuple[str, str, str]] = []

    class _Transport:
        def send_customer(self, customer_key: str, destination: object, text: str) -> str:
            calls.append((customer_key, str(destination), text))
            return "telegram-never-used"

    source = SimpleNamespace(_read_events=lambda: ())
    adapter = CoordinatorLifecycleAdapter(coordinator, source, _Transport())
    prepare = coordinator.prepare_delivery

    def reserve_then_mutate(draft_id: str, reservation_owner: object):
        action = prepare(draft_id, reservation_owner)
        mutate(coordinator)
        return action

    coordinator.prepare_delivery = reserve_then_mutate
    with pytest.raises(RuntimeError, match="validation"):
        adapter.send("client_001", "draft-001")

    assert calls == []
    ledger = json.loads(coordinator._deliveries_path.read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "pending"
def test_customer_start_callback_is_stable_opaque_and_telegram_sized() -> None:
    module = _module()
    assert module is not None

    callback = module.customer_start_callback("client_001")

    assert callback == module.customer_start_callback("client_001")
    assert callback.startswith("cs1:")
    assert len(callback.encode("utf-8")) <= 64
    assert "client_001" not in callback
    token = module.parse_customer_start_callback(callback)
    assert token is not None and len(token) == 24
    assert all(character in "0123456789abcdef" for character in token)
    assert module.customer_start_callback("client_002") != callback
    assert module.parse_customer_start_callback("cs1:client_001") is None


def test_customer_start_callback_resolves_only_exact_live_customer_route(
    tmp_path: Path,
) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(
        tmp_path,
        _registry(tmp_path, include_disabled_second=True),
    )
    callback = module.customer_start_callback("client_001")

    assert coordinator.resolve_customer_start(
        module.IncomingAddress("client", "customer-chat", "customer-topic"),
        callback,
    ) is not None
    assert coordinator.resolve_customer_start(
        module.IncomingAddress("other-user", "customer-chat", "customer-topic"),
        callback,
    ) is None
    assert coordinator.resolve_customer_start(
        module.IncomingAddress("client", "other-chat", "customer-topic"),
        callback,
    ) is None
    assert coordinator.resolve_customer_start(
        module.IncomingAddress("client", "customer-chat", "other-topic"),
        callback,
    ) is None

    disabled_callback = module.customer_start_callback("client_002")
    assert coordinator.resolve_customer_start(
        module.IncomingAddress("disabled-client", "disabled-chat", "disabled-topic"),
        disabled_callback,
    ) is None


def test_manual_customer_aliases_render_one_reusable_card_with_stable_callback(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        module = _module()
        assert module is not None
        coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
        adapter = object.__new__(TelegramAdapter)
        adapter._get_nutrition_coaching = lambda: coordinator
        adapter._nutrition_coaching_declared_enabled = lambda: True
        adapter._send_nutrition_topic = AsyncMock()
        adapter._physique_markup = lambda prompt: prompt
        adapter._enqueue_text_event = MagicMock()
        expected_callback = module.customer_start_callback("client_001")

        for alias in ("체크인 시작", "오늘 체크인"):
            message = SimpleNamespace(
                text=alias,
                chat=SimpleNamespace(id="customer-chat", type="supergroup"),
                from_user=SimpleNamespace(id="client"),
                message_thread_id="customer-topic",
                is_topic_message=True,
            )
            await adapter._handle_text_message(
                SimpleNamespace(message=message, effective_message=message, update_id=1),
                None,
            )

        calls = adapter._send_nutrition_topic.await_args_list
        assert len(calls) == 3
        assert "언제든 체크인을 시작" in calls[0].kwargs["text"]
        for call in calls[1:]:
            prompt = call.kwargs["reply_markup"]
            assert call.kwargs["chat_id"] == "customer-chat"
            assert call.kwargs["topic_id"] == "customer-topic"
            assert "오늘 체크인을 시작하거나 이어갈 수 있습니다." in call.kwargs["text"]
            assert prompt.buttons == (("오늘 체크인 시작", expected_callback),)
        adapter._enqueue_text_event.assert_not_called()

    asyncio.run(_run())


def test_scheduled_customer_card_reuses_stable_callback_without_opening_session(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        module = _module()
        assert module is not None
        coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
        adapter = object.__new__(TelegramAdapter)
        adapter._bot = SimpleNamespace()
        adapter._get_nutrition_coaching = lambda: coordinator
        adapter._send_message_strict_topic = AsyncMock(
            return_value=SimpleNamespace(message_id=81),
        )
        adapter._thread_kwargs_for_send = lambda _chat, topic, _metadata: {
            "message_thread_id": topic,
        }
        adapter._physique_markup = lambda prompt: prompt
        now = datetime(2026, 7, 21, 8, 17, tzinfo=ZoneInfo("Asia/Seoul"))

        first = await adapter._send_nutrition_coaching_tick(now)
        second = await adapter._send_nutrition_coaching_tick(now)

        assert first.success is True and second.success is True
        calls = adapter._send_message_strict_topic.await_args_list
        assert len(calls) == 1
        call = calls[0]
        prompt = call.kwargs["reply_markup"]
        assert prompt.buttons == (
            ("오늘 체크인 시작", module.customer_start_callback("client_001")),
        )
        assert "오늘 체크인을 시작하거나 이어갈 수 있습니다." in call.kwargs["text"]
        wizard_root = coordinator.customer("client_001").data_root / "wizard"
        assert not list((wizard_root / "drafts").glob("*.json"))

    asyncio.run(_run())


def test_customer_start_callback_click_binds_clicked_card_and_resumes_open_draft(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        from gateway.platforms.telegram import TelegramAdapter

        module = _module()
        assert module is not None
        coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
        adapter = object.__new__(TelegramAdapter)
        adapter._get_nutrition_coaching = lambda: coordinator
        adapter._physique_markup = lambda prompt: prompt
        adapter._render_nutrition_completion = AsyncMock()
        address = module.IncomingAddress("client", "customer-chat", "customer-topic")
        callback = module.customer_start_callback("client_001")
        message = SimpleNamespace(
            message_id="44",
            chat_id="customer-chat",
            chat=SimpleNamespace(id="customer-chat", type="supergroup"),
            message_thread_id="customer-topic",
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id="client"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_nutrition_customer_start_callback(
            query,
            callback,
            message,
        )
        await adapter._handle_nutrition_customer_start_callback(
            query,
            callback,
            message,
        )

        assert query.answer.await_count == 2
        assert query.edit_message_text.await_count == 2
        bridge = coordinator.resolve(address).bridge
        storage = bridge._service._storage
        assert len(tuple(storage._drafts.glob("*.json"))) == 1
        assert bridge.active_prompt() is not None
        assert all(
            event.event_type.value != "nutrition_checkin"
            for event in bridge._service._events._read_events()
        )

    asyncio.run(_run())


def test_completed_customer_day_is_terminal_without_new_session_or_event(
    tmp_path: Path,
) -> None:
    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(tmp_path, _registry(tmp_path))
    address = module.IncomingAddress("client", "customer-chat", "customer-topic")
    opening = coordinator.open_launcher("client_001")
    assert opening.callback_data is not None
    assert coordinator.bind_launcher("client_001", opening.callback_data, "44")
    assert coordinator.handle_callback(
        module.CallbackInput(opening.callback_data, address, "44"),
    ).reply.accepted
    bridge = coordinator.resolve(address).bridge

    answers = (
        "70", "2300", "150 280 65", "계획대로 3식", "2.5", "7", "4",
        "normal", "4", "식욕 3/5, 스트레스 2/5", "하체 70분", "skip",
    )
    actions = (
        "value", "value", "value", "value", "value", "value", "select",
        "select", "select", "value", "value", "select",
    )
    for action, value in zip(actions, answers, strict=True):
        assert bridge.apply_model_action(action, value).accepted
    summary = next(
        callback for label, callback in bridge.active_prompt().buttons
        if label == "저장"
    )
    completed = coordinator.handle_callback(
        module.CallbackInput(summary, address, "44"),
    )
    assert completed.completion is not None

    draft_count = len(tuple(bridge._service._storage._drafts.glob("*.json")))
    event_count = len(bridge._service._events._read_events())
    again = coordinator.open_launcher("client_001")

    assert again.accepted is True
    assert again.callback_data is None
    assert "이미 완료되었습니다" in again.notice
    assert again.prompt is not None
    assert len(tuple(bridge._service._storage._drafts.glob("*.json"))) == draft_count
    assert len(bridge._service._events._read_events()) == event_count
def test_weekly_destination_pin_precedes_revision_authority_gate():
    adapter = object.__new__(TelegramAdapter)
    adapter._coaching_processing_allowed = lambda _surface, _value: True
    first_summary = {
        "average_weight_kg": "80.0",
        "prior_average_weight_kg": "79.0",
        "weekly_change_percent": "1.2",
        "ends_on": "2026-07-21",
    }
    second_summary = {
        **first_summary,
        "weekly_change_percent": "1.3",
    }
    frozen = WeeklyGroundingInput.from_summary(
        first_summary,
        customer_key="client_001",
    )
    current = [frozen]
    destination_pin = adapter._nutrition_schedule_destination(
        SimpleNamespace(user_id="owner", chat_id="owner-chat", topic_id="owner-topic")
    )
    assert destination_pin == {
        "user_id": "owner",
        "chat_id": "owner-chat",
        "topic_id": "owner-topic",
    }
    assert adapter._coaching_authority_valid(
        "weekly",
        frozen,
        lambda: current[0],
    ) is True
    current[0] = WeeklyGroundingInput.from_summary(
        second_summary,
        customer_key="client_001",
    )
    assert adapter._coaching_authority_valid(
        "weekly",
        frozen,
        lambda: current[0],
    ) is False
def test_adaptive_config_requires_explicit_schedule_confirmation_enablement() -> None:
    from gateway.platforms.nutrition_coaching_config import AdaptiveNutritionConfig

    base = {
        "adaptive_nutrition": {
            "enabled": True,
            "delivery_enabled": False,
            "review_operator": {
                "user_id": "owner",
                "chat_id": "review-chat",
                "topic_id": 59,
                "version": 1,
            },
        }
    }
    disabled = AdaptiveNutritionConfig.from_extra(base)
    assert disabled is not None
    assert disabled.schedule_confirm_enabled is False

    base["adaptive_nutrition"]["schedule_confirm_enabled"] = True
    enabled = AdaptiveNutritionConfig.from_extra(base)
    assert enabled is not None
    assert enabled.schedule_confirm_enabled is True
def _schedule_confirm_integration_fixture(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "_physique_coach_customer_admin_fixtures",
        PROFILE_PACKAGE / "tests" / "test_customer_admin.py",
    )
    assert spec is not None and spec.loader is not None
    fixture_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture_module
    spec.loader.exec_module(fixture_module)

    profile_root, registry_path, data_root, checklist_path = fixture_module._activation_fixture(
        tmp_path
    )
    fixture_module.activate_customer(
        profile_root,
        data_root,
        "client_001",
        checklist_path,
        kst_date=date(2026, 8, 1),
    )
    fixture_module._write_owner_approved_registration_artifacts(data_root)
    fixture_module.approve_dual_coach_risk_policy(
        profile_root,
        "client_001",
        version="1",
        owner_actor=fixture_module.TelegramAddress(
            user_id="1", chat_id="-100", topic_id="10"
        ),
        approved_at_kst="2026-08-01T09:00:00+09:00",
    )
    fixture_module.approve_adaptive_registration_inputs(
        profile_root,
        "client_001",
        inputs={
            "version": "v1",
            "meal_count": 3,
            "budget_band": "standard",
            "cooking_access": "home",
            "preferences": ["한식"],
            "exclusions": ["고수"],
            "allergies": ["땅콩"],
            "training_schedule": [{
                "date": "2026-08-03",
                "weekday": 0,
                "time": "18:00",
                "load_category": "high",
            }],
        },
        approved_by={"user_id": "1", "chat_id": "-100", "topic_id": "10"},
        approved_at_kst="2026-08-01T10:00:00+09:00",
    )

    module = _module()
    assert module is not None
    coordinator = module.NutritionCoachingCoordinator(
        profile_root,
        load_customer_registry(registry_path, profile_root),
        registry_path=registry_path,
        review_operator={
            "user_id": "reviewer",
            "chat_id": "review-chat",
            "topic_id": "59",
            "version": 1,
        },
    )
    customer = coordinator.customer("client_001")
    assert customer is not None

    from checkin_cli.models import build_schedule_reference_event
    from checkin_cli.store import CanonicalEventTransaction

    transaction = CanonicalEventTransaction.for_customer_runtime(customer)
    reference = build_schedule_reference_event(
        "client_001",
        date(2026, 8, 3),
        "09:30:00",
        customer_confirmed=True,
        trainer_confirmed=True,
        last_change_note="gateway integration source fact",
        occurred_at_kst="2026-08-01T09:00:00+09:00",
        recorded_at_kst="2026-08-01T09:00:00+09:00",
    )
    transaction.append_schedule_reference(reference, customer_key="client_001")
    return module, coordinator, customer, transaction


def _schedule_confirm_request(module, *, reference, review_operator=("reviewer", "review-chat", "59")):
    capability_id = "a" * 24
    capability = module.AdaptiveOperatorCapability(
        schema_version="1.0",
        capability_id=capability_id,
        review_operator=review_operator,
        review_operator_version=1,
        canonical_owner=("1", "-100", "10"),
        canonical_owner_version=1,
        customer_key="client_001",
        action="schedule_confirm",
        proposal_digest="b" * 64,
        revision=1,
        config_digest="c" * 64,
        registry_digest="d" * 64,
        consent_digest="e" * 64,
        activation_digest="f" * 64,
        issued_kst="2026-08-01T09:00:00+09:00",
        expires_kst="2026-08-01T10:00:00+09:00",
        nonce_digest=hashlib.sha256(capability_id.encode("ascii")).hexdigest(),
        originating_message_id="review-card-1",
        originating_chat_id=review_operator[1],
        originating_topic_id=review_operator[2],
        schedule_event_id=reference.event_id,
        schedule_event_digest=reference.event_digest,
    )
    return module.ScheduleConfirmationRequest(
        capability,
        reference.event_id,
        reference.event_digest,
    )


def test_schedule_confirm_integration_gateway_request_replays_one_canonical_and_projection(
    tmp_path: Path,
) -> None:
    module, coordinator, customer, transaction = _schedule_confirm_integration_fixture(tmp_path)
    facade = coordinator.schedule_confirm_handler
    reference = facade.current_reference("client_001")
    request = _schedule_confirm_request(module, reference=reference)

    first = facade.confirm(request)
    replay = facade.confirm(request)

    events = [
        json.loads(line)
        for line in transaction.events_path.read_text(encoding="utf-8").splitlines()
    ]
    projections = [
        json.loads(line)
        for line in (customer.nutrition_plans_root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    confirmations = [row for row in events if row["event_type"] == "schedule_confirmation"]
    schedule_projections = [
        row for row in projections if row["event_type"] == "schedule_strategy_confirmed"
    ]

    assert first["canonical_event"]["event_id"] == replay["canonical_event"]["event_id"]
    assert len(confirmations) == len(schedule_projections) == 1
    assert confirmations[0]["schedule_confirmation"]["reference_event_id"] == reference.event_id
    assert schedule_projections[0]["payload"]["source_reference_id"] == reference.event_id


def test_schedule_confirm_integration_rejects_stale_reference_and_wrong_review_authority(
    tmp_path: Path,
) -> None:
    module, coordinator, customer, transaction = _schedule_confirm_integration_fixture(tmp_path)
    facade = coordinator.schedule_confirm_handler
    stale_reference = facade.current_reference("client_001")
    stale_request = _schedule_confirm_request(module, reference=stale_reference)

    from checkin_cli.models import build_schedule_reference_event
    from checkin_cli.store import CanonicalEventTransaction

    current = transaction.current_schedule_reference("client_001")
    assert current is not None
    correction = build_schedule_reference_event(
        "client_001",
        date(2026, 8, 3),
        "10:00:00",
        customer_confirmed=True,
        trainer_confirmed=True,
        last_change_note="superseding source fact",
        supersedes=current.event_id,
        predecessor_digest=CanonicalEventTransaction.schedule_reference_digest(current),
        occurred_at_kst="2026-08-01T09:05:00+09:00",
        recorded_at_kst="2026-08-01T09:05:00+09:00",
    )
    transaction.append_schedule_reference(correction, customer_key="client_001")
    before = transaction.events_path.read_bytes()
    adaptive_path = customer.nutrition_plans_root / "events.jsonl"
    adaptive_before = adaptive_path.read_bytes()

    with pytest.raises(module.AdaptiveWorkflowError, match="reference is stale"):
        facade.confirm(stale_request)
    assert transaction.events_path.read_bytes() == before

    current_reference = facade.current_reference("client_001")
    wrong_topic = _schedule_confirm_request(
        module,
        reference=current_reference,
        review_operator=("reviewer", "review-chat", "other-topic"),
    )
    with pytest.raises(module.AdaptiveWorkflowError, match="authority is stale"):
        facade.confirm(wrong_topic)
    assert transaction.events_path.read_bytes() == before
    with pytest.raises(module.AdaptiveWorkflowError, match="reference is stale"):
        module.ScheduleConfirmationRequest(
            stale_request.capability,
            stale_request.schedule_event_id,
            "0" * 64,
        )

    adapter = module.AdaptiveOperatorService(
        coordinator,
        review_operator={"user_id": "reviewer", "chat_id": "review-chat", "topic_id": 59, "version": 1},
        schedule_confirm_handler=facade,
        schedule_confirm_enabled=True,
    )
    assert adapter.accepts(module.IncomingAddress("reviewer", "review-chat", "59"))
    assert not adapter.accepts(module.IncomingAddress("wrong-user", "review-chat", "59"))
    assert not adapter.accepts(module.IncomingAddress("reviewer", "review-chat", "other-topic"))
    assert adaptive_path.read_bytes() == adaptive_before
def test_schedule_confirm_callback_claims_once_and_rejects_invalid_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, coordinator, customer, transaction = _schedule_confirm_integration_fixture(tmp_path)
    facade = coordinator.schedule_confirm_handler
    proposal = SimpleNamespace(digest="b" * 64, revision=1)
    monkeypatch.setattr(
        coordinator,
        "adaptive_nutrition_coordinator",
        lambda _key: SimpleNamespace(_latest_production_proposal=lambda: proposal),
    )
    now = [datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))]
    service = module.AdaptiveOperatorService(
        coordinator,
        review_operator={"user_id": "reviewer", "chat_id": "review-chat", "topic_id": 59, "version": 1},
        schedule_confirm_handler=facade,
        schedule_confirm_enabled=True,
        now_provider=lambda: now[0],
        profile_root=coordinator.profile_root,
    )
    address = module.IncomingAddress("reviewer", "review-chat", "59")

    def issue() -> str:
        return service.issue_session(
            action="schedule_confirm",
            customer_key="client_001",
            proposal_digest=proposal.digest,
            revision=proposal.revision,
            originating_message_id="review-card-1",
            originating_chat_id="review-chat",
            originating_topic_id="59",
        )

    callback = issue()
    first = service.handle_callback(callback, address, message_id="review-card-1")
    replay = service.handle_callback(callback, address, message_id="review-card-1")
    assert first["status"] == "schedule_confirm"
    assert replay["status"] == "duplicate"

    canonical_before = transaction.events_path.read_bytes()
    adaptive_path = customer.nutrition_plans_root / "events.jsonl"
    adaptive_before = adaptive_path.read_bytes()
    for rejected_callback, rejected_address, message_id in (
        (issue(), module.IncomingAddress("wrong", "review-chat", "59"), "review-card-1"),
        (issue(), module.IncomingAddress("reviewer", "review-chat", "wrong-topic"), "review-card-1"),
        (issue(), address, "forwarded-card"),
    ):
        assert service.handle_callback(rejected_callback, rejected_address, message_id=message_id)["status"] == "rejected"
        assert transaction.events_path.read_bytes() == canonical_before
        assert adaptive_path.read_bytes() == adaptive_before

    expired = issue()
    now[0] = datetime(2026, 8, 1, 11, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert service.handle_callback(expired, address, message_id="review-card-1")["status"] == "rejected"
    now[0] = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    stale = issue()
    from checkin_cli.models import build_schedule_reference_event
    from checkin_cli.store import CanonicalEventTransaction

    previous = transaction.current_schedule_reference("client_001")
    assert previous is not None
    transaction.append_schedule_reference(
        build_schedule_reference_event(
            "client_001", date(2026, 8, 3), "10:30:00",
            customer_confirmed=True, trainer_confirmed=True, last_change_note="superseded",
            supersedes=previous.event_id,
            predecessor_digest=CanonicalEventTransaction.schedule_reference_digest(previous),
            occurred_at_kst="2026-08-01T09:10:00+09:00",
            recorded_at_kst="2026-08-01T09:10:00+09:00",
        ),
        customer_key="client_001",
    )
    after_supersede = transaction.events_path.read_bytes()
    assert service.handle_callback(stale, address, message_id="review-card-1")["status"] == "rejected"
    assert transaction.events_path.read_bytes() == after_supersede
def test_schedule_confirm_replay_recovers_after_consume_before_confirm_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, coordinator, customer, transaction = _schedule_confirm_integration_fixture(tmp_path)
    proposal = SimpleNamespace(digest="b" * 64, revision=1)
    monkeypatch.setattr(
        coordinator,
        "adaptive_nutrition_coordinator",
        lambda _key: SimpleNamespace(_latest_production_proposal=lambda: proposal),
    )
    service = module.AdaptiveOperatorService(
        coordinator,
        review_operator={"user_id": "reviewer", "chat_id": "review-chat", "topic_id": 59, "version": 1},
        schedule_confirm_handler=coordinator.schedule_confirm_handler,
        schedule_confirm_enabled=True,
        profile_root=coordinator.profile_root,
    )
    callback = service.issue_session(
        action="schedule_confirm",
        customer_key="client_001",
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        originating_message_id="review-card-1",
        originating_chat_id="review-chat",
        originating_topic_id="59",
    )
    original_consume = service._consume

    def crash_after_consume(session, *, state):
        original_consume(session, state=state)
        if state == "consumed":
            raise RuntimeError("injected crash after consume")

    monkeypatch.setattr(service, "_consume", crash_after_consume)
    address = module.IncomingAddress("reviewer", "review-chat", "59")
    with pytest.raises(RuntimeError, match="injected crash"):
        service.handle_callback(callback, address, message_id="review-card-1")

    monkeypatch.setattr(service, "_consume", original_consume)
    replay = service.handle_callback(callback, address, message_id="review-card-1")
    assert replay["status"] == "duplicate"
    assert replay["duplicate"] is True
    assert set(("canonical_event", "canonical_sequence", "adaptive_projection")) <= replay.keys()

    events = [json.loads(line) for line in transaction.events_path.read_text().splitlines()]
    projections = [
        json.loads(line)
        for line in (customer.nutrition_plans_root / "events.jsonl").read_text().splitlines()
    ]
    assert sum(row["event_type"] == "schedule_confirmation" for row in events) == 1
    assert sum(row["event_type"] == "schedule_strategy_confirmed" for row in projections) == 1
def test_dual_coach_review_cards_are_customer_local_and_deterministic(monkeypatch) -> None:
    module = _module()
    assert module is not None

    class _Store:
        def read(self):
            return [
                {
                    "event_id": "risk-1",
                    "event_type": "dual_coach_risk_review",
                    "payload": {
                        "customer_key": "client_001",
                        "terminal_checkin_id": "checkin-1",
                        "policy_version": "1",
                        "policy_digest": "policy-pin",
                        "reasons": ["pain_present"],
                        "held": True,
                    },
                },
                {
                    "event_id": "foreign-1",
                    "event_type": "dual_coach_risk_review",
                    "payload": {"customer_key": "client_002"},
                },
            ]

    class _Registered:
        def __init__(self, _customer):
            self.adaptive_store = _Store()

    class _EventSource:
        def __init__(self, _coordinator, _customer_key):
            pass

        def events_for(self, _customer_key):
            return SimpleNamespace(
                _read_events=lambda: (
                    SimpleNamespace(
                        event_type="schedule_reference",
                        schedule_reference=SimpleNamespace(last_change_note="초기 일정"),
                    ),
                    SimpleNamespace(
                        event_type="schedule_correction",
                        schedule_reference=SimpleNamespace(
                            last_change_note="수면 회복을 위해 수요일 휴식으로 변경"
                        ),
                    ),
                )
            )

    coordinator = SimpleNamespace(
        refresh_live_registry=lambda: True,
        _by_key={"client_001": object()},
        customer=lambda key: object() if key == "client_001" else None,
    )
    service = object.__new__(module.DualCoachReviewService)
    service._coordinator = coordinator
    service._review_operator = ("reviewer", "review-chat", "59")
    monkeypatch.setattr(module, "CoordinatorEventSource", _EventSource)
    import checkin_cli.customer_coaching as customer_coaching

    monkeypatch.setattr(customer_coaching, "RegisteredCustomerDualCoachCoordinator", _Registered)

    cards = service.cards()

    assert len(cards) == 1
    assert cards[0].card_id == "dual-coach-review:client_001:risk-1"
    assert "원본 체크인: checkin-1" in cards[0].text
    assert "사유: pain_present" in cards[0].text
    assert "안전 상태: 보류" in cards[0].text
    assert "정책 핀: 1/policy-pin" in cards[0].text
    assert "수면 회복을 위해 수요일 휴식으로 변경" in cards[0].text
    assert "고객 발송 없음" in cards[0].text
    assert service.accepts(module.IncomingAddress("reviewer", "review-chat", "59"))
    assert not service.accepts(module.IncomingAddress("wrong-user", "review-chat", "59"))
def test_dual_coach_review_journal_failure_returns_bounded_operator_diagnostic(monkeypatch) -> None:
    module = _module()
    assert module is not None

    class _Store:
        def read(self):
            raise OSError("corrupt")

    class _Registered:
        def __init__(self, _customer):
            self.adaptive_store = _Store()

    coordinator = SimpleNamespace(
        refresh_live_registry=lambda: True,
        _by_key={"client_001": object(), "client_002": object()},
        customer=lambda _key: object(),
    )
    service = object.__new__(module.DualCoachReviewService)
    service._coordinator = coordinator
    monkeypatch.setattr(
        __import__("checkin_cli.customer_coaching", fromlist=["RegisteredCustomerDualCoachCoordinator"]),
        "RegisteredCustomerDualCoachCoordinator",
        _Registered,
    )

    cards = service.cards()

    assert [card.customer_key for card in cards] == ["client_001", "client_002"]
    assert all(card.event_type == "review_journal_diagnostic" for card in cards)
    assert all("고객 발송 없음" in card.text for card in cards)
def test_dual_coach_review_publication_claim_is_append_only_across_restart(tmp_path: Path) -> None:
    module = _module()
    assert module is not None
    card = module.DualCoachReviewCard(
        "dual-coach-review:client_001:risk-1",
        "client_001",
        "dual_coach_risk_review",
        "운영자 검토 전용",
    )

    first = object.__new__(module.DualCoachReviewService)
    first._publication_lock = module.RLock()
    first._publication_ledger_path = tmp_path / "dual_coach_review_publications.jsonl"
    assert first.claim_publication(card) is True
    first.record_publication(card, "operator-101")

    restarted = object.__new__(module.DualCoachReviewService)
    restarted._publication_lock = module.RLock()
    restarted._publication_ledger_path = first._publication_ledger_path
    assert restarted.claim_publication(card) is False
    rows = [json.loads(line) for line in first._publication_ledger_path.read_text().splitlines()]
    assert [row["state"] for row in rows] == ["claimed", "published"]
    assert rows[-1]["published_message_id"] == "operator-101"
    assert module.DualCoachReviewService.publication_receipt(
        SimpleNamespace(message_id="operator-102")
    ) == "operator-102"
