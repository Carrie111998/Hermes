"""End-to-end contract tests for mandatory provider preflight hooks."""

import builtins
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from run_agent import AIAgent


PLUGIN_NAME = "synthetic-policy-guard"
OFFICIAL_CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _write_plugin(hermes_home: Path, mode: str) -> None:
    plugin_dir = hermes_home / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": PLUGIN_NAME, "version": "0.1.0"}),
        encoding="utf-8",
    )
    callback_body = {
        "none": "return None",
        "raise": 'raise RuntimeError("callback leaked secret SENTINEL-CREDENTIAL")',
        "malformed": 'return "not-a-directive"',
        "block": (
            'return {"action": "block", "reason": '
            '"route denied opaque-secret-1234567890: '
            'https://blocked.invalid/v1?token=SENTINEL-CREDENTIAL"}'
        ),
        "route": (
            f'if kw.get("provider") == "openai-codex" and '
            f'kw.get("base_url") == {OFFICIAL_CODEX_URL!r}:\n'
            '        return {"action": "allow"}\n'
            '    return {"action": "block", "reason": "route denied"}'
        ),
    }[mode]
    (plugin_dir / "__init__.py").write_text(
        "def _preflight(**kw):\n"
        f"    {callback_body}\n\n"
        "def register(ctx):\n"
        '    ctx.register_hook("pre_api_request", _preflight)\n',
        encoding="utf-8",
    )


def _configure_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Optional[str],
    mandatory: bool = True,
) -> Path:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    if mode is not None:
        _write_plugin(hermes_home, mode)
    plugins_cfg: Dict[str, Any] = {"enabled": [PLUGIN_NAME]}
    if mandatory:
        plugins_cfg["mandatory_hooks"] = {"pre_api_request": [PLUGIN_NAME]}
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": plugins_cfg}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()
    return hermes_home


def _response(content: str = "allowed") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="synthetic-response", usage=None)


def _run_agent(
    *,
    provider: str = "custom",
    base_url: str = "https://blocked.invalid/v1",
    api_mode: str = "chat_completions",
):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="SENTINEL-CREDENTIAL",
            provider=provider,
            model="SENTINEL-MODEL",
            base_url=base_url,
            api_mode=api_mode,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response()
    agent._persist_session = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    return agent


def _codex_transport_result() -> Dict[str, Any]:
    return {
        "final_response": "allowed",
        "messages": [{"role": "assistant", "content": "allowed"}],
        "api_calls": 1,
        "completed": True,
        "failed": False,
    }


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        (None, "mandatory_hook_missing"),
        ("none", "mandatory_hook_malformed_result"),
        ("raise", "mandatory_hook_exception"),
        ("malformed", "mandatory_hook_malformed_result"),
        ("block", "mandatory_hook_blocked"),
    ],
)
def test_mandatory_preflight_failures_stop_before_provider_dispatch(
    tmp_path,
    monkeypatch,
    mode,
    expected_code,
):
    _configure_home(tmp_path, monkeypatch, mode=mode)
    agent = _run_agent()

    result = agent.run_conversation("SENTINEL-MESSAGE")

    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == expected_code
    assert result["api_calls"] == 0
    agent.client.chat.completions.create.assert_not_called()
    assert "SENTINEL-CREDENTIAL" not in result["final_response"]
    assert "opaque-secret-1234567890" not in result["final_response"]
    assert "https://blocked.invalid" not in result["final_response"]
    if mode == "block":
        assert "route denied" in result["final_response"]


