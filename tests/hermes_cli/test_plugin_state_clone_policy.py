"""Public behavior tests for plugin-owned profile clone policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.profiles import create_profile


def _context(name: str) -> PluginContext:
    return PluginContext(PluginManifest(name=name), PluginManager())


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture()
def profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def test_clone_all_excludes_declared_state_and_preserves_undeclared_bytes(
    profile_home: Path,
) -> None:
    identity_bound = _context("identity-bound-plugin")
    identity_bound.state.declare_identity_bound()
    identity_bound.state.set("credential", {"token": "profile-secret"})

    ordinary = _context("ordinary-plugin")
    ordinary.state.set("cursor", {"page": 7})
    (ordinary.state.data_dir / "nested").mkdir()
    (ordinary.state.data_dir / "nested" / "opaque.bin").write_bytes(
        b"\x00\xffplugin-owned\r\n"
    )
    ordinary_before = _file_bytes(ordinary.state.data_dir)

    clone = create_profile("clone-all", clone_all=True, no_alias=True)

    identity_relative = identity_bound.state.data_dir.relative_to(profile_home)
    ordinary_relative = ordinary.state.data_dir.relative_to(profile_home)
    assert not (clone / identity_relative).exists()
    assert _file_bytes(clone / ordinary_relative) == ordinary_before


def test_fresh_and_ordinary_clone_keep_existing_plugin_state_semantics(
    profile_home: Path,
) -> None:
    (profile_home / "config.yaml").write_text("model: source-model\n", encoding="utf-8")
    identity_bound = _context("identity-bound-plugin")
    identity_bound.state.declare_identity_bound()
    identity_bound.state.set("credential", "profile-secret")
    ordinary = _context("ordinary-plugin")
    ordinary.state.set("cursor", 7)

    fresh = create_profile("fresh", no_alias=True)
    cloned = create_profile("ordinary-clone", clone_config=True, no_alias=True)

    assert not (fresh / "plugin-data").exists()
    assert not (cloned / "plugin-data").exists()
    assert not (fresh / "config.yaml").exists()
    cloned_config = yaml.safe_load((cloned / "config.yaml").read_text(encoding="utf-8"))
    assert cloned_config["model"] == "source-model"


def test_clone_all_copies_declared_state_when_policy_probe_fails(
    profile_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_bound = _context("identity-bound-plugin")
    identity_bound.state.declare_identity_bound()
    identity_bound.state.set("credential", "profile-secret")
    state_before = _file_bytes(identity_bound.state.data_dir)

    policy_artifacts = [
        path
        for path in identity_bound.state.data_dir.iterdir()
        if path.name not in {"state.json", ".state.json.lock"}
    ]
    assert len(policy_artifacts) == 1
    policy_artifact = policy_artifacts[0]
    real_stat = os.stat
    probe_failed = False

    def fail_policy_probe_once(path, *args, **kwargs):
        nonlocal probe_failed
        if not probe_failed and Path(path) == policy_artifact:
            probe_failed = True
            raise OSError("simulated filesystem metadata failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fail_policy_probe_once)

    clone = create_profile("fail-open-clone", clone_all=True, no_alias=True)

    assert probe_failed is True
    relative = identity_bound.state.data_dir.relative_to(profile_home)
    assert _file_bytes(clone / relative) == state_before
