from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib

from ares_runtime.local_runtime import AresLocalPaths, AresLocalRuntime


def _runtime(tmp_path: Path) -> AresLocalRuntime:
    return AresLocalRuntime(
        AresLocalPaths(
            state_root=tmp_path / "state",
            data_root=tmp_path / "data",
            agent_home=tmp_path / "ares-home",
            launcher_path=tmp_path / "bin" / "ares",
            unit_path=tmp_path / "unit" / "ares-gateway.service",
        )
    )


def _release(runtime: AresLocalRuntime, revision: str) -> Path:
    source = runtime.paths.releases_dir / revision / "source"
    source.mkdir(parents=True)
    return source


def test_current_link_is_the_only_active_runtime_pointer(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = "a" * 40
    second = "b" * 40
    first_source = _release(runtime, first)
    second_source = _release(runtime, second)

    runtime._activate(first)
    runtime._activate(second)

    assert runtime.active_release() == (second, second_source.resolve())
    assert runtime.previous_release() == (first, first_source.resolve())


def test_rollback_swaps_current_and_previous_without_a_worktree_fallback(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = "a" * 40
    second = "b" * 40
    first_source = _release(runtime, first)
    second_source = _release(runtime, second)
    runtime._activate(first)
    runtime._activate(second)

    runtime._atomic_link(runtime.paths.current_link, first_source.resolve())
    runtime._atomic_link(runtime.paths.previous_link, second_source.resolve())

    assert runtime.active_release() == (first, first_source.resolve())
    assert runtime.previous_release() == (second, second_source.resolve())


def test_config_only_tracks_update_source_not_active_release(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime._write_config(remote="https://github.com/RecursiveIntell/Ares.git", branch="main")

    payload = json.loads(runtime.paths.config_path.read_text(encoding="utf-8"))

    assert payload == {
        "branch": "main",
        "remote": "https://github.com/RecursiveIntell/Ares.git",
        "schema_version": 1,
    }


def test_launcher_resolves_the_selected_runtime_dynamically(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime._install_launcher()

    launcher = runtime.paths.launcher_path.read_text(encoding="utf-8")

    assert str(runtime.paths.current_link) in launcher
    assert "-m ares_runtime.local_runtime" in launcher
    assert "Coding" not in launcher


def test_systemd_environment_preserves_an_existing_session_bus(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/existing/runtime")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/existing/runtime/bus")

    environment = AresLocalRuntime._systemd_environment()

    assert environment["XDG_RUNTIME_DIR"] == "/existing/runtime"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/existing/runtime/bus"
    assert environment["PATH"] == os.environ["PATH"]


def test_ares_runtime_is_included_in_the_noneditable_distribution() -> None:
    project = Path(__file__).parents[2] / "pyproject.toml"
    data = tomllib.loads(project.read_text(encoding="utf-8"))

    assert "ares_runtime" in data["tool"]["setuptools"]["packages"]["find"]["include"]
