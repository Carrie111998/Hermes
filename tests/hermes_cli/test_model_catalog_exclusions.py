"""Focused policy tests for provider-scoped model catalog exclusions."""

from unittest.mock import patch


RETIRED = "glm-4.5-air"
CURRENT = "glm-5.3-flash"
POLICY = {"zai": [RETIRED]}


def test_filter_is_exact_provider_scoped_and_fail_open():
    from hermes_cli.model_catalog import filter_model_catalog_exclusions

    assert filter_model_catalog_exclusions(
        "ZAI", [RETIRED.upper(), CURRENT], POLICY
    ) == [CURRENT]
    assert filter_model_catalog_exclusions(
        "openrouter", [RETIRED, CURRENT], POLICY
    ) == [RETIRED, CURRENT]
    assert filter_model_catalog_exclusions(
        "zai", [RETIRED, CURRENT], {"zai": RETIRED}
    ) == [RETIRED, CURRENT]


def test_merged_inventory_hides_retired_model_without_mutating_source():
    from hermes_cli.inventory import ConfigContext, build_models_payload

    source_rows = [
        {
            "slug": "zai",
            "name": "Z.AI",
            "models": [RETIRED, CURRENT],
            "total_models": 2,
        }
    ]
    context = ConfigContext(
        current_provider="zai",
        current_model=CURRENT,
        current_base_url="",
        user_providers={},
        custom_providers=[],
        excluded_models=POLICY,
    )

    with (
        patch(
            "hermes_cli.model_switch.list_authenticated_providers",
            return_value=source_rows,
        ),
        patch("hermes_cli.inventory._moa_provider_row", return_value=None),
        patch("hermes_cli.providers.is_routing_aggregator", return_value=False),
    ):
        payload = build_models_payload(context)

    assert payload["providers"][0]["models"] == [CURRENT]
    assert payload["providers"][0]["total_models"] == 1
    assert source_rows[0]["models"] == [RETIRED, CURRENT]


def test_automatic_default_and_fallback_skip_retired_model(monkeypatch):
    from hermes_cli import models as models_module
    from hermes_cli.fallback_config import get_fallback_chain

    config = {
        "model_catalog": {"excluded_models": POLICY},
        "fallback_providers": [
            {"provider": "zai", "model": RETIRED},
            {"provider": "zai", "model": CURRENT},
        ],
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setitem(models_module._PROVIDER_MODELS, "zai", [RETIRED, CURRENT])

    assert models_module.get_default_model_for_provider("zai") == CURRENT
    assert (
        models_module.pick_silent_default_model([RETIRED, CURRENT], provider="zai")
        == CURRENT
    )
    assert get_fallback_chain(config) == [{"provider": "zai", "model": CURRENT}]


def test_classic_setup_picker_hides_retired_but_keeps_manual_entry():
    from hermes_cli.auth import _prompt_model_selection

    captured = {}

    def capture(_title, choices, **_kwargs):
        captured["choices"] = choices
        return -1

    config = {"model_catalog": {"excluded_models": POLICY}}
    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.curses_ui.curses_radiolist", side_effect=capture),
    ):
        assert (
            _prompt_model_selection(
                [RETIRED, CURRENT],
                current_model=RETIRED,
                confirm_provider="zai",
            )
            is None
        )

    rendered = repr(captured["choices"])
    assert RETIRED not in rendered
    assert CURRENT in rendered
    assert "Enter custom model name" in captured["choices"]

    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.curses_ui.curses_radiolist", return_value=1),
        patch("hermes_cli.cli_output.line_input", return_value=RETIRED),
    ):
        assert (
            _prompt_model_selection(
                [RETIRED, CURRENT],
                confirm_provider="zai",
            )
            is None
        )
