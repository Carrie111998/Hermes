"""TDD coverage for the agent-init runtime registry binding."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent import runtime_registry as registry_module
from agent.agent_init import _initialize_runtime_registry
from agent.runtime_registry import RegistryLoadError, load_registry
from hermes_cli.config_defaults import DEFAULT_CONFIG
from run_agent import AIAgent


LIVE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "runtime_registry" / "live"


def _agent(**values: object) -> Any:
    defaults = {
        "provider": "native-provider",
        "model": "native-model",
        "client": object(),
    }
    defaults.update(values)
    agent = object.__new__(AIAgent)
    for key, value in defaults.items():
        setattr(agent, key, value)
    return agent


def _published_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "published"
    shutil.copytree(LIVE_FIXTURE, root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["promotionState"] = "PUBLISHED"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _patch_loader(monkeypatch, root: Path) -> None:
    real_loader = load_registry
    monkeypatch.setattr(
        registry_module,
        "load_registry",
        lambda *, mode: real_loader(root, mode=mode),
    )


def test_config_default_keeps_routing_disabled_and_production_mode() -> None:
    assert DEFAULT_CONFIG["routing"] == {
        "enabled": False,
        "registry_mode": "production",
    }


def test_production_rejection_preserves_native_runtime(monkeypatch) -> None:
    agent = _agent()
    native = (agent.provider, agent.model, agent.client)

    def reject(*, mode: str):
        assert mode == "production"
        raise RegistryLoadError(
            "promotion state READY_FOR_REVIEW is not allowed in production",
            code="promotion_rejected",
            path="manifest.json",
        )

    monkeypatch.setattr(registry_module, "load_registry", reject)

    _initialize_runtime_registry(
        agent,
        {"routing": {"enabled": True, "registry_mode": "production"}},
    )

    assert (agent.provider, agent.model, agent.client) == native
    assert agent.runtime_registry.status == "inactive"
    assert agent.runtime_registry.inactive_reason["code"] == "promotion_rejected"
    assert agent.runtime_registry.version is None
    assert agent.runtime_snapshot is None


def test_configured_registry_source_dir_is_passed_to_loader(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def load(*, root=None, mode: str):
        seen["root"] = root
        seen["mode"] = mode
        raise RegistryLoadError("stop after capture", code="test_capture")

    monkeypatch.setattr(registry_module, "load_registry", load)

    _initialize_runtime_registry(
        _agent(),
        {
            "routing": {
                "enabled": True,
                "registry_mode": "preview",
                "source_dir": str(tmp_path),
            }
        },
    )

    assert Path(str(seen["root"])) == tmp_path
    assert seen["mode"] == "preview"


def test_preview_mode_activates_explicit_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    candidate_root = tmp_path / "candidate"
    shutil.copytree(LIVE_FIXTURE, candidate_root)
    _patch_loader(monkeypatch, candidate_root)
    agent = _agent()

    _initialize_runtime_registry(
        agent,
        {"routing": {"enabled": True, "registry_mode": "preview"}},
    )

    assert agent.runtime_registry.status == "candidate"
    assert agent.runtime_registry.version == "2026-08-22.18"
    assert agent.runtime_registry.inactive_reason is None
    assert agent.runtime_snapshot is not None
    assert agent.runtime_snapshot.is_candidate is True


def test_published_snapshot_activates_in_production(monkeypatch, tmp_path: Path) -> None:
    _patch_loader(monkeypatch, _published_fixture(tmp_path))
    agent = _agent()

    _initialize_runtime_registry(
        agent,
        {"routing": {"enabled": True, "registry_mode": "production"}},
    )

    assert agent.runtime_registry.status == "active"
    assert agent.runtime_registry.version == "2026-08-22.18"
    assert agent.runtime_snapshot is not None
    assert agent.runtime_snapshot.promotion_state == "PUBLISHED"


def test_bad_registry_fails_closed_without_touching_native_runtime(monkeypatch) -> None:
    agent = _agent()
    native = (agent.provider, agent.model, agent.client)

    def bad_registry(*, mode: str):
        raise ValueError("malformed registry")

    monkeypatch.setattr(registry_module, "load_registry", bad_registry)

    _initialize_runtime_registry(
        agent,
        {"routing": {"enabled": True, "registry_mode": "production"}},
    )

    assert (agent.provider, agent.model, agent.client) == native
    assert agent.runtime_registry.status == "inactive"
    assert agent.runtime_registry.inactive_reason == {
        "code": "registry_load_failed",
        "error_type": "ValueError",
    }
    assert agent.runtime_snapshot is None


def test_attached_snapshot_remains_immutable_after_disk_changes(
    monkeypatch, tmp_path: Path
) -> None:
    candidate_root = tmp_path / "candidate"
    shutil.copytree(LIVE_FIXTURE, candidate_root)
    _patch_loader(monkeypatch, candidate_root)
    agent = _agent()

    _initialize_runtime_registry(
        agent,
        {"routing": {"enabled": True, "registry_mode": "preview"}},
    )
    snapshot = agent.runtime_snapshot
    assert snapshot is not None
    original_level = snapshot.bundle["route_policy"]["default_route"]["level"]
    (candidate_root / "route-policy.json").write_text("changed", encoding="utf-8")
    assert snapshot.bundle["route_policy"]["default_route"]["level"] == original_level
    with pytest.raises(TypeError):
        snapshot.bundle["route_policy"] = {}  # type: ignore[index]
