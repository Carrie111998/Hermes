from __future__ import annotations

import sys

import pytest
import yaml

from agent.turn_gate import (
    GateDecision,
    GateState,
    RuntimeIdentity,
    TurnGateBlocked,
    TurnGateRequest,
    acquire_outer_turn,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    snapshot_turn_gate_providers,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _configure_gate(provider_id: str) -> None:
    configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": provider_id,
                    "runtime_identity": {"machine_id": "test-machine"},
                }
            }
        }
    )


class PluginGateProvider:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id

    def acquire(self, request):
        return GateDecision(
            provider_id=self.provider_id,
            state=GateState.OPEN,
            lease_id="lease-plugin",
            generation=26,
        )

    def validate(self, decision, checkpoint):
        return decision

    def release(self, decision):
        return None


@pytest.fixture(autouse=True)
def reset_gate():
    clear_turn_gate_registry_for_testing()
    yield
    clear_turn_gate_registry_for_testing()


def gate_request() -> TurnGateRequest:
    identity = RuntimeIdentity(
        machine_id="test-machine",
        profile="default",
        surface="gateway",
        session_instance_id="test-session",
        gateway_instance_id="test-gateway",
        turn_id="test-turn",
    )
    return TurnGateRequest(
        entrypoint="gateway",
        purpose="business",
        identity=identity,
    )


def test_plugin_context_registers_provider_under_its_own_manifest_id():
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    context = PluginContext(manifest, manager)
    context.register_turn_gate_provider(
        PluginGateProvider("secure-gate"), api_version=1
    )
    _configure_gate("secure-gate")

    with acquire_outer_turn(gate_request()) as lease:
        assert lease.provider_id == "secure-gate"


def test_plugin_provider_must_declare_exact_turn_gate_api_version():
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    context = PluginContext(manifest, manager)

    with pytest.raises(ValueError, match="API version"):
        context.register_turn_gate_provider(PluginGateProvider("secure-gate"))
    with pytest.raises(ValueError, match="API version"):
        context.register_turn_gate_provider(
            PluginGateProvider("secure-gate"),
            api_version=2,
        )


def test_plugin_provider_identity_mismatch_fails_closed():
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    context = PluginContext(manifest, manager)
    context.register_turn_gate_provider(
        PluginGateProvider("impersonated-gate"), api_version=1
    )
    _configure_gate("secure-gate")

    with pytest.raises(TurnGateBlocked, match="identity mismatch"):
        with acquire_outer_turn(gate_request()):
            pass


def test_force_reload_unregisters_stale_provider_even_in_safe_mode(monkeypatch):
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    context = PluginContext(manifest, manager)
    context.register_turn_gate_provider(
        PluginGateProvider("secure-gate"), api_version=1
    )
    _configure_gate("secure-gate")
    manager._discovered = True
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")

    manager.discover_and_load(force=True)

    with pytest.raises(TurnGateBlocked, match="required provider"):
        with acquire_outer_turn(gate_request()):
            pass


def test_failed_plugin_load_rolls_back_registered_gate_provider(tmp_path):
    plugin_dir = tmp_path / "failed-gate"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from agent.turn_gate import GateDecision, GateState\n\n"
        "class Provider:\n"
        "    def acquire(self, request):\n"
        "        return GateDecision(provider_id='failed-gate', state=GateState.OPEN, lease_id='failed-plugin-lease', generation=26)\n"
        "    def validate(self, decision, checkpoint):\n"
        "        return decision\n"
        "    def release(self, decision):\n"
        "        return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_turn_gate_provider(Provider(), api_version=1)\n"
        "    raise RuntimeError('replacement failed after registration')\n",
        encoding="utf-8",
    )
    manager = PluginManager()
    manifest = PluginManifest(
        name="failed-gate",
        key="failed-gate",
        source="user",
        path=str(plugin_dir),
    )

    manager._load_plugin(manifest)
    _configure_gate("failed-gate")

    assert manager._plugins["failed-gate"].enabled is False
    assert "replacement failed" in (manager._plugins["failed-gate"].error or "")
    with pytest.raises(TurnGateBlocked, match="required provider"):
        with acquire_outer_turn(gate_request()):
            pass


