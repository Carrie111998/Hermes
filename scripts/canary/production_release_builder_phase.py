#!/usr/bin/env python3
"""Offline, unprivileged builder phase for a pinned production release.

The phase consumes one root-published, immutable input directory and creates
one builder-owned candidate.  It deliberately has no Git client, package
index, network, source-build, or candidate-import path.  A root phase must
later prove the builder UID process-free and publish the candidate with
``production_release_builder_runtime``.

Retries are intentionally fail-closed: the output directory must be empty.
Any prior object, including a terminal receipt from an earlier invocation,
requires a root-controlled cleanup or a new job id.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import unicodedata
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, NoReturn, Protocol, Sequence

from scripts.canary import production_release_builder_runtime as builder


REQUEST_SCHEMA = "muncho-production-release-builder-request.v1"
UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-builder-request.v1"
)
SOURCE_V3_MANIFEST_SCHEMA = "muncho-production-release-source-v3-manifest.v1"
RUNTIME_DEPENDENCY_MANIFEST_SCHEMA = (
    "muncho-production-release-runtime-wheel-manifest.v1"
)
PAYLOAD_MANIFEST_SCHEMA = "muncho-production-release-builder-payload-manifest.v1"
TERMINAL_RECEIPT_SCHEMA = "muncho-production-release-builder-terminal-receipt.v1"
UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-builder-terminal-receipt.v1"
)

PRODUCTION_JOB_ROOT = Path("/var/lib/muncho-release-updates")
REQUEST_NAME = "request.json"
SOURCE_MANIFEST_NAME = "source-v3-manifest.json"
RUNTIME_MANIFEST_NAME = "runtime-dependency-manifest.json"
TREE_LISTING_NAME = "source-tree.ls"
SOURCE_BLOB_DIRECTORY_NAME = "source-blobs"
RUNTIME_WHEEL_DIRECTORY_NAME = "runtime-wheels"
UV_NAME = "uv"
CANDIDATE_NAME = "candidate"
RETAINED_WHEEL_DIRECTORY_NAME = ".builder-wheelhouse"
PAYLOAD_MANIFEST_NAME = "production-release-builder-payload-manifest.json"
TERMINAL_RECEIPT_NAME = "production-release-builder-terminal-receipt.json"
INTERPRETER_RELATIVE_PATH = ".venv/bin/python"
ENTRYPOINT_RELATIVE_PATH = "scripts/canary/production_release_update_entrypoint.py"
UNIT_INPUT_ROTATION_STAGER_PURPOSE = "unit-input-rotation-stager"
UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH = (
    "scripts/canary/production_cutover_unit_input_rotation.py"
)
BUILDER_USER = "muncho-release-builder"
BUILDER_GROUP = "muncho-release-builder"
BUILDER_UID = builder.BUILDER_UID
BUILDER_GID = builder.BUILDER_GID

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_UV_BYTES = 512 * 1024 * 1024
MAX_PYTHON_BYTES = 512 * 1024 * 1024
MAX_WHEELS = 10_000
MAX_SOURCE_BLOBS = builder.MAX_GIT_TREE_ENTRIES
MAX_COMMAND_SECONDS = 3600

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TREE_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_JOB_ID = re.compile(r"^[0-9a-f]{40}$")
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,239}\.whl$")
_PYTHON_PATH = re.compile(r"^/usr/bin/python3\.[0-9]{1,3}$")

_REQUEST_FIELDS = frozenset({
    "schema",
    "job_id",
    "release_revision",
    "source_tree_oid",
    "source_v3_manifest_name",
    "source_v3_manifest_sha256",
    "runtime_dependency_manifest_name",
    "runtime_dependency_manifest_sha256",
    "uv_name",
    "uv_sha256",
    "uv_size",
    "python_executable_path",
    "python_executable_sha256",
    "python_executable_size",
    "candidate_name",
    "interpreter_relative_path",
    "entrypoint_relative_path",
    "builder_identity",
    "resume_policy",
    "secret_material_recorded",
    "secret_digest_recorded",
    "request_sha256",
})
_PURPOSE_BOUND_REQUEST_FIELDS = _REQUEST_FIELDS | {"purpose"}
_SOURCE_MANIFEST_FIELDS = frozenset({
    "schema",
    "release_revision",
    "source_tree_oid",
    "object_format",
    "tree_listing_name",
    "tree_listing_sha256",
    "tree_listing_size",
    "tree_entry_count",
    "blob_directory_name",
    "blobs",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})
_BLOB_FIELDS = frozenset({"object_id", "filename", "sha256", "size"})
_RUNTIME_MANIFEST_FIELDS = frozenset({
    "schema",
    "release_revision",
    "wheel_directory_name",
    "wheels",
    "installation",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})
_WHEEL_FIELDS = frozenset({"filename", "sha256", "size"})
_INSTALLATION = {
    "installer": "uv-pip-install",
    "offline": True,
    "no_index": True,
    "no_build": True,
    "only_binary": ":all:",
    "no_dependencies": True,
    "exact": True,
    "link_mode": "copy",
    "compile_bytecode": False,
}
_PAYLOAD_MANIFEST_FIELDS = frozenset({
    "schema",
    "release_revision",
    "source_tree_oid",
    "builder_identity",
    "payload_entries",
    "payload_entry_count",
    "payload_bytes",
    "payload_tree_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})
_TERMINAL_RECEIPT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "candidate_name",
    "source_tree_oid",
    "builder_request_sha256",
    "builder_request_identity_sha256",
    "source_v3_manifest_sha256",
    "source_v3_manifest_identity_sha256",
    "runtime_dependency_manifest_sha256",
    "runtime_dependency_manifest_identity_sha256",
    "uv_sha256",
    "python_executable_sha256",
    "source_materialization_sha256",
    "retained_wheels_sha256",
    "payload_manifest_name",
    "payload_manifest_sha256",
    "payload_manifest_file_sha256",
    "payload_tree_sha256",
    "interpreter_relative_path",
    "interpreter_sha256",
    "entrypoint_relative_path",
    "entrypoint_sha256",
    "venv_argv_sha256",
    "install_argv_sha256",
    "command_environment_sha256",
    "builder_identity",
    "resume_policy",
    "terminal",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_PURPOSE_BOUND_TERMINAL_RECEIPT_FIELDS = _TERMINAL_RECEIPT_FIELDS | {"purpose"}
_BUILDER_IDENTITY = {
    "user": BUILDER_USER,
    "group": BUILDER_GROUP,
    "uid": BUILDER_UID,
    "gid": BUILDER_GID,
}
_RESERVED_SOURCE_ROOTS = frozenset({
    ".venv",
    RETAINED_WHEEL_DIRECTORY_NAME,
    PAYLOAD_MANIFEST_NAME,
    TERMINAL_RECEIPT_NAME,
    builder.MANIFEST_NAME,
    builder.RECEIPT_NAME,
})
_INPUT_ROOT_NAMES = frozenset({
    REQUEST_NAME,
    SOURCE_MANIFEST_NAME,
    RUNTIME_MANIFEST_NAME,
    TREE_LISTING_NAME,
    SOURCE_BLOB_DIRECTORY_NAME,
    RUNTIME_WHEEL_DIRECTORY_NAME,
    UV_NAME,
})
_ROOT_FILE_MODES = frozenset({0o400, 0o440, 0o444})
_ROOT_EXECUTABLE_MODES = frozenset({0o500, 0o550, 0o555})
_ROOT_DIRECTORY_MODES = frozenset({0o500, 0o550, 0o555, 0o700, 0o750, 0o755})
_BUILDER_OUTPUT_DIRECTORY_MODES = frozenset({0o700, 0o750})


class ProductionReleaseBuilderPhaseError(RuntimeError):
    """Stable and deliberately secret-free builder phase failure."""


def _fail(code: str, exc: BaseException | None = None) -> NoReturn:
    del exc
    raise ProductionReleaseBuilderPhaseError(code) from None


def _read_posix_identity(name: Literal["geteuid", "getegid"]) -> int:
    reader = getattr(os, name, None)
    if not callable(reader):
        _fail("release_builder_phase_posix_identity_unavailable")
    try:
        value = reader()
    except (OSError, TypeError, ValueError) as exc:
        _fail("release_builder_phase_posix_identity_unavailable", exc)
    if type(value) is not int or value < 0:
        _fail("release_builder_phase_posix_identity_unavailable")
    return value


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("release_builder_phase_json_invalid", exc)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _self_hashed(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    raw = dict(value)
    digest = raw.get(digest_field)
    unsigned = {key: item for key, item in raw.items() if key != digest_field}
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        _fail(code)
    return raw


def _decode_canonical_document(raw: bytes) -> Mapping[str, Any]:
    if (
        not isinstance(raw, bytes)
        or not 1 < len(raw) <= MAX_JSON_BYTES
        or not raw.endswith(b"\n")
        or b"\n" in raw[:-1]
    ):
        _fail("release_builder_phase_document_invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, item in items:
            if not isinstance(name, str) or name in result:
                raise ValueError("duplicate")
            result[name] = item
        return result

    try:
        value = json.loads(
            raw[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _fail("release_builder_phase_document_invalid", exc)
    if not isinstance(value, Mapping) or raw != canonical_bytes(value) + b"\n":
        _fail("release_builder_phase_document_invalid")
    return dict(value)


def _read_held(held: builder.HeldRegularFile) -> bytes:
    try:
        raw = os.pread(held.descriptor, held.identity.size, 0)
    except OSError as exc:
        _fail("release_builder_phase_input_unavailable", exc)
    if len(raw) != held.identity.size:
        _fail("release_builder_phase_input_changed")
    held.assert_stable()
    return raw


def _relative_path(value: Any, expected: str, code: str) -> str:
    if value != expected or not isinstance(value, str):
        _fail(code)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail(code)
    return value


def validate_request(
    value: Any,
    *,
    expected_job_id: str,
) -> Mapping[str, Any]:
    request_schema = value.get("schema") if isinstance(value, Mapping) else None
    if request_schema == UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA:
        request_fields = _PURPOSE_BOUND_REQUEST_FIELDS
        expected_entrypoint = UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
        expected_purpose: str | None = UNIT_INPUT_ROTATION_STAGER_PURPOSE
    else:
        request_fields = _REQUEST_FIELDS
        expected_entrypoint = ENTRYPOINT_RELATIVE_PATH
        expected_purpose = None
    raw = _self_hashed(
        value,
        fields=request_fields,
        digest_field="request_sha256",
        code="release_builder_phase_request_invalid",
    )
    identity = raw.get("builder_identity")
    if (
        raw.get("schema")
        not in {REQUEST_SCHEMA, UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA}
        or (
            expected_purpose is not None
            and raw.get("purpose") != expected_purpose
        )
        or _JOB_ID.fullmatch(str(expected_job_id)) is None
        or raw.get("job_id") != expected_job_id
        or raw.get("release_revision") != expected_job_id
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or _TREE_OID.fullmatch(str(raw.get("source_tree_oid", ""))) is None
        or raw.get("source_v3_manifest_name") != SOURCE_MANIFEST_NAME
        or raw.get("runtime_dependency_manifest_name") != RUNTIME_MANIFEST_NAME
        or raw.get("uv_name") != UV_NAME
        or raw.get("candidate_name") != CANDIDATE_NAME
        or any(
            _SHA256.fullmatch(str(raw.get(name, ""))) is None
            for name in (
                "source_v3_manifest_sha256",
                "runtime_dependency_manifest_sha256",
                "uv_sha256",
                "python_executable_sha256",
            )
        )
        or type(raw.get("uv_size")) is not int
        or not 0 < raw["uv_size"] <= MAX_UV_BYTES
        or type(raw.get("python_executable_size")) is not int
        or not 0 < raw["python_executable_size"] <= MAX_PYTHON_BYTES
        or _PYTHON_PATH.fullmatch(str(raw.get("python_executable_path", ""))) is None
        or identity != _BUILDER_IDENTITY
        or raw.get("resume_policy") != "reject-nonempty-output-requires-root-cleanup"
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail("release_builder_phase_request_invalid")
    _relative_path(
        raw.get("interpreter_relative_path"),
        INTERPRETER_RELATIVE_PATH,
        "release_builder_phase_request_invalid",
    )
    _relative_path(
        raw.get("entrypoint_relative_path"),
        expected_entrypoint,
        "release_builder_phase_request_invalid",
    )
    return raw


def validate_source_manifest(
    value: Any,
    *,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_SOURCE_MANIFEST_FIELDS,
        digest_field="manifest_sha256",
        code="release_builder_phase_source_manifest_invalid",
    )
    blobs = raw.get("blobs")
    object_format = raw.get("object_format")
    oid_pattern = re.compile(
        r"^[0-9a-f]{40}$" if object_format == "sha1" else r"^[0-9a-f]{64}$"
    )
    if (
        raw.get("schema") != SOURCE_V3_MANIFEST_SCHEMA
        or raw.get("release_revision") != request["release_revision"]
        or raw.get("source_tree_oid") != request["source_tree_oid"]
        or object_format not in {"sha1", "sha256"}
        or len(str(raw.get("source_tree_oid", "")))
        != (40 if object_format == "sha1" else 64)
        or raw.get("tree_listing_name") != TREE_LISTING_NAME
        or _SHA256.fullmatch(str(raw.get("tree_listing_sha256", ""))) is None
        or type(raw.get("tree_listing_size")) is not int
        or not 0 < raw["tree_listing_size"] <= builder.MAX_GIT_TREE_BYTES
        or type(raw.get("tree_entry_count")) is not int
        or not 0 < raw["tree_entry_count"] <= builder.MAX_GIT_TREE_ENTRIES
        or raw.get("blob_directory_name") != SOURCE_BLOB_DIRECTORY_NAME
        or not isinstance(blobs, list)
        or not 0 < len(blobs) <= MAX_SOURCE_BLOBS
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail("release_builder_phase_source_manifest_invalid")
    normalized: list[dict[str, Any]] = []
    for item in blobs:
        if not isinstance(item, Mapping) or set(item) != _BLOB_FIELDS:
            _fail("release_builder_phase_source_manifest_invalid")
        record = dict(item)
        oid = record.get("object_id")
        if (
            not isinstance(oid, str)
            or oid_pattern.fullmatch(oid) is None
            or record.get("filename") != f"{oid}.blob"
            or _SHA256.fullmatch(str(record.get("sha256", ""))) is None
            or type(record.get("size")) is not int
            or not 0 <= record["size"] <= builder.MAX_BLOB_BYTES
        ):
            _fail("release_builder_phase_source_manifest_invalid")
        normalized.append(record)
    if normalized != sorted(normalized, key=lambda item: str(item["object_id"])) or len({
        str(item["object_id"]) for item in normalized
    }) != len(normalized):
        _fail("release_builder_phase_source_manifest_invalid")
    return raw


def validate_runtime_manifest(
    value: Any,
    *,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_RUNTIME_MANIFEST_FIELDS,
        digest_field="manifest_sha256",
        code="release_builder_phase_runtime_manifest_invalid",
    )
    wheels = raw.get("wheels")
    if (
        raw.get("schema") != RUNTIME_DEPENDENCY_MANIFEST_SCHEMA
        or raw.get("release_revision") != request["release_revision"]
        or raw.get("wheel_directory_name") != RUNTIME_WHEEL_DIRECTORY_NAME
        or not isinstance(wheels, list)
        or not 0 < len(wheels) <= MAX_WHEELS
        or raw.get("installation") != _INSTALLATION
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail("release_builder_phase_runtime_manifest_invalid")
    normalized: list[dict[str, Any]] = []
    for item in wheels:
        if not isinstance(item, Mapping) or set(item) != _WHEEL_FIELDS:
            _fail("release_builder_phase_runtime_manifest_invalid")
        record = dict(item)
        filename = record.get("filename")
        if (
            not isinstance(filename, str)
            or _WHEEL_NAME.fullmatch(filename) is None
            or _SHA256.fullmatch(str(record.get("sha256", ""))) is None
            or type(record.get("size")) is not int
            or not 0 < record["size"] <= builder.MAX_WHEEL_BYTES
        ):
            _fail("release_builder_phase_runtime_manifest_invalid")
        normalized.append(record)
    if normalized != sorted(normalized, key=lambda item: str(item["filename"])) or len({
        str(item["filename"]) for item in normalized
    }) != len(normalized):
        _fail("release_builder_phase_runtime_manifest_invalid")
    return raw


def _validated_payload_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("release_builder_phase_payload_manifest_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail("release_builder_phase_payload_manifest_invalid")
    for part in path.parts:
        _validate_component(part)
    return value


def validate_payload_manifest(value: Any) -> Mapping[str, Any]:
    """Validate the complete builder-owned payload projection."""

    raw = _self_hashed(
        value,
        fields=_PAYLOAD_MANIFEST_FIELDS,
        digest_field="manifest_sha256",
        code="release_builder_phase_payload_manifest_invalid",
    )
    entries = raw.get("payload_entries")
    if (
        raw.get("schema") != PAYLOAD_MANIFEST_SCHEMA
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or _TREE_OID.fullmatch(str(raw.get("source_tree_oid", ""))) is None
        or raw.get("builder_identity") != _BUILDER_IDENTITY
        or not isinstance(entries, list)
        or not entries
        or len(entries) > builder.MAX_RELEASE_ENTRIES
        or raw.get("payload_entry_count") != len(entries)
        or type(raw.get("payload_bytes")) is not int
        or not 0 <= raw["payload_bytes"] <= builder.MAX_RELEASE_BYTES
        or _SHA256.fullmatch(str(raw.get("payload_tree_sha256", ""))) is None
        or raw.get("payload_tree_sha256") != sha256_bytes(canonical_bytes(entries))
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        _fail("release_builder_phase_payload_manifest_invalid")
    observed_bytes = 0
    observed_paths: list[str] = []
    for item in entries:
        if not isinstance(item, Mapping):
            _fail("release_builder_phase_payload_manifest_invalid")
        path = _validated_payload_path(item.get("path"))
        kind = item.get("kind")
        common = {
            "path",
            "kind",
            "mode",
            "uid",
            "gid",
            "xattrs",
        }
        expected_fields = common if kind == "directory" else common | {"size", "sha256"}
        if (
            set(item) != expected_fields
            or kind not in {"directory", "file"}
            or item.get("mode") not in {"0444", "0555"}
            or (kind == "directory" and item.get("mode") != "0555")
            or item.get("uid") != BUILDER_UID
            or item.get("gid") != BUILDER_GID
            or item.get("xattrs") != []
        ):
            _fail("release_builder_phase_payload_manifest_invalid")
        if kind == "file":
            size = item.get("size")
            if (
                type(size) is not int
                or size < 0
                or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            ):
                _fail("release_builder_phase_payload_manifest_invalid")
            observed_bytes += size
        observed_paths.append(path)
    if (
        observed_paths != sorted(observed_paths)
        or len(set(observed_paths)) != len(observed_paths)
        or observed_bytes != raw["payload_bytes"]
    ):
        _fail("release_builder_phase_payload_manifest_invalid")
    return raw


def validate_terminal_receipt(value: Any) -> Mapping[str, Any]:
    """Validate the final builder receipt without trusting its producer."""

    receipt_schema = value.get("schema") if isinstance(value, Mapping) else None
    if receipt_schema == UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA:
        receipt_fields = _PURPOSE_BOUND_TERMINAL_RECEIPT_FIELDS
        expected_entrypoint = UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
        expected_purpose: str | None = UNIT_INPUT_ROTATION_STAGER_PURPOSE
    else:
        receipt_fields = _TERMINAL_RECEIPT_FIELDS
        expected_entrypoint = ENTRYPOINT_RELATIVE_PATH
        expected_purpose = None
    raw = _self_hashed(
        value,
        fields=receipt_fields,
        digest_field="receipt_sha256",
        code="release_builder_phase_terminal_receipt_invalid",
    )
    digest_fields = receipt_fields - {
        "schema",
        "purpose",
        "release_revision",
        "candidate_name",
        "source_tree_oid",
        "payload_manifest_name",
        "interpreter_relative_path",
        "entrypoint_relative_path",
        "builder_identity",
        "resume_policy",
        "terminal",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    if (
        raw.get("schema")
        not in {
            TERMINAL_RECEIPT_SCHEMA,
            UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA,
        }
        or (
            expected_purpose is not None
            and raw.get("purpose") != expected_purpose
        )
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or _TREE_OID.fullmatch(str(raw.get("source_tree_oid", ""))) is None
        or raw.get("candidate_name") != CANDIDATE_NAME
        or raw.get("payload_manifest_name") != PAYLOAD_MANIFEST_NAME
        or raw.get("interpreter_relative_path") != INTERPRETER_RELATIVE_PATH
        or raw.get("entrypoint_relative_path") != expected_entrypoint
        or raw.get("builder_identity") != _BUILDER_IDENTITY
        or raw.get("resume_policy") != "reject-nonempty-output-requires-root-cleanup"
        or raw.get("terminal") is not True
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
        or any(
            _SHA256.fullmatch(str(raw.get(name, ""))) is None for name in digest_fields
        )
    ):
        _fail("release_builder_phase_terminal_receipt_invalid")
    return raw


@dataclass(frozen=True)
class IdentityRecord:
    name: str
    numeric_id: int
    primary_group_id: int | None = None


class IdentityResolver(Protocol):
    def user_by_name(self, name: str) -> IdentityRecord: ...

    def user_by_uid(self, uid: int) -> IdentityRecord: ...

    def group_by_name(self, name: str) -> IdentityRecord: ...

    def group_by_gid(self, gid: int) -> IdentityRecord: ...

    def all_users(self) -> Sequence[IdentityRecord]: ...

    def all_groups(self) -> Sequence[IdentityRecord]: ...


class NssIdentityResolver:
    def user_by_name(self, name: str) -> IdentityRecord:
        item = pwd.getpwnam(name)
        return IdentityRecord(item.pw_name, item.pw_uid, item.pw_gid)

    def user_by_uid(self, uid: int) -> IdentityRecord:
        item = pwd.getpwuid(uid)
        return IdentityRecord(item.pw_name, item.pw_uid, item.pw_gid)

    def group_by_name(self, name: str) -> IdentityRecord:
        item = grp.getgrnam(name)
        return IdentityRecord(item.gr_name, item.gr_gid)

    def group_by_gid(self, gid: int) -> IdentityRecord:
        item = grp.getgrgid(gid)
        return IdentityRecord(item.gr_name, item.gr_gid)

    def all_users(self) -> Sequence[IdentityRecord]:
        return tuple(
            IdentityRecord(item.pw_name, item.pw_uid, item.pw_gid)
            for item in pwd.getpwall()
        )

    def all_groups(self) -> Sequence[IdentityRecord]:
        return tuple(
            IdentityRecord(item.gr_name, item.gr_gid) for item in grp.getgrall()
        )


def validate_builder_identity(
    resolver: IdentityResolver,
    *,
    effective_uid: int | None = None,
    effective_gid: int | None = None,
) -> Mapping[str, Any]:
    observed_uid = (
        _read_posix_identity("geteuid")
        if effective_uid is None
        else effective_uid
    )
    observed_gid = (
        _read_posix_identity("getegid")
        if effective_gid is None
        else effective_gid
    )
    if observed_uid != BUILDER_UID or observed_gid != BUILDER_GID:
        _fail("release_builder_phase_effective_identity_invalid")
    try:
        by_name = resolver.user_by_name(BUILDER_USER)
        by_uid = resolver.user_by_uid(BUILDER_UID)
        group_by_name = resolver.group_by_name(BUILDER_GROUP)
        group_by_gid = resolver.group_by_gid(BUILDER_GID)
        users = tuple(resolver.all_users())
        groups = tuple(resolver.all_groups())
    except (KeyError, LookupError, OSError, TypeError, ValueError) as exc:
        _fail("release_builder_phase_nss_identity_invalid", exc)
    expected_user = IdentityRecord(BUILDER_USER, BUILDER_UID, BUILDER_GID)
    expected_group = IdentityRecord(BUILDER_GROUP, BUILDER_GID)
    matching_users = tuple(
        item
        for item in users
        if item.name == BUILDER_USER or item.numeric_id == BUILDER_UID
    )
    matching_groups = tuple(
        item
        for item in groups
        if item.name == BUILDER_GROUP or item.numeric_id == BUILDER_GID
    )
    if (
        by_name != expected_user
        or by_uid != expected_user
        or group_by_name.name != expected_group.name
        or group_by_name.numeric_id != expected_group.numeric_id
        or group_by_gid.name != expected_group.name
        or group_by_gid.numeric_id != expected_group.numeric_id
        or matching_users != (expected_user,)
        or len(matching_groups) != 1
        or matching_groups[0].name != BUILDER_GROUP
        or matching_groups[0].numeric_id != BUILDER_GID
    ):
        _fail("release_builder_phase_nss_identity_invalid")
    return dict(_BUILDER_IDENTITY)


@dataclass
class HeldDirectory(AbstractContextManager["HeldDirectory"]):
    path: Path
    descriptor: int
    identity: builder.FileIdentity
    _closed: bool = False

    def names(self) -> tuple[str, ...]:
        self.assert_stable()
        try:
            names = tuple(sorted(os.listdir(self.descriptor)))
        except OSError as exc:
            _fail("release_builder_phase_directory_unavailable", exc)
        for name in names:
            _validate_component(name)
        self.assert_stable()
        return names

    def assert_stable(self) -> None:
        if self._closed:
            _fail("release_builder_phase_directory_closed")
        try:
            current = builder.FileIdentity.from_stat(os.fstat(self.descriptor))
            reachable = builder.FileIdentity.from_stat(os.lstat(self.path))
        except OSError as exc:
            _fail("release_builder_phase_directory_changed", exc)
        binding = lambda item: (
            item.device,
            item.inode,
            item.mode,
            item.uid,
            item.gid,
        )
        if binding(current) != binding(self.identity) or binding(reachable) != binding(
            self.identity
        ):
            _fail("release_builder_phase_directory_changed")

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _validate_component(value: str) -> None:
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        _fail("release_builder_phase_path_invalid", exc)
    if (
        not raw
        or len(raw) > builder.MAX_COMPONENT_BYTES
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        _fail("release_builder_phase_path_invalid")


def _open_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
) -> HeldDirectory:
    descriptor: int | None = None
    try:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_CLOEXEC")
        ):
            _fail("release_builder_phase_directory_invalid")
        before = builder.FileIdentity.from_stat(os.lstat(path))
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        opened = builder.FileIdentity.from_stat(os.fstat(descriptor))
        after = builder.FileIdentity.from_stat(os.lstat(path))
        if (
            before != opened
            or opened != after
            or not stat.S_ISDIR(opened.mode)
            or stat.S_ISLNK(opened.mode)
            or opened.uid != expected_uid
            or opened.gid != expected_gid
            or stat.S_IMODE(opened.mode) not in allowed_modes
            or stat.S_IMODE(opened.mode) & 0o022
        ):
            _fail("release_builder_phase_directory_invalid")
        return HeldDirectory(path, descriptor, opened)
    except ProductionReleaseBuilderPhaseError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        _fail("release_builder_phase_directory_invalid", exc)


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        pass_fds: tuple[int, ...],
    ) -> None: ...


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    pass_fds: tuple[int, ...],
) -> None:
    try:
        completed = subprocess.run(
            tuple(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            close_fds=True,
            pass_fds=pass_fds,
            timeout=MAX_COMMAND_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("release_builder_phase_command_failed", exc)
    if completed.returncode != 0:
        _fail("release_builder_phase_command_failed")


def _command_environment() -> Mapping[str, str]:
    return {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_OFFLINE": "1",
        "UV_NO_CONFIG": "1",
        "UV_NO_CACHE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }


def _replace_venv_interpreters_with_held_python(
    candidate: Path,
    held_python: builder.HeldRegularFile,
    *,
    python_executable_path: Path,
    physical_uid: int,
    physical_gid: int,
) -> tuple[str, ...]:
    """Replace uv's interpreter symlinks with exact, single-link copies."""

    names = ("python", "python3", python_executable_path.name)
    if (
        len(set(names)) != len(names)
        or _PYTHON_PATH.fullmatch(str(python_executable_path)) is None
    ):
        _fail("release_builder_phase_python_contract_invalid")
    bin_path = candidate / ".venv" / "bin"
    directory: int | None = None
    try:
        directory, _identity = builder._open_directory_path(
            bin_path,
            expected_uid=physical_uid,
            expected_gid=physical_gid,
            allowed_modes=frozenset({0o700, 0o750, 0o755}),
        )
        for name in names:
            _validate_component(name)
            state = builder.FileIdentity.from_stat(
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            )
            if not (
                stat.S_ISLNK(state.mode)
                or (
                    stat.S_ISREG(state.mode)
                    and not stat.S_ISLNK(state.mode)
                    and state.uid == physical_uid
                    and state.gid == physical_gid
                    and state.links == 1
                )
            ):
                _fail("release_builder_phase_venv_interpreter_invalid")
            os.unlink(name, dir_fd=directory)
            builder._copy_held_to_directory(
                held_python,
                directory,
                name,
                mode=0o555,
                destination_uid=physical_uid,
                destination_gid=physical_gid,
            )
        os.fsync(directory)
        for name in names:
            state = builder.FileIdentity.from_stat(
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            )
            if (
                not stat.S_ISREG(state.mode)
                or stat.S_ISLNK(state.mode)
                or state.uid != physical_uid
                or state.gid != physical_gid
                or state.links != 1
                or stat.S_IMODE(state.mode) != 0o555
            ):
                _fail("release_builder_phase_venv_interpreter_invalid")
    except ProductionReleaseBuilderPhaseError:
        raise
    except (builder.ProductionReleaseBuilderError, OSError) as exc:
        _fail("release_builder_phase_venv_interpreter_invalid", exc)
    finally:
        if directory is not None:
            os.close(directory)
    held_python.assert_stable()
    return names


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    try:
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("release_builder_phase_record_write_failed")
            view = view[written:]
    except OSError as exc:
        _fail("release_builder_phase_record_write_failed", exc)


