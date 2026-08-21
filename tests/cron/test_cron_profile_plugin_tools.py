"""Runtime regressions for profile-scoped cron plugin tools."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


PUBLISHER_TOOL = "matcher_publish_score_batch"
PUBLISHER_TOOLSET = "matcher_score_publisher"
MATCHER_TOOLSETS = [
    "search",
    "file_read",
    PUBLISHER_TOOLSET,
    "no_mcp",
]
EXPECTED_MATCHER_TOOLS = {
    "web_search",
    "read_file",
    "search_files",
    PUBLISHER_TOOL,
}


def _write_plugin(profile_home: Path) -> None:
    plugin_dir = profile_home / "plugins" / "matcher-score-publisher"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: matcher-score-publisher",
                "version: 1.0.0",
                "provides_tools:",
                f"  - {PUBLISHER_TOOL}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "import json",
                "",
                "def _publish(args, **kwargs):",
                "    return json.dumps({'ok': True})",
                "",
                "def register(ctx):",
                "    ctx.register_tool(",
                f"        name={PUBLISHER_TOOL!r},",
                f"        toolset={PUBLISHER_TOOLSET!r},",
                "        schema={",
                f"            'name': {PUBLISHER_TOOL!r},",
                "            'description': 'Publish one score batch',",
                "            'parameters': {'type': 'object', 'properties': {}},",
                "        },",
                "        handler=_publish,",
                "    )",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def isolated_matcher_runtime(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    main_home = root / "profiles" / "main"
    matcher_home = root / "profiles" / "matcher"
    main_home.mkdir(parents=True)
    matcher_home.mkdir(parents=True)
    (root / "cron").mkdir(parents=True)

    (main_home / "config.yaml").write_text(
        "model: test-model\nplugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    (matcher_home / "config.yaml").write_text(
        "\n".join(
            [
                "model: test-model",
                "plugins:",
                "  enabled:",
                "    - matcher-score-publisher",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_plugin(matcher_home)

    monkeypatch.setenv("HERMES_HOME", str(main_home))
    monkeypatch.setattr("cron.jobs.CRON_DIR", root / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", root / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", root / "cron" / "output")

    import cron.scheduler as sched
    import hermes_cli.plugins as plugins
    from tools.registry import registry

    monkeypatch.setattr(sched, "_hermes_home", None)
    previous_manager = plugins._plugin_manager
    previous_publisher_entry = registry.get_entry(PUBLISHER_TOOL)
    previous_modules = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("hermes_plugins.matcher_score_publisher")
    }
    registry.deregister(PUBLISHER_TOOL)
    plugins._plugin_manager = plugins.PluginManager()

    try:
        yield root, main_home, matcher_home
    finally:
        registry.deregister(PUBLISHER_TOOL)
        if previous_publisher_entry is not None:
            registry.register(
                name=previous_publisher_entry.name,
                toolset=previous_publisher_entry.toolset,
                schema=previous_publisher_entry.schema,
                handler=previous_publisher_entry.handler,
                check_fn=previous_publisher_entry.check_fn,
                requires_env=previous_publisher_entry.requires_env,
                is_async=previous_publisher_entry.is_async,
                description=previous_publisher_entry.description,
                emoji=previous_publisher_entry.emoji,
                max_result_size_chars=previous_publisher_entry.max_result_size_chars,
                dynamic_schema_overrides=previous_publisher_entry.dynamic_schema_overrides,
            )
        plugins._plugin_manager = previous_manager
        for name in list(sys.modules):
            if name.startswith("hermes_plugins.matcher_score_publisher"):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)


def _install_runtime_stubs(monkeypatch, observed: dict) -> None:
    import cron.scheduler as sched

    class FakeAgent:
        def __init__(self, **kwargs):
            import model_tools
            from tools.registry import registry

            enabled = kwargs.get("enabled_toolsets")
            disabled = kwargs.get("disabled_toolsets")
            observed["enabled_toolsets"] = enabled
            observed["publisher_registered_before_agent"] = (
                registry.get_entry(PUBLISHER_TOOL) is not None
            )
            observed["tool_names"] = {
                item["function"]["name"]
                for item in model_tools.get_tool_definitions(
                    enabled_toolsets=enabled,
                    disabled_toolsets=disabled,
                    quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
            }

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": "done", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

        def close(self):
            return None

    fake_run_agent = type(sys)("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "test",
            "api_key": "test-key",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(sched, "_build_job_prompt", lambda *_a, **_kw: "score")
    monkeypatch.setattr(sched, "_resolve_origin", lambda _job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda _job: None)
    monkeypatch.setattr(
        sched,
        "_resolve_cron_enabled_toolsets",
        lambda _job, cfg: sched._merge_mcp_into_per_job_toolsets(
            list(MATCHER_TOOLSETS), cfg
        ),
    )
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import tools.mcp_tool as mcp_tool

    def record_mcp_discovery():
        observed["mcp_discovery_calls"] = observed.get("mcp_discovery_calls", 0) + 1
        return []

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", record_mcp_discovery)


def test_profile_cron_additively_loads_enabled_plugin_before_agent_init(
    isolated_matcher_runtime, monkeypatch
):
    import cron.scheduler as sched
    from hermes_cli.plugins import discover_plugins
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.registry import registry

    _root, main_home, _matcher_home = isolated_matcher_runtime
    observed: dict = {}
    _install_runtime_stubs(monkeypatch, observed)

    token = set_hermes_home_override(main_home)
    try:
        discover_plugins()
    finally:
        reset_hermes_home_override(token)
    assert registry.get_entry(PUBLISHER_TOOL) is None

    success, _output, _response, error = sched.run_job(
        {
            "id": "matcher-runtime",
            "name": "matcher-runtime",
            "profile": "matcher",
            "enabled_toolsets": list(MATCHER_TOOLSETS),
            "schedule_display": "manual",
        }
    )

    assert success is True, error
    assert observed["publisher_registered_before_agent"] is True
    assert registry.get_entry(PUBLISHER_TOOL) is not None


def test_profile_plugin_load_failure_prevents_agent_initialization(
    isolated_matcher_runtime, monkeypatch
):
    import cron.scheduler as sched
    import hermes_cli.plugins as plugins

    _root, _main_home, _matcher_home = isolated_matcher_runtime
    observed: dict = {}
    _install_runtime_stubs(monkeypatch, observed)

    monkeypatch.setattr(
        plugins.PluginManager,
        "load_profile_tools",
        lambda self, profile_home: (_ for _ in ()).throw(
            ValueError("profile tool contract failed")
        ),
    )

    success, _output, _response, error = sched.run_job(
        {
            "id": "matcher-plugin-failure",
            "name": "matcher-plugin-failure",
            "profile": "matcher",
            "enabled_toolsets": list(MATCHER_TOOLSETS),
            "schedule_display": "manual",
        }
    )

    assert success is False
    assert "profile tool contract failed" in (error or "")
    assert "publisher_registered_before_agent" not in observed


def test_profile_cron_exposes_only_read_search_and_publisher_tools(
    isolated_matcher_runtime, monkeypatch
):
    import cron.scheduler as sched
    from hermes_cli.plugins import discover_plugins
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _root, main_home, _matcher_home = isolated_matcher_runtime
    observed: dict = {}
    _install_runtime_stubs(monkeypatch, observed)

    token = set_hermes_home_override(main_home)
    try:
        discover_plugins()
    finally:
        reset_hermes_home_override(token)

    success, _output, _response, error = sched.run_job(
        {
            "id": "matcher-tools",
            "name": "matcher-tools",
            "profile": "matcher",
            "enabled_toolsets": list(MATCHER_TOOLSETS),
            "schedule_display": "manual",
        }
    )

    assert success is True, error
    assert observed["enabled_toolsets"] == MATCHER_TOOLSETS[:-1]
    assert observed["tool_names"] == EXPECTED_MATCHER_TOOLS
    assert observed["tool_names"].isdisjoint(
        {
            "terminal",
            "process",
            "send_message",
            "files",
            "write_file",
            "patch",
            "edit_file",
            "execute_code",
        }
    )
    assert not any(name.startswith("mcp_") for name in observed["tool_names"])
    assert observed.get("mcp_discovery_calls", 0) == 0