def test_failed_same_id_replacement_restores_previous_gate_provider(tmp_path):
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    PluginContext(manifest, manager).register_turn_gate_provider(
        PluginGateProvider("secure-gate"), api_version=1
    )

    plugin_dir = tmp_path / "replacement-gate"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from agent.turn_gate import GateDecision, GateState\n\n"
        "class Replacement:\n"
        "    def acquire(self, request):\n"
        "        return GateDecision(provider_id='secure-gate', state=GateState.OPEN, lease_id='partial-replacement', generation=26)\n"
        "    def validate(self, decision, checkpoint): return decision\n"
        "    def release(self, decision): return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_turn_gate_provider(Replacement(), api_version=1)\n"
        "    raise RuntimeError('same-id replacement failed')\n",
        encoding="utf-8",
    )
    replacement_manifest = PluginManifest(
        name="secure-gate",
        key="secure-gate",
        source="user",
        path=str(plugin_dir),
    )

    manager._load_plugin(replacement_manifest)
    _configure_gate("secure-gate")

    assert manager._plugins["secure-gate"].enabled is False
    with acquire_outer_turn(gate_request()) as lease:
        assert lease is not None
        assert lease.lease_id == "lease-plugin"


@pytest.mark.parametrize("failure_before_registration", [False, True])
def test_force_full_round_failure_restores_previous_gate_provider_and_state(
    tmp_path, monkeypatch, failure_before_registration
):
    """A ``force=True`` reload clears the live provider set up front, then runs
    the whole discovery round via the REAL ``_load_plugin``. If a plugin's
    ``register(ctx)`` registers a same-ID replacement provider and THEN raises,
    ``_load_plugin`` swallows the exception and only records ``loaded.error`` —
    so ``_discover_and_load_inner`` returns "successfully". The force round must
    still be judged failed: the previously-active provider object, the manager's
    provider tracking, and the other manager state cleared up front must all be
    restored exactly — not left holding an empty/half-applied gate registry that
    the up-front teardown already stripped of the live provider.
    """
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)

    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    old_provider = PluginGateProvider("secure-gate")
    PluginContext(manifest, manager).register_turn_gate_provider(
        old_provider, api_version=1
    )

    # Other pre-existing manager state that force-reload wipes before the round.
    sentinel_plugin = object()
    manager._plugins["pre-existing"] = sentinel_plugin
    manager._discovered = True

    # Exercise both real swallowed-error paths for the same-ID plugin: failure
    # during module import before registration, and failure after registering a
    # replacement provider. Either one must fail the whole force round.
    plugin_dir = tmp_path / "replacement-gate"
    plugin_dir.mkdir()
    if failure_before_registration:
        module_source = "raise RuntimeError('same-id replacement failed mid-round')\n"
    else:
        module_source = (
            "from agent.turn_gate import GateDecision, GateState\n\n"
            "class Replacement:\n"
            "    def acquire(self, request):\n"
            "        return GateDecision(provider_id='secure-gate', state=GateState.OPEN, lease_id='lease-replacement', generation=26)\n"
            "    def validate(self, decision, checkpoint): return decision\n"
            "    def release(self, decision): return None\n\n"
            "def register(ctx):\n"
            "    ctx.register_turn_gate_provider(Replacement(), api_version=1)\n"
            "    raise RuntimeError('same-id replacement failed mid-round')\n"
        )
    (plugin_dir / "__init__.py").write_text(module_source, encoding="utf-8")
    replacement_manifest = PluginManifest(
        name="secure-gate",
        key="secure-gate",
        source="user",
        path=str(plugin_dir),
    )

    def fake_inner():
        # The real round runs _load_plugin, which swallows either import or
        # post-registration RuntimeError and only records loaded.error.
        manager._load_plugin(replacement_manifest)
        loaded = manager._plugins["secure-gate"]
        assert loaded.enabled is False
        assert "same-id replacement failed" in (loaded.error or "")

    monkeypatch.setattr(manager, "_discover_and_load_inner", fake_inner)

    with pytest.raises(TurnGateBlocked, match="same-id replacement failed"):
        manager.discover_and_load(force=True)

    _configure_gate("secure-gate")

    # The exact previous provider object, its tracking, and other manager state
    # must be restored after the failed force round.
    restored = snapshot_turn_gate_providers().get("secure-gate")
    assert restored is not None
    assert restored.provider is old_provider
    assert "secure-gate" in manager._turn_gate_provider_ids
    assert manager._plugins.get("pre-existing") is sentinel_plugin
    with acquire_outer_turn(gate_request()) as lease:
        assert lease is not None
        assert lease.lease_id == "lease-plugin"


