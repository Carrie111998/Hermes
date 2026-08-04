"""Root-only stopped runtime for one exact Canonical Writer schema upgrade.

The wire dialogue is deliberately separate from the helper-only schema
reconciliation protocol.  It accepts one exact historical generation and one
exact current release artifact, performs the upgrade in a single locked
transaction, closes the database session, waits for Cloud SQL user deletion,
and only then emits a terminal receipt from a fresh writer session.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from gateway import canonical_writer_foundation as foundation
from gateway import canonical_writer_foundation_phase_b as phase_b
from gateway import canonical_writer_phase_b_runtime as phase_b_runtime
from gateway import canonical_writer_schema_reconciliation_control_bootstrap as control_bootstrap
from gateway import canonical_writer_schema_reconciliation_runtime as reconciliation_runtime
from gateway.canonical_writer_db import (
    CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY,
    CredentialSource,
    QueryResult,
)
from gateway.canonical_writer_schema_reconciliation import (
    BASE_ARTIFACT_NAME,
    SchemaContract,
    SchemaReconciliationError,
    _load_sealed_artifacts,
    _target_policy,
    collect_schema_contract,
)
from gateway.canonical_writer_schema_upgrade import (
    SOURCE_BASE_ARTIFACT_SHA256,
    SOURCE_SCHEMA_REVISION,
    UPGRADE_ADMIN_DATABASE_ROLES,
    UPGRADE_TERMINAL_SCHEMA,
    SchemaUpgradePlan,
    collect_upgrade_admin_authority_receipt,
    execute_atomic_schema_upgrade,
)


APPLY_OWNER_SSHSIG_NAMESPACE = (
    "muncho-canonical-writer-schema-upgrade-apply-owner-v1"
)
CLEANUP_OWNER_SSHSIG_NAMESPACE = (
    "muncho-canonical-writer-schema-upgrade-cleanup-owner-v1"
)

APPLY_MAGIC = b"MCU1"
CLEANUP_MAGIC = b"MCX1"
OPAQUE_CREDENTIAL_BYTES = 64
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_GATE_TTL_SECONDS = 1_800
MAX_CLAIM_TTL_SECONDS = 300

GATE_SCHEMA = "muncho-canonical-writer-schema-upgrade-gate.v1"
APPLY_CLAIM_SCHEMA = "muncho-canonical-writer-schema-upgrade-owner-apply.v1"
INTERMEDIATE_SCHEMA = "muncho-canonical-writer-schema-upgrade-intermediate.v1"
CLEANUP_CLAIM_SCHEMA = "muncho-canonical-writer-schema-upgrade-owner-cleanup.v1"
TERMINAL_SCHEMA = "muncho-canonical-writer-schema-upgrade-runtime-terminal.v1"
FAILURE_SCHEMA = "muncho-canonical-writer-schema-upgrade-failure.v1"
CLOUD_AUTHORITY_SCHEMA = "muncho-cloud-sql-schema-upgrade-admin-authority.v1"
CLOUD_ABSENCE_SCHEMA = "muncho-cloud-sql-schema-upgrade-admin-absence.v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
# The stopped schema-upgrade login deliberately reuses the installed control
# observer's one-time reconciler namespace. Its authority is still a distinct,
# exact dual-role contract and is never accepted by normal reconciliation.
_ADMIN = re.compile(r"^muncho_canary_reconciler_[0-9a-f]{16}$")
_URLSAFE_CREDENTIAL = re.compile(rb"^[A-Za-z0-9_-]{64}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_OPERATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+\-]{0,511}$")
_STABLE_ERROR = re.compile(r"^schema_upgrade_[a-z0-9_]{2,80}$")

_RELEASE_BINDING_FIELDS = frozenset(
    {
        "release_manifest_sha256",
        "stopped_release_receipt_file_sha256",
        "stopped_release_receipt_sha256",
        "release_artifact_sha256",
        "python_version",
        "interpreter_sha256",
        "activation_inventory_sha256",
    }
)

_GATE_FIELDS = frozenset(
    {
        "schema",
        "ok",
        "state",
        "release_revision",
        *_RELEASE_BINDING_FIELDS,
        "plan_sha256",
        "source_schema_revision",
        "source_base_artifact_sha256",
        "source_contract_sha256",
        "target_contract_sha256",
        "target_base_artifact_sha256",
        "transactional_migration_body_sha256",
        "initial_control_observation_sha256",
        "initial_writer_managed_hba_receipt_sha256",
        "host_identity_sha256",
        "services_stopped_sha256",
        "project",
        "sql_instance",
        "database",
        "postgresql_major",
        "tls_server_name",
        "temporary_schema_upgrade_admin_username",
        "temporary_schema_upgrade_admin_username_sha256",
        "database_roles_requested",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "owner_public_fingerprint",
        "run_nonce_sha256",
        "issued_at_unix",
        "expires_at_unix",
        "services_stopped",
        "secret_material_recorded",
        "gate_sha256",
    }
)

_CLOUD_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "project",
        "instance",
        "username_sha256",
        "host",
        "type",
        "user_present",
        "owner_subject_sha256",
        "mutation_context_sha256",
        "baseline_operation_names",
        "baseline_user_operations",
        "authority_operation",
        "broad_schema_upgrade_authority",
        "database_roles_requested",
        "normal_reconciliation_executor",
        "resource_etag_sha256",
        "receipt_sha256",
    }
)

_APPLY_UNSIGNED_FIELDS = frozenset(
    {
        "schema",
        "action",
        "approved",
        "gate_sha256",
        "release_revision",
        "plan_sha256",
        "temporary_schema_upgrade_admin_username_sha256",
        "owner_subject_sha256",
        "owner_key_id",
        "cloud_sql_authority_receipt",
        "cloud_sql_authority_receipt_sha256",
        "credential_length",
        "issued_at_unix",
        "expires_at_unix",
        "nonce_sha256",
        "secret_material_recorded",
    }
)
_APPLY_SIGNED_FIELDS = frozenset({*_APPLY_UNSIGNED_FIELDS, "apply_claim_sha256"})
_APPLY_FIELDS = frozenset({*_APPLY_SIGNED_FIELDS, "signature_sshsig"})

_CLOUD_ABSENCE_FIELDS = frozenset(
    {
        "schema",
        "temporary_schema_upgrade_admin_absent",
        "project",
        "instance",
        "username_sha256",
        "owner_subject_sha256",
        "mutation_context_sha256",
        "user_absent",
        "baseline_operation_names",
        "baseline_user_operations",
        "known_operation_names",
        "response_known_authority_operation_names",
        "response_known_delete_operation_names",
        "post_baseline_authority_operations",
        "response_known_candidate_observed",
        "post_baseline_authority_operation_count",
        "terminal_user_operations",
        "mutation_ambiguity_observed",
        "quiet_window_seconds",
        "evidence_sha256",
    }
)

_CLEANUP_UNSIGNED_FIELDS = frozenset(
    {
        "schema",
        "action",
        "approved",
        "gate_sha256",
        "release_revision",
        "plan_sha256",
        "temporary_schema_upgrade_admin_username_sha256",
        "owner_subject_sha256",
        "owner_key_id",
        "apply_claim_sha256",
        "intermediate_sha256",
        "cloud_sql_absence_receipt",
        "cloud_sql_absence_receipt_sha256",
        "issued_at_unix",
        "expires_at_unix",
        "nonce_sha256",
        "secret_material_recorded",
    }
)
_CLEANUP_SIGNED_FIELDS = frozenset(
    {*_CLEANUP_UNSIGNED_FIELDS, "cleanup_claim_sha256"}
)
_CLEANUP_FIELDS = frozenset({*_CLEANUP_SIGNED_FIELDS, "signature_sshsig"})

_UPGRADE_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "ok",
        "state",
        "release_revision",
        "plan_sha256",
        "authorization_sha256",
        "initial_contract_sha256",
        "final_contract_sha256",
        "canonical_truth_receipt_sha256",
        "initial_observation_sha256",
        "final_observation_sha256",
        "writer_managed_hba_receipt_sha256",
        "admin_managed_hba_receipt_sha256",
        "mutation_applied",
        "deployment_lock_key",
        "started_at_unix",
        "secret_material_recorded",
        "receipt_sha256",
    }
)

_INTERMEDIATE_FIELDS = frozenset(
    {
        "schema",
        "ok",
        "state",
        "gate_sha256",
        "release_revision",
        "plan_sha256",
        "apply_claim_sha256",
        "before_admin_authority_receipt_sha256",
        "after_admin_authority_receipt_sha256",
        "upgrade_receipt",
        "upgrade_receipt_sha256",
        "database_session_closed",
        "database_capability_terminated",
        "services_stopped_sha256",
        "observed_at_unix",
        "secret_material_recorded",
        "intermediate_sha256",
    }
)

_TERMINAL_FIELDS = frozenset(
    {
        "schema",
        "ok",
        "state",
        "gate_sha256",
        "release_revision",
        "plan_sha256",
        "apply_claim_sha256",
        "intermediate_sha256",
        "cleanup_claim_sha256",
        "upgrade_receipt_sha256",
        "target_contract_sha256",
        "writer_managed_hba_receipt_sha256",
        "canonical_truth_receipt_sha256",
        "temporary_schema_upgrade_admin_absent",
        "database_admin_absence_exact",
        "services_stopped_sha256",
        "completed_at_unix",
        "secret_material_recorded",
        "terminal_sha256",
    }
)

_FAILURE_FIELDS = frozenset(
    {
        "schema",
        "ok",
        "wire_stage",
        "error_code",
        "gate_sha256",
        "release_revision",
        "plan_sha256",
        "transcript_head_sha256",
        "secret_material_recorded",
        "receipt_sha256",
    }
)

_REMOTE_STAGES = frozenset({"apply_to_intermediate", "cleanup_to_terminal"})


class SchemaUpgradeRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SchemaUpgradeRuntimeError(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SchemaUpgradeRuntimeError("schema_upgrade_json_invalid") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _hashed(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    unsigned = copy.deepcopy(dict(value))
    return {**unsigned, field: _sha256_json(unsigned)}


def _strict(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    return copy.deepcopy(dict(value))


def _hashed_mapping(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    raw = _strict(value, fields, code)
    digest = raw.get(digest_field)
    unsigned = {key: item for key, item in raw.items() if key != digest_field}
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != _sha256_json(unsigned)
    ):
        _fail(code)
    return raw


def _validate_ttl(
    value: Mapping[str, Any],
    *,
    now_unix: int,
    maximum_seconds: int,
    code: str,
) -> None:
    issued = value.get("issued_at_unix")
    expires = value.get("expires_at_unix")
    if (
        type(now_unix) is not int
        or type(issued) is not int
        or type(expires) is not int
        or not issued <= now_unix < expires
        or not 1 <= expires - issued <= maximum_seconds
    ):
        _fail(code)


def _require_signature(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("-----BEGIN SSH SIGNATURE-----\n")
        or not value.endswith("\n-----END SSH SIGNATURE-----\n")
        or len(value.encode("ascii", errors="ignore")) > 16_384
    ):
        _fail(code)
    return value


def _operation_names(value: Any, code: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or _OPERATION_NAME.fullmatch(item) is None
        for item in value
    ):
        _fail(code)
    if value != sorted(set(value)):
        _fail(code)
    return list(value)


def _operation_rows(value: Any, code: str) -> list[list[Any]]:
    if not isinstance(value, list):
        _fail(code)
    rows: list[list[Any]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 5:
            _fail(code)
        name, operation_type, status, actor_sha256, succeeded = item
        if (
            not isinstance(name, str)
            or _OPERATION_NAME.fullmatch(name) is None
            or operation_type not in {"CREATE_USER", "UPDATE_USER", "DELETE_USER"}
            or status != "DONE"
            or not isinstance(actor_sha256, str)
            or _SHA256.fullmatch(actor_sha256) is None
            or type(succeeded) is not bool
        ):
            _fail(code)
        rows.append(list(item))
    if [row[0] for row in rows] != sorted({row[0] for row in rows}):
        _fail(code)
    return rows


def _verify_signature(
    signature: Any,
    *,
    message: bytes,
    public_key_ed25519_hex: str,
    namespace: str,
    code: str,
) -> None:
    try:
        phase_b.verify_phase_b_sshsig(
            _require_signature(signature, code),
            message=message,
            public_key_ed25519_hex=public_key_ed25519_hex,
            namespace=namespace,
        )
    except (TypeError, ValueError, phase_b.PhaseBError) as exc:
        raise SchemaUpgradeRuntimeError(code) from exc


def validate_gate_for_owner(
    value: Any,
    *,
    expected_release_revision: str,
    expected_owner_subject_sha256: str,
    owner_public_key_ed25519_hex: str,
    owner_public_fingerprint: str,
    now_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_gate_invalid"
    raw = _hashed_mapping(
        value,
        fields=_GATE_FIELDS,
        digest_field="gate_sha256",
        code=code,
    )
    username = raw.get("temporary_schema_upgrade_admin_username")
    digest_names = (
        *_RELEASE_BINDING_FIELDS - {"python_version"},
        "plan_sha256",
        "source_base_artifact_sha256",
        "source_contract_sha256",
        "target_contract_sha256",
        "target_base_artifact_sha256",
        "transactional_migration_body_sha256",
        "initial_control_observation_sha256",
        "initial_writer_managed_hba_receipt_sha256",
        "host_identity_sha256",
        "services_stopped_sha256",
        "temporary_schema_upgrade_admin_username_sha256",
        "owner_subject_sha256",
        "owner_key_id",
        "run_nonce_sha256",
    )
    if (
        raw.get("schema") != GATE_SCHEMA
        or raw.get("ok") is not True
        or raw.get("state")
        not in {
            "exact_source_stopped_upgrade_ready",
            "exact_target_stopped_upgrade_replay_ready",
        }
        or raw.get("release_revision") != expected_release_revision
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or raw.get("source_schema_revision") != SOURCE_SCHEMA_REVISION
        or raw.get("source_base_artifact_sha256")
        != SOURCE_BASE_ARTIFACT_SHA256
        or raw.get("source_contract_sha256") == raw.get("target_contract_sha256")
        or any(
            not isinstance(raw.get(name), str)
            or _SHA256.fullmatch(str(raw.get(name))) is None
            for name in digest_names
        )
        or raw.get("project") != foundation.PROJECT
        or raw.get("sql_instance") != foundation.SQL_INSTANCE
        or raw.get("database") != foundation.SQL_DATABASE
        or raw.get("postgresql_major") != 18
        or raw.get("tls_server_name") != foundation.SQL_TLS_SERVER_NAME
        or not isinstance(username, str)
        or _ADMIN.fullmatch(username) is None
        or raw["temporary_schema_upgrade_admin_username_sha256"]
        != hashlib.sha256(username.encode("ascii")).hexdigest()
        or raw.get("database_roles_requested")
        != list(UPGRADE_ADMIN_DATABASE_ROLES)
        or raw.get("python_version")
        != reconciliation_runtime.EXPECTED_PYTHON_VERSION
        or raw.get("owner_subject_sha256") != expected_owner_subject_sha256
        or raw.get("owner_public_key_ed25519_hex")
        != owner_public_key_ed25519_hex
        or raw.get("owner_public_fingerprint") != owner_public_fingerprint
        or _FINGERPRINT.fullmatch(str(raw.get("owner_public_fingerprint", "")))
        is None
        or raw.get("services_stopped") is not True
        or raw.get("secret_material_recorded") is not False
    ):
        _fail(code)
    _validate_ttl(
        raw,
        now_unix=now_unix,
        maximum_seconds=MAX_GATE_TTL_SECONDS,
        code=code,
    )
    return raw


def _validate_cloud_authority(
    value: Any,
    *,
    gate: Mapping[str, Any],
) -> Mapping[str, Any]:
    code = "schema_upgrade_cloud_authority_invalid"
    raw = _hashed_mapping(
        value,
        fields=_CLOUD_AUTHORITY_FIELDS,
        digest_field="receipt_sha256",
        code=code,
    )
    baseline_names = _operation_names(raw.get("baseline_operation_names"), code)
    baseline_rows = _operation_rows(raw.get("baseline_user_operations"), code)
    authority_rows = _operation_rows([raw.get("authority_operation")], code)
    authority = authority_rows[0]
    if (
        raw.get("schema") != CLOUD_AUTHORITY_SCHEMA
        or raw.get("project") != gate["project"]
        or raw.get("instance") != gate["sql_instance"]
        or raw.get("username_sha256")
        != gate["temporary_schema_upgrade_admin_username_sha256"]
        or raw.get("host") != ""
        or raw.get("type") != "BUILT_IN"
        or raw.get("user_present") is not True
        or raw.get("owner_subject_sha256") != gate["owner_subject_sha256"]
        or raw.get("mutation_context_sha256") != gate["gate_sha256"]
        or raw.get("broad_schema_upgrade_authority") is not True
        or raw.get("database_roles_requested")
        != list(UPGRADE_ADMIN_DATABASE_ROLES)
        or raw.get("normal_reconciliation_executor") is not False
        or _SHA256.fullmatch(str(raw.get("resource_etag_sha256", ""))) is None
        or any(row[0] not in baseline_names for row in baseline_rows)
        or authority[0] in baseline_names
        or authority[1] not in {"CREATE_USER", "UPDATE_USER"}
        or authority[3] != gate["owner_subject_sha256"]
        or authority[4] is not True
    ):
        _fail(code)
    return raw


def build_owner_apply_unsigned(
    *,
    gate: Mapping[str, Any],
    cloud_sql_authority_receipt: Mapping[str, Any],
    issued_at_unix: int,
    expires_at_unix: int,
    nonce_sha256: str,
) -> Mapping[str, Any]:
    authority = _validate_cloud_authority(cloud_sql_authority_receipt, gate=gate)
    unsigned = {
        "schema": APPLY_CLAIM_SCHEMA,
        "action": "apply_exact_schema_upgrade",
        "approved": True,
        "gate_sha256": gate["gate_sha256"],
        "release_revision": gate["release_revision"],
        "plan_sha256": gate["plan_sha256"],
        "temporary_schema_upgrade_admin_username_sha256": gate[
            "temporary_schema_upgrade_admin_username_sha256"
        ],
        "owner_subject_sha256": gate["owner_subject_sha256"],
        "owner_key_id": gate["owner_key_id"],
        "cloud_sql_authority_receipt": authority,
        "cloud_sql_authority_receipt_sha256": authority["receipt_sha256"],
        "credential_length": OPAQUE_CREDENTIAL_BYTES,
        "issued_at_unix": issued_at_unix,
        "expires_at_unix": expires_at_unix,
        "nonce_sha256": nonce_sha256,
        "secret_material_recorded": False,
    }
    if set(unsigned) != _APPLY_UNSIGNED_FIELDS or _SHA256.fullmatch(
        str(nonce_sha256)
    ) is None:
        _fail("schema_upgrade_apply_claim_invalid")
    return {**unsigned, "apply_claim_sha256": _sha256_json(unsigned)}


def owner_apply_signature_payload(value: Mapping[str, Any]) -> bytes:
    raw = _strict(value, _APPLY_SIGNED_FIELDS, "schema_upgrade_apply_claim_invalid")
    return _canonical_bytes(raw)


def _validate_apply_claim(
    value: Any,
    *,
    gate: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_apply_claim_invalid"
    raw = _strict(value, _APPLY_FIELDS, code)
    unsigned = {key: raw[key] for key in _APPLY_UNSIGNED_FIELDS}
    expected = build_owner_apply_unsigned(
        gate=gate,
        cloud_sql_authority_receipt=raw["cloud_sql_authority_receipt"],
        issued_at_unix=raw["issued_at_unix"],
        expires_at_unix=raw["expires_at_unix"],
        nonce_sha256=raw["nonce_sha256"],
    )
    if raw.get("apply_claim_sha256") != expected["apply_claim_sha256"] or any(
        unsigned[key] != expected[key] for key in _APPLY_UNSIGNED_FIELDS
    ):
        _fail(code)
    _validate_ttl(raw, now_unix=now_unix, maximum_seconds=MAX_CLAIM_TTL_SECONDS, code=code)
    _verify_signature(
        raw["signature_sshsig"],
        message=owner_apply_signature_payload(expected),
        public_key_ed25519_hex=gate["owner_public_key_ed25519_hex"],
        namespace=APPLY_OWNER_SSHSIG_NAMESPACE,
        code=code,
    )
    return raw


def build_owner_cleanup_unsigned(
    *,
    gate: Mapping[str, Any],
    apply_claim: Mapping[str, Any],
    intermediate: Mapping[str, Any],
    cloud_sql_absence_receipt: Mapping[str, Any],
    issued_at_unix: int,
    expires_at_unix: int,
    nonce_sha256: str,
) -> Mapping[str, Any]:
    if not isinstance(apply_claim, Mapping):
        _fail("schema_upgrade_cleanup_claim_invalid")
    authority = _validate_cloud_authority(
        apply_claim.get("cloud_sql_authority_receipt"),
        gate=gate,
    )
    absence = _validate_cloud_absence(
        cloud_sql_absence_receipt,
        gate=gate,
        authority=authority,
    )
    unsigned = {
        "schema": CLEANUP_CLAIM_SCHEMA,
        "action": "confirm_schema_upgrade_admin_absent",
        "approved": True,
        "gate_sha256": gate["gate_sha256"],
        "release_revision": gate["release_revision"],
        "plan_sha256": gate["plan_sha256"],
        "temporary_schema_upgrade_admin_username_sha256": gate[
            "temporary_schema_upgrade_admin_username_sha256"
        ],
        "owner_subject_sha256": gate["owner_subject_sha256"],
        "owner_key_id": gate["owner_key_id"],
        "apply_claim_sha256": apply_claim.get("apply_claim_sha256"),
        "intermediate_sha256": intermediate.get("intermediate_sha256"),
        "cloud_sql_absence_receipt": absence,
        "cloud_sql_absence_receipt_sha256": absence["evidence_sha256"],
        "issued_at_unix": issued_at_unix,
        "expires_at_unix": expires_at_unix,
        "nonce_sha256": nonce_sha256,
        "secret_material_recorded": False,
    }
    if set(unsigned) != _CLEANUP_UNSIGNED_FIELDS or any(
        _SHA256.fullmatch(str(unsigned.get(name, ""))) is None
        for name in (
            "apply_claim_sha256",
            "intermediate_sha256",
            "nonce_sha256",
        )
    ):
        _fail("schema_upgrade_cleanup_claim_invalid")
    return {**unsigned, "cleanup_claim_sha256": _sha256_json(unsigned)}


def owner_cleanup_signature_payload(value: Mapping[str, Any]) -> bytes:
    raw = _strict(
        value,
        _CLEANUP_SIGNED_FIELDS,
        "schema_upgrade_cleanup_claim_invalid",
    )
    return _canonical_bytes(raw)


def _validate_cloud_absence(
    value: Any,
    *,
    gate: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> Mapping[str, Any]:
    code = "schema_upgrade_cloud_absence_invalid"
    raw = _hashed_mapping(
        value,
        fields=_CLOUD_ABSENCE_FIELDS,
        digest_field="evidence_sha256",
        code=code,
    )
    baseline_names = _operation_names(raw.get("baseline_operation_names"), code)
    baseline_rows = _operation_rows(raw.get("baseline_user_operations"), code)
    known_names = _operation_names(raw.get("known_operation_names"), code)
    known_authority = _operation_names(
        raw.get("response_known_authority_operation_names"), code
    )
    known_deletes = _operation_names(
        raw.get("response_known_delete_operation_names"), code
    )
    post_authority = _operation_rows(
        raw.get("post_baseline_authority_operations"), code
    )
    terminal_rows = _operation_rows(raw.get("terminal_user_operations"), code)
    authority_row = list(authority["authority_operation"])
    terminal_by_name = {row[0]: row for row in terminal_rows}
    quiet_window = raw.get("quiet_window_seconds")
    if (
        raw.get("schema") != CLOUD_ABSENCE_SCHEMA
        or raw.get("temporary_schema_upgrade_admin_absent") is not True
        or raw.get("project") != gate["project"]
        or raw.get("instance") != gate["sql_instance"]
        or raw.get("username_sha256")
        != gate["temporary_schema_upgrade_admin_username_sha256"]
        or raw.get("owner_subject_sha256") != gate["owner_subject_sha256"]
        or raw.get("mutation_context_sha256") != gate["gate_sha256"]
        or raw.get("user_absent") is not True
        or raw.get("response_known_candidate_observed") is not True
        or raw.get("post_baseline_authority_operation_count") != 1
        or baseline_names != authority["baseline_operation_names"]
        or baseline_rows != authority["baseline_user_operations"]
        or known_authority != [authority_row[0]]
        or post_authority != [authority_row]
        or raw.get("post_baseline_authority_operation_count") != 1
        or not known_deletes
        or set(known_names) != {authority_row[0], *known_deletes}
        or set(terminal_by_name)
        != {*(row[0] for row in baseline_rows), *known_names}
        or terminal_by_name.get(authority_row[0]) != authority_row
        or any(
            terminal_by_name.get(name) is None
            or terminal_by_name[name][1] != "DELETE_USER"
            or terminal_by_name[name][2] != "DONE"
            or terminal_by_name[name][3] != gate["owner_subject_sha256"]
            or terminal_by_name[name][4] is not True
            for name in known_deletes
        )
        or any(terminal_by_name.get(row[0]) != row for row in baseline_rows)
        or type(raw.get("mutation_ambiguity_observed")) is not bool
        or not isinstance(quiet_window, (int, float))
        or isinstance(quiet_window, bool)
        or not 180 <= quiet_window <= 3_600
    ):
        _fail(code)
    return raw


def _validate_cleanup_claim(
    value: Any,
    *,
    gate: Mapping[str, Any],
    apply_claim: Mapping[str, Any],
    intermediate: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_cleanup_claim_invalid"
    raw = _strict(value, _CLEANUP_FIELDS, code)
    expected = build_owner_cleanup_unsigned(
        gate=gate,
        apply_claim=apply_claim,
        intermediate=intermediate,
        cloud_sql_absence_receipt=raw["cloud_sql_absence_receipt"],
        issued_at_unix=raw["issued_at_unix"],
        expires_at_unix=raw["expires_at_unix"],
        nonce_sha256=raw["nonce_sha256"],
    )
    if raw.get("cleanup_claim_sha256") != expected["cleanup_claim_sha256"] or any(
        raw[key] != expected[key] for key in _CLEANUP_UNSIGNED_FIELDS
    ):
        _fail(code)
    _validate_ttl(raw, now_unix=now_unix, maximum_seconds=MAX_CLAIM_TTL_SECONDS, code=code)
    _verify_signature(
        raw["signature_sshsig"],
        message=owner_cleanup_signature_payload(expected),
        public_key_ed25519_hex=gate["owner_public_key_ed25519_hex"],
        namespace=CLEANUP_OWNER_SSHSIG_NAMESPACE,
        code=code,
    )
    return raw


def _validate_upgrade_receipt(
    value: Any,
    *,
    gate: Mapping[str, Any],
    apply_claim: Mapping[str, Any],
    observed_at_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_intermediate_invalid"
    raw = _hashed_mapping(
        value,
        fields=_UPGRADE_RECEIPT_FIELDS,
        digest_field="receipt_sha256",
        code=code,
    )
    replay = gate.get("state") == "exact_target_stopped_upgrade_replay_ready"
    expected_state = "already_exact_target" if replay else "exact_target_committed"
    expected_initial = (
        gate["target_contract_sha256"]
        if replay
        else gate["source_contract_sha256"]
    )
    digest_fields = (
        "canonical_truth_receipt_sha256",
        "initial_observation_sha256",
        "final_observation_sha256",
        "writer_managed_hba_receipt_sha256",
        "admin_managed_hba_receipt_sha256",
    )
    if (
        raw.get("schema") != UPGRADE_TERMINAL_SCHEMA
        or raw.get("ok") is not True
        or raw.get("state") != expected_state
        or raw.get("release_revision") != gate["release_revision"]
        or raw.get("plan_sha256") != gate["plan_sha256"]
        or raw.get("authorization_sha256")
        != apply_claim["apply_claim_sha256"]
        or raw.get("initial_contract_sha256") != expected_initial
        or raw.get("final_contract_sha256") != gate["target_contract_sha256"]
        or any(
            not isinstance(raw.get(name), str)
            or _SHA256.fullmatch(raw[name]) is None
            for name in digest_fields
        )
        or raw.get("mutation_applied") is not (not replay)
        or raw.get("deployment_lock_key")
        != CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY
        or type(raw.get("started_at_unix")) is not int
        or not gate["issued_at_unix"]
        <= raw["started_at_unix"]
        <= observed_at_unix
        or raw.get("secret_material_recorded") is not False
    ):
        _fail(code)
    if replay:
        if raw["initial_observation_sha256"] != raw["final_observation_sha256"]:
            _fail(code)
    return raw


def validate_intermediate_for_owner(
    value: Any,
    *,
    gate: Mapping[str, Any],
    apply_claim: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_intermediate_invalid"
    raw = _hashed_mapping(
        value,
        fields=_INTERMEDIATE_FIELDS,
        digest_field="intermediate_sha256",
        code=code,
    )
    receipt = _validate_upgrade_receipt(
        raw.get("upgrade_receipt"),
        gate=gate,
        apply_claim=apply_claim,
        observed_at_unix=raw.get("observed_at_unix", -1),
    )
    digest_fields = (
        "before_admin_authority_receipt_sha256",
        "after_admin_authority_receipt_sha256",
    )
    if (
        raw.get("schema") != INTERMEDIATE_SCHEMA
        or raw.get("ok") is not True
        or raw.get("state")
        != "upgrade_committed_session_closed_awaiting_cloud_cleanup"
        or raw.get("gate_sha256") != gate["gate_sha256"]
        or raw.get("release_revision") != gate["release_revision"]
        or raw.get("plan_sha256") != gate["plan_sha256"]
        or raw.get("apply_claim_sha256")
        != apply_claim.get("apply_claim_sha256")
        or any(
            not isinstance(raw.get(name), str)
            or _SHA256.fullmatch(raw[name]) is None
            for name in digest_fields
        )
        or raw.get("upgrade_receipt") != receipt
        or raw.get("upgrade_receipt_sha256") != receipt["receipt_sha256"]
        or raw.get("database_session_closed") is not True
        or raw.get("database_capability_terminated") is not True
        or raw.get("services_stopped_sha256")
        != gate["services_stopped_sha256"]
        or type(raw.get("observed_at_unix")) is not int
        or not gate["issued_at_unix"] <= raw["observed_at_unix"] <= now_unix
        or raw.get("secret_material_recorded") is not False
    ):
        _fail(code)
    return raw


def validate_terminal_for_owner(
    value: Any,
    *,
    gate: Mapping[str, Any],
    apply_claim: Mapping[str, Any],
    intermediate: Mapping[str, Any],
    cleanup_claim: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_terminal_invalid"
    raw = _hashed_mapping(
        value,
        fields=_TERMINAL_FIELDS,
        digest_field="terminal_sha256",
        code=code,
    )
    if (
        raw.get("schema") != TERMINAL_SCHEMA
        or raw.get("ok") is not True
        or raw.get("state") != "exact_target_admin_absent_services_stopped"
        or raw.get("gate_sha256") != gate["gate_sha256"]
        or raw.get("release_revision") != gate["release_revision"]
        or raw.get("plan_sha256") != gate["plan_sha256"]
        or raw.get("apply_claim_sha256")
        != apply_claim.get("apply_claim_sha256")
        or raw.get("intermediate_sha256")
        != intermediate.get("intermediate_sha256")
        or raw.get("cleanup_claim_sha256")
        != cleanup_claim.get("cleanup_claim_sha256")
        or raw.get("upgrade_receipt_sha256")
        != intermediate.get("upgrade_receipt_sha256")
        or raw.get("target_contract_sha256")
        != gate["target_contract_sha256"]
        or not isinstance(raw.get("writer_managed_hba_receipt_sha256"), str)
        or _SHA256.fullmatch(raw["writer_managed_hba_receipt_sha256"]) is None
        or raw.get("canonical_truth_receipt_sha256")
        != intermediate.get("upgrade_receipt", {}).get(
            "canonical_truth_receipt_sha256"
        )
        or raw.get("temporary_schema_upgrade_admin_absent") is not True
        or raw.get("database_admin_absence_exact") is not True
        or raw.get("services_stopped_sha256")
        != gate["services_stopped_sha256"]
        or type(raw.get("completed_at_unix")) is not int
        or not intermediate.get("observed_at_unix", now_unix + 1)
        <= raw["completed_at_unix"]
        <= now_unix
        or raw.get("secret_material_recorded") is not False
    ):
        _fail(code)
    return raw


def validate_failure_for_owner(
    value: Any,
    *,
    gate: Mapping[str, Any],
    expected_wire_stage: str,
    expected_transcript_head_sha256: str,
) -> Mapping[str, Any]:
    code = "schema_upgrade_failure_invalid"
    raw = _hashed_mapping(
        value,
        fields=_FAILURE_FIELDS,
        digest_field="receipt_sha256",
        code=code,
    )
    if (
        expected_wire_stage not in _REMOTE_STAGES
        or raw.get("schema") != FAILURE_SCHEMA
        or raw.get("ok") is not False
        or raw.get("wire_stage") != expected_wire_stage
        or not isinstance(raw.get("error_code"), str)
        or _STABLE_ERROR.fullmatch(raw["error_code"]) is None
        or raw.get("gate_sha256") != gate["gate_sha256"]
        or raw.get("release_revision") != gate["release_revision"]
        or raw.get("plan_sha256") != gate["plan_sha256"]
        or raw.get("transcript_head_sha256")
        != expected_transcript_head_sha256
        or raw.get("secret_material_recorded") is not False
    ):
        _fail(code)
    return raw


def _read_exact(stream: BinaryIO, size: int, code: str) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            _fail(code)
        data.extend(chunk)
    return bytes(data)


def _read_frame(stream: BinaryIO, *, magic: bytes, code: str) -> Mapping[str, Any]:
    header = _read_exact(stream, 8, code)
    observed_magic, size = struct.unpack(">4sI", header)
    if observed_magic != magic or not 2 <= size <= MAX_JSON_BYTES:
        _fail(code)
    raw = _read_exact(stream, size, code)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SchemaUpgradeRuntimeError(code) from exc
    if not isinstance(value, Mapping) or raw != _canonical_bytes(value):
        _fail(code)
    return value


def build_frame(
    magic: bytes,
    value: Mapping[str, Any],
    *,
    credential: bytes | bytearray | None = None,
) -> bytearray:
    if magic not in {APPLY_MAGIC, CLEANUP_MAGIC}:
        _fail("schema_upgrade_frame_invalid")
    raw = _canonical_bytes(value)
    suffix = b"" if credential is None else bytes(credential)
    if (
        not 2 <= len(raw) <= MAX_JSON_BYTES
        or (magic == APPLY_MAGIC and len(suffix) != OPAQUE_CREDENTIAL_BYTES)
        or (magic == CLEANUP_MAGIC and suffix)
    ):
        _fail("schema_upgrade_frame_invalid")
    return bytearray(struct.pack(">4sI", magic, len(raw)) + raw + suffix)


def _emit(stream: BinaryIO, value: Mapping[str, Any]) -> None:
    try:
        stream.write(_canonical_bytes(value) + b"\n")
        stream.flush()
    except (OSError, ValueError) as exc:
        raise SchemaUpgradeRuntimeError("schema_upgrade_output_failed") from exc


def _zeroize(value: bytearray | None) -> None:
    if value is not None:
        for index in range(len(value)):
            value[index] = 0


ApplyCallback = Callable[[Mapping[str, Any], Mapping[str, Any], bytearray], Mapping[str, Any]]
CleanupCallback = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
    Mapping[str, Any],
]


def run_protocol(
    gate: Mapping[str, Any],
    *,
    apply_callback: ApplyCallback,
    cleanup_callback: CleanupCallback,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    now: Callable[[], int] = lambda: int(time.time()),
) -> Mapping[str, Any]:
    source = sys.stdin.buffer if input_stream is None else input_stream
    sink = sys.stdout.buffer if output_stream is None else output_stream
    validated_gate = validate_gate_for_owner(
        gate,
        expected_release_revision=gate.get("release_revision"),
        expected_owner_subject_sha256=gate.get("owner_subject_sha256"),
        owner_public_key_ed25519_hex=gate.get("owner_public_key_ed25519_hex"),
        owner_public_fingerprint=gate.get("owner_public_fingerprint"),
        now_unix=now(),
    )
    _emit(sink, validated_gate)
    credential: bytearray | None = None
    stage = "apply_to_intermediate"
    transcript = validated_gate["gate_sha256"]
    try:
        apply_raw = _read_frame(
            source,
            magic=APPLY_MAGIC,
            code="schema_upgrade_apply_frame_invalid",
        )
        apply_claim = _validate_apply_claim(
            apply_raw,
            gate=validated_gate,
            now_unix=now(),
        )
        transcript = apply_claim["apply_claim_sha256"]
        credential = bytearray(
            _read_exact(
                source,
                OPAQUE_CREDENTIAL_BYTES,
                "schema_upgrade_credential_invalid",
            )
        )
        if _URLSAFE_CREDENTIAL.fullmatch(credential) is None:
            _fail("schema_upgrade_credential_invalid")
        try:
            intermediate = apply_callback(validated_gate, apply_claim, credential)
        finally:
            _zeroize(credential)
        intermediate = validate_intermediate_for_owner(
            intermediate,
            gate=validated_gate,
            apply_claim=apply_claim,
            now_unix=now(),
        )
        _emit(sink, intermediate)
        stage = "cleanup_to_terminal"
        transcript = intermediate["intermediate_sha256"]
        cleanup_raw = _read_frame(
            source,
            magic=CLEANUP_MAGIC,
            code="schema_upgrade_cleanup_frame_invalid",
        )
        if source.read(1) != b"":
            _fail("schema_upgrade_cleanup_frame_invalid")
        cleanup = _validate_cleanup_claim(
            cleanup_raw,
            gate=validated_gate,
            apply_claim=apply_claim,
            intermediate=intermediate,
            now_unix=now(),
        )
        transcript = cleanup["cleanup_claim_sha256"]
        terminal = cleanup_callback(
            validated_gate,
            apply_claim,
            intermediate,
            cleanup,
        )
        terminal = validate_terminal_for_owner(
            terminal,
            gate=validated_gate,
            apply_claim=apply_claim,
            intermediate=intermediate,
            cleanup_claim=cleanup,
            now_unix=now(),
        )
        _emit(sink, terminal)
        return terminal
    except BaseException as exc:
        code = (
            exc.code
            if isinstance(exc, SchemaUpgradeRuntimeError)
            and _STABLE_ERROR.fullmatch(exc.code)
            else "schema_upgrade_failed_closed"
        )
        unsigned = {
            "schema": FAILURE_SCHEMA,
            "ok": False,
            "wire_stage": stage,
            "error_code": code,
            "gate_sha256": validated_gate["gate_sha256"],
            "release_revision": validated_gate["release_revision"],
            "plan_sha256": validated_gate["plan_sha256"],
            "transcript_head_sha256": transcript,
            "secret_material_recorded": False,
        }
        try:
            _emit(sink, _hashed(unsigned, "receipt_sha256"))
        except BaseException:
            pass
        raise
    finally:
        _zeroize(credential)


@dataclass(frozen=True)
class _RuntimeDependencies:
    base_dependencies: reconciliation_runtime._RuntimeDependencies = field(
        default_factory=reconciliation_runtime._RuntimeDependencies
    )
    prepare_base: Callable[[Any], Any] = reconciliation_runtime._prepare_runtime
    protocol_runner: Callable[..., Mapping[str, Any]] = run_protocol


@dataclass
class _RuntimeContext:
    base: Any
    plan: SchemaUpgradePlan
    target: SchemaContract
    artifact: Any
    gate: Mapping[str, Any]
    apply_used: bool = False
    cleanup_used: bool = False


def _close_session(session: Any) -> None:
    try:
        session.close()
    except BaseException as exc:
        raise SchemaUpgradeRuntimeError("schema_upgrade_database_close_failed") from exc


def _prepare_runtime(dependencies: _RuntimeDependencies) -> _RuntimeContext:
    try:
        base = dependencies.prepare_base(dependencies.base_dependencies)
        artifact = _load_sealed_artifacts(base.revision)[BASE_ARTIFACT_NAME]
        plan = SchemaUpgradePlan.build(
            release_revision=base.revision,
            target=base.target,
            artifact=artifact,
        )
        writer_config = base.dependencies.writer_config()
        observed_at = base.dependencies.now()
        writer_hba = base.dependencies.collect_hba(
            writer_config,
            now_unix=observed_at,
            ttl_seconds=300,
        )
        session = base.dependencies.open_session(writer_config)
        try:
            control = control_bootstrap._observe_foundation(
                session,
                phase="post_cleanup",
                observed_at_unix=base.dependencies.now,
            )
            observed = collect_schema_contract(
                session,
                config=writer_config,
                policy=_target_policy(base.target.attestation),
                managed_hba_receipt=writer_hba,
                subject_user=writer_config.user,
                allow_missing_helper=True,
            )
        finally:
            _close_session(session)
        if control.get("state") != "exact_installed" or observed.sha256 not in {
            plan.value["source_contract_sha256"],
            plan.value["target_contract_sha256"],
        }:
            _fail("schema_upgrade_source_generation_invalid")
        issued_at = base.dependencies.now()
        nonce = base.dependencies.random_bytes(32)
        username = "muncho_canary_reconciler_" + plan.sha256[:16]
        if not isinstance(nonce, bytes) or len(nonce) != 32:
            _fail("schema_upgrade_clock_invalid")
        unsigned_gate = {
            "schema": GATE_SCHEMA,
            "ok": True,
            "state": (
                "exact_source_stopped_upgrade_ready"
                if observed.sha256 == plan.value["source_contract_sha256"]
                else "exact_target_stopped_upgrade_replay_ready"
            ),
            "release_revision": base.revision,
            **base.initial_release_binding,
            "plan_sha256": plan.sha256,
            "source_schema_revision": SOURCE_SCHEMA_REVISION,
            "source_base_artifact_sha256": SOURCE_BASE_ARTIFACT_SHA256,
            "source_contract_sha256": plan.value["source_contract_sha256"],
            "target_contract_sha256": plan.value["target_contract_sha256"],
            "target_base_artifact_sha256": plan.value[
                "target_base_artifact_sha256"
            ],
            "transactional_migration_body_sha256": plan.value[
                "transactional_migration_body_sha256"
            ],
            "initial_control_observation_sha256": control["observation_sha256"],
            "initial_writer_managed_hba_receipt_sha256": writer_hba.sha256,
            "host_identity_sha256": base.initial_host_state["state_sha256"],
            "services_stopped_sha256": base.initial_services_state["state_sha256"],
            "project": foundation.PROJECT,
            "sql_instance": foundation.SQL_INSTANCE,
            "database": foundation.SQL_DATABASE,
            "postgresql_major": 18,
            "tls_server_name": foundation.SQL_TLS_SERVER_NAME,
            "temporary_schema_upgrade_admin_username": username,
            "temporary_schema_upgrade_admin_username_sha256": hashlib.sha256(
                username.encode("ascii")
            ).hexdigest(),
            "database_roles_requested": list(UPGRADE_ADMIN_DATABASE_ROLES),
            "owner_subject_sha256": reconciliation_runtime.OWNER_SUBJECT_SHA256,
            "owner_public_key_ed25519_hex": (
                reconciliation_runtime.OWNER_PUBLIC_KEY_ED25519_HEX
            ),
            "owner_key_id": reconciliation_runtime.OWNER_KEY_ID,
            "owner_public_fingerprint": (
                reconciliation_runtime.OWNER_PUBLIC_FINGERPRINT
            ),
            "run_nonce_sha256": hashlib.sha256(nonce).hexdigest(),
            "issued_at_unix": issued_at,
            "expires_at_unix": issued_at + MAX_GATE_TTL_SECONDS,
            "services_stopped": True,
            "secret_material_recorded": False,
        }
        gate = _hashed(unsigned_gate, "gate_sha256")
        validate_gate_for_owner(
            gate,
            expected_release_revision=base.revision,
            expected_owner_subject_sha256=reconciliation_runtime.OWNER_SUBJECT_SHA256,
            owner_public_key_ed25519_hex=(
                reconciliation_runtime.OWNER_PUBLIC_KEY_ED25519_HEX
            ),
            owner_public_fingerprint=reconciliation_runtime.OWNER_PUBLIC_FINGERPRINT,
            now_unix=issued_at,
        )
        return _RuntimeContext(
            base=base,
            plan=plan,
            target=base.target,
            artifact=artifact,
            gate=gate,
        )
    except SchemaUpgradeRuntimeError:
        raise
    except BaseException as exc:
        raise SchemaUpgradeRuntimeError("schema_upgrade_pre_gate_invalid") from exc


def _revalidate_stopped(context: _RuntimeContext, code: str) -> None:
    try:
        reconciliation_runtime._revalidate_stopped_boundary(context.base, code=code)
    except BaseException as exc:
        raise SchemaUpgradeRuntimeError(code) from exc


def _schema_upgrade_apply_error_code(primary: BaseException) -> str:
    """Preserve only a bounded, non-secret schema-upgrade invariant code."""

    code = getattr(primary, "code", None)
    if (
        isinstance(primary, (SchemaUpgradeRuntimeError, SchemaReconciliationError))
        and isinstance(code, str)
        and _STABLE_ERROR.fullmatch(code) is not None
    ):
        return code
    return "schema_upgrade_apply_failed"


def _runtime_apply(
    context: _RuntimeContext,
    gate: Mapping[str, Any],
    claim: Mapping[str, Any],
    credential: bytearray,
) -> Mapping[str, Any]:
    if gate != context.gate or context.apply_used:
        _fail("schema_upgrade_apply_state_invalid")
    context.apply_used = True
    _revalidate_stopped(context, "schema_upgrade_stopped_boundary_drifted")
    session = None
    primary: BaseException | None = None
    try:
        started_at = context.base.dependencies.now()
        writer_config = context.base.dependencies.writer_config()
        writer_hba = context.base.dependencies.collect_hba(
            writer_config,
            now_unix=started_at,
            ttl_seconds=300,
        )
        with phase_b_runtime._secret_descriptor(credential) as descriptor:
            admin_config = phase_b_runtime._database_config(
                gate["temporary_schema_upgrade_admin_username"],
                credential=CredentialSource(
                    fd=descriptor,
                    expected_uid=0,
                    expected_gid=0,
                    allowed_modes=frozenset({0o400}),
                ),
                application_name="muncho-exact-schema-upgrade",
            )
            admin_hba = context.base.dependencies.collect_hba(
                admin_config,
                now_unix=started_at,
                ttl_seconds=300,
            )
            session = context.base.dependencies.open_session(admin_config)
        before_authority = collect_upgrade_admin_authority_receipt(
            session,
            observed_at_unix=context.base.dependencies.now(),
        )
        _revalidate_stopped(context, "schema_upgrade_stopped_boundary_drifted")
        _validate_apply_claim(claim, gate=gate, now_unix=context.base.dependencies.now())
        upgrade_receipt = execute_atomic_schema_upgrade(
            context.plan,
            target=context.target,
            artifact=context.artifact,
            session=session,
            writer_config=writer_config,
            writer_managed_hba_receipt=writer_hba,
            admin_managed_hba_receipt=admin_hba,
            authorization_sha256=claim["apply_claim_sha256"],
            started_at_unix=started_at,
        )
        after_authority = collect_upgrade_admin_authority_receipt(
            session,
            observed_at_unix=context.base.dependencies.now(),
        )
    except BaseException as exc:
        primary = exc
    finally:
        _zeroize(credential)
        if session is not None:
            try:
                _close_session(session)
            except BaseException as close_error:
                if primary is None:
                    primary = close_error
    if primary is not None:
        if isinstance(primary, SchemaUpgradeRuntimeError):
            raise primary
        raise SchemaUpgradeRuntimeError(
            _schema_upgrade_apply_error_code(primary)
        ) from primary
    _revalidate_stopped(context, "schema_upgrade_stopped_boundary_drifted")
    unsigned = {
        "schema": INTERMEDIATE_SCHEMA,
        "ok": True,
        "state": "upgrade_committed_session_closed_awaiting_cloud_cleanup",
        "gate_sha256": gate["gate_sha256"],
        "release_revision": gate["release_revision"],
        "plan_sha256": gate["plan_sha256"],
        "apply_claim_sha256": claim["apply_claim_sha256"],
        "before_admin_authority_receipt_sha256": before_authority[
            "receipt_sha256"
        ],
        "after_admin_authority_receipt_sha256": after_authority["receipt_sha256"],
        "upgrade_receipt": upgrade_receipt,
        "upgrade_receipt_sha256": upgrade_receipt["receipt_sha256"],
        "database_session_closed": True,
        "database_capability_terminated": True,
        "services_stopped_sha256": gate["services_stopped_sha256"],
        "observed_at_unix": context.base.dependencies.now(),
        "secret_material_recorded": False,
    }
    return _hashed(unsigned, "intermediate_sha256")


_ADMIN_ABSENCE_SQL = r"""
SELECT NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '__ADMIN__'
       ) AS exact_admin_absent,
       NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles
            WHERE rolname ~ '^muncho_canary_reconciler_[0-9a-f]{16}$'
       ) AS upgrade_admin_inventory_empty,
       NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
           WHERE member.rolname = '__ADMIN__'
       ) AS upgrade_admin_memberships_absent
