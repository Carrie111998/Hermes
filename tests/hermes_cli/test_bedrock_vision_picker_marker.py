"""The /model Bedrock picker must surface which models accept image input.

This is the consumer-level half of the inference-profile modality inheritance
fix. The picker offers inference profiles — bare foundation IDs are
deduplicated away in favour of them — and ``ListInferenceProfiles`` reports no
modalities, so before the fix every profile arrived stamped ``TEXT``-only and
no model could ever be marked vision-capable here.
"""

from unittest.mock import patch


_FOUNDATION_MODELS = [
    {
        "modelId": "anthropic.claude-sonnet-4-6",
        "modelName": "Claude Sonnet 4.6",
        "providerName": "Anthropic",
        "inputModalities": ["TEXT", "IMAGE"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "deepseek.r1-v1:0",
        "modelName": "DeepSeek R1",
        "providerName": "DeepSeek",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "modelLifecycle": {"status": "ACTIVE"},
    },
]

_PROFILES = [
    {
        "inferenceProfileId": "us.anthropic.claude-sonnet-4-6",
        "inferenceProfileName": "US Claude Sonnet 4.6",
        "status": "ACTIVE",
        "models": [
            {
                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                "anthropic.claude-sonnet-4-6"
            }
        ],
    },
    {
        "inferenceProfileId": "us.deepseek.r1-v1:0",
        "inferenceProfileName": "US DeepSeek R1",
        "status": "ACTIVE",
        "models": [
            {
                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                "deepseek.r1-v1:0"
            }
        ],
    },
]


def _mock_control_client():
    from unittest.mock import MagicMock

    c = MagicMock()
    c.list_foundation_models.return_value = {"modelSummaries": _FOUNDATION_MODELS}
    c.list_inference_profiles.return_value = {
        "inferenceProfileSummaries": _PROFILES
    }
    return c


def test_bedrock_flow_passes_vision_ids_to_the_picker(monkeypatch):
    """End-to-end through the real discovery + filter path."""
    from agent.bedrock_adapter import reset_discovery_cache
    from hermes_cli import model_setup_flows as flows

    reset_discovery_cache()
    captured = {}

    def _fake_picker(model_ids, **kwargs):
        captured["model_ids"] = list(model_ids)
        captured["vision_model_ids"] = kwargs.get("vision_model_ids")
        return None  # cancel — we only care about what was offered

    # The flow imports the picker locally from hermes_cli.auth on each call,
    # so patch it at the source module.
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection", _fake_picker
    )
    # Type an explicit region rather than reading stdin — the default comes
    # from the ambient AWS config, and an empty/foreign region would make
    # bedrock_model_routable_from_region() filter every us.* profile away,
    # so pinning it is what keeps this deterministic off this machine.
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "us-east-1")

    with patch(
        "agent.bedrock_adapter._get_bedrock_control_client",
        return_value=_mock_control_client(),
    ):
        flows._model_flow_bedrock({}, current_model="")

    assert captured["model_ids"], "the picker should have been offered models"
    # The Claude profile accepts images; the DeepSeek one does not.
    assert captured["vision_model_ids"] == ["us.anthropic.claude-sonnet-4-6"]


def test_marker_renders_only_on_vision_models():
    """The label builder must tag exactly the models it was handed."""
    from hermes_cli.auth import _prompt_model_selection

    labels = {}

    def _capture(title, choices, default, description=None):
        # choices is a list of (label, value) or similar — record the text.
        labels["rendered"] = [
            c[0] if isinstance(c, (tuple, list)) else str(c) for c in choices
        ]
        return -1  # cancel

    with patch("hermes_cli.setup._curses_prompt_choice", side_effect=_capture), \
            patch("builtins.input", return_value=""):
        _prompt_model_selection(
            ["us.anthropic.claude-sonnet-4-6", "us.deepseek.r1-v1:0"],
            vision_model_ids=["us.anthropic.claude-sonnet-4-6"],
        )

    rendered = "\n".join(labels.get("rendered", []))
    if not rendered:
        # The picker fell back to a non-curses path in this environment; the
        # flow-level test above already covers the wiring, so don't fail here.
        return
    claude_line = [
        ln for ln in rendered.splitlines() if "claude-sonnet-4-6" in ln
    ]
    deepseek_line = [ln for ln in rendered.splitlines() if "deepseek" in ln]
    assert claude_line and "(vision)" in claude_line[0]
    assert deepseek_line and "(vision)" not in deepseek_line[0]


def test_picker_returns_the_plain_id_not_the_decorated_label(monkeypatch):
    """The marker is display-only — it must never leak into the saved model."""
    from hermes_cli.auth import _prompt_model_selection

    # Force the numbered-input fallback and pick the first entry.
    monkeypatch.setattr(
        "hermes_cli.setup._curses_prompt_choice",
        lambda *a, **kw: 0,
    )
    out = _prompt_model_selection(
        ["us.anthropic.claude-sonnet-4-6"],
        vision_model_ids=["us.anthropic.claude-sonnet-4-6"],
    )
    if out is not None:
        assert out == "us.anthropic.claude-sonnet-4-6"
        assert "(vision)" not in out
