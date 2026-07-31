"""Tests for the Groq provider profile."""
from providers import get_provider_profile


class TestGroqProfile:
    def test_discovery(self):
        p = get_provider_profile("groq")
        assert p is not None
        assert p.name == "groq"

    def test_base_url(self):
        p = get_provider_profile("groq")
        assert "groq.com" in p.base_url

    def test_env_vars(self):
        p = get_provider_profile("groq")
        assert "GROQ_API_KEY" in p.env_vars

    def test_disabled_reasoning_sends_none(self):
        """Groq accepts only 'none' or 'default' for reasoning_effort."""
        p = get_provider_profile("groq")
        eb, tl = p.build_api_kwargs_extras(reasoning_config={"enabled": False})
        assert "think" not in eb, "extra_body must not contain 'think'"
        assert "reasoning" not in eb, "extra_body must not contain 'reasoning'"
        assert tl["reasoning_effort"] == "none"

    def test_effort_none_explicit(self):
        p = get_provider_profile("groq")
        eb, tl = p.build_api_kwargs_extras(reasoning_config={"effort": "none"})
        assert tl["reasoning_effort"] == "none"
        assert eb == {}

    def test_enabled_with_effort_sends_default(self):
        """Groq only supports 'none' or 'default' — map any effort to 'default'."""
        p = get_provider_profile("groq")
        for effort in ("low", "medium", "high", "max", "ultra"):
            eb, tl = p.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": effort}
            )
            assert tl["reasoning_effort"] == "default", f"effort={effort}"
            assert eb == {}, f"extra_body must be empty for effort={effort}"

    def test_enabled_no_effort_omits_field(self):
        """When enabled but no effort set, omit so server default applies."""
        p = get_provider_profile("groq")
        eb, tl = p.build_api_kwargs_extras(reasoning_config={"enabled": True})
        assert "reasoning_effort" not in tl
        assert eb == {}

    def test_none_config_is_noop(self):
        p = get_provider_profile("groq")
        eb, tl = p.build_api_kwargs_extras(reasoning_config=None)
        assert eb == {}
        assert tl == {}
