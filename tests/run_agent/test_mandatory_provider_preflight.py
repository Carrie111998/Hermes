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
        "second_call": (
            'if kw.get("api_call_count") == 1:\n'
            '        return {"action": "allow"}\n'
            '    return {"action": "block", "reason": "second call denied"}'
        ),
        "block_exfil": (
            'if "EXFIL" in str(kw.get("request", {})):\n'
            '        return {"action": "block", "reason": "exfil denied"}\n'
            '    return {"action": "allow"}'
        ),
    }[mode]
    (plugin_dir / "__init__.py").write_text(
        "def _preflight(**kw):\n"
        f"    {callback_body}\n\n"
        "def register(ctx):\n"
        '    ctx.register_hook("pre_api_request", _preflight)\n',
        encoding="utf-8",
    )


def _write_exfil_middleware(hermes_home: Path) -> str:
    plugin_name = "synthetic-exfil-middleware"
    plugin_dir = hermes_home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": plugin_name, "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def _rewrite(request=None, **kw):\n"
        "    rewritten = dict(request or {})\n"
        "    field = 'input' if 'input' in rewritten else 'messages'\n"
        "    rewritten[field] = list(rewritten.get(field) or []) + "
        "[{'role': 'user', 'content': 'EXFIL'}]\n"
        "    return {'request': rewritten}\n\n"
        "def register(ctx):\n"
        "    ctx.register_middleware('llm_request', _rewrite)\n",
        encoding="utf-8",
    )
    return plugin_name


def _write_exfil_execution_middleware(hermes_home: Path) -> str:
    plugin_name = "synthetic-exfil-execution-middleware"
    plugin_dir = hermes_home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": plugin_name, "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def _rewrite(request=None, next_call=None, **kw):\n"
        "    rewritten = dict(request or {})\n"
        "    field = 'input' if 'input' in rewritten else 'messages'\n"
        "    rewritten[field] = list(rewritten.get(field) or []) + "
        "[{'role': 'user', 'content': 'EXFIL'}]\n"
        "    return next_call(rewritten)\n\n"
        "def register(ctx):\n"
        "    ctx.register_middleware('llm_execution', _rewrite)\n",
        encoding="utf-8",
    )
    return plugin_name


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
    assert events == ["middleware", "allow-guard", "guard", "audit"]
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
        "middleware",
        "allow-guard",
        "guard",
        "audit",
        "audit",
    ]
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


@pytest.mark.parametrize(
    ("mode", "expected_outcome"),
    [
        (None, "missing"),
        ("none", "malformed"),
        ("raise", "exception"),
        ("malformed", "malformed"),
        ("block", "blocked"),
    ],
)
def test_mandatory_denial_audit_is_sanitized_and_payload_free(
    tmp_path,
    monkeypatch,
    caplog,
    mode,
    expected_outcome,
):
    _configure_home(tmp_path, monkeypatch, mode=mode)
    agent = _run_agent()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        result = agent.run_conversation("SENTINEL-MESSAGE")

    assert result["failed"] is True
    records = [
        record
        for record in caplog.records
        if record.name == "hermes_cli.plugins"
        and '"event": "mandatory_hook_preflight"' in record.message
    ]
    assert len(records) == 1
    event = json.loads(records[0].message)
    assert set(event) == {"event", "hook", "outcome", "plugin"}
    assert event == {
        "event": "mandatory_hook_preflight",
        "hook": "pre_api_request",
        "outcome": expected_outcome,
        "plugin": PLUGIN_NAME,
    }
    for forbidden in (
        "SENTINEL-MESSAGE",
        "SENTINEL-CREDENTIAL",
        "SENTINEL-MODEL",
        "https://blocked.invalid",
    ):
        assert forbidden not in records[0].message


def test_invalid_mandatory_config_emits_sanitized_denial_audit(
    tmp_path,
    monkeypatch,
    caplog,
):
    hermes_home = _configure_home(
        tmp_path, monkeypatch, mode="route", mandatory=False
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"mandatory_hooks": ["invalid"]}}),
        encoding="utf-8",
    )
    agent = _run_agent()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        result = agent.run_conversation("SENTINEL-MESSAGE")

    assert result["failure_reason"] == "mandatory_hook_config_invalid"
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "hermes_cli.plugins"
        and '"event": "mandatory_hook_preflight"' in record.message
    ]
    assert records == [{
        "event": "mandatory_hook_preflight",
        "hook": "pre_api_request",
        "outcome": "config_invalid",
        "plugin": "config",
    }]


@pytest.mark.parametrize(
    "api_mode",
    ["chat_completions", "codex_responses", "codex_app_server"],
)
def test_request_middleware_rewrite_is_guarded_before_transport(
    tmp_path,
    monkeypatch,
    api_mode,
):
    hermes_home = _configure_home(
        tmp_path, monkeypatch, mode="block_exfil", mandatory=True
    )
    middleware_name = _write_exfil_middleware(hermes_home)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": [PLUGIN_NAME, middleware_name],
                    "mandatory_hooks": {"pre_api_request": [PLUGIN_NAME]},
                }
            }
        ),
        encoding="utf-8",
    )
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()
    agent = _run_agent(api_mode=api_mode)
    standard_transport = MagicMock(return_value=_response())
    app_server_transport = MagicMock(return_value=_codex_transport_result())
    agent._interruptible_api_call = standard_transport
    agent._interruptible_streaming_api_call = standard_transport
    agent._run_codex_app_server_turn = app_server_transport

    result = agent.run_conversation("safe before middleware")

    assert result["failure_reason"] == "mandatory_hook_blocked"
    assert result["api_calls"] == 0
    standard_transport.assert_not_called()
    app_server_transport.assert_not_called()


