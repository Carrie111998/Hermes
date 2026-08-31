"""Behavior contracts for delegation model evidence (#98934)."""

from types import SimpleNamespace

from tools.delegation_model_evidence import (
    format_model_evidence,
    make_model_evidence,
    record_actual_response,
)


def test_runtime_model_without_provider_does_not_claim_actual_provider():
    evidence = make_model_evidence(
        requested_provider="openrouter",
        requested_model="requested/model",
        resolved_provider="openrouter",
        resolved_model="resolved/model",
        selection_source="task",
    )

    record_actual_response(evidence, SimpleNamespace(model="actual/model"))

    assert evidence["actual"] == {
        "provider": "not-reported",
        "model": "actual/model",
    }
    assert evidence["substitution"] == {
        "model": {"resolved": "resolved/model", "actual": "actual/model"}
    }


def test_explicit_runtime_provider_and_model_are_recorded():
    evidence = make_model_evidence(
        requested_provider="openrouter",
        requested_model="requested/model",
        resolved_provider="openrouter",
        resolved_model="resolved/model",
        selection_source="delegation_config",
    )

    record_actual_response(
        evidence,
        SimpleNamespace(
            model="actual/model",
            model_extra={"provider": "anthropic"},
        ),
    )

    assert evidence["actual"] == {
        "provider": "anthropic",
        "model": "actual/model",
    }
    assert evidence["substitution"] == {
        "provider": {"resolved": "openrouter", "actual": "anthropic"},
        "model": {"resolved": "resolved/model", "actual": "actual/model"},
    }


def test_credential_shaped_identifiers_are_withheld_everywhere():
    secret_provider = "sk-abcdefghijklmnopqrstuvwxyz"
    secret_model = "ghp_abcdefghijklmnopqrstuvwxyz"
    evidence = make_model_evidence(
        requested_provider=secret_provider,
        requested_model=secret_model,
        resolved_provider=secret_provider,
        resolved_model=secret_model,
        selection_source="task",
    )
    record_actual_response(
        evidence,
        SimpleNamespace(model=secret_model, provider=secret_provider),
    )

    rendered = format_model_evidence(evidence)
    assert secret_provider not in repr(evidence)
    assert secret_model not in repr(evidence)
    assert secret_provider not in rendered
    assert secret_model not in rendered
    assert "redacted" in rendered


def test_missing_evidence_is_explicit_and_never_renders_question_mark():
    evidence = make_model_evidence(
        requested_provider=None,
        requested_model=None,
        resolved_provider=None,
        resolved_model=None,
        selection_source="parent",
    )

    rendered = format_model_evidence(evidence)

    assert evidence["actual"] == {
        "provider": "not-reported",
        "model": "not-reported",
    }
    assert "not-reported" in rendered
    assert "?" not in rendered
