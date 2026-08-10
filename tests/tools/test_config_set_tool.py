"""Behavioral contracts for the opt-in safe configuration tool."""

from __future__ import annotations

import json
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
import yaml

from hermes_cli.tools_config import _get_platform_tools, tools_disable_enable_command
from model_tools import get_tool_definitions
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.config_set_tool import (
    CONFIG_SET_TOOL_SCHEMA,
    WRITABLE_CONFIG_KEYS,
    _is_credential_shaped,
    _is_whitelisted,
    config_set_value,
)


_VALID_VALUE_BY_KEY = {
    "compression.enabled": "false",
    "compression.threshold": "0.75",
    "display.show_reasoning": "true",
    "display.skin": "default",
    "display.tool_progress": "off",
    "stt.local.model": "tiny",
    "tts.deepinfra.voice": "default",
    "tts.edge.voice": "en-US-AriaNeural",
    "tts.elevenlabs.voice_id": "pNInz6obpgDQGcFmaJgB",
    "tts.gemini.voice": "Kore",
    "tts.kittentts.voice": "Jasper",
    "tts.minimax.voice_id": "English_expressive_narrator",
    "tts.mistral.voice_id": "c69964a6-ab8b-4f8a-9465-ec0925096ec8",
    "tts.openai.voice": "alloy",
    "tts.xai.voice_id": "eve",
}


def _write_config(home: Path, text: str = "{}\n") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _call(key: str, value, session_id: str = "test-session") -> dict:
    return json.loads(config_set_value(key, value, session_id=session_id))


@pytest.mark.parametrize("key", sorted(WRITABLE_CONFIG_KEYS))
def test_every_approved_leaf_is_authorized(key: str):
    assert _is_whitelisted(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "",
        "stt",
        "stt.enabled.extra",
        "stt.future_setting",
        "STT.ENABLED",
        " stt.enabled",
        "stt.enabled ",
        ".stt.enabled",
        "stt..enabled",
        "stt[enabled]",
        "stt.0.enabled",
        "displayed.skin",
        "compression_extra.enabled",
        "mcp_servers.context7.command",
        "mcp_servers.context7.args",
        "mcp_servers.context7.env.API_KEY",
        "mcp_servers.context7.url",
        "mcp_servers.context7.headers.Authorization",
        "stt.enabled",
        "stt.provider",
        "tts.provider",
        "tts.piper.voice",
        "custom_providers.example.command",
        "platform_toolsets.cli",
        "auxiliary.approval.enabled",
        "approvals.mode",
        "security.redact_secrets",
        "terminal.backend",
        "delegation.max_spawn_depth",
        "model.default",
        "webhook.enabled",
    ],
)
def test_unknown_sibling_descendant_and_trust_boundary_keys_are_denied(key: str):
    assert _is_whitelisted(key) is False


def test_schema_enumerates_only_the_reviewed_leaves():
    key_schema = CONFIG_SET_TOOL_SCHEMA["parameters"]["properties"]["key"]
    assert set(key_schema["enum"]) == WRITABLE_CONFIG_KEYS
    assert CONFIG_SET_TOOL_SCHEMA["parameters"]["additionalProperties"] is False
    assert set(_VALID_VALUE_BY_KEY) == WRITABLE_CONFIG_KEYS


