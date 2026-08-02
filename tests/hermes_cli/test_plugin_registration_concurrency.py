"""Concurrency contracts for transactional plugin registration."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.browser_provider import BrowserProvider
from agent.image_gen_provider import ImageGenProvider
from agent.secret_sources.base import FetchResult, SecretSource
from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider
from agent.video_gen_provider import VideoGenProvider
from agent.web_search_provider import WebSearchProvider
from gateway.platform_registry import PlatformEntry, PlatformRegistry
from hermes_cli.dashboard_auth import DashboardAuthProvider
import hermes_cli.plugins as plugins_module
from hermes_cli.plugins import PluginManager
from tools.registry import registry


_SUPPORT_MODULE = "h1_transaction_test_support"
_TOOL_NAME = "h1_transaction_generation_tool"
_PLUGIN_NAME = "h1-transaction-concurrency"


class _ImageProvider(ImageGenProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def generate(self, prompt: str, aspect_ratio: str = "landscape", **kwargs: Any) -> dict:
        return {"marker": self.marker}


class _VideoProvider(VideoGenProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def generate(self, prompt: str, **kwargs: Any) -> dict:
        return {"marker": self.marker}


class _WebProvider(WebSearchProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict:
        return {"marker": self.marker}


class _BrowserProvider(BrowserProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def create_session(self, task_id: str) -> dict:
        return {"marker": self.marker}

    def close_session(self, session_id: str) -> bool:
        return True

    def emergency_cleanup(self, session_id: str) -> None:
        return None


class _TTSProvider(TTSProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def synthesize(self, text: str, output_path: str, **kwargs: Any) -> str:
        return output_path


class _STTProvider(TranscriptionProvider):
    def __init__(self, name: str, marker: str) -> None:
        self._name = name
        self.marker = marker

    @property
    def name(self) -> str:
        return self._name

    def transcribe(self, file_path: str, **kwargs: Any) -> dict:
        return {"success": True, "transcript": self.marker, "provider": self.name}


class _DashboardProvider(DashboardAuthProvider):
    name = "h1-dashboard-test"
    display_name = "H1 dashboard test"

    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self.display_name = marker
        self.marker = marker

    def start_login(self, *, redirect_uri: str):
        raise NotImplementedError

    def complete_login(self, **kwargs: Any):
        raise NotImplementedError

    def verify_session(self, *, access_token: str):
        return None

    def refresh_session(self, *, refresh_token: str):
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


class _SecretProvider(SecretSource):
    shape = "mapped"

    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self.label = marker
        self.marker = marker

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        return FetchResult()


def _external_values(marker: str, namespace: str) -> dict[str, Any]:
    names = {
        surface: f"h1-{namespace}-{surface.replace('_', '-')}"
        for surface in (
            "image_gen",
            "video_gen",
            "web",
            "browser",
            "secret",
            "tts",
            "stt",
            "dashboard",
            "platform",
        )
    }
    names["secret"] = f"h1_{namespace.replace('-', '_')}_secret"
    return {
        "image_gen": _ImageProvider(names["image_gen"], marker),
        "video_gen": _VideoProvider(names["video_gen"], marker),
        "web": _WebProvider(names["web"], marker),
        "browser": _BrowserProvider(names["browser"], marker),
        "secret": _SecretProvider(names["secret"], marker),
        "tts": _TTSProvider(names["tts"], marker),
        "stt": _STTProvider(names["stt"], marker),
        "dashboard": _DashboardProvider(names["dashboard"], marker),
        "platform": PlatformEntry(
            name=names["platform"],
            label=marker,
            adapter_factory=lambda config, value=marker: value,
            check_fn=lambda: True,
            source="plugin",
            plugin_name=_PLUGIN_NAME,
        ),
    }


def _write_plugin(home: Path) -> None:
    plugin_dir = home / "plugins" / _PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": _PLUGIN_NAME, "version": "1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        f'''import {_SUPPORT_MODULE} as support

def register(ctx):
    values = support.values
    ctx.register_image_gen_provider(values["image_gen"])
    ctx.register_video_gen_provider(values["video_gen"])
    ctx.register_web_search_provider(values["web"])
    ctx.register_browser_provider(values["browser"])
    ctx.register_secret_source(values["secret"])
    ctx.register_tts_provider(values["tts"])
    ctx.register_transcription_provider(values["stt"])
    ctx.register_dashboard_auth_provider(values["dashboard"])
    platform = values["platform"]
    ctx.register_platform(
        name=platform.name,
        label=platform.label,
        adapter_factory=platform.adapter_factory,
        check_fn=platform.check_fn,
    )
    if support.register_generation_state:
        ctx.register_tool(
            {_TOOL_NAME!r},
            "h1_transaction",
            {{
                "name": {_TOOL_NAME!r},
                "description": support.marker,
                "parameters": {{"type": "object", "properties": {{}}}},
            }},
            support.tool_handler,
        )
        ctx.register_hook("post_tool_call", support.hook)
    support.started.set()
    if not support.release.wait(timeout=5):
        raise RuntimeError("test release timeout")
    if support.fail:
        raise RuntimeError("planned registration failure")
''',
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [_PLUGIN_NAME]}}),
        encoding="utf-8",
    )


def _support(
    values: dict[str, Any],
    marker: str,
    *,
    released: bool = False,
    fail: bool = False,
    register_generation_state: bool = False,
) -> types.ModuleType:
    module = types.ModuleType(_SUPPORT_MODULE)
    module.values = values
    module.marker = marker
    module.started = threading.Event()
    module.release = threading.Event()
    if released:
        module.release.set()
    module.fail = fail
    module.register_generation_state = register_generation_state
    module.tool_handler = lambda args, **kwargs: marker
    module.hook = lambda **kwargs: marker
    sys.modules[_SUPPORT_MODULE] = module
    return module


def _start_discovery(
    manager: PluginManager,
    *,
    force: bool = False,
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    done = threading.Event()
    failures: list[BaseException] = []

    def _run() -> None:
        try:
            manager.discover_and_load(force=force)
        except BaseException as exc:  # pragma: no cover - asserted by callers
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, done, failures


def _join(thread: threading.Thread, done: threading.Event) -> None:
    assert done.wait(timeout=5), "plugin discovery did not finish"
    thread.join(timeout=1)
    assert not thread.is_alive()


def _external_readers(platform_registry: PlatformRegistry) -> dict[str, Any]:
    from agent import (
        browser_registry,
        image_gen_registry,
        transcription_registry,
        tts_registry,
        video_gen_registry,
        web_search_registry,
    )
    from agent.secret_sources import registry as secret_registry
    from hermes_cli.dashboard_auth import get_provider as get_dashboard_provider

    return {
        "image_gen": image_gen_registry.get_provider,
        "video_gen": video_gen_registry.get_provider,
        "web": web_search_registry.get_provider,
        "browser": browser_registry.get_provider,
        "secret": secret_registry.get_source,
        "tts": tts_registry.get_provider,
        "stt": transcription_registry.get_provider,
        "dashboard": get_dashboard_provider,
        "platform": platform_registry.get,
    }


def _read_external_generation(
    values: dict[str, Any],
    platform_registry: PlatformRegistry,
) -> dict[str, Any]:
    readers = _external_readers(platform_registry)
    return {
        surface: readers[surface](value.name)
        for surface, value in values.items()
    }


def _register_external_direct(
    surface: str,
    value: Any,
    platform_registry: PlatformRegistry,
) -> None:
    from agent import (
        browser_registry,
        image_gen_registry,
        transcription_registry,
        tts_registry,
        video_gen_registry,
        web_search_registry,
    )
    from agent.secret_sources import registry as secret_registry
    from hermes_cli.dashboard_auth import register_provider as register_dashboard

    registrations = {
        "image_gen": image_gen_registry.register_provider,
        "video_gen": video_gen_registry.register_provider,
        "web": web_search_registry.register_provider,
        "browser": browser_registry.register_provider,
        "secret": secret_registry.register_source,
        "tts": tts_registry.register_provider,
        "stt": transcription_registry.register_provider,
        "dashboard": register_dashboard,
        "platform": platform_registry.register,
    }
    registrations[surface](value)


@pytest.fixture(autouse=True)
def isolated_registries(tmp_path, monkeypatch):
    from agent import (
        browser_registry,
        image_gen_registry,
        transcription_registry,
        tts_registry,
        video_gen_registry,
        web_search_registry,
    )
    from agent.secret_sources import registry as secret_registry
    from gateway import platform_registry as platform_module
    from hermes_cli.dashboard_auth import clear_providers

    provider_modules = (
        image_gen_registry,
        video_gen_registry,
        web_search_registry,
        browser_registry,
        tts_registry,
        transcription_registry,
    )
    for module in provider_modules:
        module._reset_for_tests()
    clear_providers()
    secret_registry._reset_registry_for_tests()
    monkeypatch.setattr(secret_registry, "_ensure_builtin_sources", lambda: None)
    isolated_platform_registry = PlatformRegistry()
    monkeypatch.setattr(
        platform_module,
        "platform_registry",
        isolated_platform_registry,
    )

    with registry._lock:
        tool_state = (
            dict(registry._tools),
            dict(registry._toolset_checks),
            dict(registry._toolset_aliases),
            dict(registry._plugin_override_policy),
            registry._generation,
        )

    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    yield isolated_platform_registry

    with registry._lock:
        (
            registry._tools,
            registry._toolset_checks,
            registry._toolset_aliases,
            registry._plugin_override_policy,
            old_generation,
        ) = tool_state
        registry._generation = max(registry._generation, old_generation) + 1
    sys.modules.pop(_SUPPORT_MODULE, None)
    for name in list(sys.modules):
        if name.startswith("hermes_plugins.h1_transaction_concurrency"):
            sys.modules.pop(name, None)


def test_provider_and_platform_readers_never_observe_provisional_registration(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    values = _external_values("candidate", "visibility-success")
    support = _support(values, "candidate")
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()

    thread, done, failures = _start_discovery(manager)
    assert support.started.wait(timeout=5)

    blocked_observation = _read_external_generation(values, isolated_registries)
    support.release.set()
    _join(thread, done)
    assert failures == []
    assert all(observed is None for observed in blocked_observation.values())
    assert _read_external_generation(values, isolated_registries) == values


def test_failed_provider_and_platform_registration_never_becomes_visible(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    values = _external_values("candidate", "visibility-failure")
    support = _support(values, "candidate", fail=True)
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()

    thread, done, failures = _start_discovery(manager)
    assert support.started.wait(timeout=5)

    blocked_observation = _read_external_generation(values, isolated_registries)
    support.release.set()
    _join(thread, done)

    assert failures == []
    assert all(observed is None for observed in blocked_observation.values())
    assert all(
        observed is None
        for observed in _read_external_generation(values, isolated_registries).values()
    )
    assert manager._plugins[_PLUGIN_NAME].enabled is False


def test_force_reload_readers_observe_old_generation_until_atomic_swap(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "force")
    support = _support(
        old_values,
        "generation-a",
        released=True,
        register_generation_state=True,
    )
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_tool = registry.get_entry(_TOOL_NAME)
    assert old_tool is not None

    new_values = _external_values("generation-b", "force")
    support = _support(
        new_values,
        "generation-b",
        register_generation_state=True,
    )
    thread, done, failures = _start_discovery(manager, force=True)
    assert support.started.wait(timeout=5)

    blocked_hook = manager.invoke_hook("post_tool_call")
    blocked_tool = registry.get_entry(_TOOL_NAME)
    blocked_external = _read_external_generation(old_values, isolated_registries)

    support.release.set()
    _join(thread, done)
    assert failures == []
    assert blocked_hook == ["generation-a"]
    assert blocked_tool is old_tool
    assert blocked_external == old_values
    assert manager.invoke_hook("post_tool_call") == ["generation-b"]
    assert registry.get_entry(_TOOL_NAME).handler is support.tool_handler
    assert _read_external_generation(new_values, isolated_registries) == new_values


def test_force_reload_failure_preserves_old_generation_without_empty_window(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "force-failure")
    _support(
        old_values,
        "generation-a",
        released=True,
        register_generation_state=True,
    )
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_tool = registry.get_entry(_TOOL_NAME)
    assert old_tool is not None

    new_values = _external_values("generation-b", "force-failure")
    support = _support(
        new_values,
        "generation-b",
        fail=True,
        register_generation_state=True,
    )
    thread, done, failures = _start_discovery(manager, force=True)
    assert support.started.wait(timeout=5)

    blocked_hook = manager.invoke_hook("post_tool_call")
    blocked_tool = registry.get_entry(_TOOL_NAME)
    blocked_external = _read_external_generation(old_values, isolated_registries)

    support.release.set()
    _join(thread, done)
    assert failures == []
    assert blocked_hook == ["generation-a"]
    assert blocked_tool is old_tool
    assert blocked_external == old_values
    assert manager.invoke_hook("post_tool_call") == ["generation-a"]
    assert registry.get_entry(_TOOL_NAME) is old_tool
    observed = _read_external_generation(old_values, isolated_registries)
    assert observed == old_values
    assert {
        getattr(value, "marker", getattr(value, "label", None))
        for value in observed.values()
    } == {"generation-a"}


def test_force_reload_removes_registrations_absent_from_new_generation(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "force-remove")
    _support(
        old_values,
        "generation-a",
        released=True,
        register_generation_state=True,
    )
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    assert registry.get_entry(_TOOL_NAME) is not None
    assert _read_external_generation(old_values, isolated_registries) == old_values
    module_namespace = f"hermes_plugins.{_PLUGIN_NAME.replace('-', '_')}"
    with registry._lock:
        assert module_namespace in registry._plugin_override_policy

    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": []}}),
        encoding="utf-8",
    )
    manager.discover_and_load(force=True)

    assert manager.invoke_hook("post_tool_call") == []
    assert registry.get_entry(_TOOL_NAME) is None
    assert all(
        observed is None
        for observed in _read_external_generation(
            old_values,
            isolated_registries,
        ).values()
    )
    assert manager._plugin_tool_names == set()
    assert all(not names for names in manager._plugin_external_names.values())
    assert manager._plugins[_PLUGIN_NAME].enabled is False
    with registry._lock:
        assert module_namespace not in registry._plugin_override_policy


def test_force_reload_preserves_same_key_external_replacement_after_manager_snapshot(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    namespace = "same-key-replacement"
    old_values = _external_values("generation-a", namespace)
    _support(old_values, "generation-a", released=True)
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    replacement_values = _external_values("direct-replacement", namespace)
    new_values = _external_values("generation-b", namespace)
    _support(new_values, "generation-b", released=True)
    replacement_written = threading.Event()
    snapshot_entered = threading.Event()
    original_transactions = plugins_module._external_registry_transactions

    class _ImageSnapshotBarrier:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._started = False

        def __getattr__(self, name: str):
            return getattr(self._wrapped, name)

        def take_snapshot(self):
            if not self._started:
                self._started = True
                snapshot_entered.set()

                def write_replacement() -> None:
                    _register_external_direct(
                        "image_gen",
                        replacement_values["image_gen"],
                        isolated_registries,
                    )
                    replacement_written.set()

                writer = threading.Thread(target=write_replacement, daemon=True)
                writer.start()
                replacement_written.wait(timeout=0.2)
            return self._wrapped.take_snapshot()

    def transactions_with_barrier():
        transactions = original_transactions()
        transactions["image_gen"] = _ImageSnapshotBarrier(transactions["image_gen"])
        return transactions

    monkeypatch.setattr(
        plugins_module,
        "_external_registry_transactions",
        transactions_with_barrier,
    )

    manager.discover_and_load(force=True)

    assert snapshot_entered.is_set()
    assert replacement_written.is_set()
    observed = _read_external_generation(replacement_values, isolated_registries)
    assert observed["image_gen"] is replacement_values["image_gen"]


def test_force_reload_preserves_same_key_tool_replacement_after_manager_snapshot(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "same-key-tool")
    _support(
        old_values,
        "generation-a",
        released=True,
        register_generation_state=True,
    )
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    new_values = _external_values("generation-b", "same-key-tool")
    _support(
        new_values,
        "generation-b",
        released=True,
        register_generation_state=True,
    )
    replacement_written = threading.Event()
    snapshot_calls = 0
    original_snapshot = registry._take_transaction_snapshot

    def replacement_handler(args, **kwargs):
        return "direct-replacement"

    def write_replacement() -> None:
        registry.register(
            name=_TOOL_NAME,
            toolset="h1_transaction",
            schema={
                "name": _TOOL_NAME,
                "description": "direct replacement",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=replacement_handler,
        )
        replacement_written.set()

    def snapshot_with_barrier():
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            writer = threading.Thread(target=write_replacement, daemon=True)
            writer.start()
            assert not replacement_written.wait(timeout=0.2)
        else:
            assert replacement_written.wait(timeout=5)
        return original_snapshot()

    monkeypatch.setattr(
        registry,
        "_take_transaction_snapshot",
        snapshot_with_barrier,
    )

    manager.discover_and_load(force=True)

    assert snapshot_calls >= 1
    assert replacement_written.wait(timeout=5)
    assert registry.get_entry(_TOOL_NAME).handler is replacement_handler


@pytest.mark.parametrize(
    "conflict_surface",
    [
        "platform",
        "browser",
        "dashboard",
        "image_gen",
        "secret",
        "stt",
        "tts",
        "video_gen",
        "web",
    ],
)
def test_external_registry_commit_conflict_preserves_concurrent_writer(
    tmp_path, monkeypatch, isolated_registries, conflict_surface
):
    home = tmp_path / "hermes-home"
    candidate_values = _external_values("candidate", "conflict")
    support = _support(candidate_values, "candidate")
    _write_plugin(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()

    thread, done, failures = _start_discovery(manager)
    assert support.started.wait(timeout=5)

    concurrent_values = _external_values(
        "concurrent-writer",
        "conflict-writer",
    )
    _register_external_direct(
        conflict_surface,
        concurrent_values[conflict_surface],
        isolated_registries,
    )

    support.release.set()
    _join(thread, done)
    assert failures == []
    observed_concurrent = _read_external_generation(
        concurrent_values,
        isolated_registries,
    )
    assert observed_concurrent[conflict_surface] is concurrent_values[conflict_surface]
    assert all(
        observed is None
        for observed in _read_external_generation(
            candidate_values,
            isolated_registries,
        ).values()
    )
    assert manager._plugins[_PLUGIN_NAME].enabled is False
    assert manager._plugins[_PLUGIN_NAME].error == "RuntimeError: plugin commit failed"


def test_context_retained_after_commit_targets_live_external_registries(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    values = _external_values("initial", "retained-context")
    support = _support(values, "initial", released=True)
    support.saved_context = None
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_context = ctx\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    late = _ImageProvider("h1-retained-context-late", "late")
    support.saved_context.register_image_gen_provider(late)

    from agent.image_gen_registry import get_provider

    assert get_provider(late.name) is late


def test_retained_context_live_registration_does_not_validate_under_live_locks(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    values = _external_values("initial", "lock-validation")
    support = _support(values, "initial", released=True)
    support.saved_context = None
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_context = ctx\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    from agent import image_gen_registry

    observations: list[tuple[str, bool, bool, bool]] = []

    def observe(label: str) -> None:
        observations.append(
            (
                label,
                manager._lock._is_owned(),
                registry._lock._is_owned(),
                image_gen_registry._lock._is_owned(),
            )
        )

    class _ObservedImageProvider(_ImageProvider):
        @property
        def name(self) -> str:
            observe("provider.name")
            return super().name

    class _ObservedSchema(dict):
        def get(self, key, default=None):
            if key == "description":
                observe("schema.get")
            return super().get(key, default)

    provider = _ObservedImageProvider("h1-lock-validation-late-image", "late")
    support.saved_context.register_image_gen_provider(provider)
    support.saved_context.register_tool(
        "h1_lock_validation_late_tool",
        "h1_transaction",
        _ObservedSchema(
            {
                "name": "h1_lock_validation_late_tool",
                "description": "late",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        lambda args, **kwargs: "late",
    )

    assert observations
    assert all(
        not any((manager_locked, tool_locked, external_locked))
        for _, manager_locked, tool_locked, external_locked in observations
    ), observations


def test_retained_context_reentrant_force_reload_during_validation_fails_closed(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    values = _external_values("initial", "reentrant-force")
    support = _support(values, "initial", released=True)
    support.saved_context = None
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_context = ctx\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()

    provider_name = "h1-reentrant-force-validation-image"

    class _ReentrantImageProvider(_ImageProvider):
        @property
        def name(self) -> str:
            manager.discover_and_load(force=True)
            return super().name

    failures: list[BaseException] = []
    done = threading.Event()

    def register_from_retained_context() -> None:
        try:
            support.saved_context.register_image_gen_provider(
                _ReentrantImageProvider(provider_name, "late")
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=register_from_retained_context, daemon=True)
    thread.start()

    assert done.wait(timeout=5), "reentrant force reload self-deadlocked"
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "force reload cannot run from a live plugin registration" in str(failures[0])

    from agent.image_gen_registry import get_provider

    assert get_provider(provider_name) is None


def test_force_reload_fails_closed_instead_of_waiting_on_live_validation(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "lease-cycle")
    support = _support(old_values, "generation-a", released=True)
    support.saved_context = None
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_context = ctx\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_context = support.saved_context

    validation_started = threading.Event()
    force_done = threading.Event()
    registration_done = threading.Event()
    registration_failures: list[BaseException] = []

    class _WaitingImageProvider(_ImageProvider):
        @property
        def name(self) -> str:
            validation_started.set()
            if not force_done.wait(timeout=5):
                raise RuntimeError("force reload did not finish")
            return super().name

    provider_name = "h1-live-validation-waits-for-force"

    def register_provider() -> None:
        try:
            old_context.register_image_gen_provider(
                _WaitingImageProvider(provider_name, "late")
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            registration_failures.append(exc)
        finally:
            registration_done.set()

    registration_thread = threading.Thread(target=register_provider, daemon=True)
    registration_thread.start()
    assert validation_started.wait(timeout=5)

    new_values = _external_values("generation-b", "lease-cycle")
    support = _support(new_values, "generation-b", released=True)
    force_failures: list[BaseException] = []

    def force_reload() -> None:
        try:
            manager.discover_and_load(force=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            force_failures.append(exc)
        finally:
            force_done.set()

    force_thread = threading.Thread(target=force_reload, daemon=True)
    force_thread.start()

    assert force_done.wait(timeout=5), "force reload waited on live validation"
    assert registration_done.wait(timeout=5), "live validation did not resume"
    force_thread.join(timeout=1)
    registration_thread.join(timeout=1)
    assert not force_thread.is_alive()
    assert not registration_thread.is_alive()
    assert force_failures == []
    assert registration_failures == []
    assert manager._live_context_generation == 0
    assert manager._live_context_revoking == set()

    from agent.image_gen_registry import get_provider

    assert get_provider(provider_name) is not None


def test_successful_force_reload_revokes_prior_retained_context_before_mutation(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "revoked-context")
    support = _support(
        old_values,
        "generation-a",
        released=True,
        register_generation_state=True,
    )
    support.saved_contexts = []
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_contexts.append(ctx)\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_context = support.saved_contexts[-1]

    new_values = _external_values(
        "generation-b",
        "revoked-context",
    )
    support = _support(
        new_values,
        "generation-b",
        released=True,
        register_generation_state=True,
    )
    support.saved_contexts = []
    manager.discover_and_load(force=True)
    new_context = support.saved_contexts[-1]
    assert manager._live_context_revoking == set()

    stale_external = _ImageProvider("h1-revoked-context-late-image", "stale")
    stale_tool_name = "h1_revoked_context_late_tool"
    stale_platform_name = "h1-revoked-context-late-platform"
    stale_hook = lambda **kwargs: "stale"

    with pytest.raises(RuntimeError, match="no longer live"):
        old_context.register_image_gen_provider(stale_external)
    with pytest.raises(RuntimeError, match="no longer live"):
        old_context.register_tool(
            stale_tool_name,
            "h1_transaction",
            {
                "name": stale_tool_name,
                "description": "stale",
                "parameters": {"type": "object", "properties": {}},
            },
            lambda args, **kwargs: "stale",
        )
    with pytest.raises(RuntimeError, match="no longer live"):
        old_context.register_hook("post_tool_call", stale_hook)
    with pytest.raises(RuntimeError, match="no longer live"):
        old_context.register_platform(
            name=stale_platform_name,
            label="Stale",
            adapter_factory=lambda config: "stale",
            check_fn=lambda: True,
        )

    from agent.image_gen_registry import get_provider

    assert get_provider(stale_external.name) is None
    assert registry.get_entry(stale_tool_name) is None
    assert isolated_registries.get(stale_platform_name) is None
    assert stale_hook not in manager._hooks.get("post_tool_call", [])

    live_external = _ImageProvider("h1-revoked-context-live-image", "live")
    live_tool_name = "h1_revoked_context_live_tool"
    live_hook = lambda **kwargs: "live"
    new_context.register_image_gen_provider(live_external)
    new_context.register_tool(
        live_tool_name,
        "h1_transaction",
        {
            "name": live_tool_name,
            "description": "live",
            "parameters": {"type": "object", "properties": {}},
        },
        lambda args, **kwargs: "live",
    )
    new_context.register_hook("post_tool_call", live_hook)

    assert get_provider(live_external.name) is live_external
    assert registry.get_entry(live_tool_name) is not None
    assert live_hook in manager._hooks["post_tool_call"]


def test_failed_force_reload_keeps_prior_retained_context_live(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "failed-force-context")
    support = _support(old_values, "generation-a", released=True)
    support.saved_contexts = []
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_contexts.append(ctx)\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_context = support.saved_contexts[-1]

    new_values = _external_values("generation-b", "failed-force-context")
    support = _support(new_values, "generation-b", released=True, fail=True)
    support.saved_contexts = []
    manager.discover_and_load(force=True)

    retained_external = _ImageProvider("h1-failed-force-live-image", "retained")
    retained_hook = lambda **kwargs: "retained"
    old_context.register_image_gen_provider(retained_external)
    old_context.register_hook("post_tool_call", retained_hook)

    from agent.image_gen_registry import get_provider

    assert support.saved_contexts
    assert get_provider(retained_external.name) is retained_external
    assert retained_hook in manager._hooks["post_tool_call"]


def test_force_reload_interrupt_after_revocation_keeps_old_context_live(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "revocation-interrupt")
    support = _support(old_values, "generation-a", released=True)
    support.saved_contexts = []
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_contexts.append(ctx)\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_context = support.saved_contexts[-1]

    support = _support(
        _external_values("generation-b", "revocation-interrupt"),
        "generation-b",
        released=True,
    )
    support.saved_contexts = []
    original_begin = manager._begin_live_context_revocation

    def interrupt_after_begin(generation: int) -> None:
        original_begin(generation)
        raise KeyboardInterrupt("injected after revocation")

    monkeypatch.setattr(
        manager,
        "_begin_live_context_revocation",
        interrupt_after_begin,
    )

    with pytest.raises(KeyboardInterrupt, match="injected after revocation"):
        manager.discover_and_load(force=True)

    assert manager._live_context_generation == 0
    assert manager._live_context_revoking == set()
    retained_tool = "h1_revocation_interrupt_tool"
    retained_platform = "h1-revocation-interrupt-platform"
    old_context.register_tool(
        retained_tool,
        "h1_transaction",
        {
            "name": retained_tool,
            "description": "retained",
            "parameters": {"type": "object", "properties": {}},
        },
        lambda args, **kwargs: "retained",
    )
    old_context.register_platform(
        name=retained_platform,
        label="Retained",
        adapter_factory=lambda config: "retained",
        check_fn=lambda: True,
    )
    assert registry.get_entry(retained_tool) is not None
    assert isolated_registries.get(retained_platform) is not None


def test_force_reload_lock_acquisition_interrupt_releases_prior_locks(
    tmp_path, monkeypatch, isolated_registries
):
    home = tmp_path / "hermes-home"
    old_values = _external_values("generation-a", "lock-interrupt")
    support = _support(old_values, "generation-a", released=True)
    support.saved_contexts = []
    _write_plugin(home)
    plugin_file = home / "plugins" / _PLUGIN_NAME / "__init__.py"
    plugin_file.write_text(
        plugin_file.read_text(encoding="utf-8").replace(
            "values = support.values",
            "support.saved_contexts.append(ctx)\n    values = support.values",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    old_context = support.saved_contexts[-1]

    from agent import image_gen_registry

    original_lock = image_gen_registry._registry_state._lock

    class _InterruptingLock:
        def __init__(self) -> None:
            self.acquire_count = 0

        def acquire(self, *args, **kwargs):
            self.acquire_count += 1
            if self.acquire_count == 2:
                raise KeyboardInterrupt("injected lock acquisition failure")
            return original_lock.acquire(*args, **kwargs)

        def release(self) -> None:
            original_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            self.release()

    interrupting_lock = _InterruptingLock()
    monkeypatch.setattr(image_gen_registry._registry_state, "_lock", interrupting_lock)
    support = _support(
        _external_values("generation-b", "lock-interrupt"),
        "generation-b",
        released=True,
    )
    support.saved_contexts = []

    with pytest.raises(KeyboardInterrupt, match="injected lock acquisition failure"):
        manager.discover_and_load(force=True)

    assert interrupting_lock.acquire_count == 2
    assert manager._live_context_generation == 0
    assert manager._live_context_revoking == set()

    retained_tool = "h1_lock_interrupt_tool"
    done = threading.Event()
    failures: list[BaseException] = []

    def register_from_other_thread() -> None:
        try:
            old_context.register_tool(
                retained_tool,
                "h1_transaction",
                {
                    "name": retained_tool,
                    "description": "retained",
                    "parameters": {"type": "object", "properties": {}},
                },
                lambda args, **kwargs: "retained",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=register_from_other_thread, daemon=True)
    thread.start()
    assert done.wait(timeout=5), "previously acquired commit lock was leaked"
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert failures == []
    assert registry.get_entry(retained_tool) is not None
