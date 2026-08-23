from __future__ import annotations

import json

import pytest

import hermes_cli.update_receipt as receipts
from hermes_cli.update_contract import (
    IMAGE_MANAGED_UPDATE_REFUSED,
    UPDATE_REFUSED_EXIT,
    evaluate_update_admission,
    perform_update,
)
from hermes_cli.update_inventory import UpdatePlan


def _image_plan(*, valid: bool = True) -> UpdatePlan:
    return UpdatePlan(
        install_method="docker",
        deployment_kind="image",
        classification_reason=(
            "baked_image_provenance"
            if valid
            else "invalid_baked_image_provenance"
        ),
        image_provenance={
            "schema": 1,
            "deployment_kind": "image",
            "manager": "docker" if valid else "unknown",
            "image": "nousresearch/hermes-agent" if valid else None,
            "version": "0.20.5" if valid else None,
            "revision": "c" * 40 if valid else None,
            "marker_path": "/etc/hermes/image-provenance.json",
            "valid": valid,
            "error": None if valid else "marker_not_object",
        },
        updatable_in_place=False,
        update_mechanism="docker pull nousresearch/hermes-agent:latest",
        expected_sha="d" * 40,
        expected_version="0.20.5",
    )


def test_refusal_is_stable_correlated_and_durable(monkeypatch, tmp_path):
    receipt_dir = tmp_path / "receipts"
    correlation_id = "1" * 32
    monkeypatch.setenv("HERMES_ACTION_ID", correlation_id)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None

    refusal = perform_update(
        surface="cli",
        requested_target="main",
        plan=_image_plan(),
    )

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert UPDATE_REFUSED_EXIT == 2
    assert refusal.surface == "cli"
    assert refusal.requested_target == "main"
    assert refusal.correlation_id == correlation_id
    assert refusal.receipt_path is not None
    assert refusal.baked_identity["revision"] == "c" * 40
    assert refusal.current_identity == {
        "sha": "d" * 40,
        "version": "0.20.5",
    }
    assert refusal.message.startswith(f"{IMAGE_MANAGED_UPDATE_REFUSED}: ")
    assert "image-managed" in refusal.message
    assert "before any mutation" in refusal.message
    assert "docker pull nousresearch/hermes-agent:latest" in refusal.message

    payload = json.loads((receipt_dir / "latest.json").read_text(encoding="utf-8"))
    assert payload["outcome"] == "refused"
    assert payload["stop_reason"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert payload["surface"] == "cli"
    assert payload["requested_target"] == "main"
    assert payload["correlation_id"] == correlation_id
    assert payload["refusal"]["code"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert payload["refusal"]["correlation_id"] == correlation_id
    assert payload["refusal"]["baked_identity"]["revision"] == "c" * 40
    assert payload["refusal"]["current_identity"]["sha"] == "d" * 40
    assert payload["plan"]["deployment_kind"] == "image"
    assert payload["plan"]["image_provenance"]["valid"] is True


def test_mutable_plan_is_admitted_without_receipt(monkeypatch, tmp_path):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None
    plan = UpdatePlan(
        install_method="git",
        deployment_kind="mutable",
        classification_reason="install_method:git",
        updatable_in_place=True,
    )

    assert perform_update(surface="cli", plan=plan) is None
    assert receipts._current is None
    assert not receipt_dir.exists()


def test_legacy_docker_without_explicit_marker_keeps_legacy_admission(tmp_path):
    plan = UpdatePlan(
        install_method="docker",
        deployment_kind="image",
        classification_reason="install_method:docker",
        image_provenance=None,
        updatable_in_place=False,
        update_mechanism="docker pull nousresearch/hermes-agent:latest",
    )

    evaluated_plan, refusal = evaluate_update_admission(
        surface="cli",
        plan=plan,
    )

    assert evaluated_plan is plan
    assert refusal is None


def test_read_only_evaluation_returns_same_typed_reason_without_receipt(
    monkeypatch, tmp_path
):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None

    plan, refusal = evaluate_update_admission(
        surface="dashboard_check",
        requested_target="release",
        plan=_image_plan(),
    )

    assert plan.deployment_kind == "image"
    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert refusal.surface == "dashboard_check"
    assert refusal.requested_target == "release"
    assert refusal.correlation_id is None
    assert receipts._current is None
    assert not receipt_dir.exists()


def test_invalid_present_marker_refuses_closed_with_integrity_reason(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    refusal = perform_update(surface="dashboard_api", plan=_image_plan(valid=False))

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert refusal.baked_identity["valid"] is False
    assert refusal.baked_identity["error"] == "marker_not_object"
    assert "invalid" in refusal.message
    assert "refusing closed" in refusal.message


def test_admission_does_not_depend_on_network_or_subprocess(monkeypatch, tmp_path):
    marker = tmp_path / "image.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "version": "0.20.5",
                "revision": "e" * 40,
            }
        ),
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    calls: list[str] = []

    def _forbidden(kind: str):
        def fail(*args, **kwargs):
            calls.append(kind)
            raise AssertionError(
                f"admission must not use {kind} before image refusal"
            )

        return fail

    monkeypatch.setattr("subprocess.run", _forbidden("subprocess"))
    monkeypatch.setattr("socket.create_connection", _forbidden("network"))
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        _forbidden("live checkout identity"),
    )
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    refusal = perform_update(
        surface="dashboard_api",
        project_root=checkout,
        provenance_path=marker,
    )

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert calls == []
    assert refusal.current_identity == {
        "sha": "e" * 40,
        "version": "0.20.5",
    }


