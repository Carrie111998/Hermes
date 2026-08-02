#!/usr/bin/env python3
"""Closed production boot-disk growth contract for ai-platform-runtime-01.

This is intentionally independent from the Muncho canary 40 -> 80 GiB
contract.  It binds one existing production instance and its existing boot
disk to one 50 -> 100 GiB operation.  It contains no cloud client and grants
no stop, start, reboot, snapshot, delete, replacement, shrink, or generic
shell authority.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

from scripts.canary import passkey_v2_protocol as protocol


OBSERVATION_SCHEMA = "muncho-production-storage-observation.v1"
PLAN_SCHEMA = "muncho-production-storage-growth-plan.v1"
RUNTIME_ARTIFACT_ATTESTATION_SCHEMA = (
    "muncho-production-storage-runtime-artifact-attestation.v1"
)
OPERATION = "grow_exact_production_boot_disk_50_to_100.v1"

PROJECT = "adventico-ai-platform"
ZONE = "europe-west3-a"
INSTANCE_NAME = "ai-platform-runtime-01"
INSTANCE_ID = "1094477181810932795"
DISK_NAME = "ai-platform-runtime-01"
DISK_ID = "8330339521755118650"
DISK_TYPE = "pd-balanced"
AUTHENTICATED_ACCOUNT = "lomliev@adventico.com"
SOURCE_IMAGE_PROJECT = "debian-cloud"
SOURCE_IMAGE_NAME = "debian-12-bookworm-v20260609"
SOURCE_SIZE_GB = 50
TARGET_SIZE_GB = 100
MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES = 104_000_000_000
MINIMUM_POSTFLIGHT_AVAILABLE_BYTES = 5 * 1024**3
PREFLIGHT_MAX_AGE_SECONDS = 300

INSTANCE_SELF_LINK = (
    "https://www.googleapis.com/compute/v1/projects/"
    f"{PROJECT}/zones/{ZONE}/instances/{INSTANCE_NAME}"
)
DISK_SELF_LINK = (
    "https://www.googleapis.com/compute/v1/projects/"
    f"{PROJECT}/zones/{ZONE}/disks/{DISK_NAME}"
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_OBSERVATION_FIELDS = frozenset({
    "schema",
    "collected_at_unix",
    "authenticated_account",
    "impersonated_service_account",
    "project",
    "zone",
    "instance",
    "disk",
    "boot_attachment",
    "guest",
    "observation_sha256",
})
_INSTANCE_FIELDS = frozenset({
    "name",
    "id",
    "status",
    "zone",
    "self_link",
    "boot_disk_count",
})
_DISK_FIELDS = frozenset({
    "name",
    "id",
    "type",
    "size_gb",
    "zone",
    "self_link",
    "users",
    "status",
    "source_image_project",
    "source_image_name",
})
_ATTACHMENT_FIELDS = frozenset({
    "boot",
    "auto_delete",
    "device_name",
    "mode",
    "type",
    "source",
})
_GUEST_FIELDS = frozenset({
    "boot_id",
    "root_source",
    "root_parent",
    "root_partition_number",
    "root_filesystem",
    "mountpoint",
    "disk_size_bytes",
    "partition_size_bytes",
    "filesystem_size_bytes",
    "available_bytes",
})
_PLAN_FIELDS = frozenset({
    "schema",
    "operation",
    "release_revision",
    "source_preflight",
    "source_preflight_sha256",
    "preflight_max_age_seconds",
    "project",
    "zone",
    "instance_name",
    "instance_id",
    "instance_self_link",
    "disk_name",
    "disk_id",
    "disk_self_link",
    "disk_type",
    "boot_device_name",
    "boot_id",
    "authenticated_account",
    "source_size_gb",
    "target_size_gb",
    "minimum_postflight_filesystem_bytes",
    "minimum_postflight_available_bytes",
    "provider_request_id",
    "idempotency_key_sha256",
    "executor_binary_sha256",
    "mutation_wrapper_sha256",
    "read_only_collector_sha256",
    "remote_transport_sha256",
    "owner_cli_sha256",
    "owner_route_sha256",
    "production_cutover_transport_sha256",
    "installer_sha256",
    "runtime_artifact_attestation",
    "runtime_artifact_attestation_sha256",
    "maximum_provider_resize_operations",
    "online_partition_filesystem_growth_only",
    "stop_allowed",
    "start_allowed",
    "reboot_allowed",
    "snapshot_allowed",
    "delete_allowed",
    "replacement_allowed",
    "shrink_allowed",
    "rollback_by_shrink_allowed",
    "forward_recovery_required",
    "caller_selected_commands_allowed",
    "caller_selected_paths_allowed",
    "caller_selected_targets_allowed",
    "generic_shell_fallback_allowed",
    "plan_sha256",
})
RUNTIME_ARTIFACT_RELATIVES = {
    "plan_builder": "scripts/canary/production_storage_growth_owner_cli.py",
    "owner_cli": "scripts/canary/production_storage_growth_owner_cli.py",
    "owner_route": "scripts/canary/full_canary_owner_launcher.py",
    "executor": "scripts/canary/production_storage_growth_executor.py",
    "adapter": "scripts/canary/production_storage_growth_adapter.py",
    "production_cutover": (
        "scripts/canary/production_cutover_owner_launcher.py"
    ),
    "guest": "scripts/canary/production_storage_growth_guest.py",
    "installer": "scripts/canary/production_storage_growth_installer.py",
}
_RUNTIME_ARTIFACT_ENTRY_FIELDS = frozenset({
    "release_relative",
    "sha256",
    "size",
})
_RUNTIME_ARTIFACT_ATTESTATION_FIELDS = frozenset({
    "schema",
    "release_revision",
    "owner_support_manifest_sha256",
    "owner_support_source_tree_oid",
    "artifacts",
    "attestation_sha256",
})


class ProductionStorageGrowthError(RuntimeError):
    """Stable, secret-free contract failure."""


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _positive_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _source_execution_facts(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    """Material source facts; freshness and free bytes are re-observed."""

    guest = dict(observation["guest"])
    guest.pop("available_bytes")
    return {
        "authenticated_account": observation["authenticated_account"],
        "impersonated_service_account": observation["impersonated_service_account"],
        "project": observation["project"],
        "zone": observation["zone"],
        "instance": observation["instance"],
        "disk": observation["disk"],
        "boot_attachment": observation["boot_attachment"],
        "guest_without_available_bytes": guest,
    }


def validate_observation(
    value: Any,
    *,
    now_unix: int,
    require_fresh: bool = True,
) -> Mapping[str, Any]:
    """Validate exact cloud + guest identity and return a canonical copy."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _OBSERVATION_FIELDS
        or type(now_unix) is not int
        or now_unix <= 0
    ):
        raise ProductionStorageGrowthError("production_storage_observation_invalid")
    observation = copy.deepcopy(dict(value))
    instance = observation.get("instance")
    disk = observation.get("disk")
    attachment = observation.get("boot_attachment")
    guest = observation.get("guest")
    if (
        not isinstance(instance, Mapping)
        or set(instance) != _INSTANCE_FIELDS
        or not isinstance(disk, Mapping)
        or set(disk) != _DISK_FIELDS
        or not isinstance(attachment, Mapping)
        or set(attachment) != _ATTACHMENT_FIELDS
        or not isinstance(guest, Mapping)
        or set(guest) != _GUEST_FIELDS
    ):
        raise ProductionStorageGrowthError("production_storage_observation_invalid")
    collected = observation.get("collected_at_unix")
    size_gb = disk.get("size_gb")
    if (
        observation.get("schema") != OBSERVATION_SCHEMA
        or type(collected) is not int
        or collected <= 0
        or collected > now_unix
        or require_fresh
        and now_unix - collected > PREFLIGHT_MAX_AGE_SECONDS
        or observation.get("authenticated_account") != AUTHENTICATED_ACCOUNT
        or observation.get("impersonated_service_account") is not None
        or observation.get("project") != PROJECT
        or observation.get("zone") != ZONE
        or instance
        != {
            "name": INSTANCE_NAME,
            "id": INSTANCE_ID,
            "status": "RUNNING",
            "zone": ZONE,
            "self_link": INSTANCE_SELF_LINK,
            "boot_disk_count": 1,
        }
        or disk.get("name") != DISK_NAME
        or disk.get("id") != DISK_ID
        or disk.get("type") != DISK_TYPE
        or size_gb not in {SOURCE_SIZE_GB, TARGET_SIZE_GB}
        or disk.get("zone") != ZONE
        or disk.get("self_link") != DISK_SELF_LINK
        or disk.get("users") != [INSTANCE_SELF_LINK]
        or disk.get("status") != "READY"
        or disk.get("source_image_project") != SOURCE_IMAGE_PROJECT
        or disk.get("source_image_name") != SOURCE_IMAGE_NAME
        or attachment.get("boot") is not True
        or attachment.get("auto_delete") is not True
        or not isinstance(attachment.get("device_name"), str)
        or _DEVICE.fullmatch(attachment["device_name"]) is None
        or attachment.get("mode") != "READ_WRITE"
        or attachment.get("type") != "PERSISTENT"
        or attachment.get("source") != DISK_SELF_LINK
        or not isinstance(guest.get("boot_id"), str)
        or len(guest["boot_id"]) != 36
        or guest.get("root_source") != "/dev/sda1"
        or guest.get("root_parent") != "/dev/sda"
        or guest.get("root_partition_number") != 1
        or guest.get("root_filesystem") != "ext4"
        or guest.get("mountpoint") != "/"
        or any(
            not _positive_int(guest.get(name))
            for name in (
                "disk_size_bytes",
                "partition_size_bytes",
                "filesystem_size_bytes",
                "available_bytes",
            )
        )
        or guest["available_bytes"] > guest["filesystem_size_bytes"]
        or guest["filesystem_size_bytes"] > guest["partition_size_bytes"]
        or guest["partition_size_bytes"] > guest["disk_size_bytes"]
    ):
        raise ProductionStorageGrowthError("production_storage_observation_invalid")
    try:
        uuid.UUID(guest["boot_id"])
    except (ValueError, TypeError, AttributeError):
        raise ProductionStorageGrowthError(
            "production_storage_observation_invalid"
        ) from None
    unsigned = {
        name: item for name, item in observation.items() if name != "observation_sha256"
    }
    if observation.get("observation_sha256") != protocol.sha256_json(unsigned):
        raise ProductionStorageGrowthError("production_storage_observation_invalid")
    return observation


