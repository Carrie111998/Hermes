"""Regression tests for auxiliary vision fallback capability checks."""

from agent import auxiliary_client as aux


def test_main_agent_fallback_skips_text_only_model_for_vision(monkeypatch):
    monkeypatch.setattr(aux, "_read_main_provider", lambda: "opencode-go")
    monkeypatch.setattr(aux, "_read_main_model", lambda: "deepseek-v4-flash")
    monkeypatch.setattr(aux, "_main_model_supports_vision", lambda provider, model: False)
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda provider: False)
    monkeypatch.setattr(
        aux,
        "resolve_provider_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("text-only model was resolved")),
    )

    client, model, label = aux._try_main_agent_model_fallback(
        "openrouter", task="vision", reason="rate limit", failed_model="vision-model"
    )

    assert (client, model, label) == (None, None, "")
