"""Configured models extend or explicitly pin built-in picker rows."""

from unittest.mock import patch

from hermes_cli.model_switch import list_authenticated_providers


def _provider_row(configured_models, *, discover_models=True, max_models=None):
    with (
        patch(
            "agent.models_dev.fetch_models_dev",
            return_value={"deepseek": {"env": ["DEEPSEEK_API_KEY"], "name": "DeepSeek"}},
        ),
        patch(
            "agent.models_dev.PROVIDER_TO_MODELS_DEV",
            {"deepseek": "deepseek"},
        ),
        patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=["live-a", "shared"],
        ),
        patch("hermes_cli.providers.HERMES_OVERLAYS", {}),
        patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}),
    ):
        rows = list_authenticated_providers(
            current_provider="deepseek",
            user_providers={
                "deepseek": {
                    "discover_models": discover_models,
                    "models": configured_models,
                }
            },
            max_models=max_models,
        )
    return next(row for row in rows if row["slug"] == "deepseek")


def test_configured_models_precede_and_deduplicate_discovered_models():
    row = _provider_row({"configured-x": {}, "shared": {}})

    assert row["models"] == ["configured-x", "shared", "live-a"]
    assert row["total_models"] == 3


def test_discover_models_false_pins_builtin_picker_to_configured_models():
    row = _provider_row(
        ["glm-5.3-flash", "glm-5.3"],
        discover_models=False,
    )

    assert row["models"] == ["glm-5.3-flash", "glm-5.3"]
    assert row["total_models"] == 2
    assert "live-a" not in row["models"]