def classify_observation(
    value: Any,
    *,
    now_unix: int,
    plan: Mapping[str, Any],
) -> str:
    """Return source, partial, or target for the one immutable plan."""

    checked_plan = validate_plan(plan)
    observation = validate_observation(value, now_unix=now_unix)
    if (
        observation["boot_attachment"]["device_name"]
        != checked_plan["boot_device_name"]
        or observation["guest"]["boot_id"] != checked_plan["boot_id"]
    ):
        raise ProductionStorageGrowthError("production_storage_identity_drift")
    disk_size = observation["disk"]["size_gb"]
    guest = observation["guest"]
    if disk_size == SOURCE_SIZE_GB:
        if protocol.canonical_json_bytes(
            _source_execution_facts(observation)
        ) != protocol.canonical_json_bytes(
            _source_execution_facts(checked_plan["source_preflight"])
        ):
            raise ProductionStorageGrowthError("production_storage_preflight_drift")
        return "source"
    if (
        guest["filesystem_size_bytes"]
        >= checked_plan["minimum_postflight_filesystem_bytes"]
        and guest["available_bytes"]
        >= checked_plan["minimum_postflight_available_bytes"]
    ):
        return "target"
    return "partial"


def build_observation(**values: Any) -> Mapping[str, Any]:
    """Hash an already collected strict observation (primarily for collectors/tests)."""

    unsigned = {"schema": OBSERVATION_SCHEMA, **values}
    now_unix = unsigned.get("collected_at_unix")
    return validate_observation(
        {**unsigned, "observation_sha256": protocol.sha256_json(unsigned)},
        now_unix=now_unix,
    )