def _write_record_last(
    root_descriptor: int,
    name: str,
    value: Mapping[str, Any],
    *,
    physical_uid: int,
    physical_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> str:
    _validate_component(name)
    payload = canonical_bytes(value) + b"\n"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
            dir_fd=root_descriptor,
        )
        created = True
        _list_no_xattrs(descriptor, xattr_reader=xattr_reader)
        _write_all(descriptor, payload)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        state = builder.FileIdentity.from_stat(os.fstat(descriptor))
        reachable = builder.FileIdentity.from_stat(
            os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        )
        _list_no_xattrs(descriptor, xattr_reader=xattr_reader)
        if (
            state != reachable
            or not stat.S_ISREG(state.mode)
            or state.links != 1
            or state.uid != physical_uid
            or state.gid != physical_gid
            or stat.S_IMODE(state.mode) != 0o444
            or state.size != len(payload)
            or builder._hash_descriptor(descriptor, size=state.size)
            != sha256_bytes(payload)
        ):
            _fail("release_builder_phase_record_write_failed")
        return sha256_bytes(payload)
    except ProductionReleaseBuilderPhaseError:
        raise
    except OSError as exc:
        _fail("release_builder_phase_record_write_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # A failed partial record deliberately remains as conflict evidence.
        del created


def _list_no_xattrs(
    descriptor: int,
    *,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> None:
    try:
        names = xattr_reader(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        _fail("release_builder_phase_xattr_inspection_unavailable", exc)
    if not isinstance(names, (list, tuple)) or any(
        not isinstance(name, (str, bytes)) or not name for name in names
    ):
        _fail("release_builder_phase_xattr_inspection_unavailable")
    if names:
        _fail("release_builder_phase_xattrs_present")


@dataclass
class _PayloadAccumulator:
    entries: list[Mapping[str, Any]]
    total_bytes: int = 0


def _seal_candidate_directory(
    descriptor: int,
    relative: PurePosixPath,
    *,
    physical_uid: int,
    physical_gid: int,
    accumulator: _PayloadAccumulator,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> None:
    _list_no_xattrs(descriptor, xattr_reader=xattr_reader)
    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError as exc:
        _fail("release_builder_phase_candidate_invalid", exc)
    for name in names:
        _validate_component(name)
        if str(relative) == "." and name in {
            PAYLOAD_MANIFEST_NAME,
            TERMINAL_RECEIPT_NAME,
            builder.MANIFEST_NAME,
            builder.RECEIPT_NAME,
        }:
            _fail("release_builder_phase_candidate_conflict")
        try:
            before = builder.FileIdentity.from_stat(
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            )
        except OSError as exc:
            _fail("release_builder_phase_candidate_invalid", exc)
        path = name if str(relative) == "." else (relative / name).as_posix()
        if stat.S_ISDIR(before.mode) and not stat.S_ISLNK(before.mode):
            child: int | None = None
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=descriptor,
                )
                opened = builder.FileIdentity.from_stat(os.fstat(child))
                if before != opened:
                    _fail("release_builder_phase_candidate_changed")
                if (
                    opened.uid != physical_uid
                    or opened.gid != physical_gid
                    or stat.S_IMODE(opened.mode) & 0o022
                ):
                    _fail("release_builder_phase_candidate_invalid")
                _seal_candidate_directory(
                    child,
                    PurePosixPath(path),
                    physical_uid=physical_uid,
                    physical_gid=physical_gid,
                    accumulator=accumulator,
                    xattr_reader=xattr_reader,
                )
                os.fchmod(child, 0o555)
                os.fsync(child)
                final = builder.FileIdentity.from_stat(os.fstat(child))
                reachable = builder.FileIdentity.from_stat(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                )
                _list_no_xattrs(child, xattr_reader=xattr_reader)
                if (
                    final != reachable
                    or final.uid != physical_uid
                    or final.gid != physical_gid
                    or stat.S_IMODE(final.mode) != 0o555
                ):
                    _fail("release_builder_phase_candidate_changed")
            finally:
                if child is not None:
                    os.close(child)
            accumulator.entries.append({
                "path": path,
                "kind": "directory",
                "mode": "0555",
                "uid": BUILDER_UID,
                "gid": BUILDER_GID,
                "xattrs": [],
            })
        elif stat.S_ISREG(before.mode) and not stat.S_ISLNK(before.mode):
            child = None
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                opened = builder.FileIdentity.from_stat(os.fstat(child))
                if (
                    before != opened
                    or opened.links != 1
                    or opened.uid != physical_uid
                    or opened.gid != physical_gid
                    or stat.S_IMODE(opened.mode) & 0o022
                ):
                    _fail("release_builder_phase_candidate_invalid")
                _list_no_xattrs(child, xattr_reader=xattr_reader)
                digest = builder._hash_descriptor(child, size=opened.size)
                final_mode = 0o555 if stat.S_IMODE(opened.mode) & 0o111 else 0o444
                os.fchmod(child, final_mode)
                os.fsync(child)
                final = builder.FileIdentity.from_stat(os.fstat(child))
                reachable = builder.FileIdentity.from_stat(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                )
                _list_no_xattrs(child, xattr_reader=xattr_reader)
                if (
                    final != reachable
                    or final.uid != physical_uid
                    or final.gid != physical_gid
                    or final.links != 1
                    or stat.S_IMODE(final.mode) != final_mode
                    or builder._hash_descriptor(child, size=final.size) != digest
                ):
                    _fail("release_builder_phase_candidate_changed")
            finally:
                if child is not None:
                    os.close(child)
            accumulator.entries.append({
                "path": path,
                "kind": "file",
                "mode": f"{final_mode:04o}",
                "uid": BUILDER_UID,
                "gid": BUILDER_GID,
                "size": before.size,
                "sha256": digest,
                "xattrs": [],
            })
            accumulator.total_bytes += before.size
        else:
            _fail("release_builder_phase_candidate_invalid")
        if (
            len(accumulator.entries) > builder.MAX_RELEASE_ENTRIES
            or accumulator.total_bytes > builder.MAX_RELEASE_BYTES
        ):
            _fail("release_builder_phase_candidate_oversized")
    try:
        if tuple(sorted(os.listdir(descriptor))) != names:
            _fail("release_builder_phase_candidate_changed")
    except OSError as exc:
        _fail("release_builder_phase_candidate_changed", exc)
    _list_no_xattrs(descriptor, xattr_reader=xattr_reader)


def _validated_job_paths(
    request_path: Path,
    *,
    production: bool,
    job_root: Path,
) -> tuple[str, Path, Path]:
    try:
        request_path = Path(request_path)
        job_root = Path(job_root)
    except (TypeError, ValueError) as exc:
        _fail("release_builder_phase_path_invalid", exc)
    if (
        not request_path.is_absolute()
        or not job_root.is_absolute()
        or request_path.name != REQUEST_NAME
        or request_path.parent.name != "input"
        or request_path.parent.parent.parent != job_root
        or _JOB_ID.fullmatch(request_path.parent.parent.name) is None
        or (production and job_root != PRODUCTION_JOB_ROOT)
        or (not production and job_root == PRODUCTION_JOB_ROOT)
    ):
        _fail("release_builder_phase_path_invalid")
    job_id = request_path.parent.parent.name
    return job_id, request_path.parent, request_path.parent.parent / "output"


def _run_builder_phase_for_test(
    request_path: Path,
    *,
    production: bool = True,
    job_root: Path = PRODUCTION_JOB_ROOT,
    identity_resolver: IdentityResolver | None = None,
    command_runner: CommandRunner | None = None,
    effective_uid: int | None = None,
    effective_gid: int | None = None,
    test_authority_uid: int | None = None,
    test_authority_gid: int | None = None,
    test_physical_builder_uid: int | None = None,
    test_physical_builder_gid: int | None = None,
    test_xattr_reader: Callable[[int], Sequence[str | bytes]] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Build one exact candidate without network or target-code execution."""

    if type(production) is not bool:
        _fail("release_builder_phase_contract_invalid")
    if production:
        if not sys.platform.startswith("linux") or any(
            item is not None
            for item in (
                effective_uid,
                effective_gid,
                test_authority_uid,
                test_authority_gid,
                test_physical_builder_uid,
                test_physical_builder_gid,
                test_xattr_reader,
                checkpoint,
            )
        ):
            _fail("release_builder_phase_contract_invalid")
        authority_uid = authority_gid = 0
        physical_builder_uid = BUILDER_UID
        physical_builder_gid = BUILDER_GID
    else:
        authority_uid = (
            _read_posix_identity("geteuid")
            if test_authority_uid is None
            else test_authority_uid
        )
        authority_gid = (
            _read_posix_identity("getegid")
            if test_authority_gid is None
            else test_authority_gid
        )
        physical_builder_uid = (
            _read_posix_identity("geteuid")
            if test_physical_builder_uid is None
            else test_physical_builder_uid
        )
        physical_builder_gid = (
            _read_posix_identity("getegid")
            if test_physical_builder_gid is None
            else test_physical_builder_gid
        )
    xattr_reader = (
        builder._read_descriptor_xattrs
        if production
        else (
            (lambda _descriptor: ()) if test_xattr_reader is None else test_xattr_reader
        )
    )
    if any(
        type(item) is not int or item < 0
        for item in (
            authority_uid,
            authority_gid,
            physical_builder_uid,
            physical_builder_gid,
        )
    ):
        _fail("release_builder_phase_contract_invalid")
    resolver = NssIdentityResolver() if identity_resolver is None else identity_resolver
    runner = _run_command if command_runner is None else command_runner
    validate_builder_identity(
        resolver,
        effective_uid=effective_uid,
        effective_gid=effective_gid,
    )
    job_id, input_root, output_root = _validated_job_paths(
        Path(request_path),
        production=production,
        job_root=Path(job_root),
    )

    stack = ExitStack()
    try:
        input_directory = stack.enter_context(
            _open_directory(
                input_root,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_DIRECTORY_MODES,
            )
        )
        output_directory = stack.enter_context(
            _open_directory(
                output_root,
                expected_uid=physical_builder_uid,
                expected_gid=physical_builder_gid,
                allowed_modes=_BUILDER_OUTPUT_DIRECTORY_MODES,
            )
        )
        if input_directory.names() != tuple(sorted(_INPUT_ROOT_NAMES)):
            _fail("release_builder_phase_input_set_invalid")
        if output_directory.names():
            _fail("release_builder_phase_output_not_empty")

        request_file = stack.enter_context(
            builder.open_held_regular(
                Path(request_path),
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_FILE_MODES,
                maximum_bytes=MAX_JSON_BYTES,
            )
        )
        request = validate_request(
            _decode_canonical_document(_read_held(request_file)),
            expected_job_id=job_id,
        )
        entrypoint_relative_path = str(request["entrypoint_relative_path"])
        if request["schema"] == UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA:
            terminal_receipt_schema = (
                UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA
            )
            request_purpose: str | None = str(request["purpose"])
        else:
            terminal_receipt_schema = TERMINAL_RECEIPT_SCHEMA
            request_purpose = None
        source_file = stack.enter_context(
            builder.open_held_regular(
                input_root / SOURCE_MANIFEST_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_FILE_MODES,
                maximum_bytes=MAX_JSON_BYTES,
                expected_sha256=str(request["source_v3_manifest_sha256"]),
            )
        )
        source_manifest = validate_source_manifest(
            _decode_canonical_document(_read_held(source_file)),
            request=request,
        )
        runtime_file = stack.enter_context(
            builder.open_held_regular(
                input_root / RUNTIME_MANIFEST_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_FILE_MODES,
                maximum_bytes=MAX_JSON_BYTES,
                expected_sha256=str(request["runtime_dependency_manifest_sha256"]),
            )
        )
        runtime_manifest = validate_runtime_manifest(
            _decode_canonical_document(_read_held(runtime_file)),
            request=request,
        )
        tree_file = stack.enter_context(
            builder.open_held_regular(
                input_root / TREE_LISTING_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_FILE_MODES,
                maximum_bytes=builder.MAX_GIT_TREE_BYTES,
                expected_sha256=str(source_manifest["tree_listing_sha256"]),
            )
        )
        if tree_file.identity.size != source_manifest["tree_listing_size"]:
            _fail("release_builder_phase_source_manifest_invalid")
        entries = builder.parse_git_tree(
            _read_held(tree_file),
            object_format=str(source_manifest["object_format"]),
        )
        if len(entries) != source_manifest["tree_entry_count"]:
            _fail("release_builder_phase_source_manifest_invalid")
        if any(
            PurePosixPath(entry.path).parts[0] in _RESERVED_SOURCE_ROOTS
            for entry in entries
        ):
            _fail("release_builder_phase_source_path_reserved")
        if not any(entry.path == entrypoint_relative_path for entry in entries):
            # The request schema selects one exact, purpose-bound entrypoint.
            # Reject a source revision that lacks it before materializing any
            # output so the failed attempt remains exactly retryable.
            _fail("release_builder_phase_entrypoint_missing")
        manifest_blobs = {
            str(item["object_id"]): dict(item) for item in source_manifest["blobs"]
        }
        if set(manifest_blobs) != {entry.object_id for entry in entries}:
            _fail("release_builder_phase_source_blob_set_invalid")

        blob_directory = stack.enter_context(
            _open_directory(
                input_root / SOURCE_BLOB_DIRECTORY_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_DIRECTORY_MODES,
            )
        )
        expected_blob_names = tuple(
            sorted(str(item["filename"]) for item in source_manifest["blobs"])
        )
        if blob_directory.names() != expected_blob_names:
            _fail("release_builder_phase_source_blob_set_invalid")
        wheel_directory = stack.enter_context(
            _open_directory(
                input_root / RUNTIME_WHEEL_DIRECTORY_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_DIRECTORY_MODES,
            )
        )
        expected_wheel_names = tuple(
            sorted(str(item["filename"]) for item in runtime_manifest["wheels"])
        )
        if wheel_directory.names() != expected_wheel_names:
            _fail("release_builder_phase_runtime_wheel_set_invalid")
        held_wheels: list[builder.HeldRegularFile] = []
        for item in runtime_manifest["wheels"]:
            held_wheel = stack.enter_context(
                builder.open_held_regular(
                    input_root / RUNTIME_WHEEL_DIRECTORY_NAME / str(item["filename"]),
                    expected_uid=authority_uid,
                    expected_gid=authority_gid,
                    allowed_modes=_ROOT_FILE_MODES,
                    maximum_bytes=builder.MAX_WHEEL_BYTES,
                    expected_sha256=str(item["sha256"]),
                )
            )
            if held_wheel.identity.size != item["size"]:
                _fail("release_builder_phase_runtime_wheel_invalid")
            held_wheels.append(held_wheel)
        uv_file = stack.enter_context(
            builder.open_held_regular(
                input_root / UV_NAME,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_EXECUTABLE_MODES,
                maximum_bytes=MAX_UV_BYTES,
                expected_sha256=str(request["uv_sha256"]),
            )
        )
        if uv_file.identity.size != request["uv_size"]:
            _fail("release_builder_phase_request_invalid")
        python_file = stack.enter_context(
            builder.open_held_regular(
                Path(str(request["python_executable_path"])),
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_EXECUTABLE_MODES,
                maximum_bytes=MAX_PYTHON_BYTES,
                expected_sha256=str(request["python_executable_sha256"]),
            )
        )
        if python_file.identity.size != request["python_executable_size"]:
            _fail("release_builder_phase_request_invalid")
        if checkpoint is not None:
            checkpoint("inputs_held")

        candidate = output_root / CANDIDATE_NAME

        def open_blob(
            entry: builder.GitTreeEntry,
        ) -> AbstractContextManager[builder.HeldRegularFile]:
            record = manifest_blobs.get(entry.object_id)
            if record is None:
                _fail("release_builder_phase_source_blob_set_invalid")
            held = builder.open_held_regular(
                input_root / SOURCE_BLOB_DIRECTORY_NAME / str(record["filename"]),
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_ROOT_FILE_MODES,
                maximum_bytes=builder.MAX_BLOB_BYTES,
                expected_sha256=str(record["sha256"]),
                require_nonempty=False,
            )
            if held.identity.size != record["size"]:
                held.close()
                _fail("release_builder_phase_source_blob_invalid")
            return held

        materialization = builder.materialize_git_tree(
            entries,
            candidate,
            revision=str(request["release_revision"]),
            source_tree_oid=str(request["source_tree_oid"]),
            open_blob=open_blob,
            destination_uid=physical_builder_uid,
            destination_gid=physical_builder_gid,
            parent_uid=physical_builder_uid,
            parent_gid=physical_builder_gid,
            _xattr_reader=xattr_reader,
        )
        if checkpoint is not None:
            checkpoint("source_materialized")
        os.chmod(candidate, 0o700, follow_symlinks=False)
        retained_wheels = candidate / RETAINED_WHEEL_DIRECTORY_NAME
        retained_wheels.mkdir(mode=0o700)
        retained_records: list[Mapping[str, Any]] = []
        for item in runtime_manifest["wheels"]:
            record = builder.retain_verified_wheel(
                input_root / RUNTIME_WHEEL_DIRECTORY_NAME / str(item["filename"]),
                retained_wheels,
                expected_sha256=str(item["sha256"]),
                builder_uid=authority_uid,
                builder_gid=authority_gid,
                destination_uid=physical_builder_uid,
                destination_gid=physical_builder_gid,
            )
            if record["size"] != item["size"]:
                _fail("release_builder_phase_runtime_wheel_invalid")
            retained_records.append(record)
        if wheel_directory.names() != expected_wheel_names:
            _fail("release_builder_phase_runtime_wheel_set_invalid")
        if blob_directory.names() != expected_blob_names:
            _fail("release_builder_phase_source_blob_set_invalid")
        if checkpoint is not None:
            checkpoint("wheels_retained")

        environment = _command_environment()
        uv_path = f"/proc/self/fd/{uv_file.descriptor}"
        venv_argv = (
            uv_path,
            "venv",
            "--python",
            str(request["python_executable_path"]),
            "--no-project",
            "--no-python-downloads",
            "--offline",
            "--no-index",
            "--no-cache",
            "--no-config",
            "--link-mode",
            "copy",
            "--relocatable",
            str(candidate / ".venv"),
        )
        runner(
            venv_argv,
            cwd=output_root,
            env=environment,
            pass_fds=(uv_file.descriptor,),
        )
        uv_file.assert_stable()
        python_file.assert_stable()
        _replace_venv_interpreters_with_held_python(
            candidate,
            python_file,
            python_executable_path=Path(str(request["python_executable_path"])),
            physical_uid=physical_builder_uid,
            physical_gid=physical_builder_gid,
        )
        install_argv = (
            uv_path,
            "pip",
            "install",
            "--python",
            str(candidate / INTERPRETER_RELATIVE_PATH),
            "--offline",
            "--no-index",
            "--no-cache",
            "--no-config",
            "--no-python-downloads",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--no-sources",
            "--link-mode",
            "copy",
            "--exact",
            "--strict",
            *(
                str(retained_wheels / str(item["filename"]))
                for item in runtime_manifest["wheels"]
            ),
        )
        runner(
            install_argv,
            cwd=output_root,
            env=environment,
            pass_fds=(uv_file.descriptor,),
        )
        if checkpoint is not None:
            checkpoint("runtime_installed")
        for held in (
            request_file,
            source_file,
            runtime_file,
            tree_file,
            uv_file,
            python_file,
            *held_wheels,
        ):
            held.assert_stable()
        input_directory.assert_stable()
        if input_directory.names() != tuple(sorted(_INPUT_ROOT_NAMES)):
            _fail("release_builder_phase_input_set_invalid")
        blob_directory.assert_stable()
        wheel_directory.assert_stable()
        output_directory.assert_stable()

        accumulator = _PayloadAccumulator(entries=[])
        candidate_descriptor: int | None = None
        try:
            candidate_descriptor = os.open(
                candidate,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
            _seal_candidate_directory(
                candidate_descriptor,
                PurePosixPath("."),
                physical_uid=physical_builder_uid,
                physical_gid=physical_builder_gid,
                accumulator=accumulator,
                xattr_reader=xattr_reader,
            )
            entries_sorted = sorted(
                accumulator.entries, key=lambda item: str(item["path"])
            )
            payload_unsigned = {
                "schema": PAYLOAD_MANIFEST_SCHEMA,
                "release_revision": request["release_revision"],
                "source_tree_oid": request["source_tree_oid"],
                "builder_identity": _BUILDER_IDENTITY,
                "payload_entries": entries_sorted,
                "payload_entry_count": len(entries_sorted),
                "payload_bytes": accumulator.total_bytes,
                "payload_tree_sha256": sha256_bytes(canonical_bytes(entries_sorted)),
                "secret_material_recorded": False,
                "secret_digest_recorded": False,
            }
            payload_manifest = {
                **payload_unsigned,
                "manifest_sha256": sha256_bytes(canonical_bytes(payload_unsigned)),
            }
            validate_payload_manifest(payload_manifest)
            payload_file_sha256 = _write_record_last(
                candidate_descriptor,
                PAYLOAD_MANIFEST_NAME,
                payload_manifest,
                physical_uid=physical_builder_uid,
                physical_gid=physical_builder_gid,
                xattr_reader=xattr_reader,
            )
            if checkpoint is not None:
                checkpoint("payload_manifest_written")
            with builder.open_held_regular(
                candidate / INTERPRETER_RELATIVE_PATH,
                expected_uid=physical_builder_uid,
                expected_gid=physical_builder_gid,
                allowed_modes=frozenset({0o555}),
                maximum_bytes=MAX_PYTHON_BYTES,
            ) as interpreter:
                interpreter_sha256 = interpreter.sha256
            with builder.open_held_regular(
                candidate / entrypoint_relative_path,
                expected_uid=physical_builder_uid,
                expected_gid=physical_builder_gid,
                allowed_modes=frozenset({0o444, 0o555}),
                maximum_bytes=16 * 1024 * 1024,
            ) as entrypoint:
                entrypoint_sha256 = entrypoint.sha256
            receipt_unsigned = {
                "schema": terminal_receipt_schema,
                "release_revision": request["release_revision"],
                "candidate_name": CANDIDATE_NAME,
                "source_tree_oid": request["source_tree_oid"],
                "builder_request_sha256": request_file.sha256,
                "builder_request_identity_sha256": request["request_sha256"],
                "source_v3_manifest_sha256": source_file.sha256,
                "source_v3_manifest_identity_sha256": source_manifest[
                    "manifest_sha256"
                ],
                "runtime_dependency_manifest_sha256": runtime_file.sha256,
                "runtime_dependency_manifest_identity_sha256": runtime_manifest[
                    "manifest_sha256"
                ],
                "uv_sha256": uv_file.sha256,
                "python_executable_sha256": python_file.sha256,
                "source_materialization_sha256": materialization[
                    "materialization_sha256"
                ],
                "retained_wheels_sha256": sha256_bytes(
                    canonical_bytes(retained_records)
                ),
                "payload_manifest_name": PAYLOAD_MANIFEST_NAME,
                "payload_manifest_sha256": payload_manifest["manifest_sha256"],
                "payload_manifest_file_sha256": payload_file_sha256,
                "payload_tree_sha256": payload_manifest["payload_tree_sha256"],
                "interpreter_relative_path": INTERPRETER_RELATIVE_PATH,
                "interpreter_sha256": interpreter_sha256,
                "entrypoint_relative_path": entrypoint_relative_path,
                "entrypoint_sha256": entrypoint_sha256,
                "venv_argv_sha256": sha256_bytes(canonical_bytes(list(venv_argv))),
                "install_argv_sha256": sha256_bytes(
                    canonical_bytes(list(install_argv))
                ),
                "command_environment_sha256": sha256_bytes(
                    canonical_bytes(environment)
                ),
                "builder_identity": _BUILDER_IDENTITY,
                "resume_policy": request["resume_policy"],
                "terminal": True,
                "secret_material_recorded": False,
                "secret_digest_recorded": False,
            }
            if request_purpose is not None:
                receipt_unsigned["purpose"] = request_purpose
            receipt = {
                **receipt_unsigned,
                "receipt_sha256": sha256_bytes(canonical_bytes(receipt_unsigned)),
            }
            validate_terminal_receipt(receipt)
            _write_record_last(
                candidate_descriptor,
                TERMINAL_RECEIPT_NAME,
                receipt,
                physical_uid=physical_builder_uid,
                physical_gid=physical_builder_gid,
                xattr_reader=xattr_reader,
            )
            if checkpoint is not None:
                checkpoint("terminal_receipt_written")
            os.fchmod(candidate_descriptor, 0o555)
            os.fsync(candidate_descriptor)
            os.fsync(output_directory.descriptor)
            final_names = tuple(sorted(os.listdir(candidate_descriptor)))
            if (
                TERMINAL_RECEIPT_NAME not in final_names
                or PAYLOAD_MANIFEST_NAME not in final_names
                or tuple(sorted(os.listdir(output_directory.descriptor)))
                != (CANDIDATE_NAME,)
            ):
                _fail("release_builder_phase_terminal_receipt_invalid")
        finally:
            if candidate_descriptor is not None:
                os.close(candidate_descriptor)
        if checkpoint is not None:
            checkpoint("completed")
        return receipt
    except ProductionReleaseBuilderPhaseError:
        raise
    except (
        builder.ProductionReleaseBuilderError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _fail("release_builder_phase_failed", exc)
    finally:
        stack.close()


def run_builder_phase(request_path: Path) -> Mapping[str, Any]:
    """Build one production candidate through fixed systemd authority."""

    return _run_builder_phase_for_test(request_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one pinned Muncho release candidate offline."
    )
    parser.add_argument("--request", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    run_builder_phase(arguments.request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUILDER_GID",
    "BUILDER_GROUP",
    "BUILDER_UID",
    "BUILDER_USER",
    "CANDIDATE_NAME",
    "ENTRYPOINT_RELATIVE_PATH",
    "IdentityRecord",
    "IdentityResolver",
    "INTERPRETER_RELATIVE_PATH",
    "NssIdentityResolver",
    "PAYLOAD_MANIFEST_NAME",
    "PAYLOAD_MANIFEST_SCHEMA",
    "PRODUCTION_JOB_ROOT",
    "ProductionReleaseBuilderPhaseError",
    "REQUEST_SCHEMA",
    "RUNTIME_DEPENDENCY_MANIFEST_SCHEMA",
    "SOURCE_V3_MANIFEST_SCHEMA",
    "TERMINAL_RECEIPT_NAME",
    "TERMINAL_RECEIPT_SCHEMA",
    "UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH",
    "UNIT_INPUT_ROTATION_STAGER_PURPOSE",
    "UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA",
    "UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA",
    "canonical_bytes",
    "main",
    "run_builder_phase",
    "sha256_bytes",
    "validate_builder_identity",
    "validate_payload_manifest",
    "validate_request",
    "validate_runtime_manifest",
    "validate_source_manifest",
    "validate_terminal_receipt",
]
