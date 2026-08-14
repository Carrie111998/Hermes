"""OpenCode Zen free-only gate must also apply to the desktop/TUI picker.

The desktop app's model picker (apps/desktop/src/app/session/hooks/
use-model-controls.ts) and the ink TUI's picker both select a model by
sending `config.set key=model value="<model> --provider <slug> --session"`
over the gateway RPC. That lands in `_apply_model_switch`, which forwards to
`hermes_cli.model_switch.switch_model` — the same gate already covers the
`cli.py` and `gateway/slash_commands.py` pickers via `picker_selected=True`,
but this third entry point never threaded that flag through, so a paid Zen
model picked from the desktop/TUI catalog switched successfully instead of
being blocked (issue #82764).
"""

from __future__ import annotations

from unittest.mock import patch

import tui_gateway.server as server
from agent.models_dev import ModelInfo

_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}

_PAID_MODEL = ModelInfo(
    id="gpt-5.6-luna", name="gpt-5.6-luna", family="gpt-5.6-luna",
    provider_id="opencode-zen", cost_input=0.20, cost_output=1.20,
)
_FREE_MODEL = ModelInfo(
    id="big-pickle", name="big-pickle", family="big-pickle",
    provider_id="opencode-zen", cost_input=0.0, cost_output=0.0,
)


def _config_set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


def _picker_pick(model_id: str, model_info, picker_selected: bool) -> dict:
    session = {
        "session_key": "k1",
        "agent": None,
        "model_override": None,
    }
    params = {
        "key": "model",
        "session_id": "s1",
        "value": f"{model_id} --provider opencode-zen --session",
    }
    if picker_selected:
        params["picker_selected"] = True
    with (
        patch.dict(server._sessions, {"s1": session}, clear=False),
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
        patch("hermes_cli.config.load_config", return_value={}),
        patch.object(server, "_write_config_key"),
        patch.object(server, "_emit"),
    ):
        return _config_set(params)


class TestOpenCodeZenPickerGate:
    def test_picker_selection_of_paid_model_is_blocked(self):
        resp = _picker_pick("gpt-5.6-luna", _PAID_MODEL, picker_selected=True)

        assert "error" in resp, f"expected the paid Zen model to be blocked, got {resp}"
        assert "gpt-5.6-luna" in resp["error"]["message"]

    def test_picker_selection_of_free_model_is_allowed(self):
        resp = _picker_pick("big-pickle", _FREE_MODEL, picker_selected=True)

        assert "result" in resp, f"expected the free Zen model to switch, got {resp}"
        assert resp["result"]["value"] == "big-pickle"
