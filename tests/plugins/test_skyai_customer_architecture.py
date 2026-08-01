from __future__ import annotations

import json
from pathlib import Path

from plugins.skyai_customer import dev_gateway, public_tools


ARCHITECTURE_PATH = Path("plugins/skyai_customer/ARCHITECTURE.md")
QA_PRINCIPLES_PATH = Path("plugins/skyai_customer/fixtures/qa_behavior_principles.json")
COMPARE_SCENARIOS_PATH = Path("plugins/skyai_customer/fixtures/compare_scenarios.json")


def test_architecture_contract_declares_hermes_led_reasoning() -> None:
    text = ARCHITECTURE_PATH.read_text(encoding="utf-8")
    compact = " ".join(text.split())

    assert "Hermes reasons" in text
    assert "The SkyAI backend and tools provide public facts" in text
    assert "Do not reintroduce keyword routers" in text
    assert "only canonical source" in compact
    assert "historical v1/clean-room archive" in compact
    assert "Keyword classifiers, synthetic response templates" in compact
    assert "wire compatibility only" in compact
    assert "Plugin vs Fork" in text
    assert "Three-Hour Upstream Hermes Update Flow" in text
    assert "must not auto-merge or deploy" in compact


def test_skyai_prompt_is_principle_based_not_script_pack() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "Hermes мисли" in prompt
    assert "не е заповед какво да кажеш" in prompt
    assert len(prompt) < 6000
    assert "SkyAI sales playbook" not in prompt
    assert "do_not_say" not in prompt
    assert "customer_facing_flow" not in prompt
    assert "tone_anchors" not in prompt


def test_skyai_prompt_treats_prior_turns_as_shared_context() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()
    architecture = " ".join(ARCHITECTURE_PATH.read_text(encoding="utf-8").split())

    assert "Историята е общ контекст" in prompt
    assert "Отговаряй само с новото" in prompt
    assert "сравни всяко твърдение и стъпка" in prompt
    assert "ако смисълът вече е даден, изтрий го" in prompt
    assert "последната реплика" in prompt
    assert "Полезността или свързаността не оправдава повторение" in prompt
    assert "поправка/недоволство" in prompt
    assert "поправи само новото" in prompt
    assert "изрично искане или корекция" in prompt
    assert "само нужната част" in prompt
    assert "Conversation history is shared reasoning context" in architecture
    assert "prior information is" in architecture
    assert "presumed known" in architecture
    assert "delta in the latest" in architecture
    assert "summarizing the old answer" in architecture
    assert "compare every claim and next step in its draft" in architecture
    assert "Usefulness or relevance does not justify" in architecture
    assert "prompt-and-evaluation principle" in architecture
    assert "backend deduplication" in architecture
    assert "keyword rule" in architecture


def test_campaign_gift_validity_precedes_transfer_reasoning() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "не извеждай едната дата от другата" in prompt
    assert "историческите условия на конкретната кампания" in prompt
    assert "отделно от ползването на основния ваучер" in prompt
    assert "„Неизползван“ не означава „използваем сега“" in prompt
    assert "само че изтичане е възможно и е нужна проверка" in prompt
    assert "не обявявай подаръка за изтекъл" in prompt
    assert "не предлагай прехвърляне, ръчно изключение или ескалация" in prompt
    assert prompt.index(
        "точната дата на покупката или създаването на entitlement"
    ) < prompt.index(
        "собственост, профил или прехвърляне"
    )


def test_campaign_gift_validity_is_general_evaluation_material() -> None:
    principles = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))
    scenarios = json.loads(COMPARE_SCENARIOS_PATH.read_text(encoding="utf-8"))

    principle = next(
        case for case in principles if case["id"] == "campaign_gift_time_validity"
    )
    assert principle["source_threads"] == ["generalized_campaign_validity_regression"]
    assert "received or gifted" in principle["principle"]
    assert "purchase or entitlement-creation date" in principle["principle"]
    assert "historical campaign terms" in principle["principle"]
    assert "separate from main-voucher use" in principle["principle"]
    assert "Unused is not the same as currently usable" in principle["principle"]
    assert "expiry is possible" in principle["principle"]
    assert "never declare expiry as fact" in principle["principle"]
    assert "before transfer or exception guidance" in principle["principle"]

    scenario = next(
        case for case in scenarios if case["id"] == "campaign_gift_time_validity"
    )
    assert scenario["history"] == [
        {
            "role": "user",
            "content": (
                "Получих основния ваучер като подарък преди няколко години, "
                "а в профила виждам неизползван подарък от кампания."
            ),
        }
    ]
    assert scenario["message"] == (
        "Щом пише „неизползван“, мога ли да го ползвам сега "
        "или да го дам на друг човек?"
    )
    assert "historical campaign terms" in scenario["focus"]
    assert "no expiry claim without evidence" in scenario["focus"]