def test_approved_openai_codex_official_route_dispatches_once(tmp_path, monkeypatch):
    _configure_home(tmp_path, monkeypatch, mode="route")
    agent = _run_agent(provider="openai-codex", base_url=OFFICIAL_CODEX_URL)

    result = agent.run_conversation("approved request")

    assert result["completed"] is True
    assert result["final_response"] == "allowed"
    assert result["api_calls"] == 1
    agent.client.chat.completions.create.assert_called_once()


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        (None, "mandatory_hook_missing"),
        ("none", "mandatory_hook_malformed_result"),
        ("raise", "mandatory_hook_exception"),
        ("malformed", "mandatory_hook_malformed_result"),
        ("block", "mandatory_hook_blocked"),
    ],
)
def test_mandatory_preflight_failures_stop_before_codex_app_server_transport(
    tmp_path,
    monkeypatch,
    mode,
    expected_code,
):
    _configure_home(tmp_path, monkeypatch, mode=mode)
    agent = _run_agent(api_mode="codex_app_server")
    transport = MagicMock(return_value=_codex_transport_result())
    agent._run_codex_app_server_turn = transport

    result = agent.run_conversation("SENTINEL-MESSAGE")

    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == expected_code
    assert result["api_calls"] == 0
    transport.assert_not_called()
    assert result["final_response"] == result["error"]
    assert "SENTINEL-CREDENTIAL" not in result["final_response"]
    assert "opaque-secret-1234567890" not in result["final_response"]
    assert "https://blocked.invalid" not in result["final_response"]


def test_approved_openai_codex_app_server_route_dispatches_once(
    tmp_path,
    monkeypatch,
):
    _configure_home(tmp_path, monkeypatch, mode="route")
    agent = _run_agent(
        provider="openai-codex",
        base_url=OFFICIAL_CODEX_URL,
        api_mode="codex_app_server",
    )
    transport = MagicMock(return_value=_codex_transport_result())
    agent._run_codex_app_server_turn = transport

    result = agent.run_conversation("approved request")

    assert result["completed"] is True
    assert result["final_response"] == "allowed"
    transport.assert_called_once()


def test_malformed_mandatory_hook_config_fails_closed(tmp_path, monkeypatch):
    hermes_home = _configure_home(
        tmp_path, monkeypatch, mode="route", mandatory=False
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": [PLUGIN_NAME],
                    "mandatory_hooks": ["pre_api_request"],
                }
            }
        ),
        encoding="utf-8",
    )
    agent = _run_agent()

    result = agent.run_conversation("must not dispatch")

    assert result["failed"] is True
    assert result["failure_reason"] == "mandatory_hook_config_invalid"
    assert result["api_calls"] == 0
    agent.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("plugins_value", ["invalid", ["invalid"]])
def test_malformed_plugins_parent_config_fails_closed(
    tmp_path,
    monkeypatch,
    plugins_value,
):
    hermes_home = _configure_home(
        tmp_path, monkeypatch, mode="route", mandatory=False
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": plugins_value}),
        encoding="utf-8",
    )
    agent = _run_agent(api_mode="codex_app_server")
    transport = MagicMock(return_value=_codex_transport_result())
    agent._run_codex_app_server_turn = transport

    result = agent.run_conversation("must not dispatch")

    assert result["failed"] is True
    assert result["failure_reason"] == "mandatory_hook_config_invalid"
    assert result["api_calls"] == 0
    transport.assert_not_called()


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_app_server"])
def test_unexpected_mandatory_preflight_exception_fails_closed(
    tmp_path,
    monkeypatch,
    api_mode,
):
    _configure_home(tmp_path, monkeypatch, mode="route")
    agent = _run_agent(api_mode=api_mode)
    transport = MagicMock(return_value=_codex_transport_result())
    agent._run_codex_app_server_turn = transport

    hook_name = (
        "invoke_mandatory_hook"
        if api_mode == "chat_completions"
        else "invoke_hook_enforced"
    )
    with patch(
        f"hermes_cli.lifecycle.{hook_name}",
        side_effect=RuntimeError("unexpected SENTINEL-CREDENTIAL"),
    ):
        result = agent.run_conversation("must not dispatch")

    assert result["failed"] is True
    assert result["failure_reason"] == "mandatory_hook_exception"
    assert result["api_calls"] == 0
    client = agent.client
    assert client is not None
    client.chat.completions.create.assert_not_called()
    transport.assert_not_called()
    assert "SENTINEL-CREDENTIAL" not in result["final_response"]


def test_non_mandatory_observer_exception_remains_isolated(tmp_path, monkeypatch):
    _configure_home(tmp_path, monkeypatch, mode="raise", mandatory=False)
    agent = _run_agent()

    result = agent.run_conversation("observer failure must not block")

    assert result["completed"] is True
    assert result["final_response"] == "allowed"
    agent.client.chat.completions.create.assert_called_once()


