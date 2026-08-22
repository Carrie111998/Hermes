"""Tests for inventory._apply_pricing — the pricing/tier enrichment that

feeds the desktop GUI model picker (and onboarding) so it can show $/Mtok
columns + Free/Pro badges and gate paid models on free Nous accounts, the
same way the `hermes model` CLI picker does.
"""

from threading import Event
from time import monotonic

import hermes_cli.inventory as inv
import hermes_cli.models as models_mod


def _patch_pricing(monkeypatch, *, free_tier, pricing, unavailable=None):
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda slug, **kw: pricing.get(slug, {}))
    monkeypatch.setattr(models_mod, "check_nous_free_tier", lambda *, force_fresh=False: free_tier)
    monkeypatch.setattr(
        models_mod, "partition_nous_models_by_tier",
        lambda ids, pr, free_tier: (
            [m for m in ids if m not in (unavailable or [])],
            list(unavailable or []),
        ),
    )


def test_apply_pricing_formats_per_model_prices(monkeypatch):
    """Each model gets formatted input/output/cache + a free flag."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "openrouter": {
                "a/paid": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                "b/free": {"prompt": "0", "completion": "0"},
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/paid", "b/free"]}]
    inv._apply_pricing(rows)

    pricing = rows[0]["pricing"]
    assert pricing["a/paid"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}
    assert pricing["b/free"]["free"] is True
    assert pricing["b/free"]["input"] == "free"


def test_apply_pricing_free_models_get_flat_100_percent_sale(monkeypatch):
    """Free models show -100% chrome; was_* only when original was served."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "nous": {
                "a/free": {
                    "prompt": "0",
                    "completion": "0",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
                "b/natively-free": {
                    "prompt": "0",
                    "completion": "0",
                },
            }
        },
    )
    rows = [{"slug": "nous", "models": ["a/free", "b/natively-free"]}]
    inv._apply_pricing(rows)
    free = rows[0]["pricing"]["a/free"]
    assert free["free"] is True
    assert free["discount_percent"] == 100
    assert free["was_input"] == "$2.00"
    assert free["was_output"] == "$10.00"
    native = rows[0]["pricing"]["b/natively-free"]
    assert native["free"] is True
    assert native["discount_percent"] == 100
    # No gateway original → no fabricated was prices.
    assert "was_input" not in native
    assert "was_output" not in native


def test_apply_pricing_omits_sale_when_original_not_cheaper(monkeypatch):
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "nous": {
                "a/eq": {
                    "prompt": "0.000002",
                    "completion": "0.00001",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
            }
        },
    )
    rows = [{"slug": "nous", "models": ["a/eq"]}]
    inv._apply_pricing(rows)
    assert "discount_percent" not in rows[0]["pricing"]["a/eq"]