def test_external_voucher_boundary_is_a_general_hermes_principle() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()
    support = public_tools.handle_skyai_support_knowledge(include_contacts=True)
    cases = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))
    principle = next(case for case in cases if case["id"] == "voucher_issuer_boundary")

    assert "приемай неуточнения ваучер за ваучер на SkyVision" in prompt
    assert "не питай рутинно за издателя" in prompt
    assert "конкретна причина да се съмняваш в съвместимостта" in prompt
    assert "Само ваучерите на SkyVision важат в SkyVision профила" in prompt
    assert "Ако клиентът посочи друг издател" in prompt
    assert "ваучерът не може да се добави тук" in prompt
    assert "при неясен произход първо го уточни" not in prompt

    issuer_scope = support["vouchers"]["issuer_scope"]
    assert issuer_scope["skyvision_issued_vouchers"]["profile_compatible"] is True
    assert issuer_scope["externally_issued_vouchers"]["profile_compatible"] is False
    assert issuer_scope["catalog_overlap_changes_issuer_or_compatibility"] is False
    assert issuer_scope["compatibility_is_issuer_scoped"] is True
    assert "обичайно означава ваучер" in issuer_scope["unspecified_origin_in_skyvision_chat"]
    addition_facts = support["vouchers"]["customer_panel"]["addition_problem_facts"]
    assert "латиница" in addition_facts["serial_format"]
    assert addition_facts["possible_issuer_mismatch"] is True
    assert "особен статус" in " ".join(addition_facts["other_possible_states"])

    assert principle["source_threads"] == ["1530167380133544067", "1530561454191673356"]
    assert "without turning the issuer check into a routine question" in principle["principle"]
    assert "normally be treated as SkyVision-issued" in principle["principle"]
    assert "concrete reason to doubt compatibility" in principle["principle"]
    assert "identifies another platform, seller, or provider" in principle["principle"]


def test_customer_reply_contact_distinguishes_automated_sender() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()
    support = public_tools.handle_skyai_support_knowledge(include_contacts=True)
    cases = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))
    principle = next(case for case in cases if case["id"] == "customer_reply_contact")

    assert "Писмен контакт с екипа: info@skyvision.bg" in prompt
    assert "reservations@skyvision.bg е автоматичен адрес" in prompt
    assert "не канал за клиентски отговори" in prompt

    reservation_contact = support["reservation_support"]
    assert reservation_contact["customer_contact_email"] == "info@skyvision.bg"
    automated = reservation_contact["automated_notification_address"]
    assert automated["email"] == "reservations@skyvision.bg"
    assert automated["role"] == "автоматични известия за резервации"
    assert automated["monitored"] is True
    assert automated["accepts_customer_replies"] is False
    assert support["official_contacts"]["email"] == "info@skyvision.bg"

    assert principle["source_threads"] == ["1530546975835947061"]
    assert "use info@skyvision.bg" in principle["principle"]
    assert "not a customer reply channel" in principle["principle"]


def test_reservation_voucher_path_ambiguity_is_hermes_principle() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()
    architecture = " ".join(ARCHITECTURE_PATH.read_text(encoding="utf-8").split())
    cases = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))
    scenarios = json.loads(COMPARE_SCENARIOS_PATH.read_text(encoding="utf-8"))

    assert "не е ясно дали има/ползва ваучер" in prompt
    assert "Моят ваучер/профил" in prompt
    assert "директен BookNow/карта само без ваучер" in prompt
    assert "Не твърди задължителни UI стъпки" in prompt
    assert "tool/public evidence" in prompt
    assert len(prompt) < 6000

    assert "Reservation path ambiguity is Hermes reasoning context" in architecture
    assert "not a runtime intent router" in architecture
    assert "direct BookNow/card payment only when no voucher is being used" in architecture
    assert "without bounded public facts or tool evidence" in architecture

    principle = next(case for case in cases if case["id"] == "reservation_voucher_path_ambiguity")
    assert principle["source_threads"] == ["real_customer_discord_reservation_voucher_ambiguity"]
    assert "do not assume direct BookNow/card payment" in principle["principle"]
    assert "existing SkyVision voucher" in principle["principle"]
    assert "product reservation voucher option" in principle["principle"]
    assert "buying a voucher only" in principle["principle"]
    assert "unless bounded facts or tools supply them" in principle["principle"]

    scenario = next(case for case in scenarios if case["id"] == "reservation_voucher_path_ambiguity")
    assert "voucher/payment path is unknown" in scenario["focus"]
    assert "do not assume direct BookNow/card payment" in scenario["focus"]


