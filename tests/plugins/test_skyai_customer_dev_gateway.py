from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.skyai_customer import dev_gateway


def settings(tmp_path: Path, **overrides) -> dev_gateway.CanarySettings:
    values = {"profile_home": tmp_path / "profiles" / "skyai-v2-dev"}
    values.update(overrides)
    return dev_gateway.CanarySettings(**values)


def test_validate_settings_allows_loopback_without_token(tmp_path: Path) -> None:
    dev_gateway.validate_settings(settings(tmp_path))


def test_validate_settings_blocks_public_bind_without_explicit_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        dev_gateway.validate_settings(settings(tmp_path, host="0.0.0.0"))


def test_validate_settings_requires_token_for_public_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bearer token"):
        dev_gateway.validate_settings(
            settings(tmp_path, host="0.0.0.0", allow_public_bind=True)
        )


def test_validate_settings_allows_private_bind_with_explicit_gate_without_token(
    tmp_path: Path,
) -> None:
    dev_gateway.validate_settings(
        settings(tmp_path, host="10.80.0.3", allow_public_bind=True)
    )


def test_resolve_build_commit_prefers_explicit_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(dev_gateway.BUILD_COMMIT_ENV, "from-env")
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit("explicit") == "explicit"


def test_resolve_build_commit_reads_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(dev_gateway.BUILD_COMMIT_ENV, "from-env")
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit() == "from-env"


def test_resolve_build_commit_reads_runtime_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dev_gateway.BUILD_COMMIT_ENV, raising=False)
    (tmp_path / dev_gateway.BUILD_COMMIT_FILE).write_text("from-file\n", encoding="utf-8")

    assert dev_gateway.resolve_build_commit() == "from-file"


def test_extract_message_accepts_fab_style_payload() -> None:
    payload = {
        "conversation_id": "abc",
        "history": [{"role": "assistant", "content": "Здравей"}],
        "message": "Искам ваучер за двама",
    }

    assert dev_gateway.extract_message(payload) == "Искам ваучер за двама"


def test_extract_message_falls_back_to_last_customer_message() -> None:
    payload = {
        "messages": [
            {"role": "assistant", "content": "Здравей"},
            {"role": "customer", "content": "Имате ли свободни слотове?"},
        ]
    }

    assert dev_gateway.extract_message(payload) == "Имате ли свободни слотове?"


def test_extract_history_normalizes_customer_role_and_limits() -> None:
    payload = {
        "history": [
            {"role": "system", "content": "drop"},
            {"role": "customer", "content": "Първо"},
            {"role": "assistant", "content": "Второ"},
        ]
    }

    assert dev_gateway.extract_history(payload) == [
        {"role": "user", "content": "Първо"},
        {"role": "assistant", "content": "Второ"},
    ]


def test_runtime_conversation_id_compacts_long_external_ids() -> None:
    external_id = "skyai-v2-compare-20260704T203902Z-gift-calm-50-sliven-" + ("x" * 120)

    runtime_id = dev_gateway.runtime_conversation_id(external_id)

    assert len(runtime_id) <= 64
    assert runtime_id.startswith("skyai-v2-compare-")
    assert runtime_id != external_id
    assert dev_gateway.runtime_conversation_id("thread-1") == "thread-1"


def test_voice_generated_conversation_id_uses_same_call_id() -> None:
    payload: dict[str, str] = {}
    call_id = dev_gateway.voice_call_id_from_payload(payload)

    assert dev_gateway.voice_conversation_id_from_payload(payload, call_id) == f"skyai-voice-{call_id}"


@pytest.mark.asyncio
async def test_build_chat_response_dry_run_returns_fab_compatible_shape(tmp_path: Path) -> None:
    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["version"] == dev_gateway.VERSION
    assert response["behavior_version"] == dev_gateway.SKYAI_BEHAVIOR_VERSION
    assert response["conversation_id"] == "c1"
    assert response["cards"] == []
    assert response["trace"]["runtime"] == "hermes_agent"
    assert response["trace"]["behavior_version"] == dev_gateway.SKYAI_BEHAVIOR_VERSION
    assert response["trace"]["toolset"] == "skyai_customer"
    assert response["trace"]["live_model"] is False
    assert "dry-run" in response["reply"]