def test_model_options_cold_pricing_fetch_runs_off_the_request_path(monkeypatch):
    """A cold pricing endpoint must not delay the first picker payload."""
    fetch_started = Event()
    release_fetch = Event()

    def fake_pricing(_slug, *, force_refresh=False, cached_only=False):
        if cached_only:
            return {}
        fetch_started.set()
        release_fetch.wait(timeout=5)
        return {}

    row = {
        "slug": "openrouter",
        "name": "OpenRouter",
        "models": ["vendor/model"],
        "total_models": 1,
        "is_current": True,
        "is_user_defined": False,
        "source": "built-in",
    }
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", fake_pricing)
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [row],
    )
    monkeypatch.setattr(inv, "_moa_provider_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inv, "_apply_capabilities", lambda _rows: None)
    monkeypatch.setattr(inv, "_apply_featured", lambda _rows: None)
    monkeypatch.setattr(inv, "_pricing_prewarm_threads", {})

    try:
        started_at = monotonic()
        payload = inv.build_model_options_payload(
            inv.ConfigContext(
                current_provider="openrouter",
                current_model="vendor/model",
                current_base_url="",
                user_providers={},
                custom_providers=[],
            )
        )
        elapsed = monotonic() - started_at
        assert payload["providers"][0]["slug"] == "openrouter"
        assert "pricing" not in payload["providers"][0]
        assert elapsed < 2.0, f"cold picker blocked for {elapsed:.2f}s"
        assert fetch_started.wait(timeout=1), "pricing should prewarm in the background"
    finally:
        release_fetch.set()
        for thread in inv._pricing_prewarm_threads.values():
            thread.join(timeout=2)


def test_cold_nous_entitlement_keeps_models_unselectable(monkeypatch):
    """A cold nonblocking response must not expose paid models fail-open."""
    monkeypatch.setattr(
        models_mod, "get_pricing_for_provider", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(models_mod, "get_cached_nous_free_tier", lambda: None)
    rows = [{"slug": "nous", "models": ["free/model", "paid/model"]}]

    inv._apply_pricing(rows, cached_only=True)

    assert rows[0]["free_tier_pending"] is True
    assert rows[0]["unavailable_models"] == ["free/model", "paid/model"]


def test_prewarm_preserves_context_and_runs_once_per_profile(tmp_path, monkeypatch):
    """Concurrent multiplex profiles retain their own home and secret scope."""
    from agent.secret_scope import (
        current_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import (
        hermes_home_key,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.setattr(inv, "_pricing_prewarm_threads", {})
    release = Event()
    started = {"a": Event(), "b": Event()}
    observed = {}

    def capture_context(_rows):
        scope = current_secret_scope()
        label = scope["PROFILE_MARKER"]
        observed[label] = (hermes_home_key(), dict(scope))
        started[label].set()
        release.wait(timeout=5)

    monkeypatch.setattr(inv, "_apply_pricing", capture_context)

    threads = []
    try:
        for label in ("a", "b"):
            home = tmp_path / label
            home_token = set_hermes_home_override(str(home))
            secret_token = set_secret_scope({"PROFILE_MARKER": label})
            try:
                threads.append(inv._prewarm_pricing_async([{"models": []}]))
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)

        assert threads[0] is not threads[1]
        assert started["a"].wait(timeout=1)
        assert started["b"].wait(timeout=1)
        assert observed["a"] == (
            hermes_home_key(tmp_path / "a"),
            {"PROFILE_MARKER": "a"},
        )
        assert observed["b"] == (
            hermes_home_key(tmp_path / "b"),
            {"PROFILE_MARKER": "b"},
        )
    finally:
        release.set()
        for thread in threads:
            if thread is not None:
                thread.join(timeout=2)


def test_cached_only_pricing_returns_a_warm_value_without_fetching(monkeypatch):
    """Cache-only picker reads preserve pricing once the prewarm completes."""
    cache_key = "https://openrouter.ai/api"
    expected = {"vendor/model": {"prompt": "0.000001", "completion": "0.000002"}}
    monkeypatch.setattr(models_mod, "_pricing_cache", {cache_key: expected})
    monkeypatch.setattr(models_mod, "_pricing_cache_retry_after", {})
    monkeypatch.setattr(models_mod, "_pricing_provider_cache_keys", {})
    monkeypatch.setattr(
        models_mod,
        "fetch_models_with_pricing",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network fetch started")),
    )

    assert models_mod.get_pricing_for_provider(
        "openrouter", cached_only=True
    ) == expected


def test_cached_only_dynamic_pricing_is_profile_scoped(tmp_path, monkeypatch):
    """Alternating profiles read the endpoint each profile warmed."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    endpoint_a = "https://profile-a.example"
    endpoint_b = "https://profile-b.example"
    expected_a = {"a/model": {"prompt": "1", "completion": "2"}}
    expected_b = {"b/model": {"prompt": "3", "completion": "4"}}
    monkeypatch.setattr(
        models_mod,
        "_pricing_cache",
        {endpoint_a: expected_a, endpoint_b: expected_b},
    )
    monkeypatch.setattr(models_mod, "_pricing_cache_retry_after", {})
    monkeypatch.setattr(models_mod, "_pricing_provider_cache_keys", {})
    active_endpoint = {"value": endpoint_a}
    monkeypatch.setattr(
        models_mod,
        "_resolve_nous_pricing_credentials",
        lambda: ("", active_endpoint["value"]),
    )
    monkeypatch.setattr(
        models_mod,
        "fetch_models_with_pricing",
        lambda **kwargs: models_mod._pricing_cache[kwargs["base_url"]],
    )

    def in_profile(home, endpoint, *, cached_only):
        token = set_hermes_home_override(str(home))
        active_endpoint["value"] = endpoint
        try:
            return models_mod.get_pricing_for_provider(
                "nous", cached_only=cached_only
            )
        finally:
            reset_hermes_home_override(token)

    assert in_profile(tmp_path / "a", endpoint_a, cached_only=False) == expected_a
    assert in_profile(tmp_path / "b", endpoint_b, cached_only=False) == expected_b
    assert in_profile(tmp_path / "a", endpoint_b, cached_only=True) == expected_a
    assert in_profile(tmp_path / "b", endpoint_a, cached_only=True) == expected_b


