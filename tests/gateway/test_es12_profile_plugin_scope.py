"""Regression tests for the profile-scoped PluginManager cache.

A multiplexed gateway routes each inbound message to a different profile under
``gateway/run.py::_profile_runtime_scope``. Plugin discovery used to run against
one process-global PluginManager whose ``_discovered`` flag latched on the first
profile to trip it, so every profile routed afterwards got the first profile's
plugin registry and its own ``plugins/`` directory was never scanned.
"""

from pathlib import Path
import sys
import threading
import time

import pytest
import yaml

import hermes_cli.plugins as plugins_mod


def _make_profile(home: Path, plugin_name: str) -> Path:
    """Create a profile home holding exactly one enabled plugin."""
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": plugin_name, "version": "0.1.0"}), encoding="utf-8",
    )
    # Register a hook so the plugin is observable through the manager's public
    # surface rather than only through the manifest list.
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_hook('transform_llm_output', lambda **kw: {plugin_name!r})\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [plugin_name]}}), encoding="utf-8",
    )
    return home


@pytest.fixture(autouse=True)
def _clean_manager_cache():
    """The cache is process-global; do not leak managers between tests."""
    modules_before = set(sys.modules)
    plugins_mod.reset_plugin_managers()
    yield
    plugins_mod.reset_plugin_managers()
    for name in set(sys.modules) - modules_before:
        if name.startswith("hermes_plugins."):
            sys.modules.pop(name, None)


@pytest.fixture
def two_profiles(tmp_path):
    profile_a = _make_profile(tmp_path / "profile_a", "es12_plugin_a")
    profile_b = _make_profile(tmp_path / "profile_b", "es12_plugin_b")
    return profile_a, profile_b


def _loaded_plugin_names(manager) -> set:
    return {p["name"] for p in manager.list_plugins() if p["enabled"]}


def test_each_profile_gets_its_own_manager(two_profiles):
    """Two profiles must not share one PluginManager instance."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        manager_a = plugins_mod.get_plugin_manager()
    with _profile_runtime_scope(profile_b):
        manager_b = plugins_mod.get_plugin_manager()

    assert manager_a is not manager_b

    # Re-entering a profile returns that profile's manager, not a fresh one —
    # the cache lifecycle matches the old singleton within a profile.
    with _profile_runtime_scope(profile_a):
        assert plugins_mod.get_plugin_manager() is manager_a


def test_second_profile_plugins_load_after_first_profile_discovered(two_profiles):
    """The bug: profile A tripping discovery must not blank profile B."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        plugins_mod.discover_plugins()
        names_a = _loaded_plugin_names(plugins_mod.get_plugin_manager())

    # Profile B is routed *after* A has already latched discovery.
    with _profile_runtime_scope(profile_b):
        plugins_mod.discover_plugins()
        names_b = _loaded_plugin_names(plugins_mod.get_plugin_manager())

    assert "es12_plugin_a" in names_a
    assert "es12_plugin_b" in names_b
    # And neither profile sees the other's plugin.
    assert "es12_plugin_b" not in names_a
    assert "es12_plugin_a" not in names_b


