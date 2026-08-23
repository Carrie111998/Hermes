"""Fail-closed tests for the runtime registry loader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import pytest

from agent.runtime_registry import (
    BaselineTestResult,
    RegistryLoadError,
    RegistryLoader,
    load_registry,
    run_baseline_tests,
    run_registry_integrity_baseline,
    validate_promotion_state,
)


PAYLOADS = {
    "route-policy.json": {
        "schema_version": "1.0",
        "description": "route",
        "default_route": {"level": "L1", "risk": "low"},
        "level_contracts": {"L1": "basic"},
        "level_workflows": {"L1": "standard"},
        "specialized_workflows": {},
        "risk_gates": {"low": {"min_workflow": None}},
        "semantic_router": {
            "enabled": True,
            "max_calls_per_task": 1,
            "timeout_ms": 6000,
            "model_policy": "route",
            "triggers": [],
            "on_failure": "fast_gate_conservative",
            "on_low_confidence": "no_upgrade",
        },
        "workflow_rank": {"standard": 1},
    },
    "workflow-templates.json": {
        "schema_version": "1.1",
        "description": "workflows",
        "templates": {
            "standard": {
                "roles": ["responder"],
                "verify": False,
                "policies": ["route"],
            }
        },
    },
    "execution-roles.json": {
        "schema_version": "1.0",
        "description": "roles",
        "roles": {
            "responder": {
                "responsibility": "answer",
                "tools": "contextual",
                "dispatch": "self",
                "model_policy": "route",
            }
        },
    },
    "model-policies.json": {
        "schema_version": "1.2",
        "description": "policies",
        "policies": {"route": {"primary": "model"}},
    },
    "model-profiles.json": {
        "schema_version": "1.1",
        "description": "profiles",
        "profiles": {
            "model": {
                "vendor_family": "test",
                "supported_reasoning": ["low"],
                "thinking_map": {"low": "low"},
                "context_window": 1000,
                "supports_tools": True,
                "supports_images": False,
            }
        },
    },
    "capability-contracts.json": {
        "schema_version": "1.0",
        "description": "contracts",
        "contracts": {
            "basic": {
                "quality": "basic",
                "modality": ["text"],
                "tools": "basic",
                "context_class": "short",
                "reasoning_intent": "low",
            }
        },
    },
}


def _write_registry(root: Path, *, promotion_state: str = "PUBLISHED") -> None:
    root.mkdir(exist_ok=True)
    entries = []
    for name, payload in PAYLOADS.items():
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        (root / name).write_bytes(data)
        entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    prompt = b"route prompt\n"
    (root / "semantic-router-prompt.md").write_bytes(prompt)
    entries.append(
        {
            "path": "semantic-router-prompt.md",
            "sha256": hashlib.sha256(prompt).hexdigest(),
        }
    )
    manifest = {
        "schemaVersion": "hermes-workflow-registry/1.0",
        "registryVersion": "2026-08-21.1",
        "promotionState": promotion_state,
        "files": entries,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _rewrite_manifest(root: Path, **changes: object) -> None:
    manifest = _manifest(root)
    manifest.update(changes)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite_payload(root: Path, filename: str, payload: object) -> None:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    (root / filename).write_bytes(data)
    manifest = _manifest(root)
    next(entry for entry in manifest["files"] if entry["path"] == filename)[
        "sha256"
    ] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_published_registry_load_returns_immutable_snapshot(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    snapshot = load_registry(tmp_path, mode="production")

    assert snapshot.registry_version == "2026-08-21.1"
    assert snapshot.promotion_state == "PUBLISHED"
    assert snapshot.source == "production"
    assert set(snapshot.payload_hashes) == set(PAYLOADS) | {"semantic-router-prompt.md"}
    with pytest.raises(TypeError):
        snapshot.bundle["route_policy"] = {}  # type: ignore[index]

    # A loaded snapshot is stable and does not hot-reload registry bytes.
    (tmp_path / "route-policy.json").write_text("changed", encoding="utf-8")
    assert snapshot.bundle["route_policy"]["default_route"]["level"] == "L1"


def test_capability_contract_accepts_declared_output_policy(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS["capability-contracts.json"]))
    payload["contracts"]["basic"]["output_policy"] = "concise_evidence"
    _rewrite_payload(tmp_path, "capability-contracts.json", payload)

    snapshot = load_registry(tmp_path)

    assert snapshot.bundle["capability_contracts"]["contracts"]["basic"]["output_policy"] == "concise_evidence"


@pytest.mark.parametrize("value", ["brief", "", 1, None])
def test_capability_contract_rejects_invalid_output_policy(tmp_path: Path, value: object) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS["capability-contracts.json"]))
    payload["contracts"]["basic"]["output_policy"] = value
    _rewrite_payload(tmp_path, "capability-contracts.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_schema"
    assert exc_info.value.path == "capability-contracts.json.contracts.basic.output_policy"


def test_ready_for_review_is_rejected_in_production(tmp_path: Path) -> None:
    _write_registry(tmp_path, promotion_state="READY_FOR_REVIEW")

    with pytest.raises(RegistryLoadError, match="promotion"):
        load_registry(tmp_path, mode="production")


def test_ready_for_review_is_accepted_only_in_explicit_preview(tmp_path: Path) -> None:
    _write_registry(tmp_path, promotion_state="READY_FOR_REVIEW")

    snapshot = load_registry(tmp_path, mode="preview")

    assert snapshot.promotion_state == "READY_FOR_REVIEW"
    assert snapshot.is_candidate is True
    assert snapshot.source == "preview"


@pytest.mark.parametrize("state", ["DRAFT", "UNKNOWN"])
def test_unpublished_states_fail_closed_in_production(tmp_path: Path, state: str) -> None:
    _write_registry(tmp_path, promotion_state=state)

    with pytest.raises(RegistryLoadError):
        load_registry(tmp_path, mode="production")


def test_missing_manifest_file_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    (tmp_path / "route-policy.json").unlink()

    with pytest.raises(RegistryLoadError, match="missing"):
        load_registry(tmp_path)


def test_payload_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    (tmp_path / "route-policy.json").write_bytes(b"tampered")

    with pytest.raises(RegistryLoadError, match="hash"):
        load_registry(tmp_path)


def test_invalid_json_is_rejected_after_hash_validation(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    invalid = b"{not-json"
    (tmp_path / "route-policy.json").write_bytes(invalid)
    manifest = _manifest(tmp_path)
    next(entry for entry in manifest["files"] if entry["path"] == "route-policy.json")[
        "sha256"
    ] = hashlib.sha256(invalid).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryLoadError, match="JSON"):
        load_registry(tmp_path)


def test_missing_required_json_section_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS["route-policy.json"])
    payload.pop("default_route")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "route-policy.json").write_bytes(data)
    manifest = _manifest(tmp_path)
    next(entry for entry in manifest["files"] if entry["path"] == "route-policy.json")[
        "sha256"
    ] = hashlib.sha256(data).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryLoadError, match="required section"):
        load_registry(tmp_path)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    outside = tmp_path.parent / "outside-registry-payload.json"
    outside.write_bytes((tmp_path / "route-policy.json").read_bytes())
    (tmp_path / "route-policy.json").unlink()
    (tmp_path / "route-policy.json").symlink_to(outside)

    with pytest.raises(RegistryLoadError, match="escapes"):
        load_registry(tmp_path)


def test_unlisted_nested_behavior_file_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    extra = tmp_path / "nested" / "extra.json"
    extra.parent.mkdir()
    extra.write_text("{}", encoding="utf-8")

    with pytest.raises(RegistryLoadError, match="unlisted"):
        load_registry(tmp_path)


def test_root_backup_tree_is_ignored_by_live_registry_validation(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    backup_payload = tmp_path / ".backup_20260823_142543" / "route-policy.json"
    backup_payload.parent.mkdir()
    backup_payload.write_text("archived registry payload", encoding="utf-8")

    snapshot = load_registry(tmp_path)

    assert snapshot.registry_version == "2026-08-21.1"


def test_invalid_manifest_schema_version_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    _rewrite_manifest(tmp_path, schemaVersion="hermes-workflow-registry/999.0")

    with pytest.raises(RegistryLoadError, match="schemaVersion"):
        load_registry(tmp_path)


def test_invalid_registry_version_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    _rewrite_manifest(tmp_path, registryVersion="not-a-version")

    with pytest.raises(RegistryLoadError, match="registryVersion"):
        load_registry(tmp_path)


def test_path_traversal_is_rejected_before_file_access(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["files"][0]["path"] = "../outside.json"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryLoadError, match="path"):
        load_registry(tmp_path)


def test_baseline_test_interface_reports_all_results() -> None:
    results = run_baseline_tests(
        [
            lambda: BaselineTestResult(name="B01", passed=True),
            lambda: BaselineTestResult(name="B02", passed=False, detail="drift"),
        ]
    )

    assert results.passed is False
    assert [result.name for result in results.results] == ["B01", "B02"]


def test_unknown_payload_schema_version_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS["route-policy.json"])
    payload["schema_version"] = "9.9"
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "unsupported_schema_version"
    assert exc_info.value.path == "route-policy.json"


@pytest.mark.parametrize(
    ("filename", "section"),
    [
        ("route-policy.json", "level_contracts"),
        ("workflow-templates.json", "templates"),
        ("execution-roles.json", "roles"),
        ("model-policies.json", "policies"),
        ("model-profiles.json", "profiles"),
        ("capability-contracts.json", "contracts"),
    ],
)
def test_malformed_nested_section_is_registry_load_error(
    tmp_path: Path, filename: str, section: str
) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS[filename])
    payload[section] = None
    _rewrite_payload(tmp_path, filename, payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_schema"
    assert exc_info.value.path == f"{filename}.{section}"


def test_malformed_nested_value_does_not_leak_type_error(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS["route-policy.json"])
    payload["level_contracts"] = {"L1": ["basic"]}
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_schema"
    assert exc_info.value.path == "route-policy.json.level_contracts.L1"


def test_route_contract_and_workflow_references_must_close(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS["route-policy.json"])
    payload["level_contracts"] = {"L1": "missing-contract"}
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert exc_info.value.path == "route-policy.json.level_contracts.L1"


@pytest.mark.parametrize(
    ("filename", "mutation", "expected_path"),
    [
        (
            "workflow-templates.json",
            lambda payload: payload["templates"]["standard"].update(
                {"roles": ["missing-role"]}
            ),
            "workflow-templates.json.templates.standard.roles[0]",
        ),
        (
            "workflow-templates.json",
            lambda payload: payload["templates"]["standard"].update(
                {"policies": ["missing-policy"]}
            ),
            "workflow-templates.json.templates.standard.policies[0]",
        ),
        (
            "execution-roles.json",
            lambda payload: payload["roles"]["responder"].update(
                {"model_policy": "missing-policy"}
            ),
            "execution-roles.json.roles.responder.model_policy",
        ),
        (
            "model-policies.json",
            lambda payload: payload["policies"]["route"].update(
                {"primary": "missing-profile"}
            ),
            "model-policies.json.policies.route.primary",
        ),
    ],
)
def test_cross_file_references_are_closed(
    tmp_path: Path, filename: str, mutation: Callable[[dict], None], expected_path: str
) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS[filename]))
    mutation(payload)
    _rewrite_payload(tmp_path, filename, payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert exc_info.value.path == expected_path


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    real_manifest = tmp_path / "manifest-real.json"
    (tmp_path / "manifest.json").rename(real_manifest)
    (tmp_path / "manifest.json").symlink_to(real_manifest)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_manifest_file"
    assert exc_info.value.path == "manifest.json"


def test_payload_symlink_is_rejected_even_when_hash_matches(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    outside = tmp_path.parent / "payload-outside.json"
    payload_path = tmp_path / "route-policy.json"
    outside.write_bytes(payload_path.read_bytes())
    payload_path.unlink()
    payload_path.symlink_to(outside)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "unsafe_file"
    assert exc_info.value.path == "route-policy.json"


@pytest.mark.parametrize(
    ("mode", "allow_candidate", "expected_code"),
    [
        (None, False, "invalid_mode"),
        ("production", 1, "invalid_allow_candidate"),
        ("production", None, "invalid_allow_candidate"),
        ("PRODUCTION", False, "invalid_mode"),
    ],
)
def test_load_arguments_are_strictly_typed(
    tmp_path: Path, mode: object, allow_candidate: object, expected_code: str
) -> None:
    _write_registry(tmp_path)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path, mode=mode, allow_candidate=allow_candidate)  # type: ignore[arg-type]

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("filename", "remove"),
    [
        ("route-policy.json", lambda payload: payload["default_route"].pop("level")),
        (
            "workflow-templates.json",
            lambda payload: payload["templates"]["standard"].pop("verify"),
        ),
        (
            "execution-roles.json",
            lambda payload: payload["roles"]["responder"].pop("model_policy"),
        ),
        ("model-policies.json", lambda payload: payload["policies"]["route"].pop("primary")),
        (
            "model-profiles.json",
            lambda payload: payload["profiles"]["model"].pop("context_window"),
        ),
        (
            "capability-contracts.json",
            lambda payload: payload["contracts"]["basic"].pop("modality"),
        ),
    ],
)
def test_missing_nested_required_field_is_registry_load_error(
    tmp_path: Path, filename: str, remove: Callable[[dict], None]
) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS[filename]))
    remove(payload)
    _rewrite_payload(tmp_path, filename, payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path is not None
    assert exc_info.value.path.startswith(filename + ".")


def test_malformed_manifest_promotion_state_does_not_leak_type_error(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    _rewrite_manifest(tmp_path, promotionState=[])

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_promotion_state"
    assert exc_info.value.path == "manifest.json"


def test_empty_baseline_report_contains_no_fabricated_evidence() -> None:
    report = run_baseline_tests([])

    assert report.passed is False
    assert report.results == ()


def test_unknown_manifest_payload_is_a_structured_registry_error(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    unknown = b'{"schema_version":"9.9","description":"unknown","payload":{}}'
    (tmp_path / "unknown.json").write_bytes(unknown)
    manifest = _manifest(tmp_path)
    manifest["files"].append(
        {"path": "unknown.json", "sha256": hashlib.sha256(unknown).hexdigest()}
    )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "unknown_payload"
    assert exc_info.value.path == "unknown.json"


@pytest.mark.parametrize(
    "missing_field",
    [
        "level_contracts",
        "level_workflows",
        "specialized_workflows",
        "risk_gates",
        "semantic_router",
        "workflow_rank",
    ],
)
def test_route_policy_missing_required_top_level_is_structured_error(
    tmp_path: Path, missing_field: str
) -> None:
    _write_registry(tmp_path)
    payload = dict(PAYLOADS["route-policy.json"])
    payload.pop(missing_field)
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path == f"route-policy.json.{missing_field}"


@pytest.mark.parametrize("raw_path", ["route-policy.json\x00", 123, [], {}])
def test_manifest_payload_path_never_leaks_path_errors(
    tmp_path: Path, raw_path: object
) -> None:
    _write_registry(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["files"][0]["path"] = raw_path
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code in {"invalid_manifest", "invalid_path", "path_traversal"}
    assert exc_info.value.path is not None


def test_registry_root_path_never_leaks_type_or_nul_errors(tmp_path: Path) -> None:
    for invalid_root in (123, [], {}, str(tmp_path) + "\x00"):
        with pytest.raises(RegistryLoadError) as exc_info:
            load_registry(invalid_root)  # type: ignore[arg-type]
        assert exc_info.value.code in {"invalid_root", "invalid_registry_path"}
        assert exc_info.value.path == "root"


class KeyErrorPath:
    def __fspath__(self) -> str:
        raise KeyError("path lookup failed")


def test_public_registry_root_key_error_is_structured_path_error() -> None:
    for public_api in (load_registry, RegistryLoader):
        with pytest.raises(RegistryLoadError) as exc_info:
            public_api(KeyErrorPath())  # type: ignore[arg-type]
        assert exc_info.value.code == "invalid_registry_path"
        assert exc_info.value.path == "root"


def test_registry_loader_public_api_loads_explicit_root(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    snapshot = RegistryLoader(tmp_path).load(mode="preview")

    assert snapshot.registry_version == "2026-08-21.1"
    assert snapshot.source == "preview"


def test_validate_promotion_state_public_api_is_fail_closed() -> None:
    assert validate_promotion_state("PUBLISHED") is True
    assert validate_promotion_state("READY_FOR_REVIEW") is False
    assert validate_promotion_state("READY_FOR_REVIEW", mode="preview") is True
    assert validate_promotion_state("NOT_A_STATE", mode="preview") is False


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        (
            lambda payload: payload["risk_gates"].update(
                {"medium": {"min_workflow": "missing-workflow"}}
            ),
            "route-policy.json.risk_gates.medium.min_workflow",
        ),
        (
            lambda payload: payload["workflow_rank"].update({"missing-workflow": 4}),
            "route-policy.json.workflow_rank.missing-workflow",
        ),
        (
            lambda payload: payload["semantic_router"].update(
                {"model_policy": "missing-policy"}
            ),
            "route-policy.json.semantic_router.model_policy",
        ),
        (
            lambda payload: payload["semantic_router"].update(
                {"rules": {"uncertain": {"workflow": "missing-workflow"}}}
            ),
            "route-policy.json.semantic_router.rules.uncertain.workflow",
        ),
    ],
)
def test_route_policy_references_are_closed(
    tmp_path: Path, mutation: Callable[[dict], None], expected_path: str
) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS["route-policy.json"]))
    mutation(payload)
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "dangling_reference"
    assert exc_info.value.path == expected_path


def test_semantic_router_rules_require_structured_objects(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    payload = json.loads(json.dumps(PAYLOADS["route-policy.json"]))
    payload["semantic_router"]["rules"] = []
    _rewrite_payload(tmp_path, "route-policy.json", payload)

    with pytest.raises(RegistryLoadError) as exc_info:
        load_registry(tmp_path)

    assert exc_info.value.code == "invalid_schema"
    assert exc_info.value.path == "route-policy.json.semantic_router.rules"


def test_current_live_registry_fixture_loads_in_explicit_preview() -> None:
    root = Path(__file__).parents[1] / "fixtures" / "runtime_registry" / "live"

    snapshot = RegistryLoader(root).load(mode="preview")

    assert snapshot.registry_version == "2026-08-22.18"
    assert snapshot.promotion_state == "READY_FOR_REVIEW"
    assert snapshot.is_candidate is True
    assert snapshot.source == "preview"


def test_registry_integrity_baseline_executes_real_checks(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    report = run_registry_integrity_baseline(tmp_path, mode="preview")

    assert report.passed is True
    assert {result.name for result in report.results} == {
        "registry-load",
        "registry-bundle",
        "registry-hashes",
    }
    assert all("benchmark" not in result.detail.lower() for result in report.results)


def test_registry_integrity_baseline_fails_closed_for_invalid_registry(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    (tmp_path / "route-policy.json").write_bytes(b"tampered")

    report = run_registry_integrity_baseline(tmp_path, mode="preview")

    assert report.passed is False
    assert report.results
    assert any(result.passed is False for result in report.results)
