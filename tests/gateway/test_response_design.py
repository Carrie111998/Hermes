"""Behavior contract for structured mobile response design."""

from gateway.response_design import build_response_design_prompt


def test_structured_mobile_prompt_is_stable_and_actionable():
    first = build_response_design_prompt("telegram", "structured")
    second = build_response_design_prompt("telegram", "structured")

    assert first == second
    assert "## Mobile response design" in first
    assert "Problem / Risk / My advice" in first
    assert "Use the user's language" in first
    assert "numbered steps" in first
    assert "blank line" in first
    assert "commands, paths, identifiers, and values exactly" in first
    assert "Do not inflate a short answer" in first
    assert "at most one semantic emoji" in first


def test_structured_prompt_supports_whatsapp():
    prompt = build_response_design_prompt("whatsapp", "structured")

    assert "WhatsApp" in prompt
    assert "platform-supported Markdown" in prompt


def test_response_design_off_returns_no_prompt_block():
    assert build_response_design_prompt("telegram", "off") == ""


def test_unsupported_platform_is_not_enabled_by_default():
    assert build_response_design_prompt("email", "structured") == ""
