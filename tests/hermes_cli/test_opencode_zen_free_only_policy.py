"""OpenCode Zen free-only default policy (#82764).

Zen's model picker intentionally exposes its full free+paid catalog, but the
model-switch pipeline must not let a ``/model`` switch land on a paid or
unknown-cost Zen model without the user asking for it by name
(hand-typed ``--provider opencode-zen``) or opting in via
``model.allow_paid_opencode_zen: true``. Picker selections pass
explicit_provider too (it's the row's own slug), so ``picker_selected=True``
marks that case and keeps the gate active even though explicit_provider is
set.
"""

from unittest.mock import patch

from agent.models_dev import ModelInfo
from hermes_cli.model_switch import switch_model


_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


def _free_model_info(model_id: str) -> ModelInfo:
    return ModelInfo(
        id=model_id, name=model_id, family=model_id, provider_id="opencode-zen",
        cost_input=0.0, cost_output=0.0,
    )


def _paid_model_info(model_id: str) -> ModelInfo:
    return ModelInfo(
        id=model_id, name=model_id, family=model_id, provider_id="opencode-zen",
        cost_input=0.20, cost_output=1.20,
    )


def _run_zen_switch(
    raw_input: str,
    *,
    model_info,
    explicit_provider: str = "",
    allow_paid=None,
    picker_selected: bool = False,
):
    cfg = {"model": {"allow_paid_opencode_zen": allow_paid}} if allow_paid is not None else {}
    with (
        patch("hermes_cli.model_switch.resolve_alias", return_value=None),
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "sk-zen-fake",
                "base_url": "https://opencode.ai/zen/v1",
                "api_mode": "chat_completions",
            },
        ),
        patch("hermes_cli.models.validate_requested_model", return_value=_MOCK_VALIDATION),
        patch("hermes_cli.model_switch.get_model_info", return_value=model_info),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
        patch("hermes_cli.models.detect_provider_for_model", return_value=None),
        patch("hermes_cli.config.load_config", return_value=cfg),
    ):
        return switch_model(
            raw_input=raw_input,
            current_provider="opencode-zen",
            current_model="deepseek-v4-flash-free",
            current_base_url="https://opencode.ai/zen/v1",
            current_api_key="sk-zen-fake",
            explicit_provider=explicit_provider,
            picker_selected=picker_selected,
        )


class TestOpenCodeZenFreeOnlyPolicy:
    def test_paid_model_blocked_without_explicit_provider(self):
        result = _run_zen_switch("gpt-5.6-luna", model_info=_paid_model_info("gpt-5.6-luna"))

        assert not result.success
        assert "gpt-5.6-luna" in result.error_message
        assert "opencode-zen" in result.error_message

    def test_unknown_cost_model_blocked_fails_closed(self):
        result = _run_zen_switch("some-new-model", model_info=None)

        assert not result.success
        assert "some-new-model" in result.error_message

    def test_verified_free_model_allowed(self):
        result = _run_zen_switch(
            "deepseek-v4-flash-free", model_info=_free_model_info("deepseek-v4-flash-free")
        )

        assert result.success, f"switch_model failed: {result.error_message}"
        assert result.new_model == "deepseek-v4-flash-free"

    def test_explicit_provider_cli_flag_overrides_paid_block(self):
        """A hand-typed ``--provider opencode-zen`` is the override; no
        picker involved (picker_selected defaults to False)."""
        result = _run_zen_switch(
            "gpt-5.6-luna",
            model_info=_paid_model_info("gpt-5.6-luna"),
            explicit_provider="opencode-zen",
        )

        assert result.success, f"switch_model failed: {result.error_message}"

    def test_config_override_allows_paid_model(self):
        result = _run_zen_switch(
            "gpt-5.6-luna", model_info=_paid_model_info("gpt-5.6-luna"), allow_paid=True
        )

        assert result.success, f"switch_model failed: {result.error_message}"

    def test_picker_selection_of_paid_model_is_blocked(self):
        """Both interactive pickers (cli.py, gateway/slash_commands.py) pass
        explicit_provider=<row's own slug> on every selection, paid or free,
        since Zen's picker intentionally shows its full catalog. That must
        not be treated as an override the way a hand-typed --provider is."""
        result = _run_zen_switch(
            "gpt-5.6-luna",
            model_info=_paid_model_info("gpt-5.6-luna"),
            explicit_provider="opencode-zen",
            picker_selected=True,
        )

        assert not result.success
        assert "gpt-5.6-luna" in result.error_message

    def test_picker_selection_of_free_model_is_allowed(self):
        result = _run_zen_switch(
            "deepseek-v4-flash-free",
            model_info=_free_model_info("deepseek-v4-flash-free"),
            explicit_provider="opencode-zen",
            picker_selected=True,
        )

        assert result.success, f"switch_model failed: {result.error_message}"

    def test_picker_selection_of_paid_model_allowed_via_config(self):
        result = _run_zen_switch(
            "gpt-5.6-luna",
            model_info=_paid_model_info("gpt-5.6-luna"),
            explicit_provider="opencode-zen",
            picker_selected=True,
            allow_paid=True,
        )

        assert result.success, f"switch_model failed: {result.error_message}"
