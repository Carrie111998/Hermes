"""Tests that ``model_catalog.excluded_providers`` hides providers from the
interactive ``hermes model`` CLI picker.

The CLI picker (``hermes_cli.main.select_provider_and_model``) builds its
provider menu from ``CANONICAL_PROVIDERS`` via ``group_providers`` — a
separate code path from ``list_authenticated_providers``. These tests
verify the exclusion config is honored there too, matching the
gateway/TUI picker behavior.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a minimal config."""
    home = tmp_path / "hermes"
    home.mkdir()
    config_yaml = home / "config.yaml"
    config_yaml.write_text("model: old-model\ncustom_providers: []\n")
    env_file = home / ".env"
    env_file.write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return home


def _write_config(home, **top_level):
    import yaml
    cfg = {"model": "old-model", "custom_providers": []}
    cfg.update(top_level)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))


def _capture_provider_labels(config_home):
    """Drive ``select_provider_and_model`` and return the provider-menu labels
    shown to the user (the first ``_prompt_provider_choice`` call). Cancels
    immediately after capturing."""
    from hermes_cli.main import select_provider_and_model

    captured: dict = {}

    def _capture_and_cancel(labels, default=0, title=None):
        # Only capture the top-level provider menu (the first call).
        if "labels" not in captured:
            captured["labels"] = list(labels)
        return None  # cancel

    with patch("hermes_cli.main._prompt_provider_choice",
               side_effect=_capture_and_cancel), \
         patch("builtins.print"):
        select_provider_and_model()

    return captured.get("labels", [])


def test_cli_picker_hides_excluded_provider(config_home):
    """``excluded_providers: [openrouter]`` must remove the OpenRouter row
    from the ``hermes model`` provider menu."""
    _write_config(config_home, **{"model_catalog": {"excluded_providers": ["openrouter"]}})

    labels = _capture_provider_labels(config_home)
    assert labels, "provider menu was empty"
    assert not any("OpenRouter" in lbl for lbl in labels), (
        f"OpenRouter should be hidden by excluded_providers, got: {labels}"
    )


def test_cli_picker_hides_excluded_provider_by_alias(config_home):
    """Exclusion by an alias (not the canonical slug) must also hide the
    provider, matching ``list_authenticated_providers``' matching against
    hermes_id / alias names."""
    # 'openai' is an alias-style hermes id; ensure excluding it hides the
    # canonical openai provider row if present. Use the canonical slug's
    # alias from _PROVIDER_ALIASES to stay robust to renames.
    from hermes_cli.models import _PROVIDER_ALIASES, CANONICAL_PROVIDERS

    # Find a canonical provider that has at least one alias and is a leaf
    # row (not folded into a multi-member group) so its label appears
    # directly. Pick the first such provider.
    target_slug = None
    target_alias = None
    for alias, canon in _PROVIDER_ALIASES.items():
        if canon and any(p.slug == canon for p in CANONICAL_PROVIDERS):
            target_slug = canon
            target_alias = alias
            break
    if target_slug is None:
        pytest.skip("no aliased canonical provider available to test")

    from hermes_cli.models import _PROVIDER_LABELS
    target_label_fragment = _PROVIDER_LABELS.get(target_slug, target_slug)

    # Baseline: the provider appears without exclusion.
    _write_config(config_home)
    baseline = _capture_provider_labels(config_home)
    assert any(target_label_fragment in lbl for lbl in baseline), (
        f"sanity: {target_slug} ({target_label_fragment!r}) should appear by "
        f"default; labels={baseline}"
    )

    # Excluding by alias hides it.
    _write_config(
        config_home,
        **{"model_catalog": {"excluded_providers": [target_alias]}},
    )
    excluded_labels = _capture_provider_labels(config_home)
    assert not any(target_label_fragment in lbl for lbl in excluded_labels), (
        f"excluding alias {target_alias!r} should hide {target_slug}; "
        f"labels={excluded_labels}"
    )


# ---------------------------------------------------------------------------
# Virtual MoA overlay row honours excluded_providers (#1)
# ---------------------------------------------------------------------------
#
# ``moa`` is an ``auth_type="virtual"`` overlay provider that the gateway
# picker (``list_picker_providers``) and CLI inventory (``build_models_payload``)
# reinsert at the front of the provider list. It must respect the same
# ``model_catalog.excluded_providers`` policy as credential-backed providers.

_FAKE_MOA_ROW = {
    "slug": "moa",
    "name": "Mixture of Agents",
    "is_current": False,
    "is_user_defined": False,
    "models": ["balanced"],
    "total_models": 1,
    "source": "virtual",
    "authenticated": True,
    "auth_type": "virtual",
}


def test_gateway_picker_does_not_reinsert_excluded_moa(monkeypatch):
    """``list_picker_providers(include_moa=True, excluded_providers=["moa"])``
    must not surface the virtual MoA row, even when a stale ``moa`` row is
    already present in the base list."""
    from hermes_cli import model_switch

    monkeypatch.setattr(
        model_switch, "list_authenticated_providers",
        lambda **kw: [dict(_FAKE_MOA_ROW), _make_row("openai")],
    )
    monkeypatch.setattr("hermes_cli.inventory._moa_provider_row",
                        lambda *_a, **_kw: dict(_FAKE_MOA_ROW))
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    rows = model_switch.list_picker_providers(
        include_moa=True, excluded_providers=["moa"],
    )
    assert "moa" not in {str(r.get("slug", "")).lower() for r in rows}


def test_inventory_omits_moa_when_excluded(monkeypatch):
    """``build_models_payload`` must drop the virtual MoA row when ``moa`` is
    in ``ctx.excluded_providers``."""
    from hermes_cli import inventory
    from hermes_cli.inventory import ConfigContext, build_models_payload

    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **kw: [_make_row("openai")],
    )
    monkeypatch.setattr(inventory, "_moa_provider_row",
                        lambda *_a, **_kw: dict(_FAKE_MOA_ROW))

    ctx = ConfigContext(
        current_provider="openai",
        current_model="gpt-5",
        current_base_url="",
        user_providers={},
        custom_providers=[],
        excluded_providers=["moa"],
    )
    payload = build_models_payload(ctx)
    slugs = {str(r.get("slug", "")).lower() for r in payload["providers"]}
    assert "moa" not in slugs


def test_moa_remains_visible_when_not_excluded(monkeypatch):
    """Sanity: with no exclusion the virtual MoA row is still prepended by both
    the gateway picker and the CLI inventory."""
    from hermes_cli import inventory, model_switch
    from hermes_cli.inventory import ConfigContext, build_models_payload

    monkeypatch.setattr(
        model_switch, "list_authenticated_providers",
        lambda **kw: [_make_row("openai")],
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **kw: [_make_row("openai")],
    )
    monkeypatch.setattr(inventory, "_moa_provider_row",
                        lambda *_a, **_kw: dict(_FAKE_MOA_ROW))
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    rows = model_switch.list_picker_providers(include_moa=True, excluded_providers=[])
    assert rows[0]["slug"] == "moa"

    ctx = ConfigContext(
        current_provider="openai",
        current_model="gpt-5",
        current_base_url="",
        user_providers={},
        custom_providers=[],
        excluded_providers=[],
    )
    payload = build_models_payload(ctx)
    assert payload["providers"][0]["slug"] == "moa"


def _make_row(slug, models=("m1",)):
    return {
        "slug": slug,
        "name": slug.title(),
        "is_current": False,
        "is_user_defined": False,
        "models": list(models),
        "total_models": len(models),
        "source": "built-in",
    }


