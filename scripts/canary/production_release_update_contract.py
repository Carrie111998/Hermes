#!/usr/bin/env python3
"""Owner-signed authority contract for one pinned production release update.

This module is intentionally data-only.  It validates canonical public
documents and an Ed25519 owner signature inherited from the already trusted
predecessor authority.  It never fetches, builds, imports, installs, starts, or
stops target release code.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PLAN_SCHEMA = "muncho-production-release-update-plan.v5"
APPROVAL_SCHEMA = "muncho-production-release-update-approval.v1"
PUBLICATION_SCHEMA = "muncho-production-release-update-publication.v1"
PREDECESSOR_TRUST_SCHEMA = (
    "muncho-production-release-update-predecessor-trust.v1"
)
APPROVAL_PURPOSE = "production_release_update"
PUBLICATION_ACTION = "activate-pinned-release"
RELEASE_ROOT = PurePosixPath("/opt/adventico-ai-platform/hermes-agent-releases")
MAX_APPROVAL_LIFETIME_SECONDS = 3600
MAX_PLAN_AGE_AT_APPROVAL_SECONDS = 24 * 60 * 60
INTERPRETER_RELATIVE_PATH = ".venv/bin/python"
ENTRYPOINT_RELATIVE_PATH = (
    "scripts/canary/production_release_update_entrypoint.py"
)
EXPECTED_RUNTIME_UID_COUNT = 19
EXPECTED_RESERVED_GID_COUNT = 32
BUILDER_UID = 29104
BUILDER_GID = 29104

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TREE_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_IDENTITY_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,62}$")

_PLAN_FIELDS = frozenset(
    {
        "schema",
        "predecessor_revision",
        "predecessor_authority_plan_sha256",
        "predecessor_authority_approval_sha256",
        "predecessor_fixed_inputs_sha256",
        "predecessor_activation_receipt_sha256",
        "release_revision",
        "release_root",
        "source_tree_oid",
        "source_v3_manifest_sha256",
        "builder_request_sha256",
        "builder_terminal_receipt_sha256",
        "candidate_seal_receipt_sha256",
        "whole_tree_manifest_sha256",
        "runtime_dependency_manifest_sha256",
        "uv_sha256",
        "interpreter_relative_path",
        "interpreter_sha256",
        "entrypoint_relative_path",
        "entrypoint_sha256",
        "host_inventory_sha256",
        "release_consumer_set_sha256",
        "host_artifact_manifest_sha256",
        "cron_artifact_index_sha256",
        "alias_artifact_index_sha256",
        "successor_unit_input_publication_sha256",
        "activation_plan_sha256",
        "rollback_plan_sha256",
        "builder_identity",
        "release_owner",
        "reserved_runtime_uids",
        "reserved_runtime_gids",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "created_at_unix",
        "secret_material_recorded",
        "secret_digest_recorded",
        "plan_sha256",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "plan_sha256",
        "predecessor_revision",
        "release_revision",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "nonce_sha256",
        "issued_at_unix",
        "expires_at_unix",
        "approved",
        "signature_ed25519_hex",
        "approval_sha256",
    }
)
_PUBLICATION_FIELDS = frozenset(
    {
        "schema",
        "action",
        "predecessor_revision",
        "release_revision",
        "plan",
        "approval",
        "secret_material_recorded",
        "secret_digest_recorded",
        "publication_sha256",
    }
)
_IDENTITY_FIELDS = frozenset({"user", "group", "uid", "gid"})
_RELEASE_OWNER_FIELDS = frozenset({"uid", "gid"})
_PREDECESSOR_TRUST_FIELDS = frozenset(
    {
        "schema",
        "release_revision",
        "authority_plan_sha256",
        "authority_approval_sha256",
        "fixed_inputs_sha256",
        "activation_receipt_sha256",
        "owner_subject_sha256",
        "owner_public_key_ed25519_hex",
        "owner_key_id",
        "secret_material_recorded",
        "secret_digest_recorded",
        "trust_sha256",
    }
)


class ProductionReleaseUpdateContractError(RuntimeError):
    """Stable, secret-free failure at the signed update authority boundary."""


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
        raise ProductionReleaseUpdateContractError(
            "release_update_json_invalid"
        ) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(
    value: Any,
    fields: frozenset[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProductionReleaseUpdateContractError(code)
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
        raise ProductionReleaseUpdateContractError(code)
    return raw


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProductionReleaseUpdateContractError(code)
    return value


def _revision(value: Any, code: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ProductionReleaseUpdateContractError(code)
    return value


def _relative_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProductionReleaseUpdateContractError(code)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProductionReleaseUpdateContractError(code)
    return value


def _builder_identity(value: Any) -> Mapping[str, Any]:
    raw = _mapping(
        value,
        _IDENTITY_FIELDS,
        "release_update_builder_identity_invalid",
    )
    if (
        raw.get("user") != "muncho-release-builder"
        or raw.get("group") != "muncho-release-builder"
        or _IDENTITY_NAME.fullmatch(str(raw.get("user", ""))) is None
        or _IDENTITY_NAME.fullmatch(str(raw.get("group", ""))) is None
        or type(raw.get("uid")) is not int
        or type(raw.get("gid")) is not int
        or raw["uid"] != BUILDER_UID
        or raw["gid"] != BUILDER_GID
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_builder_identity_invalid"
        )
    return raw


def _release_owner(value: Any) -> Mapping[str, Any]:
    raw = _mapping(
        value,
        _RELEASE_OWNER_FIELDS,
        "release_update_owner_invalid",
    )
    if raw != {"uid": 0, "gid": 0}:
        raise ProductionReleaseUpdateContractError(
            "release_update_owner_invalid"
        )
    return raw


def validate_predecessor_trust(
    value: Any,
    *,
    expected_trust_sha256: str,
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_PREDECESSOR_TRUST_FIELDS,
        digest_field="trust_sha256",
        code="release_update_predecessor_trust_invalid",
    )
    subject = raw.get("owner_subject_sha256")
    public = raw.get("owner_public_key_ed25519_hex")
    key_id = raw.get("owner_key_id")
    if (
        _SHA256.fullmatch(str(expected_trust_sha256)) is None
        or raw.get("trust_sha256") != expected_trust_sha256
        or raw.get("schema") != PREDECESSOR_TRUST_SCHEMA
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or any(
            _SHA256.fullmatch(str(raw.get(name, ""))) is None
            for name in (
                "authority_plan_sha256",
                "authority_approval_sha256",
                "fixed_inputs_sha256",
                "activation_receipt_sha256",
            )
        )
        or not isinstance(subject, str)
        or _SHA256.fullmatch(subject) is None
        or not isinstance(public, str)
        or _SHA256.fullmatch(public) is None
        or not isinstance(key_id, str)
        or _SHA256.fullmatch(key_id) is None
        or key_id != sha256_bytes(bytes.fromhex(public))
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_predecessor_trust_invalid"
        )
    return raw


def build_predecessor_trust(
    *,
    release_revision: str,
    authority_plan_sha256: str,
    authority_approval_sha256: str,
    fixed_inputs_sha256: str,
    activation_receipt_sha256: str,
    owner_subject_sha256: str,
    owner_public_key_ed25519_hex: str,
    owner_key_id: str,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": PREDECESSOR_TRUST_SCHEMA,
        "release_revision": release_revision,
        "authority_plan_sha256": authority_plan_sha256,
        "authority_approval_sha256": authority_approval_sha256,
        "fixed_inputs_sha256": fixed_inputs_sha256,
        "activation_receipt_sha256": activation_receipt_sha256,
        "owner_subject_sha256": owner_subject_sha256,
        "owner_public_key_ed25519_hex": owner_public_key_ed25519_hex,
        "owner_key_id": owner_key_id,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return validate_predecessor_trust(
        {**unsigned, "trust_sha256": sha256_bytes(canonical_bytes(unsigned))},
        expected_trust_sha256=sha256_bytes(canonical_bytes(unsigned)),
    )


def _reserved_ids(
    value: Any,
    *,
    expected_count: int,
    code: str,
) -> tuple[int, ...]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != expected_count
        or any(type(item) is not int or item <= 0 for item in value)
        or list(value) != sorted(value)
        or len(set(value)) != len(value)
    ):
        raise ProductionReleaseUpdateContractError(code)
    return tuple(value)


def expected_release_root(revision: str) -> str:
    _revision(revision, "release_update_revision_invalid")
    return str(RELEASE_ROOT / f"hermes-agent-{revision[:12]}")


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
        code="release_update_plan_invalid",
    )
    trusted = validate_predecessor_trust(
        trusted_predecessor,
        expected_trust_sha256=expected_predecessor_trust_sha256,
    )
    predecessor = _revision(
        raw.get("predecessor_revision"),
        "release_update_plan_invalid",
    )
    revision = _revision(
        raw.get("release_revision"),
        "release_update_plan_invalid",
    )
    builder = _builder_identity(raw.get("builder_identity"))
    owner = _release_owner(raw.get("release_owner"))
    runtime_uids = _reserved_ids(
        raw.get("reserved_runtime_uids"),
        expected_count=EXPECTED_RUNTIME_UID_COUNT,
        code="release_update_plan_invalid",
    )
    runtime_gids = _reserved_ids(
        raw.get("reserved_runtime_gids"),
        expected_count=EXPECTED_RESERVED_GID_COUNT,
        code="release_update_plan_invalid",
    )
    digest_fields = (
        "predecessor_authority_plan_sha256",
        "predecessor_authority_approval_sha256",
        "predecessor_fixed_inputs_sha256",
        "predecessor_activation_receipt_sha256",
        "source_v3_manifest_sha256",
        "builder_request_sha256",
        "builder_terminal_receipt_sha256",
        "candidate_seal_receipt_sha256",
        "whole_tree_manifest_sha256",
        "runtime_dependency_manifest_sha256",
        "uv_sha256",
        "interpreter_sha256",
        "entrypoint_sha256",
        "host_inventory_sha256",
        "release_consumer_set_sha256",
        "host_artifact_manifest_sha256",
        "cron_artifact_index_sha256",
        "alias_artifact_index_sha256",
        "successor_unit_input_publication_sha256",
        "activation_plan_sha256",
        "rollback_plan_sha256",
    )
    for field in digest_fields:
        _sha256(raw.get(field), "release_update_plan_invalid")
    source_tree_oid = raw.get("source_tree_oid")
    if (
        raw.get("schema") != PLAN_SCHEMA
        or predecessor == revision
        or predecessor[:12] == revision[:12]
        or predecessor != trusted["release_revision"]
        or raw.get("predecessor_authority_plan_sha256")
        != trusted["authority_plan_sha256"]
        or raw.get("predecessor_authority_approval_sha256")
        != trusted["authority_approval_sha256"]
        or raw.get("predecessor_fixed_inputs_sha256")
        != trusted["fixed_inputs_sha256"]
        or raw.get("predecessor_activation_receipt_sha256")
        != trusted["activation_receipt_sha256"]
        or raw.get("release_root") != expected_release_root(revision)
        or not isinstance(source_tree_oid, str)
        or _TREE_OID.fullmatch(source_tree_oid) is None
        or raw.get("owner_subject_sha256")
        != trusted["owner_subject_sha256"]
        or raw.get("owner_public_key_ed25519_hex")
        != trusted["owner_public_key_ed25519_hex"]
        or raw.get("owner_key_id") != trusted["owner_key_id"]
        or type(raw.get("created_at_unix")) is not int
        or raw["created_at_unix"] <= 0
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
        or builder["uid"] == owner["uid"]
        or builder["gid"] == owner["gid"]
        or builder["uid"] in runtime_uids
        or builder["gid"] in runtime_gids
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_plan_invalid"
        )
    interpreter = _relative_path(
        raw.get("interpreter_relative_path"),
        "release_update_plan_invalid",
    )
    entrypoint = _relative_path(
        raw.get("entrypoint_relative_path"),
        "release_update_plan_invalid",
    )
    if (
        interpreter != INTERPRETER_RELATIVE_PATH
        or entrypoint != ENTRYPOINT_RELATIVE_PATH
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_plan_invalid"
        )
    return {
        **raw,
        "builder_identity": builder,
        "release_owner": owner,
        "reserved_runtime_uids": list(runtime_uids),
        "reserved_runtime_gids": list(runtime_gids),
        "interpreter_relative_path": interpreter,
        "entrypoint_relative_path": entrypoint,
    }


def approval_signature_payload(value: Mapping[str, Any]) -> bytes:
    raw = _mapping(
        value,
        _APPROVAL_FIELDS,
        "release_update_approval_invalid",
    )
    return canonical_bytes(
        {
            key: item
            for key, item in raw.items()
            if key not in {"signature_ed25519_hex", "approval_sha256"}
        }
    )


def validate_approval(
    value: Any,
    *,
    plan: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_APPROVAL_FIELDS,
        digest_field="approval_sha256",
        code="release_update_approval_invalid",
    )
    signature = raw.get("signature_ed25519_hex")
    if (
        raw.get("schema") != APPROVAL_SCHEMA
        or raw.get("purpose") != APPROVAL_PURPOSE
        or raw.get("plan_sha256") != plan.get("plan_sha256")
        or raw.get("predecessor_revision")
        != plan.get("predecessor_revision")
        or raw.get("release_revision") != plan.get("release_revision")
        or raw.get("owner_subject_sha256")
        != plan.get("owner_subject_sha256")
        or raw.get("owner_public_key_ed25519_hex")
        != plan.get("owner_public_key_ed25519_hex")
        or raw.get("owner_key_id") != plan.get("owner_key_id")
        or _SHA256.fullmatch(str(raw.get("nonce_sha256", ""))) is None
        or type(raw.get("issued_at_unix")) is not int
        or type(raw.get("expires_at_unix")) is not int
        or type(plan.get("created_at_unix")) is not int
        or not 0
        <= raw["issued_at_unix"] - plan["created_at_unix"]
        <= MAX_PLAN_AGE_AT_APPROVAL_SECONDS
        or not raw["issued_at_unix"] <= now_unix < raw["expires_at_unix"]
        or not 1
        <= raw["expires_at_unix"] - raw["issued_at_unix"]
        <= MAX_APPROVAL_LIFETIME_SECONDS
        or raw.get("approved") is not True
        or not isinstance(signature, str)
        or _SIGNATURE.fullmatch(signature) is None
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_approval_invalid"
        )
    try:
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(str(plan["owner_public_key_ed25519_hex"]))
        ).verify(
            bytes.fromhex(signature),
            approval_signature_payload(raw),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ProductionReleaseUpdateContractError(
            "release_update_approval_invalid"
        ) from exc
    return raw


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
        code="release_update_publication_invalid",
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
        now_unix=now_unix,
    )
    if (
        raw.get("schema") != PUBLICATION_SCHEMA
        or raw.get("action") != PUBLICATION_ACTION
        or raw.get("predecessor_revision")
        != plan["predecessor_revision"]
        or raw.get("release_revision") != plan["release_revision"]
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateContractError(
            "release_update_publication_invalid"
        )
    return {**raw, "plan": plan, "approval": approval}


def build_plan(
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    values: Mapping[str, Any],
) -> Mapping[str, Any]:
    trusted = validate_predecessor_trust(
        trusted_predecessor,
        expected_trust_sha256=expected_predecessor_trust_sha256,
    )
    unsigned = {
        "schema": PLAN_SCHEMA,
        **dict(values),
        "predecessor_revision": trusted["release_revision"],
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
        "owner_subject_sha256": trusted["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": trusted[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": trusted["owner_key_id"],
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
        now_unix=now_unix,
    )
    unsigned = {
        "schema": PUBLICATION_SCHEMA,
        "action": PUBLICATION_ACTION,
        "predecessor_revision": validated_plan["predecessor_revision"],
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