@pytest.mark.parametrize(
    "api_mode",
    ["chat_completions", "codex_responses", "codex_app_server"],
)
def test_execution_middleware_rewrite_is_guarded_before_transport(
    tmp_path,
    monkeypatch,
    api_mode,
):
    hermes_home = _configure_home(
        tmp_path, monkeypatch, mode="block_exfil", mandatory=True
    )
    middleware_name = _write_exfil_execution_middleware(hermes_home)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": [PLUGIN_NAME, middleware_name],
                    "mandatory_hooks": {"pre_api_request": [PLUGIN_NAME]},
                }
            }
        ),
        encoding="utf-8",
    )
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()
    agent = _run_agent(api_mode=api_mode)
    standard_transport = MagicMock(return_value=_response())
    app_server_transport = MagicMock(return_value=_codex_transport_result())
    agent._interruptible_api_call = standard_transport
    agent._interruptible_streaming_api_call = standard_transport
    agent._run_codex_app_server_turn = app_server_transport

    result = agent.run_conversation("safe before middleware")

    assert result["failure_reason"] == "mandatory_hook_blocked"
    assert result["api_calls"] == 0
    standard_transport.assert_not_called()
    app_server_transport.assert_not_called()


def test_nested_plugin_bare_manifest_name_resolves_to_canonical_key(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "nested-home"
    plugin_dir = hermes_home / "plugins" / "observability" / "langfuse"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "langfuse", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def _guard(**kw):\n"
        "    return {'action': 'allow'}\n\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_api_request', _guard)\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": ["langfuse"],
                "mandatory_hooks": {"pre_api_request": ["langfuse"]},
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()

    result = _run_agent().run_conversation("allowed")

    assert result["completed"] is True


def test_ambiguous_bare_mandatory_plugin_name_fails_closed(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "ambiguous-home"
    for category in ("one", "two"):
        plugin_dir = hermes_home / "plugins" / category / "guard"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "shared-guard", "version": "0.1.0"}),
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "def _guard(**kw): return {'action': 'allow'}\n"
            "def register(ctx): ctx.register_hook('pre_api_request', _guard)\n",
            encoding="utf-8",
        )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": ["shared-guard"],
                "mandatory_hooks": {"pre_api_request": ["shared-guard"]},
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()
    agent = _run_agent()

    result = agent.run_conversation("must not dispatch")

    assert result["failure_reason"] == "mandatory_hook_ambiguous"
    assert result["api_calls"] == 0
    agent.client.chat.completions.create.assert_not_called()


def test_later_tool_loop_preflight_block_preserves_completed_call_count(
    tmp_path,
    monkeypatch,
):
    _configure_home(tmp_path, monkeypatch, mode="second_call")
    agent = _run_agent()
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="noop", arguments="{}"),
    )
    first_message = SimpleNamespace(content="", tool_calls=[tool_call])
    first_response = SimpleNamespace(
        choices=[SimpleNamespace(message=first_message, finish_reason="tool_calls")],
        model="synthetic-response",
        usage=None,
    )
    agent.client.chat.completions.create.side_effect = [first_response]
    agent.valid_tool_names = {"noop"}

    def execute_tools(assistant_message, messages, *_args):
        for call in assistant_message.tool_calls:
            messages.append({
                "role": "tool",
                "name": call.function.name,
                "tool_call_id": call.id,
                "content": "done",
            })

    agent._execute_tool_calls = execute_tools

    result = agent.run_conversation("run one tool round")

    assert result["failure_reason"] == "mandatory_hook_blocked"
    assert result["api_calls"] == 1
    assert agent.client.chat.completions.create.call_count == 1


def test_mandatory_failure_result_defaults_to_one_completed_call():
    from agent.conversation_loop import _mandatory_preflight_failure_result
    from hermes_cli.plugins import MandatoryHookError

    result = _mandatory_preflight_failure_result(
        MandatoryHookError(
            "mandatory_hook_blocked", "pre_api_request", PLUGIN_NAME
        ),
        [],
    )

    assert result["api_calls"] == 1


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
def test_iteration_limit_summary_preflight_blocks_before_transport(
    tmp_path,
    monkeypatch,
    api_mode,
):
    from agent.chat_completion_helpers import handle_max_iterations

    _configure_home(tmp_path, monkeypatch, mode="block_exfil")
    agent = _run_agent(api_mode=api_mode)
    assert agent.client is not None
    chat_transport = agent.client.chat.completions.create
    codex_transport = MagicMock()
    agent._run_codex_stream = codex_transport

    result = handle_max_iterations(
        agent,
        [{"role": "user", "content": "EXFIL"}],
        api_call_count=1,
    )

    assert "blocked by mandatory preflight" in result
    chat_transport.assert_not_called()
    codex_transport.assert_not_called()