def validate_runtime_artifact_attestation(value: Any) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RUNTIME_ARTIFACT_ATTESTATION_FIELDS
        or value.get("schema") != RUNTIME_ARTIFACT_ATTESTATION_SCHEMA
        or not isinstance(value.get("release_revision"), str)
        or _SHA40.fullmatch(value["release_revision"]) is None
        or not _is_sha256(value.get("owner_support_manifest_sha256"))
        or not isinstance(value.get("owner_support_source_tree_oid"), str)
        or _SHA40.fullmatch(value["owner_support_source_tree_oid"]) is None
        or not isinstance(value.get("artifacts"), Mapping)
        or set(value["artifacts"]) != set(RUNTIME_ARTIFACT_RELATIVES)
    ):
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_attestation_invalid"
        )
    artifacts: dict[str, Mapping[str, Any]] = {}
    for name, relative in RUNTIME_ARTIFACT_RELATIVES.items():
        item = value["artifacts"].get(name)
        if (
            not isinstance(item, Mapping)
            or set(item) != _RUNTIME_ARTIFACT_ENTRY_FIELDS
            or item.get("release_relative") != relative
            or not _is_sha256(item.get("sha256"))
            or type(item.get("size")) is not int
            or item["size"] <= 0
            or item["size"] > 8 * 1024 * 1024
        ):
            raise ProductionStorageGrowthError(
                "production_storage_runtime_artifact_attestation_invalid"
            )
        artifacts[name] = dict(item)
    unsigned = {
        name: item for name, item in value.items() if name != "attestation_sha256"
    }
    if (
        value.get("attestation_sha256") != protocol.sha256_json(unsigned)
        or artifacts["plan_builder"] != artifacts["owner_cli"]
    ):
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_attestation_invalid"
        )
    return copy.deepcopy(dict(value))


