from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import hermes_cli.update_inventory as inventory
from hermes_cli.update_contract import evaluate_update_admission


def _marker(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema": 1,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": "0.20.5",
        "revision": "b" * 40,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_baked_marker_outranks_bind_mounted_git_and_mutable_hints(
    monkeypatch, tmp_path
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / ".install_method").write_text("git\n", encoding="utf-8")
    marker = _marker(tmp_path / "image-provenance.json")
    monkeypatch.setenv("HERMES_MANAGED", "false")

    def _must_not_consult_mutable_install_state(*args, **kwargs):
        raise AssertionError("baked marker must outrank mutable install state")

    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method",
        _must_not_consult_mutable_install_state,
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_managed_system",
        _must_not_consult_mutable_install_state,
    )

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.install_method == "docker"
    assert plan.deployment_kind == "image"
    assert plan.updatable_in_place is False
    assert plan.classification_reason == "baked_image_provenance"
    assert plan.image_provenance is not None
    assert plan.image_provenance["revision"] == "b" * 40
    assert plan.image_provenance["marker_path"] == str(marker)


def test_marker_absence_preserves_git_behavior(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: tmp_path / "empty-home"
    )

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=tmp_path / "missing.json",
        include_runtimes=False,
    )

    assert plan.install_method == "git"
    assert plan.deployment_kind == "mutable"
    assert plan.classification_reason == "install_method:git"
    assert plan.updatable_in_place is True
    assert plan.image_provenance is None


def test_invalid_present_marker_still_classifies_as_image(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = tmp_path / "image-provenance.json"
    marker.write_text("[]", encoding="utf-8")

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.install_method == "docker"
    assert plan.deployment_kind == "image"
    assert plan.updatable_in_place is False
    assert plan.classification_reason == "invalid_baked_image_provenance"
    assert plan.image_provenance is not None
    assert plan.image_provenance["valid"] is False


def test_include_runtimes_false_stops_before_profile_and_process_probes(
    monkeypatch, tmp_path
):
    marker = _marker(tmp_path / "image-provenance.json")

    def _unexpected_probe(*args, **kwargs):
        raise AssertionError("runtime probe crossed admission-only boundary")

    monkeypatch.setattr(
        "hermes_cli.profiles._get_default_hermes_home", _unexpected_probe
    )
    monkeypatch.setattr("hermes_cli.gateway._get_service_pids", _unexpected_probe)
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes", _unexpected_probe
    )

    plan = inventory.collect_runtime_inventory(
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.deployment_kind == "image"
    assert plan.profiles == []
    assert plan.runtimes == []


def test_plan_serializes_deployment_and_baked_identity(tmp_path):
    marker = _marker(tmp_path / "image-provenance.json")

    payload = inventory.collect_runtime_inventory(
        provenance_path=marker,
        include_runtimes=False,
    ).to_dict()

    assert json.loads(json.dumps(payload))["image_provenance"] == {
        "schema": 1,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": "0.20.5",
        "revision": "b" * 40,
        "marker_path": str(marker),
        "valid": True,
        "error": None,
    }


def test_image_plan_inventory_reads_live_status_without_mutating_home(
    monkeypatch, tmp_path
):
    home = tmp_path / "home"
    home.mkdir()
    state = home / "gateway_state.json"
    state.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "code_sha": "c" * 40,
                "code_version": "0.20.5",
                "supervisor": "desktop",
            }
        ),
        encoding="utf-8",
    )
    marker = _marker(tmp_path / "image-provenance.json")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(inventory, "_pid_exists_read_only", lambda pid: pid > 0)
    before = {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }

    plan = inventory.collect_runtime_inventory(provenance_path=marker)

    after = {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    assert plan.profiles == ["default"]
    assert len(plan.runtimes) == 1
    runtime = plan.runtimes[0]
    assert runtime.pid == os.getpid()
    assert runtime.code_sha == "c" * 40
    assert runtime.code_version == "0.20.5"
    assert runtime.supervisor == "desktop"
    assert before == after


@pytest.mark.parametrize(
    ("method", "kind", "updatable"),
    [
        ("git", "mutable", True),
        ("unknown", "mutable", True),
        ("docker", "image", False),
        ("nix", "package", False),
        ("nixos", "package", False),
        ("home-manager", "package", False),
        ("apt", "package", False),
        ("pip", "mutable", False),
    ],
)
def test_marker_absence_preserves_every_existing_install_classifier(
    monkeypatch, tmp_path, method, kind, updatable
):
    calls: list[tuple[str, object]] = []
    identity = {"sha": "1" * 40, "version": "0.19.0"}
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method",
        lambda project_root: calls.append(("detect", project_root)) or method,
    )
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.recommended_update_command_for_method",
        lambda detected: calls.append(("recommend", detected))
        or f"update-via-{detected}",
    )
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: calls.append(("identity", refresh)) or identity,
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=tmp_path / "absent.json",
        include_runtimes=False,
    )

    assert plan.install_method == method
    assert plan.deployment_kind == kind
    assert plan.classification_reason == f"install_method:{method}"
    assert plan.updatable_in_place is updatable
    assert plan.update_mechanism == f"update-via-{method}"
    assert plan.expected_sha == identity["sha"]
    assert plan.expected_version == identity["version"]
    assert calls == [
        ("detect", checkout),
        ("recommend", method),
        ("identity", True),
    ]

    evaluated, refusal = evaluate_update_admission(surface="cli", plan=plan)
    assert evaluated is plan
    assert refusal is None


