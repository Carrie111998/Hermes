from __future__ import annotations

import json
from pathlib import Path

from plugins.skyai_customer import dev_gateway, public_tools


ARCHITECTURE_PATH = Path("plugins/skyai_customer/ARCHITECTURE.md")
QA_PRINCIPLES_PATH = Path("plugins/skyai_customer/fixtures/qa_behavior_principles.json")


def test_architecture_contract_declares_hermes_led_reasoning() -> None:
    text = ARCHITECTURE_PATH.read_text(encoding="utf-8")

    assert "Hermes reasons" in text
    assert "The SkyAI backend and tools provide public facts" in text
    assert "Do not reintroduce keyword routers" in text
    assert "Plugin vs Fork" in text
    assert "Daily Upstream Hermes Update Flow" in text


def test_skyai_prompt_is_principle_based_not_script_pack() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "Hermes мисли" in prompt
    assert "не е заповед какво да кажеш" in prompt
    assert len(prompt) < 5000
    assert "SkyAI sales playbook" not in prompt
    assert "do_not_say" not in prompt
    assert "customer_facing_flow" not in prompt
    assert "tone_anchors" not in prompt


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
    }
    assert all("principle" in case for case in cases)
    assert all("scoring" in case for case in cases)