@pytest.mark.asyncio
async def test_build_chat_response_allows_injected_runner(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen.update(
            {
                "message": message,
                "history": history,
                "conversation_id": conversation_id,
                "profile_home": canary_settings.profile_home,
            }
        )
        return "Отговор от тестов runner"

    response = await dev_gateway.build_chat_response(
        {
            "session_id": "thread-1",
            "message": "Покажи ми подарък",
            "history": [{"role": "customer", "content": "Здравей"}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["reply"] == "Отговор от тестов runner"
    assert seen["message"] == "Покажи ми подарък"
    assert seen["history"] == [{"role": "user", "content": "Здравей"}]
    assert seen["conversation_id"] == "thread-1"
    assert seen["profile_home"] == tmp_path / "profiles" / "skyai-v2-dev"


@pytest.mark.asyncio
async def test_build_chat_response_exposes_resolved_model_trace(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {
            "final_response": "Отговор от Hermes.",
            "trace": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            },
        }

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["trace"]["model"] == "gpt-5.6-sol"
    assert response["trace"]["provider"] == "openai-codex"
    assert response["trace"]["api_mode"] == "codex_responses"


@pytest.mark.asyncio
async def test_build_chat_response_fails_closed_on_provider_error(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return {
            "final_response": (
                "API call failed after 3 retries: HTTP 429: "
                "The usage limit has been reached"
            ),
            "completed": False,
            "failed": True,
            "error": "The usage limit has been reached",
            "failure_reason": "rate_limit",
            "trace": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
                "credential_pool_size": 4,
                "credential_rotated": False,
            },
        }

    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "error"
    assert response["error"] == "provider_unavailable"
    assert response["reply"] == dev_gateway.SKYAI_MODEL_UNAVAILABLE_MESSAGE
    assert "429" not in response["reply"]
    assert "usage limit" not in response["reply"].lower()
    assert response["cards"] == []
    assert response["trace"]["failure_reason"] == "rate_limit"
    assert response["trace"]["credential_pool_size"] == 4
    assert response["trace"]["credential_rotated"] is False


@pytest.mark.asyncio
async def test_build_chat_response_passes_voice_system_prompt_to_hermes_runner(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update(
            {
                "message": message,
                "conversation_id": conversation_id,
                "system_prompt": system_prompt,
            }
        )
        return "Говоря кратко, защото това е телефонен разговор."

    response = await dev_gateway.build_chat_response(
        {
            "conversation_id": "voice-c1",
            "message": "Как мога да се свържа с екипа?",
            "metadata": {"surface": "pbx_voice", "source": "zycoo-coovox-u20"},
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["trace"]["surface"] == "voice"
    assert seen["message"] == "Как мога да се свържа с екипа?"
    assert seen["conversation_id"] == "voice-c1"
    assert "Voice режим" in seen["system_prompt"]
    assert "Клиентът вече се е свързал с официалната линия" in seen["system_prompt"]
    assert "не го връщай към 'официален канал'" in seen["system_prompt"]
    assert "не изброявай телефона, имейла или работното време" in seen["system_prompt"]
    assert "без markdown, сурови URL-и, дълги списъци" in seen["system_prompt"]
    assert "spoken_reply е авторитетният отговор" in seen["system_prompt"]
    assert "Скъсявай за телефон, но не променяй бизнес фактите" in seen["system_prompt"]
    assert "не казвай 'нека проверя'" in seen["system_prompt"]


def test_create_app_registers_dev_routes(tmp_path: Path) -> None:
    app = dev_gateway.create_app(settings(tmp_path))
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/health") in routes
    assert ("GET", "/ready") in routes
    assert ("GET", "/version") in routes
    assert ("GET", "/widget/chatkit/") in routes
    assert ("POST", "/chatkit/dev-message") in routes
    assert ("POST", "/chatkit/message") in routes
    assert ("POST", "/qa/compare") in routes
    assert ("POST", "/voice/start") in routes
    assert ("POST", "/voice/turn") in routes
    assert ("POST", "/voice/event") in routes
    assert ("POST", "/voice/end") in routes


@pytest.mark.asyncio
async def test_build_voice_start_response_returns_contract_shape(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_start_response(
        {
            "call_id": "call-1",
            "conversation_id": "voice-c1",
            "caller_id": "+35970020200",
            "pbx_extension": "399",
            "recording_notice_played": False,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["contract_version"] == "skyai-voice-contract.v0.1"
    assert response["call_id"] == "call-1"
    assert response["conversation_id"] == "voice-c1"
    assert response["action"] == "speak"
    assert response["end_call"] is False
    assert response["transfer"] is None
    assert response["transfer_reason"] is None
    assert response["target"] is None
    assert response["notes"] == []
    assert response["unavailable"] is False
    assert response["trace"]["runtime"] == "skyai_voice_adapter"
    assert response["trace"]["raw_audio_stored"] is False
    assert response["trace"]["customer_mutations_allowed"] is False


@pytest.mark.asyncio
async def test_build_voice_turn_response_uses_v2_chat_adapter(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen.update(
            {
                "message": message,
                "history": history,
                "conversation_id": conversation_id,
                "profile_home": canary_settings.profile_home,
            }
        )
        return {
            "reply": "Разбира се, ето идея за подарък.",
            "cards": [{"title": "Ваучер за подарък на стойност", "price_text": "стойност по избор"}],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-2",
            "conversation_id": "voice-c2",
            "turn_index": 1,
            "transcript": "Търся подарък за рожден ден.",
            "is_final": True,
            "stt_confidence": 0.91,
            "caller_id": "+35970020200",
            "did": "+35924259795",
            "pbx_extension": "399",
            "department": "sales",
            "language": "bg-BG",
            "source": "zycoo-coovox-u20",
            "history": [{"role": "assistant", "content": "Здравейте"}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert response["spoken_reply"] == "Разбира се, ето идея за подарък."
    assert not response["spoken_reply"].startswith("Извинете")
    assert response["display_reply"] == response["spoken_reply"]
    assert response["cards"] == [{"title": "Ваучер за подарък на стойност", "price_text": "стойност по избор"}]
    assert response["transfer"] is None
    assert response["transfer_reason"] is None
    assert response["target"] is None
    assert response["trace"]["backend_target"] == "skyai_v2_chatkit"
    assert response["trace"]["voice_backend_target"] == "skyai_v2_chatkit"
    assert response["trace"]["stt_confidence"] == 0.91
    assert seen["message"] == "Търся подарък за рожден ден."
    assert seen["history"] == [{"role": "assistant", "content": "Здравейте"}]
    assert seen["conversation_id"] == "voice-c2"


@pytest.mark.asyncio
async def test_voice_first_clear_turn_after_greeting_goes_to_hermes(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update(
            {
                "message": message,
                "history": history,
                "system_prompt": system_prompt,
            }
        )
        return "За спокоен рожден ден бих започнал с красив релакс подарък близо до Вас."

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-clear-first-turn",
            "conversation_id": "voice-clear-first-turn",
            "turn_index": 1,
            "transcript": "Търся подарък за рожден ден на приятелка, нещо спокойно.",
            "stt_confidence": 0.92,
            "history": [{"role": "assistant", "content": "Здравейте, свързахте се със SkyVision."}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "Извинете, не Ви" not in response["spoken_reply"]
    assert "voice_reason" not in response["trace"]
    assert seen["message"] == "Търся подарък за рожден ден на приятелка, нещо спокойно."
    assert seen["history"] == [{"role": "assistant", "content": "Здравейте, свързахте се със SkyVision."}]
    assert "Voice режим" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_business_metadata_does_not_replace_semantic_reasoning(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "system_prompt": system_prompt})
        return "Ще го подходим консултативно: повод, човек, локация и усещане."

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-domain-intent",
            "conversation_id": "voice-domain-intent",
            "transcript": "Моля за идея за подарък за рожден ден около Сливен.",
            "stt_confidence": 0.9,
            "metadata": {
                "surface": "pbx_voice",
                "domain_intent": "birthday_gift",
                "gift_intent": "true",
                "support_intent": "false",
            },
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert seen["message"] == "Моля за идея за подарък за рожден ден около Сливен."
    assert "domain_intent" not in seen["message"]
    assert "birthday_gift" not in seen["system_prompt"]
    assert "backend-ът няма phrase list" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_cheaper_than_voucher_value_keeps_residual_policy(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "system_prompt": system_prompt})
        return (
            "Ако избраното преживяване е по-евтино от стойността на ваучера, "
            "остатъкът остава като ваучерна стойност за следващо преживяване. "
            "Ако е по-скъпо, тогава се доплаща разликата."
        )

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-residual-voucher",
            "conversation_id": "voice-residual-voucher",
            "transcript": "Какво става ако избраното преживяване е по-евтино от стойността на ваучера?",
            "stt_confidence": 0.93,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "остатъкът остава" in response["spoken_reply"]
    assert "ваучерна стойност" in response["spoken_reply"]
    assert seen["message"].startswith("Какво става ако избраното преживяване е по-евтино")
    assert "остатъкът остава като ваучерна стойност" in seen["system_prompt"]


@pytest.mark.asyncio
async def test_voice_follow_up_gets_history_instead_of_repeating_wrong_answer(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        seen.update({"message": message, "history": history})
        return "Точно така, при по-евтино преживяване остатъкът не се губи, а остава като ваучерна стойност."

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-follow-up",
            "conversation_id": "voice-follow-up",
            "transcript": "Не, имах предвид ако е по-евтино, не по-скъпо.",
            "stt_confidence": 0.96,
            "history": [
                {"role": "user", "content": "Какво ако е по-евтино?"},
                {"role": "assistant", "content": "Ще доплатите разликата."},
            ],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "остатъкът не се губи" in response["spoken_reply"]
    assert seen["message"] == "Не, имах предвид ако е по-евтино, не по-скъпо."
    assert seen["history"] == [
        {"role": "user", "content": "Какво ако е по-евтино?"},
        {"role": "assistant", "content": "Ще доплатите разликата."},
    ]


@pytest.mark.asyncio
async def test_voice_basic_policy_answer_does_not_force_checking_phrase(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings, system_prompt):
        return "При BookNow, ако изпълнителят не може да проведе резервацията, парите ще бъдат възстановени."

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-basic-policy",
            "conversation_id": "voice-basic-policy",
            "transcript": "Какво става, ако времето е лошо и BookNow резервацията отпадне?",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "парите ще бъдат възстановени" in response["spoken_reply"]
    assert "нека проверя" not in response["spoken_reply"].casefold()
    assert "проверявам" not in response["spoken_reply"].casefold()


@pytest.mark.asyncio
async def test_build_voice_turn_sanitizes_spoken_reply_for_tts(tmp_path: Path) -> None:
    async def fake_runner(*args, **kwargs):
        return {
            "reply": (
                "Ето подробности: [Полет](https://skyvision.bg/example) е чудесен избор. "
                + ("Много красив подарък. " * 80)
            ),
            "cards": [],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-tts",
            "conversation_id": "voice-tts",
            "transcript": "Разкажи ми повече за този подарък.",
            "stt_confidence": 0.95,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert "https://skyvision.bg/example" in response["display_reply"]
    assert "https://skyvision.bg/example" not in response["spoken_reply"]
    assert "Полет" in response["spoken_reply"]
    assert len(response["spoken_reply"]) < len(response["display_reply"])


@pytest.mark.asyncio
async def test_build_voice_turn_low_confidence_clarifies_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("low-confidence voice turns must not call Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-3",
            "conversation_id": "voice-c3",
            "transcript": "шшш",
            "stt_confidence": 0.2,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "clarify"
    assert "повторите" in response["spoken_reply"]
    assert response["trace"]["voice_reason"] == "low_stt_confidence"
    assert response["trace"]["raw_audio_stored"] is False


@pytest.mark.asyncio
async def test_build_voice_turn_dtmf_zero_transfers_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("DTMF 0 must transfer without calling Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-dtmf",
            "conversation_id": "voice-dtmf",
            "dtmf_event": "0",
            "transcript": "",
            "stt_confidence": 0.99,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "dtmf_0"}
    assert response["transfer_reason"] == "dtmf_0"
    assert response["target"] == "operator_queue"


@pytest.mark.asyncio
async def test_build_voice_turn_human_request_transfers_from_structured_hermes_action(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen["message"] = message
        return {
            "final_response": "Разбира се, ще Ви прехвърля към колега.",
            "messages": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "voice_action": "transfer_to_human",
                            "transfer": {
                                "target": "operator_queue",
                                "reason": "hermes_requested_handoff",
                            },
                            "spoken_reply": "Разбира се, ще Ви прехвърля към колега.",
                            "display_reply": "Hermes requested human handoff.",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-human",
            "conversation_id": "voice-human",
            "transcript": "Моля, свържете ме с човек от екипа.",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["spoken_reply"] == "Разбира се, ще Ви прехвърля към колега."
    assert response["transfer_reason"] == "hermes_requested_handoff"
    assert response["target"] == "operator_queue"
    assert response["trace"]["voice_action_source"] == "hermes_tool"
    assert seen["message"] == "Моля, свържете ме с човек от екипа."


@pytest.mark.asyncio
async def test_build_voice_turn_does_not_transfer_for_ordinary_person_words(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen["message"] = message
        return "Подходящ подарък за този човек може да е ваучер на стойност."

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-person",
            "conversation_id": "voice-person",
            "transcript": "Търся подарък за спокоен човек.",
            "stt_confidence": 0.94,
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "speak"
    assert response["transfer"] is None
    assert seen["message"] == "Търся подарък за спокоен човек."


@pytest.mark.asyncio
async def test_build_voice_turn_repeated_silence_clarifies_without_model_call(tmp_path: Path) -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("silence turns must not call Hermes")

    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-silence",
            "conversation_id": "voice-silence",
            "transcript": "",
            "silence_count": 2,
        },
        settings(tmp_path, live_model=True),
        agent_runner=forbidden_runner,
    )

    assert response["status"] == "ok"
    assert response["action"] == "clarify"
    assert response["trace"]["voice_reason"] == "silence_timeout"
    assert response["trace"]["silence_count"] == 2


@pytest.mark.asyncio
async def test_build_voice_event_dtmf_zero_transfers_to_human(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_event_response(
        {
            "call_id": "call-4",
            "conversation_id": "voice-c4",
            "event_type": "dtmf",
            "dtmf": "0",
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "dtmf_0"}
    assert response["target"] == "operator_queue"
    assert response["transfer_reason"] == "dtmf_0"
    assert "човек" in response["spoken_reply"]


@pytest.mark.asyncio
async def test_build_voice_turn_v1_target_requires_configured_backend(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-5",
            "conversation_id": "voice-c5",
            "backend_target": "skyai_v1_chatkit",
            "transcript": "Искам ваучер.",
            "stt_confidence": 0.9,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {
        "target": "operator_queue",
        "reason": "voice_v1_backend_not_configured",
    }
    assert response["target"] == "operator_queue"
    assert response["transfer_reason"] == "voice_v1_backend_not_configured"
    assert response["trace"]["backend_target"] == "skyai_v1_chatkit"


@pytest.mark.asyncio
async def test_build_voice_turn_invalid_backend_target_transfers_to_human(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_turn_response(
        {
            "call_id": "call-invalid",
            "conversation_id": "voice-invalid",
            "backend_target": "unknown",
            "transcript": "Здравейте",
            "stt_confidence": 0.9,
        },
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["action"] == "transfer_to_human"
    assert response["transfer"] == {"target": "operator_queue", "reason": "invalid_voice_backend_target"}
    assert response["transfer_reason"] == "invalid_voice_backend_target"
    assert response["target"] == "operator_queue"
    assert response["unavailable"] is True


@pytest.mark.asyncio
async def test_build_voice_end_response_ends_call_without_mutations(tmp_path: Path) -> None:
    response = await dev_gateway.build_voice_end_response(
        {
            "call_id": "call-6",
            "conversation_id": "voice-c6",
            "ended_by": "caller",
            "duration_seconds": 42,
            "recording_stored": False,
            "transcript_stored": True,
        },
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["action"] == "end_call"
    assert response["end_call"] is True
    assert response["trace"]["ended_by"] == "caller"
    assert response["trace"]["duration_seconds"] == 42
    assert response["trace"]["recording_stored"] is False
    assert response["trace"]["transcript_stored"] is True
    assert response["trace"]["customer_mutations_allowed"] is False


def test_render_widget_html_contains_fab_compatible_chat_endpoint(tmp_path: Path) -> None:
    html = dev_gateway.render_widget_html(settings(tmp_path, version="test-version"))

    assert "<title>SkyAI асистент | SkyVision</title>" in html
    assert 'meta name="skyvision-clean-dev-version" content="test-version"' in html
    assert "<h1>SkyAI асистент</h1>" in html
    assert "#32BCAD" in html
    assert "#275E7C" in html
    assert "test-version" in html
    assert "fetch('/chatkit/message'" in html
    assert "message--typing" in html
    assert "card__image" in html
    assert "appendCards(payload.cards)" in html
    assert "skyai-widget-transcript:" in html
    assert "function persistTranscript" in html
    assert "function restoreTranscript" in html
    assert "appendMessage(item.role, item.text, { persist: false })" in html
    assert "appendCards(item.cards, { persist: false })" in html
    assert "renderAssistantMarkdown" in html
    assert "message--rich" in html
    assert "node.innerHTML = renderAssistantMarkdown(text)" in html
    assert "escapeHtml" in html
    assert "message__heading" in html
    assert "line.match(/^#{1,4}" in html
    assert "document.addEventListener('click'" in html
    assert 'target="_top"' in html
    assert "anchor.target = '_top'" in html
    assert "window.location.href = href" not in html
    assert "function isTestSession" in html
    assert "is_test: isTestSession() ? '1' : ''" in html


def test_system_prompt_links_campaign_bonus_id_to_slots_tool() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "Hermes мисли" in prompt
    assert "не вземат customer-visible семантични решения" in prompt
    assert "никакъв tool output като готова реплика" in prompt
    assert "скрита класификация" in prompt
    assert "skyai_product_slots" in prompt
    assert "skyai_support_knowledge" in prompt
    assert "клиентския панел „Ваучери“" in prompt
    assert "добавяне/управление на ваучери" in prompt
    assert "EUR е основната цена" in prompt
    assert "Catalog tool-ът връща кандидати" in prompt
    assert "не заповед" in prompt
    assert "Hermes сам носи отговорност" in prompt
    assert "няма display-level card adapter" in prompt
    assert "selection_context/category_mix" in prompt
    assert "не пълни" in prompt
    assert "не приемай автоматично" in prompt
    assert "първо мисли близко" in prompt
    assert "приеми, че близостта е важна" in prompt
    assert "nearest_returned_items" in prompt
    assert "чак след това попитай дали може да разшириш" in prompt
    assert "започни направо с желаната посока" in prompt
    assert "positive-only" in prompt
    assert "не използвай конструкции от типа 'без X/Y'" in prompt
    assert "бонусът е благодарност към купувача/резервиращия" in prompt
    assert "бонусният полет се изпълнява единствено от летище Приморско" in prompt
    assert "независимо къде е основната закупена услуга" in prompt
    assert "по правило бонусът е за купувача/резервиращия" in prompt
    assert "акаунта/данните му" in prompt
    assert "не се прехвърля автоматично" in prompt
    assert "Емил Ломлиев" in prompt
    assert "съосновател с Малина през 2007" in prompt
    assert "пилот-инструктор" in prompt
    assert "+359 886 417 142" in prompt
    assert "Подаръчните бонуси нямат ваучерен/сериен номер" in prompt
    assert "не се добавят ръчно" in prompt
    assert "ако купувачът е логнат" in prompt
    assert "автоматично в профила" in prompt
    assert "се обвързва с имейла от поръчката" in prompt
    assert "профил със същия имейл" in prompt
    assert "не започвай с директно 'да'" in prompt
    assert "не представяй бонуса като подарък за получателя" in prompt
    assert "BookNow е директна резервация" in prompt
    assert "парите ще бъдат възстановени" in prompt
    assert "не като несигурна възможност" in prompt
    assert "не загатвай, че можеш да завършиш заявка" in prompt
    assert "Клиентът трябва сам да отвори" in prompt
    assert "продуктовия public_url" in prompt
    assert "Историята е общ контекст" in prompt
    assert "Отговаряй само с новото" in prompt
    assert "сравни всяко твърдение и стъпка" in prompt
    assert "ако смисълът вече е даден, изтрий го" in prompt
    assert "Полезността или свързаността не оправдава повторение" in prompt
    assert "поправка/недоволство" in prompt
    assert "поправи само новото" in prompt
    assert "изрично искане или корекция" in prompt
    assert "само нужната част" in prompt
    assert "Два ваучера не се обединяват автоматично" in prompt
    assert "остатъкът остава като ваучерна стойност" in prompt
    assert "Опцията за удължаване е налична" in prompt
    assert "customer-safe обучение от реални email/support казуси" in prompt
    assert "intent/state reasoning, а не като шаблон" in prompt
    assert "приемай неуточнения ваучер за ваучер на SkyVision" in prompt
    assert "не питай рутинно за издателя" in prompt
    assert "конкретна причина да се съмняваш в съвместимостта" in prompt
    assert "Само ваучерите на SkyVision важат в SkyVision профила" in prompt
    assert "Ако клиентът посочи друг издател" in prompt
    assert "ваучерът не може да се добави тук" in prompt
    assert "се обслужва от издателя си" in prompt
    assert "при неясен произход първо го уточни" not in prompt
    assert "Писмен контакт с екипа: info@skyvision.bg" in prompt
    assert "reservations@skyvision.bg е автоматичен адрес" in prompt
    assert "не канал за клиентски отговори" in prompt
    assert "не коментирай самото ограничение" in prompt
    assert "представи се кратко като SkyAI" in prompt
    assert "не решавай учебни задачи" in prompt
    assert len(prompt) < 5200
    assert "силно попадение" not in prompt
    assert "ще й легне" not in prompt
    assert "ако клиентът уточни нещо" not in prompt


def test_build_cards_from_reply_enriches_visible_product_links(monkeypatch) -> None:
    seen = {}

    def fake_detail(product_url="", product_path=""):
        seen["product_url"] = product_url
        return {
            "status": "ok",
            "detail": {
                "title": "Полет с жирокоптер MTO-Sport",
                "public_url": "https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/",
                "price_eur": "101.75",
                "price_bgn": "199.00",
                "location": "Приморско",
                "images": [{"src": "https://cdn.example/gyro.jpg"}],
            },
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    cards = dev_gateway.build_cards_from_reply(
        "Виж [този полет](https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/). "
        "Кампанията е тук: https://skyvision.bg/campaign/free-panoramic-flight/"
    )

    assert seen["product_url"].startswith("https://skyvision.bg/подарък/полет-с-жирокоптер/")
    assert cards == [
        {
            "title": "Полет с жирокоптер MTO-Sport",
            "public_url": "https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/",
            "url": "https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/",
            "price_eur": "101.75",
            "price_bgn": "199.00",
            "location": "Приморско",
            "image": "https://cdn.example/gyro.jpg",
        }
    ]


def test_build_cards_from_reply_supports_value_voucher_special_url() -> None:
    cards = dev_gateway.build_cards_from_reply(
        "Ако искаш да оставиш избора на нея, виж https://skyvision.bg/подарък/ваучер-за-подарък-на-стойност/"
    )

    assert cards == [
        {
            "title": "Ваучер за подарък на стойност",
            "public_url": "https://skyvision.bg/подарък/ваучер-за-подарък-на-стойност/",
            "url": "https://skyvision.bg/подарък/ваучер-за-подарък-на-стойност/",
            "price_text": "стойност по избор",
            "location": "валиден за SkyVision каталога",
        }
    ]


def test_build_cards_from_reply_caps_visible_product_cards(monkeypatch) -> None:
    def fake_detail(product_url="", product_path=""):
        return {
            "status": "ok",
            "detail": {
                "title": product_url.rsplit("/", 2)[-2],
                "public_url": product_url,
                "price_eur": "40.00",
            },
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    cards = dev_gateway.build_cards_from_reply(
        " ".join(
            [
                "https://skyvision.bg/подарък/офроуд-атв/one/",
                "https://skyvision.bg/подарък/ирисова-фотография/two/",
                "https://skyvision.bg/подарък/издигане-с-балон/three/",
                "https://skyvision.bg/подарък/терапия/four/",
            ]
        )
    )

    assert len(cards) == 3
    assert [card["title"] for card in cards] == ["one", "two", "three"]


def test_build_cards_from_reply_preserves_hermes_link_order(monkeypatch) -> None:
    details = {
        "https://skyvision.bg/подарък/офроуд-атв-под-наем/one/": "Офроуд разходка с АТВ до Пловдив",
        "https://skyvision.bg/подарък/приключения-с-мотор/two/": "ОФРОУД ТУР С ЕЛЕКТРИЧЕСКИ МОТОР край Пловдив",
        "https://skyvision.bg/подарък/ирисова-фотография/three/": "Ирисова фотография в Пловдив",
        "https://skyvision.bg/подарък/терапия/four/": "Терапия за мъже в Пловдив",
    }

    def fake_detail(product_url="", product_path=""):
        return {
            "status": "ok",
            "detail": {
                "title": details[product_url],
                "public_url": product_url,
                "price_eur": "40.00",
            },
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    cards = dev_gateway.build_cards_from_reply(" ".join(details.keys()))

    assert [card["title"] for card in cards] == [
        "Офроуд разходка с АТВ до Пловдив",
        "ОФРОУД ТУР С ЕЛЕКТРИЧЕСКИ МОТОР край Пловдив",
        "Ирисова фотография в Пловдив",
    ]


def test_dev_gateway_has_no_display_level_card_adapter() -> None:
    source = Path(dev_gateway.__file__).read_text(encoding="utf-8")

    assert "_select_visible_cards" not in source
    assert "_card_similarity_score" not in source
    assert "MAX_CARD_CANDIDATE_LINKS" not in source


def test_resolve_profile_runtime_reads_model_dict() -> None:
    runtime = dev_gateway._resolve_profile_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
            }
        }
    )

    assert runtime == {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "",
    }


def test_resolve_agent_runtime_refreshes_codex_credentials() -> None:
    seen = {}

    def fake_codex_resolver(**kwargs):
        seen.update(kwargs)
        return {
            "api_key": "fresh-oauth-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }

    runtime = dev_gateway._resolve_agent_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            }
        },
        codex_credential_resolver=fake_codex_resolver,
    )

    assert seen == {"refresh_if_expiring": True}
    assert runtime == {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "fresh-oauth-token",
    }


def test_resolve_agent_runtime_attaches_profile_credential_pool() -> None:
    pool = object()
    seen = {}

    def fake_runtime_resolver(**kwargs):
        seen.update(kwargs)
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "selected-pool-token",
            "credential_pool": pool,
        }

    runtime = dev_gateway._resolve_agent_runtime(
        {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            }
        },
        runtime_provider_resolver=fake_runtime_resolver,
    )

    assert seen == {
        "requested": "openai-codex",
        "target_model": "gpt-5.6-sol",
    }
    assert runtime["api_key"] == "selected-pool-token"
    assert runtime["credential_pool"] is pool


def test_sanitize_runtime_error_redacts_token_markers() -> None:
    assert dev_gateway.sanitize_runtime_error(
        RuntimeError("Bearer abc123 access_token=secret refresh_token:secret2 api_key=secret3")
    ) == "Bearer [redacted] access_token=[redacted] refresh_token=[redacted] api_key=[redacted]"


def test_format_discord_mirror_message_uses_customer_visible_shape() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {"conversation_id": "c1", "message": "Търся подарък"},
        {
            "status": "ok",
            "version": "v-test",
            "behavior_version": "v2.1",
            "conversation_id": "c1",
            "reply": "Имаме чудесни идеи.",
            "trace": {
                "runtime": "hermes_agent",
                "toolset": "skyai_customer",
                "live_model": True,
                "fallback": False,
                "latency_ms": 12,
            },
        },
    )

    assert "**Клиент**" in message
    assert "**SkyAI**" in message
    assert "Търся подарък" in message
    assert "Имаме чудесни идеи." in message
    assert "version=v2.1" in message
    assert "runtime_version=v-test" in message
    assert "toolset=skyai_customer" in message


def test_classify_discord_conversation_marks_skyvision1_as_test() -> None:
    result = dev_gateway.classify_discord_conversation(
        {
            "conversation_id": "c1",
            "metadata": {"page_referrer": "https://skyvision1.7s2go.com/?qa=1"},
        },
        "c1",
    )

    assert result["kind"] == "test"
    assert result["badge"] == "🧪 TEST"


def test_classify_discord_conversation_keeps_skyvision_prod_real() -> None:
    result = dev_gateway.classify_discord_conversation(
        {
            "conversation_id": "skyai-prod-real",
            "metadata": {
                "surface": "widget_chatkit_dev",
                "page_referrer": "https://skyvision.bg/подарък/масаж/",
            },
        },
        "skyai-prod-real",
    )

    assert result["kind"] == "real"


def test_classify_discord_conversation_marks_prod_referrer_with_test_marker() -> None:
    result = dev_gateway.classify_discord_conversation(
        {
            "conversation_id": "skyvision1-chat-mqzho5p8-g1zcay",
            "metadata": {
                "surface": "widget_chatkit_dev",
                "page_referrer": "https://skyvision.bg/?codex_prod_v2_cutover=20260707",
            },
        },
        "skyvision1-chat-mqzho5p8-g1zcay",
    )

    assert result["kind"] == "test"
    assert result["badge"] == "🧪 TEST"
    assert result["reason"] == "url_marker"


def test_format_discord_mirror_message_marks_test_conversations() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {
            "conversation_id": "skyai-v2-compare-run",
            "message": "QA тест",
            "metadata": {"page_referrer": "https://skyvision1.7s2go.com/"},
        },
        {
            "status": "ok",
            "version": "v-test",
            "conversation_id": "skyai-v2-compare-run",
            "reply": "Тестов отговор.",
            "trace": {
                "runtime": "hermes_agent",
                "toolset": "skyai_customer",
                "live_model": True,
                "fallback": False,
                "latency_ms": 12,
            },
        },
    )

    assert message.startswith("**🧪 TEST / QA разговор**")
    assert "origin_class=test" in message


@pytest.mark.asyncio
async def test_discord_thread_name_marks_test_threads(tmp_path: Path, monkeypatch) -> None:
    created_names: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        assert content.startswith("🧪 TEST SkyAI v2 разговор")
        return {"id": "starter-message"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        created_names.append(name)
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    thread_id = await dev_gateway._discord_target_channel_id(
        settings=settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="channel",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
        conversation_id="skyai-v2-compare-thread-123",
        request_payload={"metadata": {"page_referrer": "https://skyvision1.7s2go.com/"}},
    )

    assert thread_id == "thread-1"
    assert created_names == ["🧪 TEST · SkyAI v2 · skyai-v2-compare-thread-123"]


@pytest.mark.asyncio
async def test_discord_thread_name_keeps_prod_threads_plain(tmp_path: Path, monkeypatch) -> None:
    created_names: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        assert not content.startswith("🧪 TEST")
        return {"id": "starter-message"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        created_names.append(name)
        return {"id": "thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    await dev_gateway._discord_target_channel_id(
        settings=settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="channel",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
        conversation_id="skyai-prod-real-thread",
        request_payload={"metadata": {"page_referrer": "https://skyvision.bg/"}},
    )

    assert created_names == ["SkyAI v2 · skyai-prod-real-thread"]


@pytest.mark.asyncio
async def test_mirror_to_discord_skips_when_disabled(tmp_path: Path) -> None:
    result = await dev_gateway.mirror_to_discord(
        {"message": "Здравей"},
        {"status": "ok", "reply": "Здравей", "trace": {}},
        settings(tmp_path),
    )

    assert result == {"status": "skipped", "reason": "disabled"}


def test_format_voice_discord_mirror_message_marks_dev_voice_as_test() -> None:
    message = dev_gateway.format_voice_discord_mirror_message(
        {
            "call_id": "call-voice-1",
            "conversation_id": "voice-c1",
            "transcript": "Искам оператор.",
            "caller_id": "+35970020200",
            "did": "+35924259795",
            "pbx_extension": "399",
            "language": "bg-BG",
            "source": "zycoo-coovox-u20",
            "stt_confidence": 0.96,
        },
        {
            "status": "ok",
            "version": "v-test",
            "contract_version": "skyai-voice-contract.v0.1",
            "call_id": "call-voice-1",
            "conversation_id": "voice-c1",
            "action": "transfer_to_human",
            "spoken_reply": "Свързвам Ви с оператор.",
            "display_reply": "Caller requested handoff.",
            "transfer": {"target": "operator_queue", "reason": "caller_requested_human"},
            "end_call": False,
            "trace": {
                "runtime": "skyai_voice_adapter",
                "backend_target": "skyai_v2_chatkit",
                "raw_audio_stored": False,
            },
        },
        stage="turn",
    )

    assert message.startswith("**🎙️ 🧪 TEST / QA Voice разговор**")
    assert "**Клиент / STT**" in message
    assert "Искам оператор." in message
    assert "**SkyAI / spoken**" in message
    assert "Свързвам Ви с оператор." in message
    assert "pbx_extension=399" in message
    assert "stage=turn" in message
    assert "action=transfer_to_human" in message
    assert "origin_class=test" in message


@pytest.mark.asyncio
async def test_discord_thread_name_marks_voice_threads(tmp_path: Path, monkeypatch) -> None:
    starter_messages: list[str] = []
    created_names: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        starter_messages.append(content)
        return {"id": "starter-message"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        created_names.append(name)
        return {"id": "voice-thread-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    thread_id = await dev_gateway._discord_target_channel_id(
        settings=settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
        conversation_id="voice-call-123",
        request_payload={"pbx_extension": "399"},
        surface="voice",
    )

    assert thread_id == "voice-thread-1"
    assert starter_messages == ["🧪 TEST 🎙️ Voice SkyAI разговор `voice-call-123`"]
    assert created_names == ["🧪 TEST · 🎙️ Voice SkyAI · voice-call-123"]


@pytest.mark.asyncio
async def test_mirror_voice_to_discord_skips_when_disabled(tmp_path: Path) -> None:
    result = await dev_gateway.mirror_voice_to_discord(
        {"call_id": "call-voice", "transcript": "Здравейте"},
        {
            "status": "ok",
            "version": "v-test",
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "action": "speak",
            "spoken_reply": "Здравейте.",
            "display_reply": "Здравейте.",
            "trace": {},
        },
        settings(tmp_path),
        stage="turn",
    )

    assert result == {"status": "skipped", "reason": "disabled"}


@pytest.mark.asyncio
async def test_mirror_voice_to_discord_posts_to_configured_channel(tmp_path: Path, monkeypatch) -> None:
    posted: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted.append((channel_id, content))
        return {"id": "voice-message-1"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    result = await dev_gateway.mirror_voice_to_discord(
        {
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "transcript": "Имате ли свободни часове?",
            "pbx_extension": "399",
        },
        {
            "status": "ok",
            "version": "v-test",
            "call_id": "call-voice",
            "conversation_id": "voice-call",
            "action": "speak",
            "spoken_reply": "Да, проверявам свободните часове.",
            "display_reply": "Да, проверявам свободните часове.",
            "trace": {"raw_audio_stored": False},
        },
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_create_threads=False,
        ),
        stage="turn",
    )

    assert result == {
        "status": "posted",
        "channel_id": "1510888721614901358",
        "message_id": "voice-message-1",
    }
    assert posted[0][0] == "1510888721614901358"
    assert posted[0][1].startswith("**🎙️ 🧪 TEST / QA Voice разговор**")


@pytest.mark.asyncio
async def test_real_only_mirror_rejects_body_origin_class_without_server_provenance(tmp_path: Path, monkeypatch) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        if content.startswith("SkyAI v2 разговор"):
            posted_channels.append(channel_id)
        return {"id": f"starter-{channel_id}"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        return {"id": f"thread-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "real-body-only", "message": "Здравей", "origin_class": "real"},
        {"status": "ok", "conversation_id": "real-body-only", "reply": "Здравей", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
    )

    assert result["status"] == "posted"
    assert result["real_customer_mirror"] == {"status": "skipped", "reason": "untrusted_provenance"}
    assert posted_channels == ["1510888721614901358"]


@pytest.mark.asyncio
async def test_real_only_mirror_accepts_exact_server_observed_production_origin(tmp_path: Path, monkeypatch) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        if content.startswith("SkyAI v2 разговор"):
            posted_channels.append(channel_id)
        return {"id": f"starter-{channel_id}"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        return {"id": f"thread-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)

    result = await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "prod-real-1",
            "message": "Искам подарък",
            "_server_request_provenance": {"origin": "https://www.skyvision.bg", "referer": "https://www.skyvision.bg/podaruk/"},
        },
        {"status": "ok", "conversation_id": "prod-real-1", "reply": "Имаме идеи", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=tmp_path / "threads.json",
        ),
    )

    assert result["status"] == "posted"
    assert result["channel_id"] == "1510888721614901358"
    assert result["real_customer_mirror"]["status"] == "posted"
    assert result["real_customer_mirror"]["channel_id"] == "1531414670844367000"
    assert posted_channels == ["1510888721614901358", "1531414670844367000"]


@pytest.mark.asyncio
async def test_real_only_mirror_test_marker_overrides_production_origin(tmp_path: Path, monkeypatch) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {"id": f"starter-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    result = await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "synthetic-prod-origin",
            "message": "SMOKE",
            "_server_request_provenance": {"origin": "https://skyvision.bg", "referer": "https://skyvision.bg/"},
            "_server_test_signal": "synthetic_smoke",
        },
        {"status": "ok", "conversation_id": "synthetic-prod-origin", "reply": "OK", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=False,
        ),
    )

    assert result["real_customer_mirror"] == {"status": "skipped", "reason": "explicit_test_signal"}
    assert posted_channels == ["1510888721614901358"]


@pytest.mark.parametrize(
    "server_provenance",
    [
        {},
        {"origin": "https://skyvision1.7s2go.com"},
        {"referer": "https://dev.skyvision.bg/widget"},
        {"origin": "http://localhost:8787"},
        {"origin": "https://preview-skyvision.bg"},
        {"origin": "https://evil.example", "referer": "https://skyvision.bg/"},
    ],
)
@pytest.mark.asyncio
async def test_real_only_mirror_fails_closed_for_missing_unknown_dev_canary_or_ambiguous_origin(
    tmp_path: Path,
    monkeypatch,
    server_provenance: dict[str, str],
) -> None:
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {"id": f"message-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    result = await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "closed-provenance",
            "message": "Здравей",
            "_server_request_provenance": server_provenance,
        },
        {"status": "ok", "conversation_id": "closed-provenance", "reply": "Здравей", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=False,
        ),
    )

    assert result["real_customer_mirror"]["status"] == "skipped"
    assert result["real_customer_mirror"]["reason"] in {"missing_provenance", "untrusted_provenance"}
    assert posted_channels == ["1510888721614901358"]


@pytest.mark.asyncio
async def test_discord_thread_state_is_independent_per_destination_channel(tmp_path: Path, monkeypatch) -> None:
    started: list[tuple[str, str]] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        return {"id": f"starter-{channel_id}"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        started.append((channel_id, message_id))
        return {"id": f"thread-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)
    store_path = tmp_path / "threads.json"

    await dev_gateway.mirror_to_discord(
        {
            "conversation_id": "same-conversation",
            "message": "Здравей",
            "_server_request_provenance": {"origin": "https://skyvision.bg"},
        },
        {"status": "ok", "conversation_id": "same-conversation", "reply": "Здравей", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=True,
            discord_mirror_thread_store=store_path,
        ),
    )

    mapping = json.loads(store_path.read_text(encoding="utf-8"))
    assert mapping["chat:1510888721614901358:same-conversation"] == "thread-1510888721614901358"
    assert mapping["chat:1531414670844367000:same-conversation"] == "thread-1531414670844367000"
    assert started == [
        ("1510888721614901358", "starter-1510888721614901358"),
        ("1531414670844367000", "starter-1531414670844367000"),
    ]


@pytest.mark.asyncio
async def test_duplicate_retry_is_idempotent_separately_per_channel(tmp_path: Path, monkeypatch) -> None:
    starter_posts: list[str] = []
    thread_posts: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        if content.startswith("SkyAI v2 разговор"):
            starter_posts.append(channel_id)
        else:
            thread_posts.append(channel_id)
        return {"id": f"message-{channel_id}-{len(starter_posts) + len(thread_posts)}"}

    def fake_start_thread(channel_id: str, message_id: str, token: str, name: str) -> dict[str, str]:
        return {"id": f"thread-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)
    monkeypatch.setattr(dev_gateway, "_discord_start_thread_from_message", fake_start_thread)
    mirror_settings = settings(
        tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id="1510888721614901358",
        discord_mirror_real_customer_channel_id="1531414670844367000",
        discord_mirror_create_threads=True,
        discord_mirror_thread_store=tmp_path / "threads.json",
    )
    payload = {
        "conversation_id": "retry-conversation",
        "message": "Здравей",
        "_server_request_provenance": {"origin": "https://skyvision.bg"},
    }
    response = {"status": "ok", "conversation_id": "retry-conversation", "reply": "Здравей", "trace": {}}

    await dev_gateway.mirror_to_discord(payload, response, mirror_settings)
    await dev_gateway.mirror_to_discord(payload, response, mirror_settings)

    assert starter_posts == ["1510888721614901358", "1531414670844367000"]
    assert thread_posts == [
        "thread-1510888721614901358",
        "thread-1531414670844367000",
        "thread-1510888721614901358",
        "thread-1531414670844367000",
    ]


@pytest.mark.asyncio
async def test_real_only_mirror_does_not_backfill_historical_thread_store(tmp_path: Path, monkeypatch) -> None:
    store_path = tmp_path / "threads.json"
    store_path.write_text(
        json.dumps({"chat:1510888721614901358:historical-conversation": "old-thread"}),
        encoding="utf-8",
    )
    posted_channels: list[str] = []

    def fake_post_message(channel_id: str, token: str, content: str) -> dict[str, str]:
        posted_channels.append(channel_id)
        return {"id": f"message-{channel_id}"}

    monkeypatch.setattr(dev_gateway, "_discord_post_message", fake_post_message)

    result = await dev_gateway.mirror_to_discord(
        {"conversation_id": "new-test-conversation", "message": "SMOKE", "_server_test_signal": "synthetic_smoke"},
        {"status": "ok", "conversation_id": "new-test-conversation", "reply": "OK", "trace": {}},
        settings(
            tmp_path,
            discord_mirror_enabled=True,
            discord_mirror_bot_token="token",
            discord_mirror_channel_id="1510888721614901358",
            discord_mirror_real_customer_channel_id="1531414670844367000",
            discord_mirror_create_threads=False,
            discord_mirror_thread_store=store_path,
        ),
    )

    mapping = json.loads(store_path.read_text(encoding="utf-8"))
    assert result["real_customer_mirror"] == {"status": "skipped", "reason": "explicit_test_signal"}
    assert "chat:1531414670844367000:historical-conversation" not in mapping
    assert posted_channels == ["1510888721614901358"]


@pytest.mark.asyncio
async def test_build_compare_response_runs_dev_and_prod_sides(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return f"DEV: {message}"

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": f"PROD: {payload['message']}",
            "cards": [{"title": "card"}],
            "trace": {"model": "gpt-5.6-sol", "latency_ms": 20},
        }

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["status"] == "ok"
    assert response["dev_v2"]["reply"] == "DEV: Има ли масаж?"
    assert response["prod_current"]["reply"] == "PROD: Има ли масаж?"
    assert response["prod_current"]["cards_count"] == 1
    assert response["cards_compare"]["prod_count"] == 1


@pytest.mark.asyncio
async def test_build_compare_response_compares_card_links_prices_and_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_detail(product_url="", product_path=""):
        return {
            "status": "ok",
            "detail": {
                "title": "Масаж за двама",
                "public_url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                "price_eur": "90.00",
                "location": "София",
                "images": [{"src": "https://cdn.example/massage.jpg"}],
            },
        }

    async def fake_runner(message, history, conversation_id, canary_settings):
        return "Бих предложил https://skyvision.bg/подарък/масаж/масаж-за-двама/"

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": "PROD reply",
            "cards": [
                {
                    "title": "Масаж за двама",
                    "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                    "price_eur": "90.00",
                    "image_url": "https://cdn.example/massage.jpg",
                }
            ],
            "trace": {"model": "gpt-5.6-sol"},
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["dev_v2"]["cards"] == [
        {
            "title": "Масаж за двама",
            "public_url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
            "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
            "price_eur": "90.00",
            "location": "София",
            "image": "https://cdn.example/massage.jpg",
        }
    ]
    assert response["cards_compare"]["shared_urls"] == [
        "https://skyvision.bg/подарък/масаж/масаж-за-двама"
    ]
    assert response["cards_compare"]["shared_titles"] == ["масаж за двама"]
    assert response["cards_compare"]["dev_missing_price_count"] == 0
    assert response["cards_compare"]["prod_missing_image_count"] == 0


@pytest.mark.asyncio
async def test_build_compare_response_requires_prod_base_url(tmp_path: Path) -> None:
    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["error"] == "compare_prod_not_configured"
