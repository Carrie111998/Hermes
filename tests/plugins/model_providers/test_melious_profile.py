"""Unit tests for the Melious provider profile.

Melious serves embedding, image, audio, and guardrail models from the same
``/v1/models`` endpoint as its chat models, and annotates each entry under
``_meta``. The profile leans on that annotation in three places, so most of
these tests feed a synthetic catalog and pin what comes back out.

No live network: every test stubs the profile's fetch seam.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def melious_profile():
    """Resolve the registered Melious profile through the real discovery path."""
    # Importing model_tools triggers plugin discovery, registering the profile.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("melious")
    assert profile is not None, "melious provider profile must be registered"
    # Discovery hands back a module-level singleton, so a cached catalog from an
    # earlier test in the same process would leak into this one. Reached via
    # setattr because the registry is typed to the ProviderProfile base, which
    # doesn't declare the subclass's cache slots.
    setattr(profile, "_catalog_cache", None)
    setattr(profile, "_catalog_fetched_at", 0.0)
    return profile


def _entry(model_id, *, kind="chat", tools=True, modalities=("text",),
           price=(1.0, 1.0), vision_flag=None):
    """Build one ``/v1/models?include_meta=true`` entry."""
    caps = {"streaming": True}
    if tools is not None:
        caps["function_calling"] = tools
    if vision_flag is not None:
        caps["vision"] = vision_flag
    meta = {
        "type": kind,
        "input_modalities": list(modalities),
        "capabilities": caps,
    }
    if price is not None:
        meta["pricing"] = {
            "input_cost_per_million_eur": price[0],
            "output_cost_per_million_eur": price[1],
            "currency": "EUR",
        }
    return {"id": model_id, "object": "model", "owned_by": "melious", "_meta": meta}


# A catalog shaped like the real one: chat models mixed with the other
# surfaces, one chat model that can't call tools, one that doesn't say.
FULL_CATALOG = [
    _entry("cheap-chat", price=(0.03, 0.13)),
    _entry("mid-chat", price=(0.5, 1.5)),
    _entry("vision-chat", modalities=("text", "image"), price=(0.1, 0.2)),
    _entry("pricey-vision", modalities=("text", "image"), price=(2.0, 4.0)),
    _entry("flagged-vision", modalities=("text",), vision_flag=True, price=(5.0, 5.0)),
    _entry("no-tools-chat", tools=False, price=(0.01, 0.01)),
    _entry("unstated-tools-chat", tools=None, price=(0.01, 0.01)),
    _entry("an-embedding", kind="embeddings", price=None),
    _entry("an-image-model", kind="image", price=None),
    _entry("an-audio-model", kind="audio", price=None),
    _entry("a-guardrail", kind="guardrail", price=None),
]


@pytest.fixture
def stub_catalog(monkeypatch):
    """Replace the HTTP seam with a canned payload; return the call counter."""
    def install(items, profile):
        calls = {"n": 0}

        def fake_fetch(*, api_key, base_url, timeout):
            calls["n"] += 1
            return items

        monkeypatch.setattr(profile, "_fetch_catalog", fake_fetch)
        return calls
    return install


class TestMeliousIdentity:
    def test_core_fields(self, melious_profile):
        p = melious_profile
        assert p.name == "melious"
        assert p.api_mode == "chat_completions"
        assert p.auth_type == "api_key"
        assert p.base_url == "https://api.melious.ai/v1"
        assert p.get_hostname() == "api.melious.ai"

    def test_env_var_order(self, melious_profile):
        # API key first, base-URL override last: auth.py / config.py / doctor.py
        # all split this tuple on the _BASE_URL suffix, and doctor sends the
        # first entry as a Bearer token.
        assert melious_profile.env_vars == ("MELIOUS_API_KEY", "MELIOUS_BASE_URL")

    def test_display_metadata_present(self, melious_profile):
        # Non-empty rather than exact wording — the copy is expected to change.
        assert melious_profile.display_name
        assert melious_profile.description
        assert melious_profile.signup_url.startswith("https://")

    def test_no_provider_wide_max_tokens(self, melious_profile):
        # The catalog reports max_output_tokens: null for all but two models,
        # so there is no provider-wide cap to impose.
        assert melious_profile.default_max_tokens is None

    def test_declares_no_attribution_headers(self, melious_profile):
        # Outbound attribution tagging is gated on a user-facing opt-in that
        # doesn't exist yet, so a new provider must not ship one.
        assert melious_profile.default_headers == {}


class TestMeliousAliases:
    @pytest.mark.parametrize("alias", ["melious-ai"])
    def test_alias_resolves_via_registry(self, melious_profile, alias):
        import providers

        resolved = providers.get_provider_profile(alias)
        assert resolved is not None
        assert resolved.name == "melious"

    def test_aliases_declared_on_profile(self, melious_profile):
        assert "melious-ai" in melious_profile.aliases


class TestMeliousCatalogFiltering:
    def test_non_chat_surfaces_are_excluded(self, melious_profile, stub_catalog):
        stub_catalog(FULL_CATALOG, melious_profile)
        got = melious_profile.fetch_models(api_key="k")

        for excluded in (
            "an-embedding", "an-image-model", "an-audio-model", "a-guardrail",
        ):
            assert excluded not in got, (
                f"{excluded} is not a chat model and must not reach the picker"
            )

    def test_only_explicit_tool_callers_survive(self, melious_profile, stub_catalog):
        stub_catalog(FULL_CATALOG, melious_profile)
        got = melious_profile.fetch_models(api_key="k")

        assert "cheap-chat" in got
        assert "no-tools-chat" not in got, "function_calling=false must be dropped"
        assert "unstated-tools-chat" not in got, (
            "an unstated capability must be treated as absent, not assumed"
        )

    def test_widens_when_nothing_advertises_tools(self, melious_profile, stub_catalog):
        """A metadata regression must not empty the picker."""
        catalog = [
            _entry("chat-a", tools=None),
            _entry("chat-b", tools=None),
            _entry("an-embedding", kind="embeddings"),
        ]
        stub_catalog(catalog, melious_profile)
        got = melious_profile.fetch_models(api_key="k")

        assert got == ["chat-a", "chat-b"], (
            "with no tool-calling annotation anywhere, fall back to all chat "
            "models rather than returning nothing"
        )

    def test_unannotated_entries_are_kept(self, melious_profile, stub_catalog):
        """No ``_meta`` at all → keep the model.

        If the annotation is dropped upstream, an unfiltered list beats an
        empty one.
        """
        stub_catalog([{"id": "bare-model"}], melious_profile)
        assert melious_profile.fetch_models(api_key="k") == ["bare-model"]

    def test_catalog_is_cached_across_calls(self, melious_profile, stub_catalog):
        calls = stub_catalog(FULL_CATALOG, melious_profile)

        first = melious_profile.fetch_models(api_key="k")
        second = melious_profile.fetch_models(api_key="k")

        assert first == second
        assert calls["n"] == 1, (
            "aux and vision resolution run on synchronous agent paths — the "
            "catalog must not be refetched per call"
        )

    def test_cold_start_is_single_flight(self, melious_profile, monkeypatch):
        """Concurrent cold callers must produce ONE upstream fetch, not N.

        Parallel subagents each resolve an aux and a vision model as they
        start. An earlier version released the lock before fetching, which
        turned a 24-thread cold start into 24 ``/v1/models`` requests.
        """
        import threading
        import time

        fetches = []
        counter_lock = threading.Lock()

        def slow_fetch(*, api_key, base_url, timeout):
            with counter_lock:
                fetches.append(1)
            time.sleep(0.05)  # widen the race window
            return [_entry("only-model")]

        monkeypatch.setattr(melious_profile, "_fetch_catalog", slow_fetch)

        results: list[object] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(melious_profile.fetch_models(api_key="k"))
            )
            for _ in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(fetches) == 1, f"expected one upstream fetch, got {len(fetches)}"
        assert results and all(r == ["only-model"] for r in results)

    def test_fetch_failure_serves_the_stale_catalog(self, melious_profile, monkeypatch):
        """A failed refresh must not evict a good entry.

        Caching the failure would push the next caller onto the generic
        unfiltered path, so a transient blip would briefly repopulate the
        picker with embedding and image models.
        """
        monkeypatch.setattr(
            melious_profile, "_fetch_catalog",
            lambda **kw: [_entry("good-model")],
        )
        assert melious_profile.fetch_models(api_key="k") == ["good-model"]

        # Expire the TTL but keep the cached value, then fail the refresh.
        setattr(melious_profile, "_catalog_fetched_at", 0.0)
        monkeypatch.setattr(melious_profile, "_fetch_catalog", lambda **kw: None)

        assert melious_profile.fetch_models(api_key="k") == ["good-model"]

    def test_custom_base_url_uses_the_generic_path(self, melious_profile, monkeypatch):
        """A user-pointed proxy need not implement ?include_meta=true."""
        called = {}

        def fake_super(self, *, api_key=None, base_url=None, timeout=8.0):
            called["base_url"] = base_url
            return ["proxy-model"]

        from providers.base import ProviderProfile

        monkeypatch.setattr(ProviderProfile, "fetch_models", fake_super)
        monkeypatch.setattr(
            melious_profile, "_fetch_catalog",
            lambda **kw: pytest.fail("annotated catalog must not be probed"),
        )

        got = melious_profile.fetch_models(
            api_key="k", base_url="https://proxy.example.com/v1"
        )
        assert got == ["proxy-model"]
        assert called["base_url"] == "https://proxy.example.com/v1"

    def test_default_base_url_still_uses_the_annotated_path(
        self, melious_profile, stub_catalog
    ):
        """Callers pass base_url unconditionally, so equality means 'not customised'."""
        calls = stub_catalog(FULL_CATALOG, melious_profile)
        got = melious_profile.fetch_models(
            api_key="k", base_url=melious_profile.base_url
        )
        assert calls["n"] == 1
        assert "an-embedding" not in got

    def test_returns_none_when_unauthenticated_and_offline(
        self, melious_profile, monkeypatch
    ):
        from providers.base import ProviderProfile

        monkeypatch.setattr(
            ProviderProfile, "fetch_models",
            lambda self, **kw: None,
        )
        monkeypatch.setattr(melious_profile, "_fetch_catalog", lambda **kw: None)
        assert melious_profile.fetch_models(api_key=None) is None


class TestMeliousAuxModel:
    def test_picks_the_cheapest_tool_caller(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog(FULL_CATALOG, melious_profile)

        assert melious_profile.resolve_aux_model() == "cheap-chat"

    def test_skips_cheaper_models_that_cannot_call_tools(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog(FULL_CATALOG, melious_profile)

        # no-tools-chat is the cheapest entry in the catalog outright.
        assert melious_profile.resolve_aux_model() != "no-tools-chat"

    def test_no_key_means_no_request(self, melious_profile, monkeypatch):
        monkeypatch.delenv("MELIOUS_API_KEY", raising=False)
        monkeypatch.setattr(
            melious_profile, "_fetch_catalog",
            lambda **kw: pytest.fail("must not probe without a key"),
        )
        assert melious_profile.resolve_aux_model() == ""

    def test_never_raises(self, melious_profile, monkeypatch):
        """Contract from ProviderProfile: return "" rather than propagating."""
        monkeypatch.setenv("MELIOUS_API_KEY", "k")

        def boom(**kwargs):
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr(melious_profile, "_fetch_catalog", boom)
        assert melious_profile.resolve_aux_model() == ""

    def test_static_default_is_a_declared_fallback(self, melious_profile):
        """The hardcoded floor under the live hook must be a real fallback id."""
        assert melious_profile.default_aux_model
        assert melious_profile.default_aux_model in melious_profile.fallback_models


class TestMeliousVisionModel:
    def test_prefers_cheapest_image_capable_tool_caller(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog(FULL_CATALOG, melious_profile)

        # cheap-chat is cheaper but text-only; pricey-vision is image-capable
        # but dearer. flagged-vision carries capabilities.vision without an
        # image modality and is dearer still.
        assert melious_profile.default_vision_model() == "vision-chat"

    def test_modality_list_is_enough_without_the_capability_flag(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        """The live catalog sets capabilities.vision on only a few of the
        models that actually accept images, so the modality list has to be
        sufficient on its own."""
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog(
            [_entry("img-only-modality", modalities=("text", "image"), price=(1.0, 1.0))],
            melious_profile,
        )
        assert melious_profile.default_vision_model() == "img-only-modality"

    def test_capability_flag_alone_also_counts(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog(
            [_entry("flag-only", modalities=("text",), vision_flag=True)],
            melious_profile,
        )
        assert melious_profile.default_vision_model() == "flag-only"

    def test_none_when_no_vision_model_qualifies(
        self, melious_profile, stub_catalog, monkeypatch
    ):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")
        stub_catalog([_entry("text-only")], melious_profile)
        assert melious_profile.default_vision_model() is None

    def test_no_key_means_no_request(self, melious_profile, monkeypatch):
        monkeypatch.delenv("MELIOUS_API_KEY", raising=False)
        monkeypatch.setattr(
            melious_profile, "_fetch_catalog",
            lambda **kw: pytest.fail("must not probe without a key"),
        )
        assert melious_profile.default_vision_model() is None

    def test_never_raises(self, melious_profile, monkeypatch):
        monkeypatch.setenv("MELIOUS_API_KEY", "k")

        def boom(**kwargs):
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr(melious_profile, "_fetch_catalog", boom)
        assert melious_profile.default_vision_model() is None

    def test_vision_default_is_needed_because_aux_is_text_only(
        self, melious_profile
    ):
        """Guards the reason the hook exists.

        ``default_aux_model`` is a Hermes 4 model and that family is text-only,
        so vision side tasks cannot reuse the aux default. If someone later
        points ``default_aux_model`` at a multimodal model this assertion
        should be revisited, not deleted.
        """
        assert melious_profile.default_aux_model.startswith("hermes-4")


class TestMeliousFallbackModels:
    def test_present_and_ordered(self, melious_profile):
        # Entry [0] is the setup default a fresh user lands on.
        assert melious_profile.fallback_models
        assert melious_profile.fallback_models[0] == "hermes-4-405b"

    def test_no_duplicates(self, melious_profile):
        models = melious_profile.fallback_models
        assert len(set(models)) == len(models)

    def test_no_non_chat_surfaces_leaked_in(self, melious_profile):
        """Curated list must not name an embedding/image/audio model.

        Checks the families Melious serves on those surfaces rather than a
        frozen id list, so it keeps working as the catalog moves.
        """
        for model in melious_profile.fallback_models:
            lowered = model.lower()
            for marker in ("bge-", "flux-", "whisper", "e5-", "guard", "voxtral"):
                assert marker not in lowered, (
                    f"{model} looks like a non-chat model"
                )


class TestMeliousVisionDeclaration:
    def test_declares_tool_result_image_support(self, melious_profile):
        """Gates the native vision fast path in tools/vision_tools.py.

        Verified against the live API before being set: an image part inside a
        role="tool" message is accepted and read.
        """
        assert melious_profile.supports_vision is True

    def test_list_type_tool_content_stays_enabled(self, melious_profile):
        """Individual models that reject list-type tool content are handled at
        runtime by run_agent.py's per-model learning, so the provider-wide
        switch must stay on — turning it off would downgrade every Melious
        vision model to a text summary.
        """
        assert melious_profile.supports_vision_tool_messages is True
