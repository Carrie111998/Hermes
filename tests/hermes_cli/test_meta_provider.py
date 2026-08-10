"""Meta Model API provider wiring tests.

Asserts the bundled ``meta`` provider profile registers with the correct
wire protocol (Responses API — the only surface that engages Muse prompt
caching), endpoint, env var, and fallback model list. No network calls.
"""

from hermes_cli.providers import determine_api_mode
from providers import get_provider_profile


def _meta_profile():
    profile = get_provider_profile("meta")
    assert profile is not None, "meta provider profile must be registered"
    return profile


def test_meta_profile_registered():
    profile = _meta_profile()
    assert profile.name == "meta"


def test_meta_profile_wire_and_endpoint():
    profile = _meta_profile()
    assert profile.api_mode == "codex_responses"
    assert profile.base_url == "https://api.meta.ai/v1"


def test_meta_profile_env_and_auth():
    profile = _meta_profile()
    assert profile.env_vars == ("META_API_KEY",)
    assert profile.auth_type == "api_key"


def test_meta_fallback_models():
    profile = _meta_profile()
    assert "muse-spark-1.2-contributor" in profile.fallback_models
    assert "muse-spark-1.2" in profile.fallback_models


def test_meta_determine_api_mode_responses():
    # The profile's transport must resolve to codex_responses independent of
    # the host-mandate fallback — both lanes agree on the Responses wire.
    assert determine_api_mode("meta", "https://api.meta.ai/v1") == "codex_responses"


def test_meta_aliases():
    profile = _meta_profile()
    for alias in ("muse", "meta-ai", "meta-model-api"):
        assert alias in profile.aliases