@pytest.mark.parametrize("managed", ["apt", "nix", "nixos", "home-manager"])
def test_marker_absence_preserves_managed_system_override(
    monkeypatch, tmp_path, managed
):
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda project_root: "git"
    )
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: managed)
    monkeypatch.setattr(
        "hermes_cli.config.recommended_update_command_for_method",
        lambda method: f"update-via-{method}",
    )
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": "2" * 40, "version": "0.19.0"},
    )

    plan = inventory.collect_runtime_inventory(
        project_root=tmp_path,
        provenance_path=tmp_path / "absent.json",
        include_runtimes=False,
    )

    assert plan.install_method == managed
    assert plan.deployment_kind == "package"
    assert plan.classification_reason == f"managed_system:{managed}"
    assert plan.updatable_in_place is False
    assert plan.image_provenance is None


@pytest.mark.parametrize(
    "mutable_hint",
    ["git", "unknown", "docker", "nix", "nixos", "home-manager", "apt", "pip"],
)
def test_baked_marker_precedence_is_independent_of_every_mutable_hint(
    monkeypatch, tmp_path, mutable_hint
):
    marker = _marker(tmp_path / "image-provenance.json")

    def _forbidden(*args, **kwargs):
        raise AssertionError(f"mutable hint {mutable_hint} crossed baked authority")

    monkeypatch.setattr("hermes_cli.config.detect_install_method", _forbidden)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", _forbidden)
    monkeypatch.setattr("hermes_cli.build_info.get_code_identity", _forbidden)

    plan = inventory.collect_runtime_inventory(
        project_root=tmp_path,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.install_method == "docker"
    assert plan.deployment_kind == "image"
    assert plan.classification_reason == "baked_image_provenance"
    assert plan.updatable_in_place is False
    assert plan.expected_sha == "b" * 40
    assert plan.expected_version == "0.20.5"


@pytest.mark.parametrize(
    ("version", "revision"),
    [(None, "b" * 40), ("0.20.5", None), (None, None)],
)
def test_baked_identity_never_falls_through_to_live_checkout(
    monkeypatch, tmp_path, version, revision
):
    marker = _marker(
        tmp_path / "image-provenance.json",
        version=version,
        revision=revision,
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("live checkout identity crossed baked authority")

    monkeypatch.setattr("hermes_cli.config.detect_install_method", _forbidden)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", _forbidden)
    monkeypatch.setattr("hermes_cli.build_info.get_code_identity", _forbidden)

    plan = inventory.collect_runtime_inventory(
        project_root=tmp_path,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.expected_sha == revision
    assert plan.expected_version == version