def observe_runtime_artifact_attestation(
    *,
    source_root: Path,
    release_revision: str,
    owner_support_manifest: Mapping[str, Any],
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> Mapping[str, Any]:
    """Hash the exact sealed owner-side release artifacts from disk."""

    uid = os.getuid() if expected_uid is None else expected_uid
    gid = os.getgid() if expected_gid is None else expected_gid
    if (
        not isinstance(source_root, Path)
        or not source_root.is_absolute()
        or type(uid) is not int
        or uid < 0
        or type(gid) is not int
        or gid < 0
        or not isinstance(owner_support_manifest, Mapping)
        or owner_support_manifest.get("release_sha") != release_revision
        or not _is_sha256(owner_support_manifest.get("manifest_sha256"))
        or not isinstance(owner_support_manifest.get("source_tree_oid"), str)
        or _SHA40.fullmatch(owner_support_manifest["source_tree_oid"]) is None
    ):
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_observation_invalid"
        )
    try:
        resolved_root = Path(os.path.realpath(source_root, strict=True))
    except OSError:
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_observation_invalid"
        ) from None
    if resolved_root != source_root:
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_observation_invalid"
        )
    artifacts: dict[str, Mapping[str, Any]] = {}
    for name, relative in RUNTIME_ARTIFACT_RELATIVES.items():
        path = source_root / relative
        try:
            metadata = path.lstat()
            resolved = Path(os.path.realpath(path, strict=True))
            payload = path.read_bytes()
        except OSError:
            raise ProductionStorageGrowthError(
                "production_storage_runtime_artifact_observation_invalid"
            ) from None
        if (
            resolved != path
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or metadata.st_nlink != 1
            or len(payload) != metadata.st_size
            or not 0 < len(payload) <= 8 * 1024 * 1024
        ):
            raise ProductionStorageGrowthError(
                "production_storage_runtime_artifact_observation_invalid"
            )
        artifacts[name] = {
            "release_relative": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    unsigned = {
        "schema": RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": release_revision,
        "owner_support_manifest_sha256": owner_support_manifest[
            "manifest_sha256"
        ],
        "owner_support_source_tree_oid": owner_support_manifest[
            "source_tree_oid"
        ],
        "artifacts": artifacts,
    }
    return validate_runtime_artifact_attestation({
        **unsigned,
        "attestation_sha256": protocol.sha256_json(unsigned),
    })


def build_plan(
    *,
    source_preflight: Mapping[str, Any],
    release_revision: str,
    runtime_artifact_attestation: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    source = validate_observation(source_preflight, now_unix=now_unix)
    artifacts = validate_runtime_artifact_attestation(
        runtime_artifact_attestation
    )
    if source["disk"]["size_gb"] != SOURCE_SIZE_GB:
        raise ProductionStorageGrowthError("production_storage_source_size_invalid")
    if artifacts["release_revision"] != release_revision:
        raise ProductionStorageGrowthError(
            "production_storage_runtime_artifact_attestation_invalid"
        )
    digest_material = {
        "schema": "muncho-production-storage-growth-idempotency.v1",
        "disk_id": DISK_ID,
        "target_size_gb": TARGET_SIZE_GB,
        "source_preflight_sha256": source["observation_sha256"],
        "release_revision": release_revision,
    }
    idempotency_key = protocol.sha256_json(digest_material)
    provider_request_id = str(uuid.UUID(idempotency_key[:32]))
    unsigned = {
        "schema": PLAN_SCHEMA,
        "operation": OPERATION,
        "release_revision": release_revision,
        "source_preflight": source,
        "source_preflight_sha256": source["observation_sha256"],
        "preflight_max_age_seconds": PREFLIGHT_MAX_AGE_SECONDS,
        "project": PROJECT,
        "zone": ZONE,
        "instance_name": INSTANCE_NAME,
        "instance_id": INSTANCE_ID,
        "instance_self_link": INSTANCE_SELF_LINK,
        "disk_name": DISK_NAME,
        "disk_id": DISK_ID,
        "disk_self_link": DISK_SELF_LINK,
        "disk_type": DISK_TYPE,
        "boot_device_name": source["boot_attachment"]["device_name"],
        "boot_id": source["guest"]["boot_id"],
        "authenticated_account": AUTHENTICATED_ACCOUNT,
        "source_size_gb": SOURCE_SIZE_GB,
        "target_size_gb": TARGET_SIZE_GB,
        "minimum_postflight_filesystem_bytes": (MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES),
        "minimum_postflight_available_bytes": (MINIMUM_POSTFLIGHT_AVAILABLE_BYTES),
        "provider_request_id": provider_request_id,
        "idempotency_key_sha256": idempotency_key,
        "executor_binary_sha256": artifacts["artifacts"]["executor"]["sha256"],
        "mutation_wrapper_sha256": artifacts["artifacts"]["guest"]["sha256"],
        "read_only_collector_sha256": artifacts["artifacts"]["guest"]["sha256"],
        "remote_transport_sha256": artifacts["artifacts"]["adapter"]["sha256"],
        "owner_cli_sha256": artifacts["artifacts"]["owner_cli"]["sha256"],
        "owner_route_sha256": artifacts["artifacts"]["owner_route"]["sha256"],
        "production_cutover_transport_sha256": artifacts["artifacts"]
        ["production_cutover"]["sha256"],
        "installer_sha256": artifacts["artifacts"]["installer"]["sha256"],
        "runtime_artifact_attestation": artifacts,
        "runtime_artifact_attestation_sha256": artifacts[
            "attestation_sha256"
        ],
        "maximum_provider_resize_operations": 1,
        "online_partition_filesystem_growth_only": True,
        "stop_allowed": False,
        "start_allowed": False,
        "reboot_allowed": False,
        "snapshot_allowed": False,
        "delete_allowed": False,
        "replacement_allowed": False,
        "shrink_allowed": False,
        "rollback_by_shrink_allowed": False,
        "forward_recovery_required": True,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
        "generic_shell_fallback_allowed": False,
    }
    return validate_plan({**unsigned, "plan_sha256": protocol.sha256_json(unsigned)})


def validate_plan(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise ProductionStorageGrowthError("production_storage_plan_invalid")
    plan = copy.deepcopy(dict(value))
    source = plan.get("source_preflight")
    try:
        checked_source = validate_observation(
            source,
            now_unix=source.get("collected_at_unix")
            if isinstance(source, Mapping)
            else 0,
            require_fresh=False,
        )
    except ProductionStorageGrowthError:
        raise ProductionStorageGrowthError("production_storage_plan_invalid") from None
    try:
        artifacts = validate_runtime_artifact_attestation(
            plan.get("runtime_artifact_attestation")
        )
    except ProductionStorageGrowthError:
        raise ProductionStorageGrowthError(
            "production_storage_plan_invalid"
        ) from None
    hash_names = (
        "executor_binary_sha256",
        "mutation_wrapper_sha256",
        "read_only_collector_sha256",
        "remote_transport_sha256",
        "owner_cli_sha256",
        "owner_route_sha256",
        "production_cutover_transport_sha256",
        "installer_sha256",
        "runtime_artifact_attestation_sha256",
        "idempotency_key_sha256",
    )
    expected_idempotency_key = protocol.sha256_json({
        "schema": "muncho-production-storage-growth-idempotency.v1",
        "disk_id": DISK_ID,
        "target_size_gb": TARGET_SIZE_GB,
        "source_preflight_sha256": checked_source["observation_sha256"],
        "release_revision": plan.get("release_revision"),
    })
    static = {
        "schema": PLAN_SCHEMA,
        "operation": OPERATION,
        "project": PROJECT,
        "zone": ZONE,
        "instance_name": INSTANCE_NAME,
        "instance_id": INSTANCE_ID,
        "instance_self_link": INSTANCE_SELF_LINK,
        "disk_name": DISK_NAME,
        "disk_id": DISK_ID,
        "disk_self_link": DISK_SELF_LINK,
        "disk_type": DISK_TYPE,
        "authenticated_account": AUTHENTICATED_ACCOUNT,
        "source_size_gb": SOURCE_SIZE_GB,
        "target_size_gb": TARGET_SIZE_GB,
        "minimum_postflight_filesystem_bytes": MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES,
        "minimum_postflight_available_bytes": MINIMUM_POSTFLIGHT_AVAILABLE_BYTES,
        "preflight_max_age_seconds": PREFLIGHT_MAX_AGE_SECONDS,
        "maximum_provider_resize_operations": 1,
        "online_partition_filesystem_growth_only": True,
        "stop_allowed": False,
        "start_allowed": False,
        "reboot_allowed": False,
        "snapshot_allowed": False,
        "delete_allowed": False,
        "replacement_allowed": False,
        "shrink_allowed": False,
        "rollback_by_shrink_allowed": False,
        "forward_recovery_required": True,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
        "generic_shell_fallback_allowed": False,
    }
    unsigned = {name: item for name, item in plan.items() if name != "plan_sha256"}
    if (
        any(plan.get(name) != expected for name, expected in static.items())
        or not isinstance(plan.get("release_revision"), str)
        or _SHA40.fullmatch(plan["release_revision"]) is None
        or artifacts["release_revision"] != plan.get("release_revision")
        or any(not _is_sha256(plan.get(name)) for name in hash_names)
        or plan.get("runtime_artifact_attestation_sha256")
        != artifacts["attestation_sha256"]
        or plan.get("executor_binary_sha256")
        != artifacts["artifacts"]["executor"]["sha256"]
        or plan.get("mutation_wrapper_sha256")
        != artifacts["artifacts"]["guest"]["sha256"]
        or plan.get("read_only_collector_sha256")
        != artifacts["artifacts"]["guest"]["sha256"]
        or plan.get("remote_transport_sha256")
        != artifacts["artifacts"]["adapter"]["sha256"]
        or plan.get("owner_cli_sha256")
        != artifacts["artifacts"]["owner_cli"]["sha256"]
        or plan.get("owner_route_sha256")
        != artifacts["artifacts"]["owner_route"]["sha256"]
        or plan.get("production_cutover_transport_sha256")
        != artifacts["artifacts"]["production_cutover"]["sha256"]
        or plan.get("installer_sha256")
        != artifacts["artifacts"]["installer"]["sha256"]
        or plan.get("source_preflight_sha256") != checked_source["observation_sha256"]
        or plan.get("boot_device_name")
        != checked_source["boot_attachment"]["device_name"]
        or plan.get("boot_id") != checked_source["guest"]["boot_id"]
        or checked_source["disk"]["size_gb"] != SOURCE_SIZE_GB
        or plan.get("idempotency_key_sha256") != expected_idempotency_key
        or not isinstance(plan.get("provider_request_id"), str)
        or plan.get("provider_request_id")
        != str(uuid.UUID(plan["idempotency_key_sha256"][:32]))
        or plan.get("plan_sha256") != protocol.sha256_json(unsigned)
    ):
        raise ProductionStorageGrowthError("production_storage_plan_invalid")
    return plan


__all__ = [
    "AUTHENTICATED_ACCOUNT",
    "DISK_ID",
    "DISK_NAME",
    "DISK_SELF_LINK",
    "DISK_TYPE",
    "INSTANCE_ID",
    "INSTANCE_NAME",
    "INSTANCE_SELF_LINK",
    "MINIMUM_POSTFLIGHT_AVAILABLE_BYTES",
    "MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES",
    "OBSERVATION_SCHEMA",
    "OPERATION",
    "PLAN_SCHEMA",
    "PREFLIGHT_MAX_AGE_SECONDS",
    "PROJECT",
    "RUNTIME_ARTIFACT_ATTESTATION_SCHEMA",
    "RUNTIME_ARTIFACT_RELATIVES",
    "ProductionStorageGrowthError",
    "SOURCE_SIZE_GB",
    "TARGET_SIZE_GB",
    "ZONE",
    "build_observation",
    "build_plan",
    "classify_observation",
    "observe_runtime_artifact_attestation",
    "validate_runtime_artifact_attestation",
    "validate_observation",
    "validate_plan",
]
