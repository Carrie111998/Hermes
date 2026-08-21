from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "grok"
    / "scripts"
    / "grok_cli_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("grok_cli_bridge", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def test_rightcode_key_is_loaded_from_crlf_dotenv_without_other_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b'RIGHTCODE_API_KEY="rightcode-secret"\r\n'
        b'DISCORD_BOT_TOKEN="must-not-enter-grok"\r\n'
    )

    child = bridge.child_environment({"HOME": "/tmp/test-home"}, env_file=env_file)

    assert child["RIGHTCODE_GROK_API_KEY"] == "rightcode-secret"
    assert child["XAI_API_KEY"] == "rightcode-secret"
    assert child["GROK_MODELS_BASE_URL"] == "https://rightapi.ai/grok/v1"
    assert "DISCORD_BOT_TOKEN" not in child


def test_existing_rightcode_environment_does_not_require_dotenv(tmp_path):
    child = bridge.child_environment(
        {"RIGHTCODE_API_KEY": "environment-secret"},
        env_file=tmp_path / "missing.env",
    )

    assert child["RIGHTCODE_GROK_API_KEY"] == "environment-secret"
    assert child["XAI_API_KEY"] == "environment-secret"
    assert child["GROK_MODELS_BASE_URL"] == "https://rightapi.ai/grok/v1"


def test_explicit_non_rightcode_route_is_preserved(tmp_path):
    source = {
        "RIGHTCODE_API_KEY": "rightcode-secret",
        "XAI_API_KEY": "explicit-xai-secret",
        "GROK_MODELS_BASE_URL": "https://grok-proxy.example/v1",
    }

    child = bridge.child_environment(source, env_file=tmp_path / "missing.env")

    assert child["RIGHTCODE_GROK_API_KEY"] == "rightcode-secret"
    assert child["XAI_API_KEY"] == "explicit-xai-secret"
    assert child["GROK_MODELS_BASE_URL"] == "https://grok-proxy.example/v1"
    assert source == {
        "RIGHTCODE_API_KEY": "rightcode-secret",
        "XAI_API_KEY": "explicit-xai-secret",
        "GROK_MODELS_BASE_URL": "https://grok-proxy.example/v1",
    }