def test_confirmed_reservation_self_cancellation_is_general_principle() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()
    support = public_tools.handle_skyai_support_knowledge(include_contacts=True)
    architecture = " ".join(ARCHITECTURE_PATH.read_text(encoding="utf-8").split())
    cases = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))
    scenarios = json.loads(COMPARE_SCENARIOS_PATH.read_text(encoding="utf-8"))

    assert "потвърдена/предстояща резервация" in prompt
    assert "профил -> Резервации" in prompt
    assert "Анулиране на резервацията" in prompt
    assert "не казвай, че екипът трябва да я анулира" in prompt
    assert "не измисляй универсален срок" in prompt
    assert "след успешно анулиране" in prompt
    assert prompt.index("Анулиране на резервацията") < prompt.index("след успешно анулиране")
    assert "info@skyvision.bg" in prompt

    reservation_support = support["reservation_support"]
    assert reservation_support["customer_profile_section"] == "Профил -> Резервации"
    assert reservation_support["self_service_cancel_action"] == "Анулиране на резервацията"
    assert reservation_support["public_terms_sections"] == ["1.2", "4.1", "17.2", "17.4", "17.5"]
    assert reservation_support["provider_defined_change_conditions"] is True
    assert reservation_support["platform_enforces_cancel_cutoff"] is True
    assert reservation_support["global_cancel_hours"] is None
    assert reservation_support["self_service_cancel_after_cutoff_available"] is False
    assert "reservation/cancel/" in reservation_support["customer_cancel_endpoint_pattern"]
    assert "direct_email" not in reservation_support

    principle = next(case for case in cases if case["id"] == "confirmed_reservation_self_cancellation")
    assert principle["source_threads"] == ["sanitized_qa_1533000762970341406"]
    assert "self-service path first" in principle["principle"]
    assert "Do not invent a universal cancellation window" in principle["principle"]
    assert "only then suggest contacting" in principle["principle"]
    assert "After successful cancellation and voucher release" in principle["principle"]

    scenario_ids = {case["id"] for case in scenarios}
    assert {
        "confirmed_reservation_wants_another_experience",
        "reservation_cancel_cutoff_unknown",
        "reservation_cancel_unavailable_or_deadline_passed",
        "reservation_already_cancelled_voucher_exchange",
        "reservation_self_cancel_reject_team_only",
    } <= scenario_ids
    assert "Confirmed reservation self-cancellation is Hermes reasoning context" in architecture
    assert "not a runtime intent router" in architecture
    assert "provider-defined conditions" in architecture
    assert "no universal cancellation window" in architecture


def test_campaign_tool_returns_fact_pack_not_customer_script() -> None:
    result = public_tools.handle_skyai_campaign_knowledge()
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["tool_contract"]["purpose"] == "public_facts_only"
    assert result["tool_contract"]["reasoning_owner"] == "hermes"
    assert "customer_facing_flow" not in serialized
    assert "do_not_say" not in serialized
    assert "tone_anchors" not in serialized
    assert "sales_tone" not in serialized


def _payload_keys(value) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_payload_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_payload_keys(child))
        return keys
    return set()


def test_customer_tools_return_facts_not_instruction_keys() -> None:
    payloads = [
        public_tools.handle_skyai_campaign_knowledge(),
        public_tools.handle_skyai_support_knowledge(include_contacts=True),
    ]

    for payload in payloads:
        keys = _payload_keys(payload)
        assert "answer_guidance" not in keys
        assert "guidance" not in keys
        assert not any(key.endswith("_guidance") for key in keys)
        assert "when_to_use" not in keys
        assert "customer_facing_flow" not in keys


def test_catalog_tool_has_no_backend_persona_or_keyword_policy() -> None:
    source = Path("plugins/skyai_customer/public_tools.py").read_text(encoding="utf-8")

    assert "_guidance" not in source

    forbidden_symbols = {
        "_CALM_QUERY_TOKENS",
        "_CALM_PRODUCT_SIGNALS",
        "_EXTREME_PRODUCT_SIGNALS",
        "_HIGH_ADRENALINE_PRODUCT_SIGNALS",
        "_NARROW_SPECIFIC_QUERY_SIGNALS",
        "_NEGATABLE_PRODUCT_TOKENS",
        "_query_traits",
        "_product_relevance_score",
        "_location_relevance_score",
        "_recipient_fit_score",
        "_diversify_ranked_products",
        "_prefer_nearby_results_when_available",
        "_catalog_recipient_context",
        "recipient_context",
        "broad_discovery",
        "single_recipient_gift",
    }
    for symbol in forbidden_symbols:
        assert symbol not in source


def test_voice_handoff_has_no_transcript_phrase_list() -> None:
    source = Path("plugins/skyai_customer/dev_gateway.py").read_text(encoding="utf-8")

    assert "VOICE_HUMAN_HANDOFF_TERMS" not in source
    assert "_voice_handoff_requested" not in source
    assert "skyai_voice_transfer_to_human" in source
    assert "voice_action_source" in source


def test_qa_feedback_is_evaluation_material_not_runtime_policy() -> None:
    cases = json.loads(QA_PRINCIPLES_PATH.read_text(encoding="utf-8"))

    assert len(cases) >= 16
    assert {case["id"] for case in cases} >= {
        "bonus_transfer_not_direct_yes",
        "respect_negative_clarification",
        "location_priority",
        "booknow_refund_language",
        "no_keyword_backend_patches",
        "session_context_concision",
        "voucher_issuer_boundary",
        "customer_reply_contact",
    }
    assert all("principle" in case for case in cases)
    assert all("scoring" in case for case in cases)
