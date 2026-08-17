"""Versioned family-alias resolution + phantom-switch guard.

Regression for the silent model-switch bug: typing ``sonnet-5`` while on a
provider that doesn't serve it used to resolve NOWHERE, get folded to the
provider default by ``_normalize_for_deepseek``'s catch-all, validate clean,
and report "switched" while nothing changed.

Two fixes under test:
1. ``resolve_alias`` resolves versioned family shorthand (``sonnet-5`` ->
   ``anthropic/claude-sonnet-5``) so the provider segment is inferred, and
   ``switch_model`` falls back to authenticated providers that carry it.
2. A name that resolves nowhere FAILS LOUDLY (success=False) instead of a
   phantom success.
"""

from unittest.mock import patch

import pytest

from hermes_cli.model_switch import (
    AmbiguousAliasError,
    _match_family_alias_with_version,
    resolve_alias,
    switch_model,
)

_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}

_MOCK_RUNTIME = {
    "api_key": "sk-test",
    "base_url": "",
    "api_mode": "chat_completions",
}


@pytest.fixture(autouse=True)
def _offline_model_catalog(monkeypatch):
    """Keep catalog lookups offline and deterministic."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "hermes_cli.models.provider_model_ids", lambda *_a, **_k: []
    )


# ── Versioned family alias matching ─────────────────────────────────────

@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("sonnet-5", ("sonnet", "5")),
        ("glm-5.2", ("glm", "5.2")),
        ("gpt-5.4", ("gpt", "5.4")),
        ("grok-3-fast", ("grok", "3-fast")),
        ("mimo-v2.5-pro", ("mimo", "v2.5-pro")),
    ],
)
def test_match_family_alias_with_version(input_str, expected):
    assert _match_family_alias_with_version(input_str) == expected


@pytest.mark.parametrize(
    "input_str",
    [
        "sonnet",          # bare alias, no version
        "claude-sonnet-5", # full model name — alias prefix "claude" but the
                           # remainder "sonnet-5" is not version-like
        "floobarp",
        "gpt5",            # bare alias
        "deepseek-v4-pro", # real V-series id — deepseek's alias family is the
                           # stale legacy "deepseek-chat", so this must NOT be
                           # misread as alias "deepseek" + version "v4-pro"
        "deepseek-v4-flash",
    ],
)
def test_match_family_alias_with_version_rejects(input_str):
    assert _match_family_alias_with_version(input_str) is None


def test_resolve_alias_versioned_sonnet5_on_anthropic():
    """sonnet-5 resolves to claude-sonnet-5 on the anthropic provider."""
    result = resolve_alias("sonnet-5", "anthropic")
    assert result == ("anthropic", "claude-sonnet-5", "sonnet-5")


def test_resolve_alias_versioned_glm52_on_zai():
    """glm-5.2 resolves to glm-5.2 on the zai provider."""
    result = resolve_alias("glm-5.2", "zai")
    assert result == ("zai", "glm-5.2", "glm-5.2")


def test_resolve_alias_versioned_sonnet5_not_on_deepseek():
    """A provider that doesn't serve the family returns None — the caller
    falls back to authenticated providers."""
    assert resolve_alias("sonnet-5", "deepseek") is None


def test_resolve_alias_versioned_prefers_exact_id():
    """Versioned input prefers the exact family-version entry over dated
    snapshots that share the prefix — never an ambiguous guess."""
    catalog = ["claude-sonnet-5", "claude-sonnet-5-20250514"]
    with (
        patch("hermes_cli.model_switch.list_provider_models", return_value=catalog),
        patch("hermes_cli.model_switch.is_aggregator", return_value=False),
        patch("hermes_cli.models._PROVIDER_MODELS", {}),
    ):
        result = resolve_alias("sonnet-5", "anthropic")
    assert result == ("anthropic", "claude-sonnet-5", "sonnet-5")


def test_resolve_alias_versioned_ambiguous_without_exact_raises():
    """No exact id, multiple dated variants — must not guess."""
    catalog = ["claude-sonnet-5-20250514", "claude-sonnet-5-20250601"]
    with (
        patch("hermes_cli.model_switch.list_provider_models", return_value=catalog),
        patch("hermes_cli.model_switch.is_aggregator", return_value=False),
        patch("hermes_cli.models._PROVIDER_MODELS", {}),
    ):
        with pytest.raises(AmbiguousAliasError):
            resolve_alias("sonnet-5", "anthropic")


# ── switch_model: versioned alias fallback across providers ─────────────

def test_switch_model_sonnet5_falls_back_to_anthropic(monkeypatch):
    """Typing sonnet-5 while on deepseek switches to anthropic/claude-sonnet-5
    via the authenticated-provider fallback — the TUI-like behavior."""
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: dict(_MOCK_RUNTIME),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_authenticated_provider_slugs",
        lambda **kw: ["anthropic"],
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: dict(_MOCK_VALIDATION),
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    result = switch_model(
        raw_input="sonnet-5",
        current_provider="deepseek",
        current_model="deepseek-v4-flash",
    )

    assert result.success is True
    assert result.new_model == "claude-sonnet-5"
    assert result.target_provider == "anthropic"
    assert result.resolved_via_alias == "sonnet-5"


def test_switch_model_unknown_name_fails_loudly(monkeypatch):
    """A name that resolves NOWHERE must NOT report a phantom success on
    deepseek (the old behavior: folded to deepseek-v4-flash, success=True)."""
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: dict(_MOCK_RUNTIME),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_authenticated_provider_slugs",
        lambda **kw: [],
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: dict(_MOCK_VALIDATION),
    )

    result = switch_model(
        raw_input="floobarp",
        current_provider="deepseek",
        current_model="deepseek-v4-flash",
    )

    assert result.success is False
    assert "floobarp" in result.error_message
    # The failure must name the phantom target so the user sees the lie.
    assert "deepseek-v4-flash" in result.error_message or "rewrite" in result.error_message


def test_switch_model_known_deepseek_id_still_switches(monkeypatch):
    """Legitimate deepseek ids still switch — the guard must not over-fire."""
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: dict(_MOCK_RUNTIME),
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: dict(_MOCK_VALIDATION),
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    result = switch_model(
        raw_input="deepseek-v4-pro",
        current_provider="deepseek",
        current_model="deepseek-v4-flash",
    )

    assert result.success is True
    assert result.new_model == "deepseek-v4-pro"
    assert result.target_provider == "deepseek"