def test_profile_hooks_do_not_bleed(two_profiles):
    """Hook registries are per-profile, not merged into one process registry."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles

    with _profile_runtime_scope(profile_a):
        plugins_mod.discover_plugins()
    with _profile_runtime_scope(profile_b):
        plugins_mod.discover_plugins()
        results_b = plugins_mod.invoke_hook("transform_llm_output", response_text="x")
    with _profile_runtime_scope(profile_a):
        results_a = plugins_mod.invoke_hook("transform_llm_output", response_text="x")

    assert results_a == ["es12_plugin_a"]
    assert results_b == ["es12_plugin_b"]


def test_profile_discovery_is_serialized_across_managers(two_profiles, monkeypatch):
    """Concurrent profile startup must not race imports in a shared namespace."""
    from gateway.run import _profile_runtime_scope

    profile_a, profile_b = two_profiles
    start = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    errors = []

    def observe_discovery(_manager):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1

    monkeypatch.setattr(
        plugins_mod.PluginManager,
        "_discover_and_load_inner",
        observe_discovery,
    )

    def discover(home):
        try:
            start.wait(timeout=1)
            with _profile_runtime_scope(home):
                plugins_mod.discover_plugins()
        except BaseException as exc:  # pragma: no cover - failure capture
            errors.append(exc)

    threads = [
        threading.Thread(target=discover, args=(profile_a,)),
        threading.Thread(target=discover, args=(profile_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1


def test_profiles_reuse_an_identical_directory_plugin_module(tmp_path):
    """Two profile managers may register one source without re-importing it."""
    from gateway.run import _profile_runtime_scope

    shared = tmp_path / "shared_plugin"
    shared.mkdir()
    (shared / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "shared_relative", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (shared / "schemas.py").write_text(
        "SHARED_VALUE = 'loaded'\n",
        encoding="utf-8",
    )
    (shared / "__init__.py").write_text(
        "from .schemas import SHARED_VALUE\n"
        "def register(ctx):\n"
        "    ctx.register_hook("
        "'transform_llm_output', lambda **kw: SHARED_VALUE"
        ")\n",
        encoding="utf-8",
    )

    homes = [tmp_path / "profile_a", tmp_path / "profile_b"]
    managers = []
    for home in homes:
        (home / "plugins").mkdir(parents=True)
        (home / "plugins" / "shared_relative").symlink_to(
            shared,
            target_is_directory=True,
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["shared_relative"]}}),
            encoding="utf-8",
        )
        with _profile_runtime_scope(home):
            plugins_mod.discover_plugins()
            managers.append(plugins_mod.get_plugin_manager())

    first = managers[0]._plugins["shared_relative"]
    second = managers[1]._plugins["shared_relative"]
    assert first.module is second.module
    assert first.error is None
    assert second.error is None
    assert managers[0].invoke_hook("transform_llm_output") == ["loaded"]
    assert managers[1].invoke_hook("transform_llm_output") == ["loaded"]


def test_profiles_reject_same_plugin_key_from_different_sources(tmp_path):
    from gateway.run import _profile_runtime_scope

    managers = []
    for index, value in enumerate(("profile-a", "profile-b")):
        home = tmp_path / f"profile_{index}"
        plugin = home / "plugins" / "shared_relative"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(
            yaml.safe_dump({"name": "shared_relative", "version": "0.1.0"}),
            encoding="utf-8",
        )
        (plugin / "schemas.py").write_text(
            f"SHARED_VALUE = {value!r}\n",
            encoding="utf-8",
        )
        (plugin / "__init__.py").write_text(
            "from .schemas import SHARED_VALUE\n"
            "def register(ctx):\n"
            "    ctx.register_hook("
            "'transform_llm_output', lambda **kw: SHARED_VALUE"
            ")\n",
            encoding="utf-8",
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["shared_relative"]}}),
            encoding="utf-8",
        )
        with _profile_runtime_scope(home):
            plugins_mod.discover_plugins()
            managers.append(plugins_mod.get_plugin_manager())

    first = managers[0]._plugins["shared_relative"]
    second = managers[1]._plugins["shared_relative"]
    assert first.enabled is True
    assert first.error is None
    assert managers[0].invoke_hook("transform_llm_output") == ["profile-a"]
    assert second.enabled is False
    assert "different source" in (second.error or "")
    assert managers[1].invoke_hook("transform_llm_output") == []


def test_profiles_do_not_reuse_a_partially_imported_identical_plugin(tmp_path):
    from gateway.run import _profile_runtime_scope

    shared = tmp_path / "shared_plugin"
    shared.mkdir()
    (shared / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "shared_failed", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (shared / "schemas.py").write_text(
        "SHARED_VALUE = 'partial'\n",
        encoding="utf-8",
    )
    (shared / "__init__.py").write_text(
        "from .schemas import SHARED_VALUE\n"
        "def register(ctx):\n"
        "    ctx.register_hook("
        "'transform_llm_output', lambda **kw: SHARED_VALUE"
        ")\n"
        "raise RuntimeError('import failed')\n",
        encoding="utf-8",
    )

    managers = []
    for name in ("profile_a", "profile_b"):
        home = tmp_path / name
        (home / "plugins").mkdir(parents=True)
        (home / "plugins" / "shared_failed").symlink_to(
            shared,
            target_is_directory=True,
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["shared_failed"]}}),
            encoding="utf-8",
        )
        with _profile_runtime_scope(home):
            plugins_mod.discover_plugins()
            managers.append(plugins_mod.get_plugin_manager())

    for manager in managers:
        loaded = manager._plugins["shared_failed"]
        assert loaded.enabled is False
        assert "import failed" in (loaded.error or "")
        assert manager.invoke_hook("transform_llm_output") == []
    assert "hermes_plugins.shared_failed" not in sys.modules
    assert "hermes_plugins.shared_failed.schemas" not in sys.modules


def test_single_profile_gateway_behaviour_unchanged(tmp_path, monkeypatch):
    """Without a profile scope the manager is a stable process singleton."""
    monkeypatch.setenv("HERMES_HOME", str(_make_profile(tmp_path / "solo", "es12_solo")))

    first = plugins_mod.get_plugin_manager()
    second = plugins_mod.get_plugin_manager()

    assert first is second


def test_explicit_pin_still_wins(tmp_path, monkeypatch):
    """``_plugin_manager`` remains the override seam tests already rely on."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "solo"))
    pinned = plugins_mod.PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", pinned)

    assert plugins_mod.get_plugin_manager() is pinned