def test_default_off_enable_disable_uses_official_tool_configuration(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert "config" not in _get_platform_tools(
        {}, "cli", include_default_mcp_servers=False
    )

    tools_disable_enable_command(
        Namespace(tools_action="enable", names=["config"], platform="cli")
    )
    enabled_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    enabled = _get_platform_tools(
        enabled_config, "cli", include_default_mcp_servers=False
    )
    assert "config" in enabled

    enabled_defs = get_tool_definitions(
        enabled_toolsets=sorted(enabled),
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    assert "hermes_config_set" in {item["function"]["name"] for item in enabled_defs}

    tools_disable_enable_command(
        Namespace(tools_action="disable", names=["config"], platform="cli")
    )
    disabled_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    disabled = _get_platform_tools(
        disabled_config, "cli", include_default_mcp_servers=False
    )
    assert "config" not in disabled


def test_tool_has_no_default_core_schema_footprint():
    assert "hermes_config_set" not in _HERMES_CORE_TOOLS
    core_bundle_tools = cast(list[str], TOOLSETS["hermes-cli"]["tools"])
    assert "hermes_config_set" not in core_bundle_tools

    default_defs = get_tool_definitions(
        enabled_toolsets=["hermes-cli"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    assert "hermes_config_set" not in {
        item["function"]["name"] for item in default_defs
    }


@pytest.mark.parametrize(
    ("key", "value", "initial"),
    [
        ("display.show_reasoning", "true", "display:\n  show_reasoning: false\n"),
        ("compression.threshold", "0.75", "compression:\n  threshold: 0.80\n"),
        ("display.tool_progress", "off", "{}\n"),
        ("tts.edge.voice", "en-US-AriaNeural", "{}\n"),
    ],
)
def test_real_tool_write_persists_and_matches_canonical_writer(
    key, value, initial, tmp_path, monkeypatch
):
    tool_home = tmp_path / "tool-home"
    direct_home = tmp_path / "direct-home"
    _write_config(tool_home, initial)
    _write_config(direct_home, initial)

    monkeypatch.setenv("HERMES_HOME", str(tool_home))
    result = _call(key, value)
    assert result["success"] is True
    assert result["key"] == key
    assert result["audit_logged"] is True
    tool_config = yaml.safe_load(
        (tool_home / "config.yaml").read_text(encoding="utf-8")
    )
    monkeypatch.setenv("HERMES_HOME", str(direct_home))
    from hermes_cli.config import set_config_value

    set_config_value(key, value)
    direct_config = yaml.safe_load(
        (direct_home / "config.yaml").read_text(encoding="utf-8")
    )
    assert tool_config == direct_config


def test_concurrent_writes_are_serialized_and_both_persist(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.config import set_config_value as canonical_writer

    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def observed_writer(key, value):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        try:
            canonical_writer(key, value)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr("hermes_cli.config.set_config_value", observed_writer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: _call(*pair),
                [
                    ("display.show_reasoning", "true"),
                    ("compression.enabled", "false"),
                ],
            )
        )

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert all(result["success"] is True for result in results)
    assert max_active == 1
    assert config["display"]["show_reasoning"] is True
    assert config["compression"]["enabled"] is False


def test_denied_mutation_is_byte_identical_and_audited(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_path = _write_config(
        home,
        "approvals:\n  mode: manual\nmcp_servers:\n  context7:\n    command: npx\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    before = config_path.read_bytes()

    result = _call("mcp_servers.context7.command", "malicious-command")

    assert result["success"] is False
    assert result["blocked"] is True
    assert result["audit_logged"] is True
    assert config_path.read_bytes() == before
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert '"status": "denied"' in audit


@pytest.mark.parametrize(
    "value",
    [
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-***",
        "sk-...",
        "sk-proj-AbC...xyz7890",
        "sk-<api-key>",
        "sk-PLACEHOLDER",
        "sk-CHANGEME",
        "Bearer this-is-a-secret-token",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
        "AKIAABCDEFGHIJKLMNOP",
        "hf_abcdefghijklmnopqrstuvwxyz1234567890",
        "«redacted:sk-…»",
    ],
)
def test_secrets_and_redaction_sentinels_are_blocked_and_not_logged(
    value: str, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    config_path = _write_config(home, "tts:\n  edge:\n    voice: safe-voice\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    before = config_path.read_bytes()

    result = _call("tts.edge.voice", value)

    assert result["success"] is False
    assert result["blocked"] is True
    assert config_path.read_bytes() == before
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert value not in audit
    assert "redacted" in audit.lower()


@pytest.mark.parametrize(
    "value",
    [
        "a normal benign voice name that is deliberately longer than thirty-two characters",
        "https://voice.example.test/path",
        "anthropic/claude-sonnet-4",
        "c69964a6-ab8b-4f8a-9465-ec0925096ec8",
        "voices/en_US-lessac-medium.onnx",
    ],
)
def test_benign_long_url_model_voice_and_path_strings_do_not_false_positive(value: str):
    assert _is_credential_shaped(value) is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stt.enabled", "yes"),
        ("stt.provider", "local_command"),
        ("stt.provider", "custom-command-provider"),
        ("tts.provider", "custom-command-provider"),
        ("stt.local.model", "future-model"),
        ("display.tool_progress", "future-mode"),
        ("compression.threshold", "nan"),
        ("compression.threshold", "0.49"),
        ("compression.threshold", "0.96"),
        ("tts.edge.voice", {"nested": "value"}),
        ("tts.edge.voice", "voice\x7fhidden"),
        ("tts.edge.voice", "voice\x85hidden"),
        ("tts.edge.voice", "voice\u200bhidden"),
        ("tts.edge.voice", "voice\u202eevil"),
    ],
)
def test_allowed_keys_still_require_safe_typed_values(
    key, value, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    config_path = _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    before = config_path.read_bytes()

    result = _call(key, value)

    assert result["success"] is False
    assert result["blocked"] is True
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("key", "value", "applies"),
    [
        ("display.skin", "default", "new_session"),
        ("display.tool_progress", "off", "new_session"),
        ("compression.enabled", "false", "new_session"),
        ("stt.local.model", "tiny", "next_invocation"),
        ("tts.edge.voice", "en-US-AriaNeural", "next_invocation"),
    ],
)
def test_response_reports_per_setting_application_semantics(
    key: str, value: str, applies: str, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _call(key, value)

    assert result["success"] is True
    assert result["applies"] == applies
    assert result["requires_process_restart"] is False


def test_prefix_collision_denial_with_secret_never_leaks_to_audit(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    result = _call("mcp_servers_v2.command", secret)

    assert result["success"] is False
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert secret not in audit


def test_structured_secret_attempt_is_rejected_and_redacted(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    result = _call("tts.edge.voice", {"nested": {"token": secret}})

    assert result["success"] is False
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert secret not in audit


def test_denied_secret_key_is_redacted_in_audit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret_key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    result = _call(secret_key, "benign")

    assert result["success"] is False
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert secret_key not in audit


def test_writer_failure_cannot_return_unrelated_process_output(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def noisy_failure(_key, _value):
        print("OTHER_SESSION_PRIVATE_OUTPUT")
        raise RuntimeError("private writer detail")

    monkeypatch.setattr("hermes_cli.config.set_config_value", noisy_failure)
    result = _call("display.show_reasoning", "true")

    assert result["success"] is False
    assert result["error"] == "Configuration write failed."
    assert "OTHER_SESSION_PRIVATE_OUTPUT" not in json.dumps(result)
    assert "OTHER_SESSION_PRIVATE_OUTPUT" in capsys.readouterr().out


def test_audit_bounds_malformed_values_and_rotates(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    log_dir = home / "logs"
    log_dir.mkdir()
    log_path = log_dir / "config_changes.jsonl"
    log_path.write_bytes(b"x" * 1_000_000)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _call("unknown." + "k" * 20_000, {"items": ["v" * 20_000] * 50})

    assert result["success"] is False
    assert (log_dir / "config_changes.jsonl.1").stat().st_size == 1_000_000
    assert log_path.stat().st_size < 10_000


def test_audit_bounds_many_key_mapping(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _call("unknown", {f"key-{index}": "value" for index in range(50_000)})

    assert result["success"] is False
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert "key-19" in audit
    assert "key-20" not in audit
    assert len(audit) < 10_000


def test_audit_redacts_and_bounds_session_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    result = _call("unknown", "value", session_id=secret + "x" * 20_000)

    assert result["success"] is False
    audit = (home / "logs" / "config_changes.jsonl").read_text(encoding="utf-8")
    assert secret not in audit
    assert len(audit) < 2_000


@pytest.mark.parametrize("key", sorted(WRITABLE_CONFIG_KEYS))
def test_each_allowed_setting_persists_with_accurate_application_semantics(
    key: str, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _call(key, _VALID_VALUE_BY_KEY[key])

    assert result["success"] is True
    if key.startswith(("stt.", "tts.")):
        assert result["applies"] == "next_invocation"
    else:
        assert result["applies"] == "new_session"
    assert result["requires_process_restart"] is False
