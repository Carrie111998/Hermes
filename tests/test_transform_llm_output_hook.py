"""Tests for notification-only ``transform_llm_output`` plugin events."""

from pathlib import Path

import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager, VALID_HOOKS


def _make_enabled_plugin(hermes_home: Path, name: str, register_body: str) -> Path:
    """Create a plugin under <hermes_home>/plugins/<name> and opt it in."""
    plugin_dir = hermes_home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0"}), encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    {register_body}\n",
        encoding="utf-8",
    )
    cfg_path = hermes_home / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return plugin_dir


def test_transform_llm_output_in_valid_hooks():
    assert "transform_llm_output" in VALID_HOOKS


def test_hook_receives_expected_kwargs(tmp_path, monkeypatch):
    """The callback sees a snapshot, but its replacement return is ignored."""
    del tmp_path, monkeypatch
    mgr = PluginManager()
    seen = []

    def callback(**kwargs):
        seen.append(kwargs)
        kwargs["response_text"] = "mutated snapshot"
        return "replacement"

    mgr._hooks["transform_llm_output"] = [callback]

    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="hello world",
        session_id="s1",
        model="anthropic/claude-sonnet-4.6",
        platform="cli",
    )
    assert results == []
    assert seen[0]["session_id"] == "s1"
    assert seen[0]["model"] == "anthropic/claude-sonnet-4.6"
    assert seen[0]["platform"] == "cli"






def test_hook_exception_does_not_replace_response(tmp_path, monkeypatch):
    """A plugin raising an exception must not break hook dispatch.

    PluginManager.invoke_hook catches per-callback exceptions, logs a
    warning, and continues — so a raising plugin contributes no entry
    to the results list, and the walk in run_agent.py finds nothing to
    replace with.
    """
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "raising_hook",
        register_body=(
            'def _boom(**kw):\n'
            '        raise RuntimeError("boom")\n'
            '    ctx.register_hook("transform_llm_output", _boom)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="keep me",
        session_id="s1",
        model="m",
        platform="cli",
    )

    final_response = "keep me"
    for _hook_result in results:
        if isinstance(_hook_result, str) and _hook_result:
            final_response = _hook_result
            break

    assert final_response == "keep me"


def test_no_plugins_returns_empty_results(tmp_path, monkeypatch):
    """With no plugins loaded, invoke_hook returns [] and the response is unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_empty"))
    plugins_mod._plugin_manager = PluginManager()

    mgr = plugins_mod._plugin_manager
    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="unchanged",
        session_id="",
        model="m",
        platform="",
    )
    assert results == []
