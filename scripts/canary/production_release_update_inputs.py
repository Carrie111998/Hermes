#!/usr/bin/env python3
"""Pure validation for the non-builder inputs of release-update Stage 0.

The signed release-update plan binds document *identities*.  For a
self-hashed JSON document that identity is the document's internal digest
field, not the SHA-256 of its newline-terminated file representation.  This
module makes that distinction explicit for every remaining Stage-0 input and
cross-binds the documents to the signed update publication.

No function in this module reads the host.  In particular, the cron artifact
index is validated as the exact, self-hashed collector envelope produced by the
trusted-cron package.  Re-attesting those rows against installed host bytes is
a separate privileged host-action gate before mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from gateway import production_alias_projection_units as alias_units
from gateway import production_cron_continuity_package as trusted_cron
from ops.muncho.runtime import trusted_cron_collector_rail as cron_rail
from scripts.canary import package_production_cutover_artifacts as host_package
from scripts.canary import production_cutover_host_authority as host_authority
from scripts.canary import production_cutover_owner_launcher as cutover_owner
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_host_observer as host_observer
from scripts.canary import production_release_unit_inputs_v4 as unit_inputs_v4
from scripts.canary import production_release_update_contract as update_contract
from scripts.canary import production_release_update_runtime as update_runtime


RELEASE_CONSUMER_SET_SCHEMA = (
    "muncho-production-release-consumer-set.v1"
)
ACTIVATION_PLAN_SCHEMA = "muncho-production-release-activation-plan.v4"
ROLLBACK_PLAN_SCHEMA = "muncho-production-release-rollback-plan.v4"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DOCUMENT_FIELDS = frozenset(
    {
        "host_inventory_sha256",
        "release_consumer_set_sha256",
        "host_artifact_manifest_sha256",
        "host_mutation_authority_sha256",
        "host_mutation_initial_collector_receipt_sha256",
        "cron_artifact_index_sha256",
        "alias_artifact_index_sha256",
        "successor_unit_input_publication_sha256",
        "activation_plan_sha256",
        "rollback_plan_sha256",
    }
)
_ARTIFACT_IDENTITY_FIELDS = frozenset(
    {
        "host_inventory_sha256",
        "release_consumer_set_sha256",
        "host_artifact_manifest_sha256",
        "host_mutation_authority_sha256",
        "host_mutation_initial_collector_receipt_sha256",
        "cron_artifact_index_sha256",
        "alias_artifact_index_sha256",
        "successor_unit_input_publication_sha256",
    }
)
_CONSUMER_SET_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "release_revision",
        "consumers",
        "consumer_count",
        "execution_service_count",
        "trigger_unit_count",
        "catalog_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
        "consumer_set_sha256",
    }
)
_ACTIVATION_PLAN_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "release_revision",
        "artifact_identities",
        "forward_phase_order",
        "forward_phase_count",
        "commit_phase",
        "first_application_mutation_phase",
        "unit_input_preauthorization_phase",
        "unit_input_finalization_phase",
        "unit_input_preauthorization_before_commit",
        "unit_input_finalization_after_commit",
        "catalog_consumer_unit_count",
        "catalog_execution_service_count",
        "catalog_trigger_unit_count",
        "mutation_path_partitions",
        "mutation_target_paths",
        "mutation_target_count",
        "mutation_target_set_sha256",
        "unmodeled_mutation_allowed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "activation_plan_sha256",
    }
)
_ROLLBACK_PLAN_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "release_revision",
        "artifact_identities",
        "rollback_phase_order",
        "rollback_phase_count",
        "preauthorized_rollback_phase_order",
        "preauthorized_rollback_phase_count",
        "unit_input_preauthorization_discriminator_phase",
        "unit_input_preauthorization_cancel_phase",
        "unit_input_preauthorization_cancel_before_host_restore",
        "commit_phase",
        "rollback_allowed_before_commit_only",
        "catalog_consumer_unit_count",
        "catalog_execution_service_count",
        "catalog_trigger_unit_count",
        "mutation_path_partitions",
        "mutation_target_paths",
        "mutation_target_count",
        "mutation_target_set_sha256",
        "unmodeled_mutation_allowed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "rollback_plan_sha256",
    }
)
_MUTATION_PARTITION_FIELDS = frozenset(
    {
        "consumer_fragment_paths",
        "consumer_drop_in_paths",
        "host_artifact_target_paths",
        "live_unit_input_paths",
        "release_pointer_paths",
    }
)
_HOST_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "release_revision",
        "source",
        "unit_inputs",
        "sealed_runtime_artifact_request",
        "host_artifact_contract",
        "artifacts",
        "plan_bindings",
        "secret_material_recorded",
        "manifest_sha256",
    }
)
_HOST_SOURCE_FIELDS = frozenset(
    {
        "template_sha256",
        "legacy_reconcile_sha256",
        "writer_migration_sha256",
        "connector_unit_template_sha256",
        "gateway_connector_drop_in_sha256",
        "connector_config_template_sha256",
        "production_capability_prerequisite_contract_sha256",
        "runtime_dependency_manifest_sha256",
        "runtime_dependency_identity_sha256",
        "sealed_runtime_artifact_request_sha256",
        "operational_asset_manifest_sha256",
        "operational_asset_verification_sha256",
        "alias_projection_package_sha256",
    }
)
_SEALED_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "release_revision",
        "target",
        "files",
        "isolated_worker_lease_mountpoint",
        "topology_fragments",
        "capability_bundle",
        "isolated_worker_bundle",
        "operational_edge_bundle",
        "owner_gate_receipt_public_key_id",
        "operational_asset_verification",
        "secret_material_recorded",
        "secret_digest_recorded",
        "request_sha256",
    }
)
_HOST_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "files",
        "required_file_count",
        "all_files_require_readback",
        "secret_material_recorded",
        "secret_digest_recorded",
        "contract_sha256",
    }
)
_CRON_INDEX_FIELDS = frozenset(
    {
        "schema",
        "release_revision",
        "plan_relative_path",
        "plan_sha256",
        "replacement_bundle_relative_path",
        "replacement_bundle_sha256",
        "collector_manifest_relative_path",
        "collector_manifest_sha256",
        "cutover_runtime_sha256",
        "cutover_entrypoint_sha256",
        "files",
        "file_count",
        "units_installed",
        "timers_enabled",
        "timers_started",
        "jobs_store_mutated",
        "secret_material_recorded",
        "artifact_index_sha256",
    }
)


class ProductionReleaseUpdateInputsError(ValueError):
    """Stable, secret-free failure at the pure Stage-0 input boundary."""


def _fail(code: str, exc: BaseException | None = None) -> NoReturn:
    del exc
    raise ProductionReleaseUpdateInputsError(code) from None


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("release_update_inputs_json_invalid", exc)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plain(value: Any) -> Any:
    if value is None or type(value) in {bool, int, float, str}:
        if isinstance(value, float):
            _fail("release_update_inputs_json_invalid")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(name, str) for name in value):
            _fail("release_update_inputs_json_invalid")
        return {name: _plain(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    _fail("release_update_inputs_json_invalid")


def _mapping(
    value: Any,
    fields: frozenset[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    return _plain(dict(value))


def _self_hashed(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    raw = _mapping(value, fields, code)
    digest = raw.get(digest_field)
    unsigned = {
        name: item for name, item in raw.items() if name != digest_field
    }
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        _fail(code)
    return raw


def _revision(value: Any, code: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        _fail(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _revisions(
    predecessor_revision: Any,
    release_revision: Any,
    code: str,
) -> tuple[str, str]:
    predecessor = _revision(predecessor_revision, code)
    release = _revision(release_revision, code)
    if predecessor == release or predecessor[:12] == release[:12]:
        _fail(code)
    return predecessor, release


def _consumer_rows() -> list[dict[str, Any]]:
    try:
        catalog = inventory.expected_consumer_catalog()
    except inventory.ProductionReleaseConsumerInventoryError as exc:
        _fail("release_update_inputs_consumer_catalog_invalid", exc)
    return [
        {
            "name": name,
            "source": spec.source,
            "kind": spec.kind,
            "fragment_path": spec.fragment_path,
            "drop_in_paths": list(spec.drop_in_paths),
            "triggers": list(spec.triggers),
            "triggered_by": list(spec.triggered_by),
            "executes_release": spec.executes_release,
        }
        for name, spec in catalog.items()
    ]


def build_release_consumer_set(
    *,
    predecessor_revision: str,
    release_revision: str,
) -> Mapping[str, Any]:
    """Build the deterministic identity of the authoritative consumer set."""

    predecessor, release = _revisions(
        predecessor_revision,
        release_revision,
        "release_update_inputs_consumer_set_invalid",
    )
    rows = _consumer_rows()
    unsigned = {
        "schema": RELEASE_CONSUMER_SET_SCHEMA,
        "predecessor_revision": predecessor,
        "release_revision": release,
        "consumers": rows,
        "consumer_count": inventory.EXPECTED_UNIT_COUNT,
        "execution_service_count": (
            inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        ),
        "trigger_unit_count": inventory.EXPECTED_TRIGGER_UNIT_COUNT,
        "catalog_sha256": sha256_bytes(canonical_bytes(rows)),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "consumer_set_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }


def validate_release_consumer_set(
    value: Any,
    *,
    predecessor_revision: str,
    release_revision: str,
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_CONSUMER_SET_FIELDS,
        digest_field="consumer_set_sha256",
        code="release_update_inputs_consumer_set_invalid",
    )
    expected = build_release_consumer_set(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
    )
    if raw != expected:
        _fail("release_update_inputs_consumer_set_invalid")
    return raw


def _mutation_path_partitions() -> dict[str, list[str]]:
    """Return the closed path partitions a transaction is allowed to write.

    The partitions intentionally overlap.  For example, a host systemd unit
    is both a consumer fragment and a host artifact target.  The flattened
    target set below is their de-duplicated union.  Recovery-gate artifacts
    are fixed, preinstalled, read-only prerequisites and transaction journal
    receipts are append-only evidence, so neither is a release mutation
    target.
    """

    try:
        catalog = inventory.expected_consumer_catalog()
    except inventory.ProductionReleaseConsumerInventoryError as exc:
        _fail("release_update_inputs_mutation_target_set_invalid", exc)
    fragment_paths = [
        spec.fragment_path for spec in catalog.values()
    ]
    drop_in_paths = [
        path
        for spec in catalog.values()
        for path in spec.drop_in_paths
    ]
    host_artifact_targets = [
        target
        for target, _binding in host_package.HOST_ARTIFACT_TARGETS.values()
    ]
    partitions = {
        "consumer_fragment_paths": sorted(set(fragment_paths)),
        "consumer_drop_in_paths": sorted(set(drop_in_paths)),
        "host_artifact_target_paths": sorted(
            set(host_artifact_targets)
        ),
        "live_unit_input_paths": sorted(
            {
                str(host_package.STAGED_UNIT_INPUT_PLAN_PATH),
                str(host_package.STAGED_UNIT_INPUT_APPROVAL_PATH),
                str(host_package.FIXED_UNIT_INPUTS_PATH),
            }
        ),
        "release_pointer_paths": [
            str(inventory.COMPATIBILITY_RELEASE_SYMLINK)
        ],
    }
    if (
        set(partitions) != _MUTATION_PARTITION_FIELDS
        or len(catalog) != inventory.EXPECTED_UNIT_COUNT
        or len(fragment_paths) != len(set(fragment_paths))
        or len(host_artifact_targets) != len(set(host_artifact_targets))
        or inventory.EXPECTED_UNIT_COUNT
        != update_runtime.EXPECTED_CONSUMER_UNIT_COUNT
        or inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        != update_runtime.EXPECTED_SERVICE_UNIT_COUNT
        or inventory.EXPECTED_TRIGGER_UNIT_COUNT
        != update_runtime.EXPECTED_TRIGGER_UNIT_COUNT
        or any(
            not paths
            or paths != sorted(paths)
            or len(paths) != len(set(paths))
            or any(
                not isinstance(path, str)
                or not path.startswith("/")
                or "\x00" in path
                for path in paths
            )
            for paths in partitions.values()
        )
    ):
        _fail("release_update_inputs_mutation_target_set_invalid")
    return partitions


def _artifact_identities(value: Any) -> dict[str, str]:
    raw = _mapping(
        value,
        _ARTIFACT_IDENTITY_FIELDS,
        "release_update_inputs_plan_invalid",
    )
    for name in sorted(raw):
        _sha256(raw[name], "release_update_inputs_plan_invalid")
    return raw


def _plan_common(
    *,
    predecessor_revision: str,
    release_revision: str,
    artifact_identities: Mapping[str, Any],
) -> tuple[
    str,
    str,
    dict[str, str],
    dict[str, list[str]],
    list[str],
    str,
]:
    predecessor, release = _revisions(
        predecessor_revision,
        release_revision,
        "release_update_inputs_plan_invalid",
    )
    identities = _artifact_identities(artifact_identities)
    partitions = _mutation_path_partitions()
    targets = sorted(
        {
            path
            for paths in partitions.values()
            for path in paths
        }
    )
    if not targets:
        _fail("release_update_inputs_mutation_target_set_invalid")
    return (
        predecessor,
        release,
        identities,
        partitions,
        targets,
        sha256_bytes(canonical_bytes(targets)),
    )


def build_activation_plan(
    *,
    predecessor_revision: str,
    release_revision: str,
    artifact_identities: Mapping[str, Any],
) -> Mapping[str, Any]:
    (
        predecessor,
        release,
        identities,
        partitions,
        targets,
        target_digest,
    ) = _plan_common(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
        artifact_identities=artifact_identities,
    )
    unsigned = {
        "schema": ACTIVATION_PLAN_SCHEMA,
        "predecessor_revision": predecessor,
        "release_revision": release,
        "artifact_identities": identities,
        "forward_phase_order": list(update_runtime.FORWARD_PHASES),
        "forward_phase_count": len(update_runtime.FORWARD_PHASES),
        "commit_phase": update_runtime.COMMIT_PHASE,
        "first_application_mutation_phase": (
            update_runtime.FIRST_APPLICATION_MUTATION_PHASE
        ),
        "unit_input_preauthorization_phase": (
            update_runtime.UNIT_INPUT_PREAUTHORIZATION_PHASE
        ),
        "unit_input_finalization_phase": "unit_inputs_finalized",
        "unit_input_preauthorization_before_commit": True,
        "unit_input_finalization_after_commit": True,
        "catalog_consumer_unit_count": inventory.EXPECTED_UNIT_COUNT,
        "catalog_execution_service_count": (
            inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        ),
        "catalog_trigger_unit_count": (
            inventory.EXPECTED_TRIGGER_UNIT_COUNT
        ),
        "mutation_path_partitions": partitions,
        "mutation_target_paths": targets,
        "mutation_target_count": len(targets),
        "mutation_target_set_sha256": target_digest,
        "unmodeled_mutation_allowed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "activation_plan_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }


def validate_activation_plan(
    value: Any,
    *,
    predecessor_revision: str,
    release_revision: str,
    artifact_identities: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_ACTIVATION_PLAN_FIELDS,
        digest_field="activation_plan_sha256",
        code="release_update_inputs_activation_plan_invalid",
    )
    expected = build_activation_plan(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
        artifact_identities=artifact_identities,
    )
    if raw != expected:
        _fail("release_update_inputs_activation_plan_invalid")
    return raw


def build_rollback_plan(
    *,
    predecessor_revision: str,
    release_revision: str,
    artifact_identities: Mapping[str, Any],
) -> Mapping[str, Any]:
    (
        predecessor,
        release,
        identities,
        partitions,
        targets,
        target_digest,
    ) = _plan_common(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
        artifact_identities=artifact_identities,
    )
    unsigned = {
        "schema": ROLLBACK_PLAN_SCHEMA,
        "predecessor_revision": predecessor,
        "release_revision": release,
        "artifact_identities": identities,
        "rollback_phase_order": list(update_runtime.ROLLBACK_PHASES),
        "rollback_phase_count": len(update_runtime.ROLLBACK_PHASES),
        "preauthorized_rollback_phase_order": list(
            update_runtime.PREAUTHORIZED_ROLLBACK_PHASES
        ),
        "preauthorized_rollback_phase_count": len(
            update_runtime.PREAUTHORIZED_ROLLBACK_PHASES
        ),
        "unit_input_preauthorization_discriminator_phase": (
            update_runtime.UNIT_INPUT_PREAUTHORIZATION_DISCRIMINATOR_PHASE
        ),
        "unit_input_preauthorization_cancel_phase": (
            update_runtime.UNIT_INPUT_PREAUTHORIZATION_CANCEL_PHASE
        ),
        "unit_input_preauthorization_cancel_before_host_restore": True,
        "commit_phase": update_runtime.COMMIT_PHASE,
        "rollback_allowed_before_commit_only": True,
        "catalog_consumer_unit_count": inventory.EXPECTED_UNIT_COUNT,
        "catalog_execution_service_count": (
            inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        ),
        "catalog_trigger_unit_count": (
            inventory.EXPECTED_TRIGGER_UNIT_COUNT
        ),
        "mutation_path_partitions": partitions,
        "mutation_target_paths": targets,
        "mutation_target_count": len(targets),
        "mutation_target_set_sha256": target_digest,
        "unmodeled_mutation_allowed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "rollback_plan_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }


def validate_rollback_plan(
    value: Any,
    *,
    predecessor_revision: str,
    release_revision: str,
    artifact_identities: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_ROLLBACK_PLAN_FIELDS,
        digest_field="rollback_plan_sha256",
        code="release_update_inputs_rollback_plan_invalid",
    )
    expected = build_rollback_plan(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
        artifact_identities=artifact_identities,
    )
    if raw != expected:
        _fail("release_update_inputs_rollback_plan_invalid")
    return raw


def _validate_sealed_request(
    value: Any,
    *,
    release_revision: str,
    unit_inputs: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_SEALED_REQUEST_FIELDS,
        digest_field="request_sha256",
        code="release_update_inputs_host_manifest_invalid",
    )
    files = raw.get("files")
    expected_names = {
        name
        for name, (_target, binding) in (
            host_package.HOST_ARTIFACT_TARGETS.items()
        )
        if binding == "release_sealed_payload"
    }
    if (
        raw.get("schema")
        != host_package.SEALED_RUNTIME_ARTIFACT_REQUEST_V4_SCHEMA
        or raw.get("release_revision") != release_revision
        or raw.get("target") != unit_inputs["target"]
        or _SHA256.fullmatch(
            str(raw.get("owner_gate_receipt_public_key_id") or "")
        )
        is None
        or not isinstance(files, Mapping)
        or set(files) != expected_names
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
        or any(
            not isinstance(raw.get(name), Mapping)
            for name in (
                "isolated_worker_lease_mountpoint",
                "topology_fragments",
                "capability_bundle",
                "isolated_worker_bundle",
                "operational_edge_bundle",
                "operational_asset_verification",
            )
        )
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    assert isinstance(files, Mapping)
    for name in sorted(expected_names):
        item = files.get(name)
        target = host_package.HOST_ARTIFACT_TARGETS[name][0]
        if (
            not isinstance(item, Mapping)
            or set(item) != {"target_path", "sha256", "uid", "gid", "mode"}
            or item.get("target_path") != target
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or type(item.get("uid")) is not int
            or type(item.get("gid")) is not int
            or min(item["uid"], item["gid"]) < 0
            or item.get("mode")
            not in {0o400, 0o440, 0o444, 0o600, 0o640, 0o644}
        ):
            _fail("release_update_inputs_host_manifest_invalid")
    return raw


def _validate_host_contract(
    value: Any,
    *,
    sealed_request: Mapping[str, Any],
    source: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_HOST_CONTRACT_FIELDS,
        digest_field="contract_sha256",
        code="release_update_inputs_host_manifest_invalid",
    )
    files = raw.get("files")
    if (
        raw.get("schema") != host_package.HOST_ARTIFACT_CONTRACT_SCHEMA
        or not isinstance(files, Mapping)
        or set(files) != set(host_package.HOST_ARTIFACT_TARGETS)
        or raw.get("required_file_count")
        != len(host_package.HOST_ARTIFACT_TARGETS)
        or raw.get("all_files_require_readback") is not True
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    assert isinstance(files, Mapping)
    sealed_files = sealed_request["files"]
    for name, (target, binding) in (
        host_package.HOST_ARTIFACT_TARGETS.items()
    ):
        item = files.get(name)
        expected_package_sha256: str | None = None
        if binding == "release_sealed_payload":
            expected_package_sha256 = sealed_files[name]["sha256"]
        elif binding == "release_reviewed_source":
            expected_package_sha256 = source[
                "gateway_connector_drop_in_sha256"
            ]
        expected = {
            "target_path": target,
            "staged_path": str(
                host_package.CUTOVER_STAGED_ROOT
                / "host"
                / Path(target).name
            ),
            "binding_class": binding,
            "package_sha256": expected_package_sha256,
            "actual_sha256_bound_by": (
                "muncho-production-cutover-host-authority.v1"
            ),
            "required_readback": True,
        }
        if item != expected:
            _fail("release_update_inputs_host_manifest_invalid")
    return raw


def validate_host_artifact_manifest(
    value: Any,
    *,
    release_revision: str,
    successor_unit_input_publication: Mapping[str, Any],
    runtime_dependency_manifest_sha256: str,
) -> Mapping[str, Any]:
    """Validate the release-sealed manifest envelope without host reads."""

    revision = _revision(
        release_revision,
        "release_update_inputs_host_manifest_invalid",
    )
    raw = _self_hashed(
        value,
        fields=_HOST_MANIFEST_FIELDS,
        digest_field="manifest_sha256",
        code="release_update_inputs_host_manifest_invalid",
    )
    source = raw.get("source")
    if (
        raw.get("schema") != host_package.MANIFEST_SCHEMA
        or raw.get("release_revision") != revision
        or raw.get("secret_material_recorded") is not False
        or not isinstance(source, Mapping)
        or set(source) != _HOST_SOURCE_FIELDS
        or any(
            _SHA256.fullmatch(str(source.get(name, ""))) is None
            for name in _HOST_SOURCE_FIELDS
        )
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    try:
        unit_inputs = host_package._unit_inputs(  # noqa: SLF001
            raw.get("unit_inputs"),
            revision=revision,
        )
    except (host_package.PackagingError, KeyError, TypeError, ValueError) as exc:
        _fail("release_update_inputs_host_manifest_invalid", exc)
    try:
        successor_plan = successor_unit_input_publication["plan"]
        successor_approval = successor_unit_input_publication["approval"]
        projected = unit_inputs_v4.project_payload_to_cutover_v4(
            successor_plan["unit_inputs"]
        )
        expected_unit_inputs = {
            "schema": host_package.UNIT_INPUT_SCHEMA_V4,
            "release_revision": revision,
            "authority_plan_sha256": successor_plan["plan_sha256"],
            "authority_approval_sha256": successor_approval[
                "approval_sha256"
            ],
            **{
                name: item
                for name, item in projected.items()
                if name != "schema"
            },
        }
    except (
        KeyError,
        TypeError,
        unit_inputs_v4.ProductionReleaseUnitInputsV4Error,
    ) as exc:
        _fail("release_update_inputs_host_manifest_invalid", exc)
    if (
        raw["unit_inputs"] != unit_inputs
        or unit_inputs != expected_unit_inputs
        or source["runtime_dependency_manifest_sha256"]
        != _sha256(
            runtime_dependency_manifest_sha256,
            "release_update_inputs_host_manifest_invalid",
        )
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    sealed = _validate_sealed_request(
        raw.get("sealed_runtime_artifact_request"),
        release_revision=revision,
        unit_inputs=unit_inputs,
    )
    if (
        sealed["owner_gate_receipt_public_key_id"]
        != successor_plan["unit_inputs"][
            "owner_gate_receipt_public_key_id"
        ]
        or source["sealed_runtime_artifact_request_sha256"]
        != sealed["request_sha256"]
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    _validate_host_contract(
        raw.get("host_artifact_contract"),
        sealed_request=sealed,
        source=source,
    )
    artifacts = raw.get("artifacts")
    release_root = update_contract.expected_release_root(revision)
    if (
        not isinstance(artifacts, Mapping)
        or set(artifacts) != set(host_package.ARTIFACTS)
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    assert isinstance(artifacts, Mapping)
    for name, actions in host_package.ARTIFACTS.items():
        item = artifacts.get(name)
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "actions", "sha256", "size"}
            or item.get("path")
            != (
                f"{release_root}/ops/muncho/cutover/artifacts/{name}"
            )
            or item.get("actions") != list(actions)
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or type(item.get("size")) is not int
            or item["size"] <= 0
        ):
            _fail("release_update_inputs_host_manifest_invalid")
    bindings = raw.get("plan_bindings")
    if (
        not isinstance(bindings, Mapping)
        or set(bindings)
        != set(host_package.PLAN_BINDINGS)
        | {host_package.ALIAS_PROJECTION_BINDING}
    ):
        _fail("release_update_inputs_host_manifest_invalid")
    assert isinstance(bindings, Mapping)
    for binding, artifact_name in host_package.PLAN_BINDINGS.items():
        artifact = artifacts[artifact_name]
        if bindings.get(binding) != {
            "path": artifact["path"],
            "sha256": artifact["sha256"],
        }:
            _fail("release_update_inputs_host_manifest_invalid")
    alias_binding = bindings.get(host_package.ALIAS_PROJECTION_BINDING)
    if alias_binding != {
        "path": (
            f"{release_root}/"
            f"{host_package.ALIAS_PROJECTION_PACKAGE_RELATIVE_ROOT}/"
            "manifest.json"
        ),
        "sha256": raw["source"]["alias_projection_package_sha256"],
    }:
        _fail("release_update_inputs_host_manifest_invalid")
    return raw


def validate_host_mutation_authority(
    value: Any,
    *,
    initial_collector_receipt: Mapping[str, Any],
    release_revision: str,
    host_artifact_manifest: Mapping[str, Any],
    host_inventory: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    """Validate the signed-plan-bound root host readback authority.

    The owner signature on the Stage-C plan supplies durable authority.  This
    receipt supplies exact staged bytes and target prestates for package
    entries whose digest is intentionally dynamic.  It interprets no task or
    truth-mode semantics.
    """

    code = "release_update_inputs_host_mutation_authority_invalid"
    revision = _revision(release_revision, code)
    try:
        initial = cutover_owner.validate_initial_collector_receipt(
            initial_collector_receipt,
            release_revision=revision,
            now_unix=now_unix,
        )
        if not isinstance(value, Mapping):
            _fail(code)
        request = host_authority.build_host_authority_request(
            initial_collector_receipt=initial,
            release_manifest_sha256=str(
                value.get("release_manifest_sha256", "")
            ),
            gateway_target_identity=value.get("gateway_target_identity", {}),
            writer_target_identity=value.get("writer_target_identity", {}),
            connector_target_identity=value.get(
                "connector_target_identity", {}
            ),
            host_transition=value.get("host_transition", {}),
            capability_topology=value.get("capability_topology", {}),
            cron_continuity_plan=value.get("cron_continuity_plan", {}),
        )
        raw = host_authority.validate_host_authority_receipt(
            value,
            host_authority_request=request,
            initial_collector_receipt=initial,
            release_revision=revision,
            now_unix=now_unix,
        )
    except (
        cutover_owner.OwnerCutoverError,
        host_authority.HostAuthorityError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        _fail(code, exc)
    raw = _plain(raw)
    manifest = _plain(host_artifact_manifest)
    inventory_receipt = _plain(host_inventory)
    contract = manifest.get("host_artifact_contract")
    transition = raw.get("host_transition")
    transition_files = (
        transition.get("files") if isinstance(transition, Mapping) else None
    )
    readback = raw.get("readback_files")
    inventory_processes = inventory_receipt.get("processes")
    inventory_observed_at = (
        inventory_receipt.get("observed_at_unix_ns", 0) // 1_000_000_000
        if type(inventory_receipt.get("observed_at_unix_ns")) is int
        else None
    )
    boot_id = (
        inventory_processes.get("boot_id")
        if isinstance(inventory_processes, Mapping)
        else None
    )
    if (
        not isinstance(contract, Mapping)
        or raw.get("release_manifest_sha256")
        != manifest.get("manifest_sha256")
        or raw.get("host_artifact_contract_sha256")
        != contract.get("contract_sha256")
        or not isinstance(transition_files, Mapping)
        or set(transition_files) != set(host_package.HOST_ARTIFACT_TARGETS)
        or not isinstance(readback, list)
        or raw.get("readback_file_count") != len(readback)
        or len(readback) != len(host_package.HOST_ARTIFACT_TARGETS)
        or type(inventory_receipt.get("observed_at_unix_ns")) is not int
        or not isinstance(inventory_observed_at, int)
        or not now_unix - host_authority.MAX_AGE_SECONDS
        <= inventory_observed_at
        <= now_unix + 30
        or abs(
            raw["observed_at_unix"]
            - inventory_observed_at
        )
        > host_authority.MAX_AGE_SECONDS
        or not isinstance(boot_id, str)
        or raw.get("source_boot_id_sha256")
        != sha256_bytes(boot_id.encode("ascii", errors="strict"))
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail(code)
    contract_files = contract.get("files")
    if not isinstance(contract_files, Mapping):
        _fail(code)
    expected_names = sorted(host_package.HOST_ARTIFACT_TARGETS)
    for expected_name, observed in zip(
        expected_names,
        readback,
        strict=True,
    ):
        transition_item = transition_files.get(expected_name)
        contract_item = contract_files.get(expected_name)
        if (
            not isinstance(transition_item, Mapping)
            or set(transition_item)
            != {
                "staged_path",
                "target_path",
                "sha256",
                "uid",
                "gid",
                "mode",
                "pre",
            }
            or not isinstance(contract_item, Mapping)
            or transition_item.get("staged_path")
            != contract_item.get("staged_path")
            or transition_item.get("target_path")
            != contract_item.get("target_path")
            or (
                contract_item.get("package_sha256") is not None
                and transition_item.get("sha256")
                != contract_item.get("package_sha256")
            )
            or contract_item.get("actual_sha256_bound_by")
            != raw.get("schema")
            or contract_item.get("required_readback") is not True
            or not isinstance(observed, Mapping)
            or set(observed)
            != {
                "name",
                "sha256",
                "size",
                "staged_uid",
                "staged_gid",
                "staged_mode",
                "target_pre",
            }
            or observed.get("name") != expected_name
            or observed.get("sha256") != transition_item.get("sha256")
            or observed.get("staged_uid") != 0
            or observed.get("staged_gid") != 0
            or observed.get("staged_mode") != 0o400
            or observed.get("target_pre") != transition_item.get("pre")
        ):
            _fail(code)
    return raw


def _expected_cron_rows() -> Mapping[str, tuple[int, bool]]:
    rows: dict[str, tuple[int, bool]] = {
        str(trusted_cron.PLAN_RELATIVE_PATH): (0o640, False),
        str(trusted_cron.REPLACEMENT_BUNDLE_RELATIVE_PATH): (
            0o600,
            True,
        ),
        str(trusted_cron.COLLECTOR_MANIFEST_RELATIVE_PATH): (
            0o640,
            False,
        ),
    }
    for spec in cron_rail.COLLECTOR_SPECS:
        stem = f"muncho-cron-{spec.source_job_id}"
        rows[
            f"cron/trusted-collector/systemd/{stem}.service"
        ] = (0o640, False)
        rows[
            f"cron/trusted-collector/systemd/{stem}.timer"
        ] = (0o640, False)
    if len(rows) != 3 + 2 * len(cron_rail.COLLECTOR_SPECS):
        _fail("release_update_inputs_cron_index_invalid")
    return dict(sorted(rows.items()))


def validate_cron_artifact_index(
    value: Any,
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    """Validate the pure trusted-cron index; installed bytes are checked later."""

    revision = _revision(
        release_revision,
        "release_update_inputs_cron_index_invalid",
    )
    raw = _self_hashed(
        value,
        fields=_CRON_INDEX_FIELDS,
        digest_field="artifact_index_sha256",
        code="release_update_inputs_cron_index_invalid",
    )
    for name in (
        "plan_sha256",
        "replacement_bundle_sha256",
        "collector_manifest_sha256",
        "cutover_runtime_sha256",
        "cutover_entrypoint_sha256",
    ):
        _sha256(raw.get(name), "release_update_inputs_cron_index_invalid")
    files = raw.get("files")
    expected = _expected_cron_rows()
    if (
        raw.get("schema") != trusted_cron.ARTIFACT_INDEX_SCHEMA
        or raw.get("release_revision") != revision
        or raw.get("plan_relative_path")
        != str(trusted_cron.PLAN_RELATIVE_PATH)
        or raw.get("replacement_bundle_relative_path")
        != str(trusted_cron.REPLACEMENT_BUNDLE_RELATIVE_PATH)
        or raw.get("collector_manifest_relative_path")
        != str(trusted_cron.COLLECTOR_MANIFEST_RELATIVE_PATH)
        or not isinstance(files, list)
        or raw.get("file_count") != len(files)
        or raw.get("file_count") != len(expected)
        or any(
            raw.get(name) is not False
            for name in (
                "units_installed",
                "timers_enabled",
                "timers_started",
                "jobs_store_mutated",
                "secret_material_recorded",
            )
        )
    ):
        _fail("release_update_inputs_cron_index_invalid")
    assert isinstance(files, list)
    if [
        item.get("relative_path") if isinstance(item, Mapping) else None
        for item in files
    ] != list(expected):
        _fail("release_update_inputs_cron_index_invalid")
    for item in files:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"relative_path", "sha256", "mode", "private"}
            or item["relative_path"] not in expected
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or (item.get("mode"), item.get("private"))
            != expected[item["relative_path"]]
        ):
            _fail("release_update_inputs_cron_index_invalid")
    return raw


def _validate_alias_structure(value: Mapping[str, Any]) -> None:
    revision = value["release_revision"]
    release_root = value["release_root"]
    interpreter = value.get("interpreter")
    if (
        not isinstance(interpreter, Mapping)
        or set(interpreter) != {"path", "sha256"}
        or interpreter.get("path") != f"{release_root}/.venv/bin/python"
        or _SHA256.fullmatch(str(interpreter.get("sha256", ""))) is None
    ):
        _fail("release_update_inputs_alias_index_invalid")
    module_paths = {
        "writer_bootstrap": alias_units.WRITER_MODULE_RELATIVE,
        "alias_projector": alias_units.PROJECTOR_MODULE_RELATIVE,
        "projection_reader": alias_units.PROJECTION_READER_RELATIVE,
        "team_registry": alias_units.TEAM_REGISTRY_RELATIVE,
        "cutover_runtime": alias_units.CUTOVER_RUNTIME_RELATIVE,
        "cutover_entrypoint": alias_units.CUTOVER_ENTRYPOINT_RELATIVE,
    }
    modules = value.get("modules")
    if not isinstance(modules, Mapping) or set(modules) != set(module_paths):
        _fail("release_update_inputs_alias_index_invalid")
    for name, relative in module_paths.items():
        if modules[name] != {
            "path": str(Path(release_root) / relative),
            "sha256": modules[name].get("sha256"),
        } or _SHA256.fullmatch(
            str(modules[name].get("sha256", ""))
        ) is None:
            _fail("release_update_inputs_alias_index_invalid")
    units = value.get("units")
    expected_units = {
        alias_units.EXPORTER_UNIT,
        alias_units.PROJECTOR_UNIT,
        alias_units.PROJECTOR_TIMER,
    }
    if not isinstance(units, Mapping) or set(units) != expected_units:
        _fail("release_update_inputs_alias_index_invalid")
    for name in sorted(expected_units):
        item = units[name]
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {"path", "artifact_path", "sha256", "uid", "gid", "mode"}
            or item.get("path") != str(alias_units.SYSTEMD_ROOT / name)
            or item.get("artifact_path")
            != str(Path(release_root) / alias_units.PACKAGE_RELATIVE_ROOT / name)
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or (item.get("uid"), item.get("gid"), item.get("mode"))
            != (0, 0, "0644")
        ):
            _fail("release_update_inputs_alias_index_invalid")
    identities = value.get("identities")
    role_names = {
        "writer": "muncho-canonical-writer",
        "projector": "muncho-projector",
        "gateway": "ai-platform-brain",
    }
    if not isinstance(identities, Mapping) or set(identities) != set(role_names):
        _fail("release_update_inputs_alias_index_invalid")
    for role, expected_name in role_names.items():
        item = identities[role]
        if (
            not isinstance(item, Mapping)
            or set(item) != {"user", "group", "uid", "gid"}
            or item.get("user") != expected_name
            or item.get("group") != expected_name
            or type(item.get("uid")) is not int
            or type(item.get("gid")) is not int
            or min(item["uid"], item["gid"]) <= 0
        ):
            _fail("release_update_inputs_alias_index_invalid")
    writer = identities["writer"]
    projector = identities["projector"]
    gateway = identities["gateway"]
    expected_directories = {
        str(alias_units.PRIVATE_EXPORT_DIRECTORY): {
            "uid": writer["uid"],
            "gid": projector["gid"],
            "mode": "0750",
        },
        str(alias_units.PROJECTOR_ROOT): {
            "uid": 0,
            "gid": 0,
            "mode": "0751",
        },
        str(alias_units.PUBLIC_PROJECTION_DIRECTORY): {
            "uid": projector["uid"],
            "gid": gateway["gid"],
            "mode": "2750",
        },
    }
    expected_files = {
        "writer_export": {
            "path": str(alias_units.PRODUCTION_WRITER_EXPORT_PATH),
            "uid": writer["uid"],
            "gid": projector["gid"],
            "mode": "0640",
            "created_by": alias_units.EXPORTER_UNIT,
        },
        "public_projection": {
            "path": str(alias_units.PRODUCTION_PUBLIC_PROJECTION_PATH),
            "uid": projector["uid"],
            "gid": gateway["gid"],
            "mode": "0640",
            "created_by": alias_units.PROJECTOR_UNIT,
        },
        "public_run_receipt": {
            "path": str(alias_units.PRODUCTION_RUN_RECEIPT_PATH),
            "uid": projector["uid"],
            "gid": gateway["gid"],
            "mode": "0640",
            "created_by": alias_units.PROJECTOR_UNIT,
        },
    }
    expected_ordering = {
        "timer_triggers": alias_units.PROJECTOR_UNIT,
        "projector_requires": alias_units.EXPORTER_UNIT,
        "exporter_before_projector": True,
        "timer_enabled_before_activation": False,
        "interval_seconds": alias_units.EXPORT_INTERVAL_SECONDS,
    }
    expected_boundary = {
        "writer_credential_path": str(alias_units.WRITER_CREDENTIAL_PATH),
        "projector_credential_paths": [],
        "gateway_credential_paths": [],
        "projector_network_private": True,
    }
    if (
        value.get("directories") != expected_directories
        or value.get("files") != expected_files
        or value.get("ordering") != expected_ordering
        or value.get("credential_boundary") != expected_boundary
    ):
        _fail("release_update_inputs_alias_index_invalid")
    _revision(revision, "release_update_inputs_alias_index_invalid")


def validate_alias_artifact_index(
    value: Any,
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    try:
        validated = alias_units.validate_package_manifest(
            value,
            expected_revision=release_revision,
        )
    except alias_units.ProductionAliasProjectionUnitError as exc:
        _fail("release_update_inputs_alias_index_invalid", exc)
    normalized = _plain(validated)
    _validate_alias_structure(normalized)
    return normalized


@dataclass(frozen=True)
class ValidatedStage0Inputs:
    """Normalized remaining documents, internal identities, and v4 inputs."""

    documents: Mapping[str, Mapping[str, Any]]
    identities: Mapping[str, str]
    fixed_v4_inputs: Mapping[str, Any]


def validate_stage0_inputs(
    documents: Mapping[str, Mapping[str, Any]],
    update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_trust_sha256: str,
    now_unix: int,
) -> ValidatedStage0Inputs:
    """Validate and cross-bind every non-builder Stage-0 JSON input."""

    if (
        not isinstance(documents, Mapping)
        or set(documents) != _DOCUMENT_FIELDS
        or any(not isinstance(value, Mapping) for value in documents.values())
    ):
        _fail("release_update_inputs_document_set_invalid")
    try:
        publication = update_contract.validate_publication(
            update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=expected_trust_sha256,
            now_unix=now_unix,
        )
    except update_contract.ProductionReleaseUpdateContractError as exc:
        _fail("release_update_inputs_publication_invalid", exc)
    plan = publication["plan"]
    predecessor = str(plan["predecessor_revision"])
    release = str(plan["release_revision"])

    try:
        host_receipt = host_observer.validate_host_observation_receipt(
            documents["host_inventory_sha256"]
        )
    except host_observer.ProductionReleaseHostObserverError as exc:
        _fail("release_update_inputs_host_inventory_invalid", exc)
    host_receipt = _plain(host_receipt)
    if (
        host_receipt.get("phase")
        != inventory.InventoryPhase.PREDECESSOR_ACTIVE.value
        or host_receipt.get("predecessor_revision") != predecessor
        or host_receipt.get("target_revision") != release
    ):
        _fail("release_update_inputs_host_inventory_invalid")
    consumer_set = validate_release_consumer_set(
        documents["release_consumer_set_sha256"],
        predecessor_revision=predecessor,
        release_revision=release,
    )
    try:
        successor = unit_inputs_v4.validate_publication(
            documents["successor_unit_input_publication_sha256"],
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=expected_trust_sha256,
            now_unix=now_unix,
        )
    except unit_inputs_v4.ProductionReleaseUnitInputsV4Error as exc:
        _fail("release_update_inputs_successor_unit_inputs_invalid", exc)
    host_manifest = validate_host_artifact_manifest(
        documents["host_artifact_manifest_sha256"],
        release_revision=release,
        successor_unit_input_publication=successor,
        runtime_dependency_manifest_sha256=plan[
            "runtime_dependency_manifest_sha256"
        ],
    )
    host_mutation_authority = validate_host_mutation_authority(
        documents["host_mutation_authority_sha256"],
        initial_collector_receipt=documents[
            "host_mutation_initial_collector_receipt_sha256"
        ],
        release_revision=release,
        host_artifact_manifest=host_manifest,
        host_inventory=host_receipt,
        now_unix=now_unix,
    )
    cron_index = validate_cron_artifact_index(
        documents["cron_artifact_index_sha256"],
        release_revision=release,
    )
    alias_index = validate_alias_artifact_index(
        documents["alias_artifact_index_sha256"],
        release_revision=release,
    )
    artifact_identities = {
        "host_inventory_sha256": host_receipt["receipt_sha256"],
        "release_consumer_set_sha256": consumer_set[
            "consumer_set_sha256"
        ],
        "host_artifact_manifest_sha256": host_manifest[
            "manifest_sha256"
        ],
        "host_mutation_authority_sha256": host_mutation_authority[
            "receipt_sha256"
        ],
        "host_mutation_initial_collector_receipt_sha256": documents[
            "host_mutation_initial_collector_receipt_sha256"
        ]["receipt_sha256"],
        "cron_artifact_index_sha256": cron_index[
            "artifact_index_sha256"
        ],
        "alias_artifact_index_sha256": alias_index["package_sha256"],
        "successor_unit_input_publication_sha256": successor[
            "publication_sha256"
        ],
    }
    activation = validate_activation_plan(
        documents["activation_plan_sha256"],
        predecessor_revision=predecessor,
        release_revision=release,
        artifact_identities=artifact_identities,
    )
    rollback = validate_rollback_plan(
        documents["rollback_plan_sha256"],
        predecessor_revision=predecessor,
        release_revision=release,
        artifact_identities=artifact_identities,
    )
    identities = {
        **artifact_identities,
        "activation_plan_sha256": activation[
            "activation_plan_sha256"
        ],
        "rollback_plan_sha256": rollback["rollback_plan_sha256"],
    }
    if any(plan.get(name) != digest for name, digest in identities.items()):
        _fail("release_update_inputs_plan_binding_invalid")
    try:
        fixed = unit_inputs_v4.derive_fixed_inputs(
            unit_input_publication=successor,
            release_update_publication=publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=expected_trust_sha256,
            now_unix=now_unix,
        )
    except unit_inputs_v4.ProductionReleaseUnitInputsV4Error as exc:
        _fail("release_update_inputs_successor_unit_inputs_invalid", exc)
    normalized_documents = {
        "host_inventory_sha256": host_receipt,
        "release_consumer_set_sha256": consumer_set,
        "host_artifact_manifest_sha256": host_manifest,
        "host_mutation_authority_sha256": host_mutation_authority,
        "host_mutation_initial_collector_receipt_sha256": _plain(
            documents["host_mutation_initial_collector_receipt_sha256"]
        ),
        "cron_artifact_index_sha256": cron_index,
        "alias_artifact_index_sha256": alias_index,
        "successor_unit_input_publication_sha256": _plain(successor),
        "activation_plan_sha256": activation,
        "rollback_plan_sha256": rollback,
    }
    return ValidatedStage0Inputs(
        documents=normalized_documents,
        identities=identities,
        fixed_v4_inputs=_plain(fixed),
    )


__all__ = [
    "ACTIVATION_PLAN_SCHEMA",
    "ProductionReleaseUpdateInputsError",
    "RELEASE_CONSUMER_SET_SCHEMA",
    "ROLLBACK_PLAN_SCHEMA",
    "ValidatedStage0Inputs",
    "build_activation_plan",
    "build_release_consumer_set",
    "build_rollback_plan",
    "canonical_bytes",
    "sha256_bytes",
    "validate_activation_plan",
    "validate_alias_artifact_index",
    "validate_cron_artifact_index",
    "validate_host_artifact_manifest",
    "validate_host_mutation_authority",
    "validate_release_consumer_set",
    "validate_rollback_plan",
    "validate_stage0_inputs",
]
