"""Exact approval bytes and ordinary chat egress have distinct contracts."""

import pytest

from gateway.config import Platform
from gateway.run import (
    _format_exec_approval_fallback,
    _sanitize_gateway_final_response,
)


def test_exact_approval_prompt_preserves_every_input_byte(monkeypatch):
    raw = "printf '%s' 'OPENAI_API_KEY=sk-proj-" + ("X" * 400) + "'"
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda _text, *, force: "SHOULD-NOT-BE-USED",
    )

    prompt = _format_exec_approval_fallback(
        raw,
        "Approve these exact bytes once",
        "/",
        approval_id="a" * 32,
    )

    assert raw in prompt
    assert raw[-120:] in prompt
    assert "..." not in prompt
    assert "SHOULD-NOT-BE-USED" not in prompt
    assert "`/approve " + ("a" * 32) + "`" in prompt
    assert "`/deny " + ("a" * 32) + "`" in prompt
    assert "approve session" not in prompt
    assert "approve always" not in prompt


@pytest.mark.parametrize("approval_id", ["", "approve", "a" * 31, "a" * 33])
def test_approval_prompt_rejects_every_non_opaque_identifier(approval_id):
    with pytest.raises(ValueError, match="opaque approval ID"):
        _format_exec_approval_fallback(
            "printf exact-bytes",
            "exact operation",
            "/",
            approval_id=approval_id,
        )


def test_normal_human_chat_egress_preserves_model_authored_bytes(monkeypatch):
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    raw = "example credential-shaped prose: sk-proj-" + ("X" * 40)
    assert _sanitize_gateway_final_response(Platform.DISCORD, raw) == raw


def test_programmatic_surfaces_keep_raw_contract(monkeypatch):
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    assert (
        _sanitize_gateway_final_response(Platform.API_SERVER, "raw protocol payload")
        == "raw protocol payload"
    )