@pytest.mark.parametrize("failure_stage", ["register", "import"])
def test_force_round_rolls_back_unrelated_plugin_failure_after_gate_replacement(
    tmp_path, monkeypatch, failure_stage
):
    """Any later plugin failure aborts every host-owned mutation in the force round."""
    from tools.registry import registry

    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    manager = PluginManager()
    manifest = PluginManifest(name="secure-gate", key="secure-gate", source="user")
    old_provider = PluginGateProvider("secure-gate")
    PluginContext(manifest, manager).register_turn_gate_provider(
        old_provider, api_version=1
    )

    sentinel_plugin = object()
    sentinel_hook = lambda **_: None
    manager._plugins["pre-existing"] = sentinel_plugin  # type: ignore[assignment]
    manager._hooks["pre_tool_call"] = [sentinel_hook]
    manager._cli_commands["pre-cli"] = {"plugin": "pre-existing"}
    manager._plugin_commands["pre-command"] = {"plugin": "pre-existing"}
    manager._discovered = True

    replacement_dir = tmp_path / "replacement-gate"
    replacement_dir.mkdir()
    (replacement_dir / "__init__.py").write_text(
        "from agent.turn_gate import GateDecision, GateState\n\n"
        "class Replacement:\n"
        "    def acquire(self, request):\n"
        "        return GateDecision(provider_id='secure-gate', state=GateState.OPEN, lease_id='replacement', generation=26)\n"
        "    def validate(self, decision, checkpoint): return decision\n"
        "    def release(self, decision): return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_turn_gate_provider(Replacement(), api_version=1)\n",
        encoding="utf-8",
    )
    replacement_manifest = PluginManifest(
        name="secure-gate",
        key="secure-gate",
        source="user",
        path=str(replacement_dir),
    )

    broken_dir = tmp_path / "broken-plugin"
    broken_dir.mkdir()
    if failure_stage == "import":
        broken_source = "raise RuntimeError('unrelated plugin failed during import')\n"
    else:
        broken_source = (
            "def partial_tool(args, **kwargs): return 'partial'\n"
            "def register(ctx):\n"
            "    ctx.register_hook('post_tool_call', lambda **kwargs: None)\n"
            "    ctx.register_tool(\n"
            "        name='force_partial_tool', toolset='force-partial',\n"
            "        schema={'name': 'force_partial_tool', 'description': 'partial', 'parameters': {'type': 'object', 'properties': {}}},\n"
            "        handler=partial_tool,\n"
            "    )\n"
            "    ctx.register_command('force-partial-command', lambda args: 'partial')\n"
            "    ctx.register_cli_command('force-partial-cli', 'partial', lambda parser: None)\n"
            "    raise RuntimeError('unrelated plugin failed during register')\n"
        )
    (broken_dir / "__init__.py").write_text(broken_source, encoding="utf-8")
    broken_manifest = PluginManifest(
        name="broken-plugin",
        key="broken-plugin",
        source="user",
        path=str(broken_dir),
    )

    with registry._lock:
        tool_state_before = {
            "tools": dict(registry._tools),
            "plugin_override_policy": dict(registry._plugin_override_policy),
            "toolset_checks": dict(registry._toolset_checks),
            "toolset_aliases": dict(registry._toolset_aliases),
            "generation": registry._generation,
        }
    modules_before = {
        name: module
        for name, module in sys.modules.items()
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }

    def fake_inner():
        manager._load_plugin(replacement_manifest)
        assert manager._plugins["secure-gate"].enabled is True
        manager._load_plugin(broken_manifest)

    monkeypatch.setattr(manager, "_discover_and_load_inner", fake_inner)

    try:
        with pytest.raises(RuntimeError, match="unrelated plugin failed"):
            manager.discover_and_load(force=True)

        restored = snapshot_turn_gate_providers().get("secure-gate")
        assert restored is not None
        assert restored.provider is old_provider
        assert manager._plugins == {"pre-existing": sentinel_plugin}
        assert manager._hooks == {"pre_tool_call": [sentinel_hook]}
        assert manager._cli_commands == {"pre-cli": {"plugin": "pre-existing"}}
        assert manager._plugin_commands == {
            "pre-command": {"plugin": "pre-existing"}
        }
        assert registry.get_entry("force_partial_tool") is None
        with registry._lock:
            assert registry._tools == tool_state_before["tools"]
            assert registry._plugin_override_policy == tool_state_before[
                "plugin_override_policy"
            ]
            assert registry._toolset_checks == tool_state_before["toolset_checks"]
            assert registry._toolset_aliases == tool_state_before["toolset_aliases"]
            assert registry._generation == tool_state_before["generation"]
        modules_after = {
            name: module
            for name, module in sys.modules.items()
            if name == "hermes_plugins" or name.startswith("hermes_plugins.")
        }
        assert modules_after == modules_before
    finally:
        # Keep the RED run hermetic even before rollback exists in production.
        with registry._lock:
            registry._tools.clear()
            registry._tools.update(tool_state_before["tools"])
            registry._plugin_override_policy.clear()
            registry._plugin_override_policy.update(
                tool_state_before["plugin_override_policy"]
            )
            registry._toolset_checks.clear()
            registry._toolset_checks.update(tool_state_before["toolset_checks"])
            registry._toolset_aliases.clear()
            registry._toolset_aliases.update(tool_state_before["toolset_aliases"])
            registry._generation = tool_state_before["generation"]
        for name in tuple(sys.modules):
            if (
                name == "hermes_plugins" or name.startswith("hermes_plugins.")
            ) and name not in modules_before:
                sys.modules.pop(name, None)
        sys.modules.update(modules_before)


