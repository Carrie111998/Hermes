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


# ---------------------------------------------------------------------------
# Preference-ordered selection (2026-08-25)
#
# Discovery used to return the FIRST vision-tagged chat model in the catalog.
# DeepInfra serves that catalog in its own popularity/recency ranking, so the
# choice was a moving target: a reorder upstream silently repointed every
# Hermes vision call, and nothing logged which model had been picked. The
# incumbent first-slot model was also a reasoning model -- ~8x the price,
# 19-35s instead of 1.5-2.7s, and an empty answer (finish_reason "length",
# no error) at a tight max_tokens.
#
# Selection is now preference-ordered with catalog order as the fallback, and
# the chosen model is logged once.
#
# These tests INJECT a sentinel preference tuple rather than naming the live
# model, so they exercise the mechanism and survive a routing change. The
# actual roster is asserted in exactly one place
# (test_preference_roster_pins_the_expected_vision_model) -- this same file's
# neighbours were rewritten three times in three commits for being coupled to
# the live provider roster.
# ---------------------------------------------------------------------------

import logging
import sys

PREFERRED = "vendor/preferred-vision-model"


@pytest.fixture
def plugin(profile):
    """The DeepInfra plugin module.

    Reached through the registered profile because the package directory is
    ``plugins/model-providers`` (hyphenated): the loader publishes it under
    the synthetic name ``plugins.model_providers.deepinfra``, which is not
    importable by a plain import statement.
    """
    return sys.modules[type(profile).__module__]


@pytest.fixture
def sentinel_preference(plugin, monkeypatch):
    """Inject a fake preference roster and clear the once-per-process guard.

    ``raising=False`` deliberately: on an implementation with no preference
    mechanism these simply become attributes nobody reads, so the tests below
    fail on the BEHAVIOUR (catalog order won, nothing was logged) instead of
    erroring on a missing name. A red that names the defect beats a red that
    names an AttributeError.
    """
    monkeypatch.setattr(
        plugin, "_VISION_MODEL_PREFERENCE", (PREFERRED,), raising=False
    )
    monkeypatch.setattr(
        plugin, "_last_reported_vision_model", None, raising=False
    )
    return plugin


def _item(model_id, tags=("chat", "vision")):
    return {"id": model_id, "metadata": {"tags": list(tags)}}


def test_preference_roster_pins_the_expected_vision_model(plugin):
    """The one place the live roster is asserted.

    Changing the vision default is a deliberate routing/cost decision, so it
    should have to touch a test that says so -- but only THIS one.
    """
    assert getattr(plugin, "_VISION_MODEL_PREFERENCE", None) == (
        "Qwen/Qwen3-VL-235B-A22B-Instruct",
    )


def test_preference_wins_over_catalog_order(profile, sentinel_preference, monkeypatch):
    """THE REGRESSION: catalog order must not decide the vision backend.

    The preferred model is placed LAST so catalog order and preference order
    disagree; the old first-match implementation returns the decoy.
    """
    _patch_creds(monkeypatch, "di-key")
    _patch_catalog(monkeypatch, [
        _item("vendor/decoy-reasoning-model"),
        TEXT_ITEM,
        _item(PREFERRED),
    ])

    assert profile.default_vision_model() == PREFERRED


def test_falls_back_to_catalog_order_when_preference_is_retired(
    profile, sentinel_preference, monkeypatch
):
    """A retired/renamed preferred id must degrade, never kill vision.

    Without this, pinning would turn an upstream retirement into a total
    vision outage instead of a downgrade.
    """
    _patch_creds(monkeypatch, "di-key")
    _patch_catalog(monkeypatch, [TEXT_ITEM, _item("vendor/some-other-vision-model")])

    assert profile.default_vision_model() == "vendor/some-other-vision-model"


def test_match_is_case_insensitive_but_returns_catalog_spelling(
    profile, sentinel_preference, monkeypatch
):
    """The returned string is sent to the API, so it must be DeepInfra's id.

    Guards a plausible bug: matching case-insensitively and then returning
    the preference tuple's own spelling, which would 404 upstream.
    """
    catalog_spelling = PREFERRED.upper()
    _patch_creds(monkeypatch, "di-key")
    _patch_catalog(monkeypatch, [_item(catalog_spelling)])

    result = profile.default_vision_model()
    assert result == catalog_spelling
    assert result != PREFERRED, "must not echo the preference tuple's spelling"


def test_selection_is_logged_once_naming_the_model(
    profile, sentinel_preference, monkeypatch, caplog
):
    """Drift is only attributable if the chosen model appears in the log."""
    _patch_creds(monkeypatch, "di-key")
    _patch_catalog(monkeypatch, [_item(PREFERRED)])

    with caplog.at_level(logging.INFO, logger=sentinel_preference.__name__):
        assert profile.default_vision_model() == PREFERRED
        named = [r for r in caplog.records if PREFERRED in r.getMessage()]
        assert len(named) == 1, "expected exactly one selection line"
        assert "preferred" in named[0].getMessage()

        # Discovery re-runs on every availability check; the log must not.
        caplog.clear()
        profile.default_vision_model()
        assert not caplog.records, "unchanged selection must not re-log"


def test_a_changed_selection_logs_again(
    profile, sentinel_preference, monkeypatch, caplog
):
    """Silent drift is the whole point -- a CHANGED answer must be visible."""
    _patch_creds(monkeypatch, "di-key")
    _patch_catalog(monkeypatch, [_item(PREFERRED)])

    with caplog.at_level(logging.INFO, logger=sentinel_preference.__name__):
        assert profile.default_vision_model() == PREFERRED
        caplog.clear()

        # Upstream retires the preferred model mid-process.
        _patch_catalog(monkeypatch, [_item("vendor/replacement-model")])
        assert profile.default_vision_model() == "vendor/replacement-model"

        messages = [r.getMessage() for r in caplog.records]
        assert any("vendor/replacement-model" in m for m in messages)
        assert any("catalog order" in m for m in messages), (
            "fallback selection must say it was NOT the preferred model"
        )
