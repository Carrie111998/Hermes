"""DeepInfra's vision-default discovery must see Hermes-managed credentials.

Regression for the 2026-08-25 restore: `default_vision_model()` gated on
`os.environ.get("DEEPINFRA_API_KEY")`, but Hermes keeps credentials in
`~/.hermes/.env` and in the auth pool and loads NEITHER into the process
environment. A correctly-installed key therefore produced None here, the
vision chain logged "deepinfra catalog unreachable or returned no
vision-tagged models -- skipping", and vision stayed dead while blaming the
network.

The gate must ask the same resolver that builds the client.
"""

import pytest

from providers import get_provider_profile


@pytest.fixture
def profile():
    p = get_provider_profile("deepinfra")
    assert p is not None, "deepinfra provider profile should be registered"
    return p


def _patch_creds(monkeypatch, api_key):
    """Point the canonical credential resolver at *api_key*."""
    import hermes_cli.auth as auth

    monkeypatch.setattr(
        auth,
        "resolve_api_key_provider_credentials",
        lambda provider_id: {
            "provider": provider_id,
            "api_key": api_key,
            "base_url": "",
            "source": "test",
        },
    )


def _patch_catalog(monkeypatch, items):
    import hermes_cli.models as models

    monkeypatch.setattr(
        models, "_fetch_deepinfra_models_by_tag", lambda tag, **kw: items,
    )


VISION_ITEM = {"id": "vendor/vision-model", "metadata": {"tags": ["chat", "vision"]}}
TEXT_ITEM = {"id": "vendor/text-model", "metadata": {"tags": ["chat"]}}


def test_key_only_in_dotenv_still_unlocks_discovery(profile, monkeypatch):
    """THE REGRESSION: key absent from os.environ, present via the resolver.

    This is exactly the shape of a normal Hermes install. Under the old
    os.environ gate this returned None.
    """
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    _patch_creds(monkeypatch, "di-key-from-dotenv")
    _patch_catalog(monkeypatch, [TEXT_ITEM, VISION_ITEM])

    assert profile.default_vision_model() == "vendor/vision-model"


def test_no_credential_anywhere_skips_the_round_trip(profile, monkeypatch):
    """Negative control -- the gate must still gate.

    Without this, a gate that simply always passed would satisfy the test
    above. Asserts the catalog is never even called.
    """
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    _patch_creds(monkeypatch, "")

    import hermes_cli.models as models

    def explode(tag, **kw):  # pragma: no cover - must not run
        raise AssertionError(
            "catalog must not be fetched when no credential is configured"
        )

    monkeypatch.setattr(models, "_fetch_deepinfra_models_by_tag", explode)

    assert profile.default_vision_model() is None


def test_requires_the_chat_surface_not_just_a_vision_tag(profile, monkeypatch):
    """An image-gen model carrying a `vision` tag is not a chat backend."""
    _patch_creds(monkeypatch, "di-key")
    # The helper is asked for the "chat" surface, so a non-chat model simply
    # never appears; an empty chat list must yield None rather than a guess.
    _patch_catalog(monkeypatch, [TEXT_ITEM])

    assert profile.default_vision_model() is None


def test_credential_resolution_failure_is_not_fatal(profile, monkeypatch):
    """A raising resolver degrades to "no key", never propagates.

    Model discovery runs on every vision availability check; an auth-store
    problem must not turn that into a crash.
    """
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    import hermes_cli.auth as auth

    def boom(provider_id):
        raise RuntimeError("auth store unreadable")

    monkeypatch.setattr(auth, "resolve_api_key_provider_credentials", boom)

    assert profile.default_vision_model() is None