def test_temp_hermes_home_discovers_required_gate_end_to_end(tmp_path, monkeypatch):
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "hermes-home"
    plugin_dir = home / "plugins" / "secure-gate"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "secure-gate", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from agent.turn_gate import GateDecision, GateState\n\n"
        "class Provider:\n"
        "    def acquire(self, request):\n"
        "        return GateDecision(provider_id='secure-gate', state=GateState.OPEN, lease_id='e2e', generation=26)\n"
        "    def validate(self, decision, checkpoint):\n"
        "        return decision\n"
        "    def release(self, decision):\n"
        "        return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_turn_gate_provider(Provider(), api_version=1)\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["secure-gate"]},
                "agent": {
                    "turn_gate": {
                        "required_provider": "secure-gate",
                        "runtime_identity": {"machine_id": "test-machine"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_mod, "_plugin_manager", PluginManager())

    plugins_mod.discover_plugins()
    with acquire_outer_turn(gate_request()) as lease:
        assert lease.provider_id == "secure-gate"
        assert lease.lease_id == "e2e"


def test_force_reload_refreshes_plugin_relative_submodules(tmp_path, monkeypatch):
    import importlib
    import shutil

    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    plugin_dir = tmp_path / "submodule-gate"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from .provider import Provider\n\n"
        "def register(ctx):\n"
        "    ctx.register_turn_gate_provider(Provider(), api_version=1)\n",
        encoding="utf-8",
    )
    provider_file = plugin_dir / "provider.py"

    def write_provider(generation: int) -> None:
        provider_file.write_text(
            "from agent.turn_gate import GateDecision, GateState\n\n"
            "class Provider:\n"
            "    def acquire(self, request):\n"
            f"        return GateDecision(provider_id='submodule-gate', state=GateState.OPEN, lease_id='submodule', generation={generation})\n"
            "    def validate(self, decision, checkpoint): return self.acquire(None)\n"
            "    def release(self, decision): return None\n",
            encoding="utf-8",
        )
        shutil.rmtree(plugin_dir / "__pycache__", ignore_errors=True)
        importlib.invalidate_caches()

    write_provider(1)
    manifest = PluginManifest(
        name="submodule-gate",
        key="submodule-gate",
        source="user",
        path=str(plugin_dir),
    )
    manager = PluginManager()
    manager._load_plugin(manifest)
    _configure_gate("submodule-gate")
    with acquire_outer_turn(gate_request()) as lease:
        assert lease.generation == 1

    write_provider(2)
    monkeypatch.setattr(
        manager,
        "_discover_and_load_inner",
        lambda: manager._load_plugin(manifest),
    )
    manager.discover_and_load(force=True)
    _configure_gate("submodule-gate")
    with acquire_outer_turn(gate_request()) as lease:
        assert lease.generation == 2