def test_non_mandatory_provider_observer_exception_does_not_leak_to_logs(
    tmp_path,
    monkeypatch,
    caplog,
):
    _configure_home(tmp_path, monkeypatch, mode="raise", mandatory=False)
    agent = _run_agent()

    with caplog.at_level(logging.WARNING):
        result = agent.run_conversation("observer failure must not block")

    assert result["completed"] is True
    client = agent.client
    assert client is not None
    assert client.chat.completions.create.call_count == 1
    assert "SENTINEL-CREDENTIAL" not in caplog.text


def test_builtin_provider_observer_exception_does_not_leak_to_logs(
    tmp_path,
    monkeypatch,
    caplog,
):
    def raise_for_provider_request(hook_name, **kwargs):
        if hook_name == "pre_api_request":
            raise RuntimeError("observer leaked SENTINEL-CREDENTIAL")

    _configure_home(tmp_path, monkeypatch, mode=None, mandatory=False)
    agent = _run_agent()

    with (
        patch("hermes_cli.observability.handles_hook", return_value=True),
        patch(
            "hermes_cli.observability.observe_lifecycle",
            side_effect=raise_for_provider_request,
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = agent.run_conversation("observer failure must not block")

    assert result["completed"] is True
    client = agent.client
    assert client is not None
    assert client.chat.completions.create.call_count == 1
    assert "SENTINEL-CREDENTIAL" not in caplog.text


def test_mandatory_allow_audit_event_is_key_allowlisted_and_payload_free(
    tmp_path,
    monkeypatch,
    caplog,
):
    _configure_home(tmp_path, monkeypatch, mode="route")
    agent = _run_agent(provider="openai-codex", base_url=OFFICIAL_CODEX_URL)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        agent.run_conversation("SENTINEL-MESSAGE")

    audit_records = [
        record for record in caplog.records
        if record.name == "hermes_cli.plugins" and '"event": "mandatory_hook_preflight"' in record.message
    ]
    assert len(audit_records) == 1
    event = json.loads(audit_records[0].message)
    assert set(event) == {"event", "hook", "outcome", "plugin"}
    assert event == {
        "event": "mandatory_hook_preflight",
        "hook": "pre_api_request",
        "outcome": "allowed",
        "plugin": PLUGIN_NAME,
    }
    audit_text = audit_records[0].message
    for forbidden in (
        "SENTINEL-MESSAGE",
        "SENTINEL-CREDENTIAL",
        "SENTINEL-MODEL",
        "https://blocked.invalid",
    ):
        assert forbidden not in audit_text


def _configure_ordering_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    guard_action: str,
) -> None:
    hermes_home = tmp_path / "ordering-home"
    observer_dir = hermes_home / "plugins" / "a-observer"
    allow_guard_dir = hermes_home / "plugins" / "b-allow-guard"
    guard_dir = hermes_home / "plugins" / "z-guard"
    observer_dir.mkdir(parents=True)
    allow_guard_dir.mkdir(parents=True)
    guard_dir.mkdir(parents=True)
    for plugin_dir in (observer_dir, allow_guard_dir, guard_dir):
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": plugin_dir.name, "version": "0.1.0"}),
            encoding="utf-8",
        )
    (observer_dir / "__init__.py").write_text(
        "import builtins\n\n"
        "def _observe(**kw):\n"
        '    builtins._mandatory_preflight_events.append("observer")\n\n'
        "def register(ctx):\n"
        '    ctx.register_hook("pre_api_request", _observe)\n',
        encoding="utf-8",
    )
    (allow_guard_dir / "__init__.py").write_text(
        "import builtins\n\n"
        "def _guard(**kw):\n"
        '    builtins._mandatory_preflight_events.append("allow-guard")\n'
        '    return {"action": "allow"}\n\n'
        "def register(ctx):\n"
        '    ctx.register_hook("pre_api_request", _guard)\n',
        encoding="utf-8",
    )
    (guard_dir / "__init__.py").write_text(
        "import builtins\n\n"
        "def _guard(**kw):\n"
        '    builtins._mandatory_preflight_events.append("guard")\n'
        f'    return {{"action": {guard_action!r}, "reason": "blocked"}}\n\n'
        "def register(ctx):\n"
        '    ctx.register_hook("pre_api_request", _guard)\n',
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["a-observer", "b-allow-guard", "z-guard"],
                    "mandatory_hooks": {
                        "pre_api_request": ["b-allow-guard", "z-guard"]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_app_server"])
def test_later_mandatory_blocker_runs_before_all_provider_side_effects(
    tmp_path,
    monkeypatch,
    api_mode,
):
    from hermes_cli.middleware import apply_llm_request_middleware

    events = []
    monkeypatch.setattr(builtins, "_mandatory_preflight_events", events, raising=False)
    _configure_ordering_home(tmp_path, monkeypatch, guard_action="block")
    agent = _run_agent(api_mode=api_mode)
    transport = MagicMock(side_effect=lambda **kw: events.append("transport"))
    agent._run_codex_app_server_turn = transport

    with (
        patch(
            "hermes_cli.observability.observe_lifecycle",
            side_effect=lambda hook_name, **kw: (
                events.append("builtin-observer")
                if hook_name == "pre_api_request"
                else None
            ),
        ),
        patch(
            "hermes_cli.plugins._audit_mandatory_hook",
            side_effect=lambda *a, **kw: events.append("audit"),
        ),
        patch(
            "hermes_cli.middleware.apply_llm_request_middleware",
            side_effect=lambda *a, **kw: (
                events.append("middleware")
                or apply_llm_request_middleware(*a, **kw)
            ),
        ),
    ):
        result = agent.run_conversation("must block")

    assert result["failure_reason"] == "mandatory_hook_blocked"
    assert events == ["allow-guard", "guard"]
    agent.client.chat.completions.create.assert_not_called()
    transport.assert_not_called()


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_app_server"])
def test_complete_mandatory_phase_precedes_observers_audit_and_transport(
    tmp_path,
    monkeypatch,
    api_mode,
):
    from hermes_cli.middleware import apply_llm_request_middleware

    events = []
    monkeypatch.setattr(builtins, "_mandatory_preflight_events", events, raising=False)
    _configure_ordering_home(tmp_path, monkeypatch, guard_action="allow")
    agent = _run_agent(api_mode=api_mode)
    client = agent.client
    assert client is not None
    client.chat.completions.create.side_effect = lambda **kw: (
        events.append("transport") or _response()
    )
    transport = MagicMock(
        side_effect=lambda **kw: (
            events.append("transport") or _codex_transport_result()
        )
    )
    agent._run_codex_app_server_turn = transport

    with (
        patch(
            "hermes_cli.observability.observe_lifecycle",
            side_effect=lambda hook_name, **kw: (
                events.append("builtin-observer")
                if hook_name == "pre_api_request"
                else None
            ),
        ),
        patch(
            "hermes_cli.plugins._audit_mandatory_hook",
            side_effect=lambda *a, **kw: events.append("audit"),
        ),
        patch(
            "hermes_cli.middleware.apply_llm_request_middleware",
            side_effect=lambda *a, **kw: (
                events.append("middleware")
                or apply_llm_request_middleware(*a, **kw)
            ),
        ),
    ):
        result = agent.run_conversation("must allow")

    assert result["completed"] is True
    expected = [
        "allow-guard",
        "guard",
        "audit",
        "audit",
    ]
    if api_mode == "chat_completions":
        expected.append("middleware")
    expected.extend([
        "builtin-observer",
        "observer",
        "transport",
    ])
    assert events == expected


def test_default_and_legacy_config_leave_mandatory_hooks_disabled(tmp_path, monkeypatch):
    from hermes_cli.config import DEFAULT_CONFIG, load_config, migrate_config, read_raw_config

    hermes_home = tmp_path / "legacy-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "_config_version": DEFAULT_CONFIG["_config_version"] - 1,
                "model": "legacy-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    migrate_config(interactive=False, quiet=True)
    loaded = load_config()
    raw = read_raw_config()
    assert loaded["model"] == "legacy-model"
    assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert "mandatory_hooks" not in raw.get("plugins", {})
    assert plugins_mod._get_mandatory_hook_plugins("pre_api_request") == []
