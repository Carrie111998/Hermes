#!/usr/bin/env python3
"""Quarantine one receipt-bound stopped writer staging residue atomically.

The stopped-release publisher deliberately refuses to cross any pre-existing
activation path.  A writer preflight, however, can durably stage the collector
pair and the complete planner bundle before a later verification step fails.
This module closes that lifecycle gap without deleting evidence: it binds one
of those two exact namespaces to its append-only collector receipt, writes a
crash-resumable intent, and atomically renames the whole staging directory into
a root-only archive while every canary service remains stopped and disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, NoReturn, Sequence

from gateway.canonical_writer_host_authority import NativeObservationPlan
from gateway.canonical_writer_release_contract import (
    GATEWAY_UNIT_NAME,
    WRITER_UNIT_NAME,
    render_phase_b_readiness_service,
)
from scripts.canary.writer_release import (
    _ACTIVATION_PATHS,
    _SERVICE_PROPERTIES,
    _STOPPED_SERVICE_UNITS,
    _collect_service_states,
    _fsync_directory,
    _publish_bytes_no_replace,
    _read_stable_root_file,
    _validate_root_directory,
    _validate_root_parent_chain,
    host_release_lifecycle_lock as _host_activation_lock,
)


LEGACY_PLAN_SCHEMA = "muncho-stopped-writer-residue-recovery-plan.v1"
LEGACY_RECEIPT_SCHEMA = "muncho-stopped-writer-residue-recovery.v1"
PLAN_SCHEMA = "muncho-stopped-writer-residue-recovery-plan.v2"
RECEIPT_SCHEMA = "muncho-stopped-writer-residue-recovery.v2"
FAILURE_SCHEMA = "muncho-stopped-writer-residue-recovery-failure.v1"

DEFAULT_WRITER_CONFIG_SOURCE_PATH = Path(
    "/etc/muncho/writer-activation/staged/writer.json"
)
DEFAULT_GATEWAY_CONFIG_SOURCE_PATH = Path(
    "/etc/muncho/writer-activation/staged/gateway.yaml"
)
DEFAULT_STAGED_NATIVE_PLAN_PATH = Path(
    "/etc/muncho/writer-activation/staged/native-observation-plan.json"
)
DEFAULT_STAGED_WRITER_UNIT_PATH = Path(
    "/etc/muncho/writer-activation/staged/muncho-canonical-writer.service"
)
DEFAULT_STAGED_PHASE_B_READINESS_UNIT_PATH = Path(
    "/etc/muncho/writer-activation/staged/"
    "muncho-canonical-writer-phase-b-readiness.service"
)
DEFAULT_STAGED_GATEWAY_UNIT_PATH = Path(
    "/etc/muncho/writer-activation/staged/hermes-cloud-gateway.service"
)
CONFIG_COLLECTOR_EVIDENCE_ROOT = Path(
    "/var/lib/muncho-writer-canary-evidence/config-collector"
)
STAGING_ROOT = DEFAULT_WRITER_CONFIG_SOURCE_PATH.parent
RECOVERY_ROOT = STAGING_ROOT.parent / "recovered-staging"

if _ACTIVATION_PATHS[:2] != (
    DEFAULT_WRITER_CONFIG_SOURCE_PATH,
    DEFAULT_GATEWAY_CONFIG_SOURCE_PATH,
):
    raise RuntimeError("stopped writer residue path contract drifted")
if not {
    DEFAULT_STAGED_NATIVE_PLAN_PATH,
    DEFAULT_STAGED_WRITER_UNIT_PATH,
    DEFAULT_STAGED_PHASE_B_READINESS_UNIT_PATH,
    DEFAULT_STAGED_GATEWAY_UNIT_PATH,
}.issubset(_ACTIVATION_PATHS):
    raise RuntimeError("stopped writer planner residue path contract drifted")

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_NAME_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_UNIT_BYTES = 256 * 1024
_MAX_PLAN_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_COLLECTOR_RECEIPT_SCHEMA = "muncho-writer-config-collector-receipt.v1"
_COLLECTOR_RECEIPT_KEYS = frozenset({
    "schema",
    "release_revision",
    "release_artifact_sha256",
    "release_manifest_path",
    "release_manifest_file_sha256",
    "writer_config_path",
    "writer_config_sha256",
    "gateway_config_path",
    "gateway_config_sha256",
    "database",
    "credential_provenance",
    "catalog_attestation_sha256",
    "public_routine_count",
    "helper_routine_count",
    "private_schema_identity_sha256",
    "managed_hba_receipt_sha256",
    "server_certificate_sha256",
    "hba_observed_at_unix",
    "hba_expires_at_unix",
    "discord_edge_enabled",
    "credential_content_or_digest_recorded",
    "collected_at_unix",
    "receipt_sha256",
})
_COLLECTOR_CREDENTIAL_KEYS = frozenset({
    "path",
    "device",
    "inode",
    "owner_uid",
    "group_gid",
    "mode",
    "link_count",
    "modification_time_ns",
    "change_time_ns",
    "content_or_digest_recorded",
})
_COLLECTOR_DATABASE_KEYS = frozenset({
    "host",
    "tls_server_name",
    "port",
    "database",
    "user",
    "ca_path",
    "ca_sha256",
})
_COLLECTOR_CREDENTIAL_PATH = "/etc/muncho/credentials/canonical-writer-db-password"
_COLLECTOR_RELEASE_BASE = Path("/opt/muncho-canary-releases")
_COLLECTOR_DATABASE_CA_PATH = Path("/etc/muncho/trust/cloudsql-server-ca.pem")
_COLLECTOR_TLS_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.europe-west3\.sql\.goog$"
)


def _collector_pair_names() -> frozenset[str]:
    return frozenset({
        DEFAULT_WRITER_CONFIG_SOURCE_PATH.name,
        DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name,
    })


def _planner_bundle_names() -> frozenset[str]:
    return _collector_pair_names() | frozenset({
        DEFAULT_STAGED_NATIVE_PLAN_PATH.name,
        DEFAULT_STAGED_WRITER_UNIT_PATH.name,
        DEFAULT_STAGED_PHASE_B_READINESS_UNIT_PATH.name,
        DEFAULT_STAGED_GATEWAY_UNIT_PATH.name,
    })


@dataclass(frozen=True)
class _CollectorReceipt:
    value: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        return str(self.value["receipt_sha256"])


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("residue recovery value is not canonical JSON") from exc
    return rendered.encode("utf-8", errors="strict")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _require_root_linux() -> None:
    getter = getattr(os, "geteuid", None)
    if not callable(getter) or int(getter()) != 0:
        raise PermissionError("stopped_writer_residue_recovery_requires_uid_0")
    if sys.platform != "linux":
        raise RuntimeError("stopped_writer_residue_recovery_requires_linux")


def _revision(value: Any, label: str = "release revision") -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _intent_path(target_revision: str) -> Path:
    return RECOVERY_ROOT / f"{_revision(target_revision)}.intent.json"


def _receipt_path(target_revision: str) -> Path:
    return RECOVERY_ROOT / f"{_revision(target_revision)}.receipt.json"


def _archive_path(
    target_revision: str,
    source_revision: str,
    collector_receipt_sha256: str,
) -> Path:
    return RECOVERY_ROOT / (
        f"{_revision(target_revision)}-"
        f"{_revision(source_revision, 'collector release revision')}-"
        f"{_digest(collector_receipt_sha256, 'collector receipt digest')}"
    )


def _trusted_config(path: Path) -> bytes:
    return _read_stable_root_file(
        path,
        maximum_bytes=_MAX_CONFIG_BYTES,
        exact_mode=0o400,
    )


def _trusted_publication(path: Path, *, maximum: int) -> bytes:
    return _read_stable_root_file(
        path,
        maximum_bytes=maximum,
        exact_mode=0o400,
    )


def _validate_exact_directory(path: Path, *, mode: int = 0o700) -> None:
    _validate_root_directory(path, exact_mode=mode)
    _validate_root_parent_chain(path.parent)


def _ensure_exact_directory(path: Path, *, mode: int = 0o700) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("residue recovery directory path is invalid")
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    _validate_root_parent_chain(current)
    for item in reversed(missing):
        os.mkdir(item, mode)
        os.chown(item, 0, 0)
        os.chmod(item, mode)
        _fsync_directory(item.parent)
    _validate_exact_directory(path, mode=mode)
    _validate_root_parent_chain(path.parent)


def _validate_staging_directory(path: Path) -> frozenset[str]:
    _validate_exact_directory(path)
    entries = frozenset(os.listdir(path))
    if entries not in {_collector_pair_names(), _planner_bundle_names()}:
        raise RuntimeError("stopped writer residue namespace is not exact")
    return entries


def _trusted_staged_artifact(root: Path, name: str) -> bytes:
    path = root / name
    if name in _collector_pair_names():
        return _trusted_config(path)
    maximum = (
        _MAX_PLAN_BYTES
        if name == DEFAULT_STAGED_NATIVE_PLAN_PATH.name
        else _MAX_UNIT_BYTES
    )
    return _trusted_publication(path, maximum=maximum)


def _staged_artifact_digests(root: Path) -> dict[str, str]:
    entries = _validate_staging_directory(root)
    return {
        name: _sha256_bytes(_trusted_staged_artifact(root, name))
        for name in sorted(entries)
    }


def _recoverable_activation_paths(names: Sequence[str]) -> frozenset[Path]:
    entries = frozenset(names)
    if entries not in {_collector_pair_names(), _planner_bundle_names()}:
        raise ValueError("residue recovery staged artifact names are invalid")
    return frozenset(STAGING_ROOT / name for name in entries)


def _plan_staged_artifacts(plan: Mapping[str, Any]) -> dict[str, str]:
    if plan.get("schema") == LEGACY_PLAN_SCHEMA:
        return {
            DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name: str(
                plan["gateway_config_sha256"]
            ),
            DEFAULT_WRITER_CONFIG_SOURCE_PATH.name: str(
                plan["writer_config_sha256"]
            ),
        }
    artifacts = plan.get("staged_artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("residue recovery staged artifacts are invalid")
    return dict(artifacts)


def _decode_native_plan(raw: bytes) -> NativeObservationPlan:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("staged native observation plan is invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("staged native observation plan is invalid")
    plan = NativeObservationPlan.from_mapping(value)
    if raw != _canonical_bytes(plan.to_mapping()) or plan.sha256 != _sha256_bytes(raw):
        raise ValueError("staged native observation plan is not canonical")
    return plan


def _validate_planner_bundle_bindings(
    *,
    root: Path,
    staged_artifacts: Mapping[str, str],
    collector: _CollectorReceipt,
) -> None:
    names = frozenset(staged_artifacts)
    if names == _collector_pair_names():
        return
    if names != _planner_bundle_names():
        raise ValueError("residue recovery staged artifact names are invalid")

    native_raw = _trusted_staged_artifact(
        root,
        DEFAULT_STAGED_NATIVE_PLAN_PATH.name,
    )
    native = _decode_native_plan(native_raw)
    value = native.value
    source_revision = _revision(
        collector.value["release_revision"],
        "collector release revision",
    )
    if (
        collector.value["writer_config_sha256"]
        != staged_artifacts[DEFAULT_WRITER_CONFIG_SOURCE_PATH.name]
        or collector.value["gateway_config_sha256"]
        != staged_artifacts[DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name]
    ):
        raise ValueError("staged config collector binding drifted")
    expected_root = f"/opt/muncho-canary-releases/{source_revision}"
    expected = {
        "revision": source_revision,
        "artifact_root": expected_root,
        "artifact_sha256": collector.value["release_artifact_sha256"],
        "release_manifest_file_sha256": collector.value[
            "release_manifest_file_sha256"
        ],
        "config_collector_receipt_sha256": collector.sha256,
        "writer_config": {
            "path": "/etc/muncho-canonical-writer/writer.json",
            "sha256": staged_artifacts[DEFAULT_WRITER_CONFIG_SOURCE_PATH.name],
        },
        "gateway_config": {
            "path": "/etc/hermes/config.yaml",
            "sha256": staged_artifacts[DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name],
        },
        "writer_unit": {
            "name": WRITER_UNIT_NAME,
            "path": f"/etc/systemd/system/{WRITER_UNIT_NAME}",
            "sha256": staged_artifacts[DEFAULT_STAGED_WRITER_UNIT_PATH.name],
        },
        "gateway_unit": {
            "name": GATEWAY_UNIT_NAME,
            "path": f"/etc/systemd/system/{GATEWAY_UNIT_NAME}",
            "sha256": staged_artifacts[DEFAULT_STAGED_GATEWAY_UNIT_PATH.name],
        },
        "database": {
            "ip_network": f"{collector.value['database']['host']}/32",
            "tls_server_name": collector.value["database"]["tls_server_name"],
            "ca_path": collector.value["database"]["ca_path"],
            "ca_sha256": collector.value["database"]["ca_sha256"],
        },
    }
    for name, binding in expected.items():
        if value.get(name) != binding:
            raise ValueError(f"staged native observation {name} binding drifted")

    phase_b_raw = _trusted_staged_artifact(
        root,
        DEFAULT_STAGED_PHASE_B_READINESS_UNIT_PATH.name,
    )
    expected_phase_b = render_phase_b_readiness_service(
        revision=source_revision,
        artifact_root=expected_root,
        artifact_sha256=str(collector.value["release_artifact_sha256"]),
    ).encode("utf-8", errors="strict")
    if phase_b_raw != expected_phase_b:
        raise ValueError("staged Phase-B readiness unit binding drifted")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("collector receipt contains duplicate keys")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"collector receipt contains non-JSON constant:{value}")


def _nonnegative_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} is invalid")
    return value


def _load_collector_receipt(
    *,
    revision: str,
    receipt_sha256: str,
) -> _CollectorReceipt:
    revision = _revision(revision, "collector evidence revision")
    receipt_sha256 = _digest(receipt_sha256, "collector receipt path digest")
    path = CONFIG_COLLECTOR_EVIDENCE_ROOT / revision / f"{receipt_sha256}.json"
    raw = _trusted_publication(path, maximum=_MAX_RECEIPT_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("collector receipt is not strict JSON") from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != _COLLECTOR_RECEIPT_KEYS
        or value.get("schema") != _COLLECTOR_RECEIPT_SCHEMA
        or value.get("release_revision") != revision
        or value.get("writer_config_path") != str(DEFAULT_WRITER_CONFIG_SOURCE_PATH)
        or value.get("gateway_config_path") != str(DEFAULT_GATEWAY_CONFIG_SOURCE_PATH)
        or value.get("discord_edge_enabled") is not False
        or value.get("credential_content_or_digest_recorded") is not False
        or raw != _canonical_bytes(value)
    ):
        raise ValueError("collector receipt identity drifted")
    if value.get("release_manifest_path") != str(
        _COLLECTOR_RELEASE_BASE / revision / "release-manifest.json"
    ):
        raise ValueError("collector release manifest path drifted")
    for name in (
        "release_artifact_sha256",
        "release_manifest_file_sha256",
        "writer_config_sha256",
        "gateway_config_sha256",
        "catalog_attestation_sha256",
        "private_schema_identity_sha256",
        "managed_hba_receipt_sha256",
        "server_certificate_sha256",
        "receipt_sha256",
    ):
        _digest(value.get(name), f"collector {name}")
    credential = value.get("credential_provenance")
    if (
        not isinstance(credential, Mapping)
        or set(credential) != _COLLECTOR_CREDENTIAL_KEYS
        or credential.get("path") != _COLLECTOR_CREDENTIAL_PATH
        or credential.get("owner_uid") != 999
        or credential.get("group_gid") != 994
        or credential.get("mode") != "0400"
        or credential.get("link_count") != 1
        or credential.get("content_or_digest_recorded") is not False
    ):
        raise ValueError("collector credential provenance drifted")
    for name in (
        "device",
        "inode",
        "modification_time_ns",
        "change_time_ns",
    ):
        _nonnegative_integer(credential.get(name), f"collector credential {name}")
    database = value.get("database")
    if (
        not isinstance(database, Mapping)
        or set(database) != _COLLECTOR_DATABASE_KEYS
        or database.get("host") != "10.91.0.3"
        or database.get("port") != 5432
        or database.get("database") != "muncho_canary_brain"
        or database.get("user") != "muncho_canary_writer_login"
        or database.get("ca_path") != str(_COLLECTOR_DATABASE_CA_PATH)
        or not isinstance(database.get("tls_server_name"), str)
        or _COLLECTOR_TLS_RE.fullmatch(database["tls_server_name"]) is None
    ):
        raise ValueError("collector database identity drifted")
    _digest(database.get("ca_sha256"), "collector database CA")
    observed = _nonnegative_integer(
        value.get("hba_observed_at_unix"),
        "collector HBA observation time",
    )
    expires = _nonnegative_integer(
        value.get("hba_expires_at_unix"),
        "collector HBA expiry time",
    )
    collected = _nonnegative_integer(
        value.get("collected_at_unix"),
        "collector collection time",
    )
    if expires - observed != 300 or not observed <= collected <= expires:
        raise ValueError("collector freshness window drifted")
    unsigned = {name: item for name, item in value.items() if name != "receipt_sha256"}
    if _sha256_json(unsigned) != receipt_sha256:
        raise ValueError("collector receipt self-digest drifted")
    return _CollectorReceipt(dict(value))


def _matching_collector_receipt(
    *,
    writer_sha256: str,
    gateway_sha256: str,
) -> tuple[_CollectorReceipt, Path]:
    _validate_exact_directory(CONFIG_COLLECTOR_EVIDENCE_ROOT)
    matches: list[tuple[_CollectorReceipt, Path]] = []
    for revision_name in sorted(os.listdir(CONFIG_COLLECTOR_EVIDENCE_ROOT)):
        revision = _revision(revision_name, "collector evidence revision")
        directory = CONFIG_COLLECTOR_EVIDENCE_ROOT / revision
        _validate_exact_directory(directory)
        for receipt_name in sorted(os.listdir(directory)):
            match = _RECEIPT_NAME_RE.fullmatch(receipt_name)
            if match is None:
                raise RuntimeError(
                    "collector evidence namespace contains an extra entry"
                )
            receipt = _load_collector_receipt(
                revision=revision,
                receipt_sha256=match.group(1),
            )
            value = receipt.value
            if (
                value["writer_config_sha256"] == writer_sha256
                and value["gateway_config_sha256"] == gateway_sha256
                and value["writer_config_path"]
                == str(DEFAULT_WRITER_CONFIG_SOURCE_PATH)
                and value["gateway_config_path"]
                == str(DEFAULT_GATEWAY_CONFIG_SOURCE_PATH)
                and value["credential_content_or_digest_recorded"] is False
                and value["credential_provenance"]["content_or_digest_recorded"]
                is False
            ):
                matches.append((receipt, directory / receipt_name))
    if len(matches) != 1:
        raise RuntimeError("stopped writer residue lacks one exact collector receipt")
    return matches[0]


def _activation_inventory(staged_names: Sequence[str]) -> list[dict[str, str]]:
    recoverable = _recoverable_activation_paths(staged_names)
    inventory: list[dict[str, str]] = []
    for path in _ACTIVATION_PATHS:
        present = os.path.lexists(path)
        if path in recoverable:
            if not present:
                raise RuntimeError("stopped writer residue is partial")
            state = "present_receipt_bound_residue"
        else:
            if present:
                raise RuntimeError("non-recoverable stopped activation path is present")
            state = "absent"
        inventory.append({"path": str(path), "state": state})
    return inventory


def _validate_service_states(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(_STOPPED_SERVICE_UNITS):
        raise ValueError("residue recovery service state is invalid")
    validated: list[dict[str, Any]] = []
    for unit, item in zip(_STOPPED_SERVICE_UNITS, value, strict=True):
        if (
            not isinstance(item, Mapping)
            or set(item) != {"unit", "state", "properties"}
            or item.get("unit") != unit
            or item.get("state") not in {"absent", "disabled_inactive"}
        ):
            raise ValueError("residue recovery service state is invalid")
        properties = item.get("properties")
        if not isinstance(properties, Mapping) or set(properties) != set(
            _SERVICE_PROPERTIES
        ):
            raise ValueError("residue recovery service state is invalid")
        expected = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "",
            "MainPID": "0",
            "FragmentPath": "",
            "DropInPaths": "",
        }
        if item["state"] == "disabled_inactive":
            expected.update({
                "LoadState": "loaded",
                "UnitFileState": "disabled",
                "FragmentPath": f"/etc/systemd/system/{unit}",
            })
        if dict(properties) != expected:
            raise ValueError("residue recovery service state is invalid")
        validated.append({
            "unit": unit,
            "state": item["state"],
            "properties": dict(properties),
        })
    return validated


def _plan_from_live(target_revision: str) -> dict[str, Any]:
    staged_artifacts = _staged_artifact_digests(STAGING_ROOT)
    inventory = _activation_inventory(tuple(staged_artifacts))
    writer_sha256 = staged_artifacts[DEFAULT_WRITER_CONFIG_SOURCE_PATH.name]
    gateway_sha256 = staged_artifacts[DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name]
    collector, collector_path = _matching_collector_receipt(
        writer_sha256=writer_sha256,
        gateway_sha256=gateway_sha256,
    )
    _validate_planner_bundle_bindings(
        root=STAGING_ROOT,
        staged_artifacts=staged_artifacts,
        collector=collector,
    )
    source_revision = _revision(
        collector.value["release_revision"],
        "collector release revision",
    )
    archive = _archive_path(target_revision, source_revision, collector.sha256)
    if os.path.lexists(archive):
        raise RuntimeError("stopped writer residue archive collides")
    unsigned: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "target_release_revision": target_revision,
        "source_release_revision": source_revision,
        "collector_receipt_path": str(collector_path),
        "collector_receipt_sha256": collector.sha256,
        "writer_config_path": str(DEFAULT_WRITER_CONFIG_SOURCE_PATH),
        "writer_config_sha256": writer_sha256,
        "gateway_config_path": str(DEFAULT_GATEWAY_CONFIG_SOURCE_PATH),
        "gateway_config_sha256": gateway_sha256,
        "staged_artifacts": staged_artifacts,
        "source_staging_root": str(STAGING_ROOT),
        "archive_path": str(archive),
        "intent_path": str(_intent_path(target_revision)),
        "receipt_path": str(_receipt_path(target_revision)),
        "activation_inventory": inventory,
        "service_states": _collect_service_states(),
        "invariants": {
            "services_started": False,
            "units_installed": False,
            "daemon_reloaded": False,
            "credential_content_or_digest_recorded": False,
            "staging_directory_renamed_atomically": True,
            "staged_configs_deleted": False,
            "staged_artifacts_deleted": False,
        },
    }
    return {**unsigned, "plan_sha256": _sha256_json(unsigned)}


def validate_plan_mapping(
    value: Any,
    *,
    expected_target_revision: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("residue recovery plan is not an object")
    schema = value.get("schema")
    if schema not in {LEGACY_PLAN_SCHEMA, PLAN_SCHEMA}:
        raise ValueError("residue recovery plan fields are not exact")
    expected_fields = {
        "schema",
        "target_release_revision",
        "source_release_revision",
        "collector_receipt_path",
        "collector_receipt_sha256",
        "writer_config_path",
        "writer_config_sha256",
        "gateway_config_path",
        "gateway_config_sha256",
        "source_staging_root",
        "archive_path",
        "intent_path",
        "receipt_path",
        "activation_inventory",
        "service_states",
        "invariants",
        "plan_sha256",
    }
    if schema == PLAN_SCHEMA:
        expected_fields.add("staged_artifacts")
    if set(value) != expected_fields:
        raise ValueError("residue recovery plan fields are not exact")
    target = _revision(value["target_release_revision"])
    source = _revision(value["source_release_revision"], "collector release revision")
    if expected_target_revision is not None and target != _revision(
        expected_target_revision
    ):
        raise ValueError("residue recovery target revision drifted")
    collector_sha = _digest(
        value["collector_receipt_sha256"], "collector receipt digest"
    )
    writer_sha = _digest(value["writer_config_sha256"], "writer config digest")
    gateway_sha = _digest(value["gateway_config_sha256"], "gateway config digest")
    artifacts = _plan_staged_artifacts(value)
    artifact_names = frozenset(artifacts)
    _recoverable_activation_paths(tuple(artifact_names))
    if any(
        not isinstance(name, str) or _digest(digest, f"staged {name} digest") != digest
        for name, digest in artifacts.items()
    ):
        raise ValueError("residue recovery staged artifacts are invalid")
    if (
        artifacts.get(DEFAULT_WRITER_CONFIG_SOURCE_PATH.name) != writer_sha
        or artifacts.get(DEFAULT_GATEWAY_CONFIG_SOURCE_PATH.name) != gateway_sha
    ):
        raise ValueError("residue recovery config artifact binding drifted")
    if (
        value["writer_config_path"] != str(DEFAULT_WRITER_CONFIG_SOURCE_PATH)
        or value["gateway_config_path"] != str(DEFAULT_GATEWAY_CONFIG_SOURCE_PATH)
        or value["source_staging_root"] != str(STAGING_ROOT)
        or value["archive_path"] != str(_archive_path(target, source, collector_sha))
        or value["intent_path"] != str(_intent_path(target))
        or value["receipt_path"] != str(_receipt_path(target))
        or value["collector_receipt_path"]
        != str(CONFIG_COLLECTOR_EVIDENCE_ROOT / source / f"{collector_sha}.json")
    ):
        raise ValueError("residue recovery fixed path drifted")
    inventory = value["activation_inventory"]
    if not isinstance(inventory, list) or len(inventory) != len(_ACTIVATION_PATHS):
        raise ValueError("residue recovery inventory is invalid")
    recoverable = _recoverable_activation_paths(tuple(artifact_names))
    for path, item in zip(_ACTIVATION_PATHS, inventory, strict=True):
        expected_state = (
            "present_receipt_bound_residue" if path in recoverable else "absent"
        )
        if item != {"path": str(path), "state": expected_state}:
            raise ValueError("residue recovery inventory drifted")
    expected_invariants = {
        "services_started": False,
        "units_installed": False,
        "daemon_reloaded": False,
        "credential_content_or_digest_recorded": False,
        "staging_directory_renamed_atomically": True,
        "staged_configs_deleted": False,
    }
    if schema == PLAN_SCHEMA:
        expected_invariants["staged_artifacts_deleted"] = False
    if value["invariants"] != expected_invariants:
        raise ValueError("residue recovery invariants drifted")
    _validate_service_states(value["service_states"])
    unsigned = {name: item for name, item in value.items() if name != "plan_sha256"}
    if _digest(value["plan_sha256"], "recovery plan digest") != _sha256_json(unsigned):
        raise ValueError("residue recovery plan digest drifted")
    del writer_sha, gateway_sha
    return dict(value)


def _load_persisted_plan(target_revision: str) -> dict[str, Any] | None:
    path = _intent_path(target_revision)
    if not os.path.lexists(path):
        return None
    raw = _trusted_publication(path, maximum=_MAX_PLAN_BYTES)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("residue recovery intent is invalid") from exc
    plan = validate_plan_mapping(
        value,
        expected_target_revision=target_revision,
    )
    if raw != _canonical_bytes(plan):
        raise ValueError("residue recovery intent is not canonical")
    return plan


def _validate_current_state(plan: Mapping[str, Any]) -> str:
    source = Path(str(plan["source_staging_root"]))
    archive = Path(str(plan["archive_path"]))
    source_exists = os.path.lexists(source)
    archive_exists = os.path.lexists(archive)
    if source_exists == archive_exists:
        raise RuntimeError("residue recovery state is ambiguous")
    current_root = source if source_exists else archive
    current_artifacts = _staged_artifact_digests(current_root)
    if current_artifacts != _plan_staged_artifacts(plan):
        raise RuntimeError("residue recovery staged artifact digest drifted")
    recoverable = _recoverable_activation_paths(tuple(current_artifacts))
    for path in _ACTIVATION_PATHS:
        if path in recoverable:
            if source_exists != os.path.lexists(path):
                raise RuntimeError("residue recovery source presence drifted")
        elif os.path.lexists(path):
            raise RuntimeError("non-recoverable stopped activation path is present")
    if _collect_service_states() != plan["service_states"]:
        raise RuntimeError("residue recovery service state drifted")
    return "source" if source_exists else "archive"


def plan_stopped_writer_residue_recovery(target_revision: str) -> dict[str, Any]:
    """Return one read-only, crash-stable recovery plan."""

    target = _revision(target_revision)
    persisted = _load_persisted_plan(target)
    plan = _plan_from_live(target) if persisted is None else persisted
    validate_plan_mapping(plan, expected_target_revision=target)
    _validate_current_state(plan)
    return plan


def _write_intent(plan: Mapping[str, Any]) -> None:
    path = Path(str(plan["intent_path"]))
    raw = _canonical_bytes(plan)
    if os.path.lexists(path):
        existing = _trusted_publication(path, maximum=_MAX_PLAN_BYTES)
        if existing != raw:
            raise RuntimeError("residue recovery intent collision exists")
        return
    _publish_bytes_no_replace(
        path,
        raw,
        maximum_bytes=_MAX_PLAN_BYTES,
        exact_mode=0o400,
    )


def _receipt_unsigned(
    plan: Mapping[str, Any],
    *,
    service_states_after: list[dict[str, Any]],
    created_at_unix: int,
) -> dict[str, Any]:
    if type(created_at_unix) is not int or created_at_unix < 0:
        raise ValueError("residue recovery receipt time is invalid")
    legacy = plan.get("schema") == LEGACY_PLAN_SCHEMA
    unsigned = {
        "schema": LEGACY_RECEIPT_SCHEMA if legacy else RECEIPT_SCHEMA,
        "ok": True,
        "state": "staging_residue_quarantined_services_stopped",
        "target_release_revision": plan["target_release_revision"],
        "source_release_revision": plan["source_release_revision"],
        "plan_sha256": plan["plan_sha256"],
        "collector_receipt_sha256": plan["collector_receipt_sha256"],
        "writer_config_sha256": plan["writer_config_sha256"],
        "gateway_config_sha256": plan["gateway_config_sha256"],
        "source_staging_root": plan["source_staging_root"],
        "archive_path": plan["archive_path"],
        "intent_path": plan["intent_path"],
        "receipt_path": plan["receipt_path"],
        "service_states_before": plan["service_states"],
        "service_states_after": service_states_after,
        "services_stopped_and_disabled": True,
        "source_activation_paths_absent": True,
        "staged_configs_deleted": False,
        "created_at_unix": created_at_unix,
    }
    if not legacy:
        unsigned["staged_artifacts"] = _plan_staged_artifacts(plan)
        unsigned["staged_artifacts_deleted"] = False
    return unsigned


def validate_receipt_mapping(
    value: Any,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    validated_plan = validate_plan_mapping(plan)
    if not isinstance(value, Mapping):
        raise ValueError("residue recovery receipt is not an object")
    created_at = value.get("created_at_unix")
    service_states_after = value.get("service_states_after")
    if (
        type(created_at) is not int
        or created_at < 0
        or service_states_after != validated_plan["service_states"]
    ):
        raise ValueError("residue recovery receipt binding drifted")
    unsigned = _receipt_unsigned(
        validated_plan,
        service_states_after=list(service_states_after),
        created_at_unix=created_at,
    )
    observed_unsigned = {
        name: item for name, item in value.items() if name != "receipt_sha256"
    }
    if observed_unsigned != unsigned:
        raise ValueError("residue recovery receipt binding drifted")
    if _digest(value.get("receipt_sha256"), "recovery receipt digest") != _sha256_json(
        unsigned
    ):
        raise ValueError("residue recovery receipt digest drifted")
    return dict(value)


def _load_terminal_receipt(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    path = Path(str(plan["receipt_path"]))
    if not os.path.lexists(path):
        return None
    raw = _trusted_publication(path, maximum=_MAX_RECEIPT_BYTES)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("residue recovery receipt is invalid") from exc
    receipt = validate_receipt_mapping(value, plan=plan)
    if raw != _canonical_bytes(receipt):
        raise ValueError("residue recovery receipt is not canonical")
    return receipt


def apply_stopped_writer_residue_recovery(
    target_revision: str,
    approved_plan_sha256: str,
    *,
    clock: Callable[[], float] = time.time,
    lifecycle_lock: Callable[[], ContextManager[Any]] = _host_activation_lock,
) -> dict[str, Any]:
    """Persist, atomically quarantine, and attest one exact residue set."""

    target = _revision(target_revision)
    approved = _digest(approved_plan_sha256, "approved recovery plan digest")
    prelock = plan_stopped_writer_residue_recovery(target)
    if prelock["plan_sha256"] != approved:
        raise PermissionError("approved residue recovery plan digest does not match")
    with lifecycle_lock():
        plan = plan_stopped_writer_residue_recovery(target)
        if plan != prelock:
            raise RuntimeError("residue recovery plan drifted before apply")
        _ensure_exact_directory(RECOVERY_ROOT)
        _write_intent(plan)
        state = _validate_current_state(plan)
        if state == "source":
            source = Path(str(plan["source_staging_root"]))
            archive = Path(str(plan["archive_path"]))
            if os.path.lexists(archive):
                raise RuntimeError("residue recovery archive collides")
            source_item = os.lstat(source)
            recovery_item = os.lstat(RECOVERY_ROOT)
            if source_item.st_dev != recovery_item.st_dev:
                raise RuntimeError("residue recovery rename is not atomic")
            os.rename(source, archive)
            _fsync_directory(source.parent)
            _fsync_directory(RECOVERY_ROOT)
        if _validate_current_state(plan) != "archive":
            raise RuntimeError("residue recovery archive was not established")
        existing = _load_terminal_receipt(plan)
        if existing is not None:
            return existing
        after = _collect_service_states()
        if after != plan["service_states"]:
            raise RuntimeError("residue recovery terminal service state drifted")
        unsigned = _receipt_unsigned(
            plan,
            service_states_after=after,
            created_at_unix=int(clock()),
        )
        receipt = {**unsigned, "receipt_sha256": _sha256_json(unsigned)}
        _publish_bytes_no_replace(
            Path(str(plan["receipt_path"])),
            _canonical_bytes(receipt),
            maximum_bytes=_MAX_RECEIPT_BYTES,
            exact_mode=0o400,
        )
        terminal = _load_terminal_receipt(plan)
        if terminal is None:
            raise RuntimeError("residue recovery terminal receipt is missing")
        return terminal


class _CanonicalArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ValueError("invalid residue recovery CLI arguments")


class _StoreOnce(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        if getattr(namespace, self.dest, None) is not None:
            raise ValueError("residue recovery CLI option was repeated")
        setattr(namespace, self.dest, values)


def _parser() -> argparse.ArgumentParser:
    parser = _CanonicalArgumentParser(
        description="Quarantine one exact stopped writer staging residue",
        allow_abbrev=False,
    )
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--revision", required=True, action=_StoreOnce)
    parser.add_argument("--approved-plan-sha256", action=_StoreOnce)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        _require_root_linux()
        if arguments.action == "plan":
            if arguments.approved_plan_sha256 is not None:
                raise ValueError("residue recovery plan received an approval")
            result = plan_stopped_writer_residue_recovery(arguments.revision)
        else:
            result = apply_stopped_writer_residue_recovery(
                arguments.revision,
                arguments.approved_plan_sha256,
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": FAILURE_SCHEMA,
                    "ok": False,
                    "error_code": "stopped_writer_residue_recovery_failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


__all__ = [
    "FAILURE_SCHEMA",
    "PLAN_SCHEMA",
    "RECEIPT_SCHEMA",
    "apply_stopped_writer_residue_recovery",
    "main",
    "plan_stopped_writer_residue_recovery",
    "validate_plan_mapping",
    "validate_receipt_mapping",
]


if __name__ == "__main__":
    raise SystemExit(main())
