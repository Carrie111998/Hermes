from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.image_provenance import (
    IMAGE_PROVENANCE_SCHEMA,
    read_image_provenance,
)


def _write_marker(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema": IMAGE_PROVENANCE_SCHEMA,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": "0.20.5",
        "revision": "a" * 40,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_absent_marker_preserves_non_image_install(tmp_path):
    assert read_image_provenance(tmp_path / "missing.json") is None


def test_embedded_nul_marker_path_returns_invalid_value_error():
    marker = Path("image\x00.json")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_presence_unreadable:ValueError"


def test_invalid_marker_path_argument_returns_invalid_type_error():
    provenance = read_image_provenance(object())

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_presence_unreadable:TypeError"


def test_valid_marker_exposes_baked_identity(tmp_path):
    marker = _write_marker(tmp_path / "image.json")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is True
    assert provenance.deployment_kind == "image"
    assert provenance.manager == "docker"
    assert provenance.image == "nousresearch/hermes-agent"
    assert provenance.version == "0.20.5"
    assert provenance.revision == "a" * 40
    assert provenance.marker_path == str(marker)


def test_present_malformed_marker_fails_closed(tmp_path):
    marker = tmp_path / "image.json"
    marker.write_text("{not-json", encoding="utf-8")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.deployment_kind == "image"
    assert provenance.valid is False
    assert provenance.error == "marker_unreadable:JSONDecodeError"


def test_boolean_schema_is_not_integer_schema_one(tmp_path):
    marker = _write_marker(tmp_path / "image.json", schema=True)

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "unsupported_marker_schema"


def test_directory_marker_fails_closed(tmp_path):
    marker = tmp_path / "image.json"
    marker.mkdir()

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_not_regular_file"


def test_dangling_symlink_is_present_and_fails_closed(tmp_path):
    marker = tmp_path / "image.json"
    marker.symlink_to(tmp_path / "missing-target.json")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_not_regular_file"


def test_inaccessible_lookup_does_not_masquerade_as_absence(monkeypatch, tmp_path):
    marker = tmp_path / "image.json"
    original_lstat = Path.lstat

    def _blocked_lstat(path: Path):
        if path == marker:
            raise PermissionError("image marker lookup denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", _blocked_lstat)

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_presence_unreadable:PermissionError"


def test_marker_disappearing_after_lstat_still_fails_closed(monkeypatch, tmp_path):
    marker = _write_marker(tmp_path / "image.json")
    original_read_text = Path.read_text

    def _vanished_read(path: Path, *args, **kwargs):
        if path == marker:
            raise FileNotFoundError("marker removed after lookup")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _vanished_read)

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.error == "marker_unreadable:FileNotFoundError"


def test_marker_classification_ignores_runtime_environment(monkeypatch, tmp_path):
    marker = _write_marker(tmp_path / "image.json")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv("HERMES_MANAGED", "false")
    monkeypatch.setenv("HERMES_INSTALL_METHOD", "git")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is True
    assert provenance.deployment_kind == "image"
    assert provenance.manager == "docker"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ([], "marker_not_object"),
        ("image", "marker_not_object"),
        (None, "marker_not_object"),
        ({}, "unsupported_marker_schema"),
        ({"schema": 0}, "unsupported_marker_schema"),
        ({"schema": 2}, "unsupported_marker_schema"),
        ({"schema": "1"}, "unsupported_marker_schema"),
        ({"schema": False}, "unsupported_marker_schema"),
        ({"schema": True}, "unsupported_marker_schema"),
        (
            {"schema": 1, "deployment_kind": "docker", "manager": "docker"},
            "invalid_deployment_kind",
        ),
        (
            {"schema": 1, "deployment_kind": "mutable", "manager": "docker"},
            "invalid_deployment_kind",
        ),
        (
            {"schema": 1, "deployment_kind": "image"},
            "missing_manager",
        ),
        (
            {"schema": 1, "deployment_kind": "image", "manager": ""},
            "missing_manager",
        ),
        (
            {"schema": 1, "deployment_kind": "image", "manager": "   "},
            "missing_manager",
        ),
        (
            {"schema": 1, "deployment_kind": "image", "manager": 7},
            "missing_manager",
        ),
        (
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "image": 7,
            },
            "invalid_image",
        ),
        (
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "version": ["0.20.5"],
            },
            "invalid_version",
        ),
        (
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "revision": {"sha": "a" * 40},
            },
            "invalid_revision",
        ),
    ],
)
def test_every_present_invalid_marker_shape_refuses_closed(
    tmp_path, payload, expected_error
):
    marker = tmp_path / "image.json"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is False
    assert provenance.deployment_kind == "image"
    assert provenance.manager == "unknown"
    assert provenance.error == expected_error


def test_optional_identity_fields_are_normalized_without_weakening_marker(tmp_path):
    marker = _write_marker(
        tmp_path / "image.json",
        manager="  docker  ",
        image="  nousresearch/hermes-agent:latest  ",
        version="   ",
        revision="  abc123  ",
    )

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is True
    assert provenance.manager == "docker"
    assert provenance.image == "nousresearch/hermes-agent:latest"
    assert provenance.version is None
    assert provenance.revision == "abc123"


@pytest.mark.parametrize("field", ["image", "version", "revision"])
def test_optional_identity_fields_may_be_absent(field, tmp_path):
    marker = _write_marker(tmp_path / "image.json")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload.pop(field)
    marker.write_text(json.dumps(payload), encoding="utf-8")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is True
    assert getattr(provenance, field) is None
