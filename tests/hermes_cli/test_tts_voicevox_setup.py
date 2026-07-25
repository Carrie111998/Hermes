"""Setup and status integration for the built-in VOICEVOX provider."""

from types import SimpleNamespace

from hermes_cli import setup, tools_config


VOICEVOX_VOICES = [
    {
        "id": "2",
        "display": "四国めたん — ノーマル (ID 2)",
        "language": "ja-JP",
    },
    {
        "id": "3",
        "display": "ずんだもん — ノーマル (ID 3)",
        "language": "ja-JP",
    },
]


def _subscription_features_without_nous():
    return SimpleNamespace(nous_auth_present=False)


def test_setup_tts_lists_voicevox_and_saves_selected_speaker(monkeypatch):
    config = {}
    saved = []

    def choose(question, choices, default=0):
        if question == "Select TTS provider:":
            return next(i for i, label in enumerate(choices) if "VOICEVOX" in label)
        if question == "Select VOICEVOX speaker/style:":
            return next(i for i, label in enumerate(choices) if "四国めたん" in label)
        raise AssertionError(f"unexpected picker: {question}")

    monkeypatch.setattr(setup, "managed_nous_tools_enabled", lambda: False)
    monkeypatch.setattr(
        setup,
        "get_nous_subscription_features",
        lambda _config: _subscription_features_without_nous(),
    )
    monkeypatch.setattr(setup, "prompt_choice", choose)
    monkeypatch.setattr(setup, "prompt", lambda _label, default="", **_kw: default)
    monkeypatch.setattr(setup, "save_config", lambda value: saved.append(value.copy()))
    monkeypatch.setattr("tools.tts_tool.list_voices", lambda *_args, **_kwargs: VOICEVOX_VOICES)

    setup._setup_tts_provider(config)

    assert config["tts"]["provider"] == "voicevox"
    assert config["tts"]["voicevox"]["base_url"] == "http://127.0.0.1:50021"
    assert config["tts"]["voicevox"]["speaker"] == 2
    assert saved


def test_setup_tts_keeps_current_speaker_when_catalog_does_not_contain_it(
    monkeypatch,
):
    config = {
        "tts": {
            "provider": "edge",
            "voicevox": {
                "base_url": "http://127.0.0.1:50021",
                "speaker": 999,
            },
        },
    }

    def choose(question, choices, default=0):
        if question == "Select TTS provider:":
            return next(i for i, label in enumerate(choices) if "VOICEVOX" in label)
        if question == "Select VOICEVOX speaker/style:":
            assert choices[default] == "Keep current (ID 999)"
            return default
        raise AssertionError(f"unexpected picker: {question}")

    monkeypatch.setattr(setup, "managed_nous_tools_enabled", lambda: False)
    monkeypatch.setattr(
        setup,
        "get_nous_subscription_features",
        lambda _config: _subscription_features_without_nous(),
    )
    monkeypatch.setattr(setup, "prompt_choice", choose)
    monkeypatch.setattr(setup, "prompt", lambda _label, default="", **_kw: default)
    monkeypatch.setattr(setup, "save_config", lambda _value: None)
    monkeypatch.setattr(
        "tools.tts_tool.list_voices",
        lambda *_args, **_kwargs: VOICEVOX_VOICES,
    )

    setup._setup_tts_provider(config)

    assert config["tts"]["provider"] == "voicevox"
    assert config["tts"]["voicevox"]["speaker"] == 999


def test_tools_picker_configures_voicevox_speaker(monkeypatch):
    config = {}
    provider = next(
        row
        for row in tools_config.TOOL_CATEGORIES["tts"]["providers"]
        if row.get("tts_provider") == "voicevox"
    )

    monkeypatch.setattr(tools_config, "_prompt", lambda _label, default="", **_kw: default)
    monkeypatch.setattr(
        tools_config,
        "_prompt_choice",
        lambda question, choices, default=0: next(
            i for i, label in enumerate(choices) if "ずんだもん" in label
        ),
    )
    monkeypatch.setattr("tools.tts_tool.list_voices", lambda *_args, **_kwargs: VOICEVOX_VOICES)

    tools_config._configure_provider(provider, config)

    assert config["tts"]["provider"] == "voicevox"
    assert config["tts"]["voicevox"]["base_url"] == "http://127.0.0.1:50021"
    assert config["tts"]["voicevox"]["speaker"] == 3


def test_voicevox_picker_status_reflects_engine_availability(monkeypatch):
    provider = next(
        row
        for row in tools_config.TOOL_CATEGORIES["tts"]["providers"]
        if row.get("tts_provider") == "voicevox"
    )
    config = {"tts": {"provider": "voicevox"}}

    monkeypatch.setattr("tools.tts_tool._check_voicevox_available", lambda _cfg=None: True)
    assert tools_config.provider_readiness_status(provider, config) == "ready"

    monkeypatch.setattr("tools.tts_tool._check_voicevox_available", lambda _cfg=None: False)
    assert tools_config.provider_readiness_status(provider, config) == "needs_setup"


def test_setup_summary_reports_voicevox_engine_status(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.tts_tool._check_voicevox_available",
        lambda _cfg=None: True,
    )

    setup._print_setup_summary({"tts": {"provider": "voicevox"}}, tmp_path)

    assert "Text-to-Speech (VOICEVOX local)" in capsys.readouterr().out