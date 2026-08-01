"""Tests du profile Mistral — style tests/providers/test_provider_profiles.py.

Usage (depuis ~/.hermes/hermes-agent avec le venv):
    ./venv/bin/python -m pytest ~/.hermes/plugins/model-providers/mistral/tests/ -v

La découverte du registre providers scanne $HERMES_HOME/plugins/model-providers/
au premier get_provider_profile(), donc le plugin utilisateur est chargé
automatiquement — pas besoin d'import manuel.
"""

from providers import get_provider_profile


def _profile():
    p = get_provider_profile("mistral")
    assert p is not None, "profile 'mistral' introuvable — le plugin utilisateur est-il découvert ?"
    return p


class TestMistralDiscovery:
    """Identité du provider (conventions du README model-providers)."""

    def test_registry_lookup(self):
        p = _profile()
        assert p.name == "mistral"
        assert p.display_name == "Mistral AI"
        assert p.signup_url  # console.mistral.ai
        assert p.env_vars == ("MISTRAL_API_KEY",)
        assert "api.mistral.ai" in p.base_url
        assert p.default_aux_model == "mistral-small-latest"

    def test_vision_flags(self):
        p = _profile()
        assert p.supports_vision is True
        assert p.supports_vision_tool_messages is True

    def test_vision_default(self):
        p = _profile()
        assert p.default_vision_model() == "mistral-large-latest"

    def test_fallback_models_are_agentic_latest_aliases(self):
        p = _profile()
        assert p.fallback_models, "fallback_models ne doit pas etre vide"
        # Règle du repo (base.py): seuls des modèles agentic (tool calling)
        # doivent apparaître — les alias -latest sont tous tool-calling-capables.
        for m in p.fallback_models:
            assert m.endswith("latest"), f"{m} n'est pas un alias -latest"
            assert m not in ("mistral-embed", "mistral-moderation-2603")


class TestMistralReasoningMapping:
    """build_api_kwargs_extras — contrat wire-format Mistral."""

    def test_reasoning_disabled_sends_none(self):
        p = _profile()
        eb, tl = p.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="mistral-small-latest")
        assert tl == {"reasoning_effort": "none"}
        assert eb == {}

    def test_positive_effort_is_omitted_not_high(self):
        # Contrat v2: jamais `high` (crash stream Hermes sur content-liste).
        p = _profile()
        for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
            eb, tl = p.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": effort},
                model="mistral-small-latest")
            assert tl == {}, f"effort={effort} ne doit envoyer aucun champ (omis)"
            assert eb == {}

    def test_effort_none_sends_none(self):
        p = _profile()
        for effort in ("none", "false", "disabled"):
            eb, tl = p.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": effort},
                model="mistral-small-latest")
            assert tl == {"reasoning_effort": "none"}

    def test_no_config_omits_everything(self):
        p = _profile()
        for rc in (None, {}):
            eb, tl = p.build_api_kwargs_extras(reasoning_config=rc, model="mistral-small-latest")
            assert tl == {} and eb == {}

    def test_reasoning_families(self):
        # Familles avec capability reasoning — le champ est géré.
        p = _profile()
        for model in ("mistral-small-latest", "mistral-small-2603",
                      "mistral-medium-latest", "mistral-medium-2604", "mistral-medium-3",
                      "magistral-small-latest", "labs-leanstral-1-5", "mistral-vibe-cli-fast"):
            _, tl = p.build_api_kwargs_extras(reasoning_config={"enabled": False}, model=model)
            assert tl == {"reasoning_effort": "none"}, model

    def test_non_reasoning_models_never_get_effort(self):
        # 400 code 3051 sinon — vérifié empiriquement sur codestral-latest.
        p = _profile()
        for model in ("codestral-latest", "codestral-2508", "devstral-latest",
                      "mistral-code-latest", "mistral-large-latest", "mistral-large-2512",
                      "ministral-8b-latest", "ministral-3b-latest", "voxtral-small-latest",
                      "mistral-medium-2505", "mistral-medium-2508"):
            for rc in ({"enabled": False}, {"enabled": True, "effort": "high"}, None):
                eb, tl = p.build_api_kwargs_extras(reasoning_config=rc, model=model)
                assert tl == {}, f"{model} + {rc}: aucun reasoning_effort attendu, got {tl}"
                assert eb == {}

    def test_unknown_or_empty_model_is_safe(self):
        p = _profile()
        for model in (None, "", "future-model-xyz"):
            eb, tl = p.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "high"}, model=model)
            assert tl == {} and eb == {}

    def test_never_emits_think_flag(self):
        # Mistral rejette extra_body.think (422 extra_forbidden).
        p = _profile()
        for rc in ({"enabled": False}, {"enabled": True, "effort": "medium"}, None):
            eb, _ = p.build_api_kwargs_extras(reasoning_config=rc, model="mistral-small-latest")
            assert "think" not in eb and "thinking" not in eb