@pytest.mark.parametrize(
    "surface",
    ["cli", "gateway", "dashboard_api", "dashboard_check", "desktop"],
)
def test_marker_absence_is_a_zero_work_compatibility_boundary(
    monkeypatch, tmp_path, surface
):
    calls: list[str] = []
    missing = tmp_path / "missing-provenance.json"

    def _read(path):
        calls.append("marker")
        assert path == missing
        return None

    def _forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} crossed marker-absence boundary")

        return fail

    monkeypatch.setattr("hermes_cli.image_provenance.read_image_provenance", _read)
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory",
        _forbidden("inventory"),
    )
    monkeypatch.setattr("hermes_cli.config.detect_install_method", _forbidden("config"))
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity", _forbidden("identity")
    )
    monkeypatch.setattr("subprocess.run", _forbidden("subprocess"))
    monkeypatch.setattr("socket.create_connection", _forbidden("network"))
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    plan, refusal = evaluate_update_admission(
        surface=surface,
        project_root=tmp_path,
        provenance_path=missing,
    )

    assert plan is None
    assert refusal is None
    assert calls == ["marker"]
    assert receipts._current is None
    assert not (tmp_path / "receipts").exists()


def test_unexpected_marker_reader_failure_never_authorizes_mutation(monkeypatch):
    def _reader_bug(*args, **kwargs):
        raise RuntimeError("unexpected provenance reader defect")

    def _forbidden(*args, **kwargs):
        raise AssertionError("inventory ran after an indeterminate marker read")

    monkeypatch.setattr(
        "hermes_cli.image_provenance.read_image_provenance",
        _reader_bug,
    )
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory",
        _forbidden,
    )

    with pytest.raises(RuntimeError, match="reader defect"):
        evaluate_update_admission(surface="cli")


@pytest.mark.parametrize(
    ("surface", "target"),
    [
        ("cli", None),
        ("cli", "release"),
        ("gateway", "main"),
        ("dashboard_api", None),
        ("dashboard_check", None),
    ],
)
def test_every_surface_gets_one_identical_typed_contract(surface, target):
    _plan, refusal = evaluate_update_admission(
        surface=surface,
        requested_target=target,
        plan=_image_plan(),
    )

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert refusal.surface == surface
    assert refusal.requested_target == target
    assert refusal.update_command == "docker pull nousresearch/hermes-agent:latest"
    assert refusal.deployment_kind == "image"
    assert refusal.install_method == "docker"
    assert refusal.classification_reason == "baked_image_provenance"
    assert refusal.message == _image_plan_message()


def _image_plan_message() -> str:
    _plan, refusal = evaluate_update_admission(surface="reference", plan=_image_plan())
    assert refusal is not None
    return refusal.message


def test_existing_receipt_correlation_is_reused_instead_of_forking_action(
    monkeypatch, tmp_path
):
    receipt_dir = tmp_path / "receipts"
    correlation_id = "a" * 32
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None
    assert (
        receipts.begin_update_receipt(
            surface="dashboard_api",
            requested_target="release",
            correlation_id=correlation_id,
        )
        == correlation_id
    )

    refusal = perform_update(
        surface="dashboard_api",
        requested_target="release",
        plan=_image_plan(),
    )

    assert refusal is not None
    assert refusal.correlation_id == correlation_id
    assert receipts._current is None
    payload = json.loads((receipt_dir / "latest.json").read_text(encoding="utf-8"))
    assert payload["correlation_id"] == correlation_id
    assert payload["refusal"]["correlation_id"] == correlation_id
    assert payload["outcome"] == "refused"


def test_receipt_failure_cannot_turn_refusal_into_authorization(monkeypatch):
    receipts._current = None

    def _unavailable(*args, **kwargs):
        raise OSError("read-only receipt filesystem")

    monkeypatch.setattr(receipts, "begin_update_receipt", _unavailable)

    refusal = perform_update(surface="cli", plan=_image_plan())

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert refusal.receipt_path is None


@pytest.mark.parametrize(
    ("version", "revision"),
    [(None, "f" * 40), ("0.20.5", None), (None, None)],
)
def test_optional_baked_identity_never_borrows_live_checkout_identity(
    monkeypatch, tmp_path, version, revision
):
    marker = tmp_path / "image.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "version": version,
                "revision": revision,
            }
        ),
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()

    def _forbidden(*args, **kwargs):
        raise AssertionError("live checkout identity crossed baked authority")

    monkeypatch.setattr("hermes_cli.build_info.get_code_identity", _forbidden)
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    refusal = perform_update(
        surface="dashboard_api",
        project_root=checkout,
        provenance_path=marker,
    )

    assert refusal is not None
    expected_identity = {"sha": revision, "version": version}
    assert refusal.current_identity == expected_identity
    payload = json.loads(
        (tmp_path / "receipts" / "latest.json").read_text(encoding="utf-8")
    )
    assert payload["pre_update"] == expected_identity
    assert payload["post_update"] == expected_identity
    assert payload["refusal"]["current_identity"] == expected_identity
