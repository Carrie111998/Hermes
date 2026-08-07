#!/usr/bin/env python3
"""Pure Stage C authority contract for successor production unit inputs.

The successor unit-input authority is published before the release-update
authority.  The release-update plan can therefore bind the unit-input
publication digest without a circular hash dependency.  Once both owner-
signed publications exist, :func:`derive_fixed_inputs` validates the reverse
binding and emits the exact v4 fixed-input document consumed by later stages.

This module only validates and builds canonical public documents.  It never
reads host state, changes ownership, stages files, or mutates a running
service.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import package_production_cutover_artifacts as v3
from scripts.canary import production_release_update_contract as release_update


PAYLOAD_SCHEMA = "muncho-production-release-unit-input-payload.v4"
PLAN_SCHEMA = "muncho-production-release-unit-input-plan.v4"
APPROVAL_SCHEMA = "muncho-production-release-unit-input-approval.v4"
PUBLICATION_SCHEMA = "muncho-production-release-unit-input-publication.v4"
FIXED_INPUTS_SCHEMA = "muncho-production-release-unit-inputs.v4"
APPROVAL_PURPOSE = "production_release_unit_inputs_v4"
PUBLICATION_ACTION = "publish-production-release-unit-input-authority-v4"
MAX_APPROVAL_LIFETIME_SECONDS = 3600
MAX_PLAN_AGE_AT_APPROVAL_SECONDS = 24 * 60 * 60
BUILDER_UID = 29104
BUILDER_GID = 29104

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_IDENTITY_FIELDS = frozenset({"user", "group", "uid", "gid"})
_ARTIFACT_DIGEST_FIELDS = frozenset(
    {
        "builder_terminal_receipt_sha256",
        "whole_tree_manifest_sha256",
        "candidate_seal_receipt_sha256",
        "runtime_dependency_manifest_sha256",
    }
)

# This list is intentionally explicit.  Stage C changes only the physical
# release ownership contract; all other v3 payload fields are projected
# byte-for-byte into the existing validator.
_V3_COMPATIBILITY_FIELDS = frozenset(
    {
        "database_ip",
        "target",
        "gateway",
        "writer",
        "projector",
        "routeback",
        "connector",
        "mac_ops",
        "browser",
        "worker",
        "writer_client_group",
        "worker_client_group",
        "operational_edge_identities",
        "operational_edge_socket_groups",
        "writer_capability_public_key_id",
        "discord_edge_receipt_public_key_id",
        "operational_edge_key_foundation_sha256",
        "operational_edge_receipt_public_key_ids",
        "discord_reconciliation_intent",
        "release_owner_uid",
        "release_owner_gid",
        "bwrap_sha256",
        "shell_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
    }
)
_PAYLOAD_FIELDS = (
    _V3_COMPATIBILITY_FIELDS
    | _ARTIFACT_DIGEST_FIELDS
    | frozenset(
        {
            "schema",
            "builder_identity",
            "reserved_runtime_uids",
            "reserved_runtime_gids",
        }
    )
)
_PLAN_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "predecessor_trust_sha256",
        "predecessor_authority_plan_sha256",
        "predecessor_authority_approval_sha256",
        "predecessor_fixed_inputs_sha256",
        "predecessor_activation_receipt_sha256",
        "release_revision",
        "unit_inputs",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "created_at_unix",
        "secret_material_recorded",
        "secret_digest_recorded",
        "plan_sha256",
    }
)
_APPROVAL_UNSIGNED_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "plan_sha256",
        "predecessor_revision",
        "predecessor_trust_sha256",
        "release_revision",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "nonce_sha256",
        "issued_at_unix",
        "expires_at_unix",
        "approved",
    }
)
_APPROVAL_FIELDS = _APPROVAL_UNSIGNED_FIELDS | frozenset(
    {"signature_ed25519_hex", "approval_sha256"}
)
_PUBLICATION_FIELDS = frozenset(
    {
        "schema",
        "action",
        "predecessor_revision",
        "predecessor_trust_sha256",
        "release_revision",
        "plan",
        "approval",
        "secret_material_recorded",
        "secret_digest_recorded",
        "publication_sha256",
    }
)
_FIXED_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "predecessor_trust_sha256",
        "predecessor_authority_plan_sha256",
        "predecessor_authority_approval_sha256",
        "predecessor_fixed_inputs_sha256",
        "predecessor_activation_receipt_sha256",
        "release_revision",
        "unit_input_authority_plan_sha256",
        "unit_input_authority_approval_sha256",
        "unit_input_authority_publication_sha256",
        "release_update_plan_sha256",
        "release_update_approval_sha256",
        "release_update_publication_sha256",
        "fixed_inputs_sha256",
    }
)
_FIXED_INPUT_FIELDS = (
    _FIXED_AUTHORITY_FIELDS | (_PAYLOAD_FIELDS - {"schema"})
)


class ProductionReleaseUnitInputsV4Error(RuntimeError):
    """Stable, secret-free v4 unit-input authority failure."""


def canonical_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used by every v4 digest."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_json_invalid"
        ) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(
    value: Any,
    fields: frozenset[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProductionReleaseUnitInputsV4Error(code)
    return dict(value)


def _self_hashed(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    raw = _mapping(value, fields, code)
    digest = raw[digest_field]
    unsigned = {key: item for key, item in raw.items() if key != digest_field}
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        raise ProductionReleaseUnitInputsV4Error(code)
    return raw


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProductionReleaseUnitInputsV4Error(code)
    return value


def _revision(value: Any, code: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ProductionReleaseUnitInputsV4Error(code)
    return value


def _builder_identity(value: Any) -> dict[str, Any]:
    raw = _mapping(
        value,
        _IDENTITY_FIELDS,
        "release_unit_inputs_v4_builder_identity_invalid",
    )
    if (
        raw.get("user") != "muncho-release-builder"
        or raw.get("group") != "muncho-release-builder"
        or type(raw.get("uid")) is not int
        or type(raw.get("gid")) is not int
        or raw["uid"] != BUILDER_UID
        or raw["gid"] != BUILDER_GID
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_builder_identity_invalid"
        )
    return raw


def _reserved_ids(
    value: Any,
    *,
    expected: list[int],
    code: str,
) -> list[int]:
    if (
        not isinstance(value, list)
        or any(type(item) is not int or item <= 0 for item in value)
        or value != sorted(value)
        or len(value) != len(set(value))
        or value != expected
    ):
        raise ProductionReleaseUnitInputsV4Error(code)
    return list(value)


def _runtime_ids(payload: Mapping[str, Any]) -> tuple[list[int], list[int]]:
    role_names = (
        "gateway",
        "writer",
        "projector",
        "routeback",
        "connector",
        "mac_ops",
        "browser",
        "worker",
    )
    identities = [payload[name] for name in role_names]
    identities.extend(payload["operational_edge_identities"].values())
    uids = sorted(int(item["uid"]) for item in identities)
    gids = [int(item["gid"]) for item in identities]
    gids.extend(
        int(item["gid"])
        for item in payload["operational_edge_socket_groups"].values()
    )
    gids.extend(
        (
            int(payload["writer_client_group"]["gid"]),
            int(payload["worker_client_group"]["gid"]),
        )
    )
    return uids, sorted(gids)


def project_payload_to_v3(value: Any) -> Mapping[str, Any]:
    """Project a v4 payload through the unchanged v3 payload validator.

    v3 represented the release owner as the gateway service identity.  The
    compatibility projection supplies that historical value only to v3 while
    the v4 document itself remains physically root-owned.
    """

    raw = _mapping(
        value,
        _PAYLOAD_FIELDS,
        "release_unit_inputs_v4_payload_invalid",
    )
    expected_v3_fields = set(_V3_COMPATIBILITY_FIELDS) | {"schema"}
    if expected_v3_fields != set(v3._UNIT_INPUT_PAYLOAD_FIELDS):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_v3_projection_invalid"
        )
    gateway = raw.get("gateway")
    if not isinstance(gateway, Mapping):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_v3_projection_invalid"
        )
    projected = {
        "schema": v3.UNIT_INPUT_PAYLOAD_SCHEMA,
        **{name: raw[name] for name in _V3_COMPATIBILITY_FIELDS},
        "release_owner_uid": gateway.get("uid"),
        "release_owner_gid": gateway.get("gid"),
    }
    try:
        return v3._unit_input_payload(projected)
    except (v3.PackagingError, KeyError, TypeError, ValueError) as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_v3_projection_invalid"
        ) from exc


def validate_payload(value: Any) -> Mapping[str, Any]:
    """Validate one strict v4 payload and its complete v3 projection."""

    raw = _mapping(
        value,
        _PAYLOAD_FIELDS,
        "release_unit_inputs_v4_payload_invalid",
    )
    projected = dict(project_payload_to_v3(raw))
    builder = _builder_identity(raw.get("builder_identity"))
    runtime_uids, runtime_gids = _runtime_ids(projected)
    if (
        raw.get("schema") != PAYLOAD_SCHEMA
        or raw.get("release_owner_uid") != 0
        or type(raw.get("release_owner_uid")) is not int
        or raw.get("release_owner_gid") != 0
        or type(raw.get("release_owner_gid")) is not int
        or len(runtime_uids) != release_update.EXPECTED_RUNTIME_UID_COUNT
        or len(runtime_gids) != release_update.EXPECTED_RESERVED_GID_COUNT
        or len(set(runtime_uids)) != len(runtime_uids)
        or len(set(runtime_gids)) != len(runtime_gids)
        or builder["uid"] in set(runtime_uids) | set(runtime_gids)
        or builder["gid"] in set(runtime_uids) | set(runtime_gids)
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_payload_invalid"
        )
    reserved_uids = _reserved_ids(
        raw.get("reserved_runtime_uids"),
        expected=runtime_uids,
        code="release_unit_inputs_v4_payload_invalid",
    )
    reserved_gids = _reserved_ids(
        raw.get("reserved_runtime_gids"),
        expected=runtime_gids,
        code="release_unit_inputs_v4_payload_invalid",
    )
    for field in _ARTIFACT_DIGEST_FIELDS:
        _sha256(raw.get(field), "release_unit_inputs_v4_payload_invalid")
    unchanged = _V3_COMPATIBILITY_FIELDS - {
        "release_owner_uid",
        "release_owner_gid",
    }
    if any(raw[name] != projected[name] for name in unchanged):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_v3_projection_invalid"
        )
    return {
        "schema": PAYLOAD_SCHEMA,
        **{
            name: projected[name]
            for name in _V3_COMPATIBILITY_FIELDS
            if name not in {"release_owner_uid", "release_owner_gid"}
        },
        "release_owner_uid": 0,
        "release_owner_gid": 0,
        "builder_identity": builder,
        "reserved_runtime_uids": reserved_uids,
        "reserved_runtime_gids": reserved_gids,
        **{name: raw[name] for name in _ARTIFACT_DIGEST_FIELDS},
    }


def build_payload(
    *,
    v3_payload: Mapping[str, Any],
    builder_identity: Mapping[str, Any],
    builder_terminal_receipt_sha256: str,
    whole_tree_manifest_sha256: str,
    candidate_seal_receipt_sha256: str,
    runtime_dependency_manifest_sha256: str,
) -> Mapping[str, Any]:
    """Build v4 from one already valid v3 payload without changing v3."""

    try:
        validated_v3 = dict(v3._unit_input_payload(v3_payload))
    except (v3.PackagingError, KeyError, TypeError, ValueError) as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_v3_projection_invalid"
        ) from exc
    runtime_uids, runtime_gids = _runtime_ids(validated_v3)
    payload = {
        "schema": PAYLOAD_SCHEMA,
        **{
            name: validated_v3[name]
            for name in _V3_COMPATIBILITY_FIELDS
            if name not in {"release_owner_uid", "release_owner_gid"}
        },
        "release_owner_uid": 0,
        "release_owner_gid": 0,
        "builder_identity": dict(builder_identity),
        "reserved_runtime_uids": runtime_uids,
        "reserved_runtime_gids": runtime_gids,
        "builder_terminal_receipt_sha256": (
            builder_terminal_receipt_sha256
        ),
        "whole_tree_manifest_sha256": whole_tree_manifest_sha256,
        "candidate_seal_receipt_sha256": candidate_seal_receipt_sha256,
        "runtime_dependency_manifest_sha256": (
            runtime_dependency_manifest_sha256
        ),
    }
    return validate_payload(payload)


def validate_plan(
    value: Any,
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_PLAN_FIELDS,
        digest_field="plan_sha256",
        code="release_unit_inputs_v4_plan_invalid",
    )
    try:
        trusted = release_update.validate_predecessor_trust(
            trusted_predecessor,
            expected_trust_sha256=expected_predecessor_trust_sha256,
        )
    except release_update.ProductionReleaseUpdateContractError as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_predecessor_trust_invalid"
        ) from exc
    predecessor = _revision(
        raw.get("predecessor_revision"),
        "release_unit_inputs_v4_plan_invalid",
    )
    revision = _revision(
        raw.get("release_revision"),
        "release_unit_inputs_v4_plan_invalid",
    )
    payload = validate_payload(raw.get("unit_inputs"))
    if (
        raw.get("schema") != PLAN_SCHEMA
        or predecessor == revision
        or predecessor[:12] == revision[:12]
        or predecessor != trusted["release_revision"]
        or raw.get("predecessor_trust_sha256")
        != trusted["trust_sha256"]
        or raw.get("predecessor_authority_plan_sha256")
        != trusted["authority_plan_sha256"]
        or raw.get("predecessor_authority_approval_sha256")
        != trusted["authority_approval_sha256"]
        or raw.get("predecessor_fixed_inputs_sha256")
        != trusted["fixed_inputs_sha256"]
        or raw.get("predecessor_activation_receipt_sha256")
        != trusted["activation_receipt_sha256"]
        or payload["discord_reconciliation_intent"]["release_revision"]
        != revision
        or raw.get("owner_subject_sha256")
        != trusted["owner_subject_sha256"]
        or raw.get("owner_public_key_ed25519_hex")
        != trusted["owner_public_key_ed25519_hex"]
        or raw.get("owner_key_id") != trusted["owner_key_id"]
        or type(raw.get("created_at_unix")) is not int
        or raw["created_at_unix"] <= 0
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_plan_invalid"
        )
    return {**raw, "unit_inputs": payload}


def build_plan(
    *,
    release_revision: str,
    unit_inputs: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    created_at_unix: int,
) -> Mapping[str, Any]:
    try:
        trusted = release_update.validate_predecessor_trust(
            trusted_predecessor,
            expected_trust_sha256=expected_predecessor_trust_sha256,
        )
    except release_update.ProductionReleaseUpdateContractError as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_predecessor_trust_invalid"
        ) from exc
    payload = validate_payload(unit_inputs)
    unsigned = {
        "schema": PLAN_SCHEMA,
        "predecessor_revision": trusted["release_revision"],
        "predecessor_trust_sha256": trusted["trust_sha256"],
        "predecessor_authority_plan_sha256": trusted[
            "authority_plan_sha256"
        ],
        "predecessor_authority_approval_sha256": trusted[
            "authority_approval_sha256"
        ],
        "predecessor_fixed_inputs_sha256": trusted["fixed_inputs_sha256"],
        "predecessor_activation_receipt_sha256": trusted[
            "activation_receipt_sha256"
        ],
        "release_revision": release_revision,
        "unit_inputs": dict(payload),
        "owner_subject_sha256": trusted["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": trusted[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": trusted["owner_key_id"],
        "created_at_unix": created_at_unix,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    plan = {**unsigned, "plan_sha256": sha256_bytes(canonical_bytes(unsigned))}
    return validate_plan(
        plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )


def approval_signature_payload(value: Mapping[str, Any]) -> bytes:
    fields = frozenset(value)
    if fields == _APPROVAL_UNSIGNED_FIELDS:
        unsigned = dict(value)
    elif fields == _APPROVAL_FIELDS:
        unsigned = {
            key: item
            for key, item in value.items()
            if key not in {"signature_ed25519_hex", "approval_sha256"}
        }
    else:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_approval_invalid"
        )
    return canonical_bytes(unsigned)


def validate_approval(
    value: Any,
    *,
    plan: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> Mapping[str, Any]:
    validated_plan = validate_plan(
        plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    raw = _self_hashed(
        value,
        fields=_APPROVAL_FIELDS,
        digest_field="approval_sha256",
        code="release_unit_inputs_v4_approval_invalid",
    )
    signature = raw.get("signature_ed25519_hex")
    if (
        raw.get("schema") != APPROVAL_SCHEMA
        or raw.get("purpose") != APPROVAL_PURPOSE
        or raw.get("plan_sha256") != validated_plan["plan_sha256"]
        or raw.get("predecessor_revision")
        != validated_plan["predecessor_revision"]
        or raw.get("predecessor_trust_sha256")
        != validated_plan["predecessor_trust_sha256"]
        or raw.get("release_revision")
        != validated_plan["release_revision"]
        or raw.get("owner_subject_sha256")
        != validated_plan["owner_subject_sha256"]
        or raw.get("owner_public_key_ed25519_hex")
        != validated_plan["owner_public_key_ed25519_hex"]
        or raw.get("owner_key_id") != validated_plan["owner_key_id"]
        or _SHA256.fullmatch(str(raw.get("nonce_sha256", ""))) is None
        or type(raw.get("issued_at_unix")) is not int
        or type(raw.get("expires_at_unix")) is not int
        or type(now_unix) is not int
        or not 0
        <= raw["issued_at_unix"] - validated_plan["created_at_unix"]
        <= MAX_PLAN_AGE_AT_APPROVAL_SECONDS
        or not raw["issued_at_unix"] <= now_unix < raw["expires_at_unix"]
        or not 1
        <= raw["expires_at_unix"] - raw["issued_at_unix"]
        <= MAX_APPROVAL_LIFETIME_SECONDS
        or raw.get("approved") is not True
        or not isinstance(signature, str)
        or _SIGNATURE.fullmatch(signature) is None
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_approval_invalid"
        )
    try:
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(
                str(validated_plan["owner_public_key_ed25519_hex"])
            )
        ).verify(
            bytes.fromhex(signature),
            approval_signature_payload(raw),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_approval_invalid"
        ) from exc
    return raw


def build_approval(
    *,
    plan: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    nonce_sha256: str,
    issued_at_unix: int,
    expires_at_unix: int,
    now_unix: int,
    signer: Callable[[bytes], bytes],
) -> Mapping[str, Any]:
    validated_plan = validate_plan(
        plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    unsigned = {
        "schema": APPROVAL_SCHEMA,
        "purpose": APPROVAL_PURPOSE,
        "plan_sha256": validated_plan["plan_sha256"],
        "predecessor_revision": validated_plan["predecessor_revision"],
        "predecessor_trust_sha256": validated_plan[
            "predecessor_trust_sha256"
        ],
        "release_revision": validated_plan["release_revision"],
        "owner_subject_sha256": validated_plan["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": validated_plan[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": validated_plan["owner_key_id"],
        "nonce_sha256": nonce_sha256,
        "issued_at_unix": issued_at_unix,
        "expires_at_unix": expires_at_unix,
        "approved": True,
    }
    try:
        signature = signer(approval_signature_payload(unsigned))
    except Exception as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_approval_signing_failed"
        ) from exc
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_approval_signing_failed"
        )
    signed = {**unsigned, "signature_ed25519_hex": signature.hex()}
    approval = {
        **signed,
        "approval_sha256": sha256_bytes(canonical_bytes(signed)),
    }
    return validate_approval(
        approval,
        plan=validated_plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )


def validate_publication(
    value: Any,
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_PUBLICATION_FIELDS,
        digest_field="publication_sha256",
        code="release_unit_inputs_v4_publication_invalid",
    )
    plan = validate_plan(
        raw.get("plan"),
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    approval = validate_approval(
        raw.get("approval"),
        plan=plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )
    if (
        raw.get("schema") != PUBLICATION_SCHEMA
        or raw.get("action") != PUBLICATION_ACTION
        or raw.get("predecessor_revision") != plan["predecessor_revision"]
        or raw.get("predecessor_trust_sha256")
        != plan["predecessor_trust_sha256"]
        or raw.get("release_revision") != plan["release_revision"]
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_publication_invalid"
        )
    return {**raw, "plan": plan, "approval": approval}


def build_publication(
    *,
    plan: Mapping[str, Any],
    approval: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> Mapping[str, Any]:
    validated_plan = validate_plan(
        plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    validated_approval = validate_approval(
        approval,
        plan=validated_plan,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )
    unsigned = {
        "schema": PUBLICATION_SCHEMA,
        "action": PUBLICATION_ACTION,
        "predecessor_revision": validated_plan["predecessor_revision"],
        "predecessor_trust_sha256": validated_plan[
            "predecessor_trust_sha256"
        ],
        "release_revision": validated_plan["release_revision"],
        "plan": dict(validated_plan),
        "approval": dict(validated_approval),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    publication = {
        **unsigned,
        "publication_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }
    return validate_publication(
        publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )


def _cross_bound_documents(
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    unit_publication = validate_publication(
        unit_input_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )
    try:
        update_publication = release_update.validate_publication(
            release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=now_unix,
        )
    except release_update.ProductionReleaseUpdateContractError as exc:
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_update_publication_invalid"
        ) from exc
    unit_plan = unit_publication["plan"]
    unit_payload = unit_plan["unit_inputs"]
    update_plan = update_publication["plan"]
    if (
        unit_publication["predecessor_revision"]
        != update_publication["predecessor_revision"]
        or unit_publication["release_revision"]
        != update_publication["release_revision"]
        or update_plan["successor_unit_input_publication_sha256"]
        != unit_publication["publication_sha256"]
        or update_plan["builder_identity"]
        != unit_payload["builder_identity"]
        or update_plan["release_owner"]
        != {
            "uid": unit_payload["release_owner_uid"],
            "gid": unit_payload["release_owner_gid"],
        }
        or update_plan["reserved_runtime_uids"]
        != unit_payload["reserved_runtime_uids"]
        or update_plan["reserved_runtime_gids"]
        != unit_payload["reserved_runtime_gids"]
        or any(
            update_plan[name] != unit_payload[name]
            for name in _ARTIFACT_DIGEST_FIELDS
        )
        or any(
            update_plan[name] != unit_plan[name]
            for name in (
                "predecessor_authority_plan_sha256",
                "predecessor_authority_approval_sha256",
                "predecessor_fixed_inputs_sha256",
                "predecessor_activation_receipt_sha256",
            )
        )
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_cross_binding_invalid"
        )
    return unit_publication, update_publication


def _validate_fixed_shape(value: Any) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_FIXED_INPUT_FIELDS,
        digest_field="fixed_inputs_sha256",
        code="release_unit_inputs_v4_fixed_inputs_invalid",
    )
    predecessor = _revision(
        raw.get("predecessor_revision"),
        "release_unit_inputs_v4_fixed_inputs_invalid",
    )
    revision = _revision(
        raw.get("release_revision"),
        "release_unit_inputs_v4_fixed_inputs_invalid",
    )
    for field in _FIXED_AUTHORITY_FIELDS - {
        "schema",
        "predecessor_revision",
        "release_revision",
        "fixed_inputs_sha256",
    }:
        _sha256(raw.get(field), "release_unit_inputs_v4_fixed_inputs_invalid")
    payload = validate_payload(
        {
            "schema": PAYLOAD_SCHEMA,
            **{
                name: raw[name]
                for name in _PAYLOAD_FIELDS
                if name != "schema"
            },
        }
    )
    if (
        raw.get("schema") != FIXED_INPUTS_SCHEMA
        or predecessor == revision
        or predecessor[:12] == revision[:12]
        or payload["discord_reconciliation_intent"]["release_revision"]
        != revision
    ):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_fixed_inputs_invalid"
        )
    return {
        **raw,
        **{name: payload[name] for name in _PAYLOAD_FIELDS if name != "schema"},
    }


def derive_fixed_inputs(
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> Mapping[str, Any]:
    """Derive the sole cross-bound fixed-input artifact without host writes."""

    unit_publication, update_publication = _cross_bound_documents(
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )
    unit_plan = unit_publication["plan"]
    unit_approval = unit_publication["approval"]
    update_plan = update_publication["plan"]
    update_approval = update_publication["approval"]
    payload = unit_plan["unit_inputs"]
    unsigned = {
        "schema": FIXED_INPUTS_SCHEMA,
        "predecessor_revision": unit_plan["predecessor_revision"],
        "predecessor_trust_sha256": unit_plan[
            "predecessor_trust_sha256"
        ],
        "predecessor_authority_plan_sha256": unit_plan[
            "predecessor_authority_plan_sha256"
        ],
        "predecessor_authority_approval_sha256": unit_plan[
            "predecessor_authority_approval_sha256"
        ],
        "predecessor_fixed_inputs_sha256": unit_plan[
            "predecessor_fixed_inputs_sha256"
        ],
        "predecessor_activation_receipt_sha256": unit_plan[
            "predecessor_activation_receipt_sha256"
        ],
        "release_revision": unit_plan["release_revision"],
        "unit_input_authority_plan_sha256": unit_plan["plan_sha256"],
        "unit_input_authority_approval_sha256": unit_approval[
            "approval_sha256"
        ],
        "unit_input_authority_publication_sha256": unit_publication[
            "publication_sha256"
        ],
        "release_update_plan_sha256": update_plan["plan_sha256"],
        "release_update_approval_sha256": update_approval[
            "approval_sha256"
        ],
        "release_update_publication_sha256": update_publication[
            "publication_sha256"
        ],
        **{
            name: payload[name]
            for name in _PAYLOAD_FIELDS
            if name != "schema"
        },
    }
    fixed = {
        **unsigned,
        "fixed_inputs_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }
    return _validate_fixed_shape(fixed)


def validate_fixed_inputs(
    value: Any,
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> Mapping[str, Any]:
    validated = _validate_fixed_shape(value)
    expected = derive_fixed_inputs(
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        now_unix=now_unix,
    )
    if dict(validated) != dict(expected):
        raise ProductionReleaseUnitInputsV4Error(
            "release_unit_inputs_v4_fixed_inputs_invalid"
        )
    return validated
