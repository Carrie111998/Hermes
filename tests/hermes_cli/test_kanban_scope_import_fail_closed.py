"""Fail closed when the dispatcher worker-scope oracle cannot be imported."""

from __future__ import annotations

import builtins
from pathlib import Path


def test_extension_surfaces_stay_closed_when_scope_oracle_import_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import plugins.memory as memory_plugins
    import providers

    real_import = builtins.__import__

    def fail_scope_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli.kanban_worker_scope":
            raise ImportError("scope oracle unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_scope_import)

    entry_point_loaded = False

    class HostileEntryPoint:
        name = "hostile"

        def load(self):
            nonlocal entry_point_loaded
            entry_point_loaded = True
            raise AssertionError("entry point must not execute")

    imported_provider_dirs: list[tuple[Path, str]] = []
    user_provider_dir = tmp_path / "user-provider"
    user_provider_dir.mkdir()
    monkeypatch.setattr(providers, "_discovered", False)
    monkeypatch.setattr(providers, "_BUNDLED_PLUGINS_DIR", tmp_path / "missing")
    monkeypatch.setattr(providers, "_user_plugins_dir", lambda: user_provider_dir)
    monkeypatch.setattr(
        providers,
        "_import_plugin_dir",
        lambda path, source: imported_provider_dirs.append((path, source)),
    )

    providers._discover_entry_point_providers()
    providers._discover_providers()

    memory_provider_dir = tmp_path / "memory-provider"
    memory_provider_dir.mkdir()
    (memory_provider_dir / "__init__.py").write_text(
        "raise AssertionError('memory plugin must not execute')\n",
        encoding="utf-8",
    )

    assert (
        memory_plugins._load_provider_from_entry_point(
            HostileEntryPoint(),
            register_skills=False,
        )
        is None
    )
    assert (
        memory_plugins._load_provider_from_dir(
            memory_provider_dir,
            register_skills=False,
        )
        is None
    )
    assert memory_plugins.discover_plugin_cli_commands() == []
    assert entry_point_loaded is False
    assert imported_provider_dirs == []
