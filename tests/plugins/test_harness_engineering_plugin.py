import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _load_plugin():
    plugin_path = Path(__file__).resolve().parents[2] / "plugins" / "harness_engineering" / "__init__.py"
    spec = importlib.util.spec_from_file_location("harness_engineering_under_test", plugin_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_allows_plain_explanation(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HARNESS_PREFLIGHT", "advisory")

    result = plugin._handle_pre_gateway_dispatch(SimpleNamespace(text="解释一下什么是 MCP"))

    assert result == {"action": "allow"}


def test_preflight_rewrites_engineering_task(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HARNESS_PREFLIGHT", "advisory")

    result = plugin._handle_pre_gateway_dispatch(SimpleNamespace(text="请修复这个 WebUI bug 并加测试"))

    assert result["action"] == "rewrite"
    assert "Harness / Agenting Engineering preflight" in result["text"]
    assert result["text"].endswith("请修复这个 WebUI bug 并加测试")


def test_preflight_strict_uses_intake_notice(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HARNESS_PREFLIGHT", "strict")

    result = plugin._handle_pre_gateway_dispatch(SimpleNamespace(text="重构 gateway 认证流程"))

    assert result["action"] == "rewrite"
    assert "intake required" in result["text"]


def test_preflight_advisory_requires_intake_for_high_risk(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HARNESS_PREFLIGHT", "advisory")

    result = plugin._handle_pre_gateway_dispatch(SimpleNamespace(text="请修改 OAuth token 存储并 force-push"))

    assert result["action"] == "rewrite"
    assert "intake required" in result["text"]


def test_task_classifier_routes_research_without_harness():
    plugin = _load_plugin()

    result = plugin.classify_task("调研并对比三个 MCP server 方案")

    assert result.task_type == "research"
    assert result.harness_required is False
    assert result.route == "research_then_report"


def test_task_classifier_routes_multi_agent_as_intake_required():
    plugin = _load_plugin()

    result = plugin.classify_task("把这个功能拆成 Kanban 多代理并行执行")

    assert result.task_type == "multi_agent_project"
    assert result.harness_required is True
    assert result.route == "intake_required"


def test_task_classifier_routes_small_code_change():
    plugin = _load_plugin()

    result = plugin.classify_task("请做一个 small fix 并加测试")

    assert result.task_type == "small_code_change"
    assert result.harness_required is False
    assert result.route == "bounded_engineering"


def test_classify_cli_emits_json(capsys):
    plugin = _load_plugin()

    try:
        plugin._handle_harness_cli(SimpleNamespace(harness_action="classify", text="重构认证流程", format="json"))
    except SystemExit as exc:
        assert exc.code == 0

    out = capsys.readouterr().out
    assert '"task_type": "high_risk_change"' in out
    assert '"route": "intake_required"' in out


def test_preflight_uses_config_when_env_unset(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.delenv("HERMES_HARNESS_PREFLIGHT", raising=False)
    monkeypatch.setattr(plugin, "_configured_preflight_mode", lambda: "off")

    result = plugin._handle_pre_gateway_dispatch(SimpleNamespace(text="请修复这个 bug"))

    assert result == {"action": "allow"}


def test_helper_command_prefers_bundled_script(monkeypatch, tmp_path):
    plugin = _load_plugin()
    bundled = tmp_path / "repo" / "skills" / "software-development" / "harness-agenting-engineering" / "scripts" / "harness_intake.py"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    user_helper = tmp_path / "home" / ".hermes" / "bin" / "hermes-harness"
    user_helper.parent.mkdir(parents=True)
    user_helper.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("PYTHON", "python3.12")
    monkeypatch.setattr(plugin, "_bundled_helper_path", lambda: bundled)
    monkeypatch.setattr(plugin, "_user_helper_path", lambda: user_helper)

    assert plugin._helper_command() == ["python3.12", str(bundled)]


def test_helper_command_falls_back_to_user_helper(monkeypatch, tmp_path):
    plugin = _load_plugin()
    bundled = tmp_path / "missing" / "harness_intake.py"
    user_helper = tmp_path / "home" / ".hermes" / "bin" / "hermes-harness"
    user_helper.parent.mkdir(parents=True)
    user_helper.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(plugin, "_bundled_helper_path", lambda: bundled)
    monkeypatch.setattr(plugin, "_user_helper_path", lambda: user_helper)

    assert plugin._helper_command() == [str(user_helper)]


def test_register_exposes_harness_command_and_gateway_hook():
    plugin = _load_plugin()
    ctx = MagicMock()

    plugin.register(ctx)

    cli_call = ctx.register_cli_command.call_args
    assert cli_call.args[0] == "harness"
    ctx.register_command.assert_called_once()
    assert ctx.register_command.call_args.args[0] == "intake"
    ctx.register_hook.assert_called_once()
    assert ctx.register_hook.call_args.args[0] == "pre_gateway_dispatch"