""".strip()


def _runtime_cleanup(
    context: _RuntimeContext,
    gate: Mapping[str, Any],
    claim: Mapping[str, Any],
    intermediate: Mapping[str, Any],
    cleanup: Mapping[str, Any],
) -> Mapping[str, Any]:
    if gate != context.gate or not context.apply_used or context.cleanup_used:
        _fail("schema_upgrade_cleanup_state_invalid")
    context.cleanup_used = True
    _revalidate_stopped(context, "schema_upgrade_stopped_boundary_drifted")
    config = context.base.dependencies.writer_config()
    observed_at = context.base.dependencies.now()
    hba = context.base.dependencies.collect_hba(
        config,
        now_unix=observed_at,
        ttl_seconds=300,
    )
    session = context.base.dependencies.open_session(config)
    primary: BaseException | None = None
    try:
        contract = collect_schema_contract(
            session,
            config=config,
            policy=_target_policy(context.target.attestation),
            managed_hba_receipt=hba,
            subject_user=config.user,
            allow_missing_helper=False,
        )
        sql = _ADMIN_ABSENCE_SQL.replace(
            "__ADMIN__", gate["temporary_schema_upgrade_admin_username"]
        )
        absence = session.query(sql, maximum_rows=1)
        if (
            not isinstance(absence, QueryResult)
            or absence.command_tag.upper() != "SELECT 1"
            or absence.columns
            != (
                "exact_admin_absent",
                "upgrade_admin_inventory_empty",
                "upgrade_admin_memberships_absent",
            )
            or absence.rows != (("t", "t", "t"),)
        ):
            _fail("schema_upgrade_database_admin_absence_invalid")
    except BaseException as exc:
        primary = exc
    finally:
        try:
            _close_session(session)
        except BaseException as close_error:
            if primary is None:
                primary = close_error
    if primary is not None:
        if isinstance(primary, SchemaUpgradeRuntimeError):
            raise primary
        raise SchemaUpgradeRuntimeError("schema_upgrade_cleanup_failed") from primary
    if contract.sha256 != context.target.sha256:
        _fail("schema_upgrade_post_cleanup_contract_invalid")
    _revalidate_stopped(context, "schema_upgrade_stopped_boundary_drifted")
    unsigned = {
        "schema": TERMINAL_SCHEMA,
        "ok": True,
        "state": "exact_target_admin_absent_services_stopped",
        "gate_sha256": gate["gate_sha256"],
        "release_revision": gate["release_revision"],
        "plan_sha256": gate["plan_sha256"],
        "apply_claim_sha256": claim["apply_claim_sha256"],
        "intermediate_sha256": intermediate["intermediate_sha256"],
        "cleanup_claim_sha256": cleanup["cleanup_claim_sha256"],
        "upgrade_receipt_sha256": intermediate["upgrade_receipt_sha256"],
        "target_contract_sha256": contract.sha256,
        "writer_managed_hba_receipt_sha256": hba.sha256,
        "canonical_truth_receipt_sha256": intermediate["upgrade_receipt"][
            "canonical_truth_receipt_sha256"
        ],
        "temporary_schema_upgrade_admin_absent": True,
        "database_admin_absence_exact": True,
        "services_stopped_sha256": gate["services_stopped_sha256"],
        "completed_at_unix": context.base.dependencies.now(),
        "secret_material_recorded": False,
    }
    return _hashed(unsigned, "terminal_sha256")


def run(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    _dependencies: _RuntimeDependencies | None = None,
) -> Mapping[str, Any]:
    dependencies = _dependencies or _RuntimeDependencies()
    context = _prepare_runtime(dependencies)
    return dependencies.protocol_runner(
        context.gate,
        apply_callback=lambda gate, claim, credential: _runtime_apply(
            context, gate, claim, credential
        ),
        cleanup_callback=lambda gate, claim, intermediate, cleanup: _runtime_cleanup(
            context, gate, claim, intermediate, cleanup
        ),
        input_stream=input_stream,
        output_stream=output_stream,
        now=context.base.dependencies.now,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments != ["upgrade"]:
            _fail("schema_upgrade_arguments_invalid")
        effective_user_id = getattr(os, "geteuid", None)
        if not callable(effective_user_id) or effective_user_id() != 0:
            _fail("schema_upgrade_root_required")
        run()
    except BaseException:
        print("canonical writer schema upgrade failed closed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "APPLY_MAGIC",
    "APPLY_OWNER_SSHSIG_NAMESPACE",
    "CLEANUP_MAGIC",
    "CLEANUP_OWNER_SSHSIG_NAMESPACE",
    "CLOUD_ABSENCE_SCHEMA",
    "CLOUD_AUTHORITY_SCHEMA",
    "FAILURE_SCHEMA",
    "INTERMEDIATE_SCHEMA",
    "OPAQUE_CREDENTIAL_BYTES",
    "TERMINAL_SCHEMA",
    "build_frame",
    "build_owner_apply_unsigned",
    "build_owner_cleanup_unsigned",
    "owner_apply_signature_payload",
    "owner_cleanup_signature_payload",
    "run",
    "run_protocol",
    "validate_failure_for_owner",
    "validate_gate_for_owner",
    "validate_intermediate_for_owner",
    "validate_terminal_for_owner",
]
