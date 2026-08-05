"""Exact short-lived permit for the two owner-approved pre-Phase-B starts.

The durable writer service always requires Phase-B readiness.  Native and
final activation must nevertheless observe the writer before the Phase-B
foundation can be derived from their receipts.  This module bridges only that
mechanical lifecycle edge with a root-published, secret-free, short-lived file
bound to the exact release, plan, config, boot, and writer identity.

There is no user-text interpretation or semantic routing here.  Every field is
an exact schema/security binding and an invalid or stale permit fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Mapping


PRE_PHASE_B_START_PERMIT_SCHEMA = "muncho-writer-pre-phase-b-start-permit.v1"
DEFAULT_PRE_PHASE_B_START_PERMIT_PATH = Path(
    "/etc/muncho-canonical-writer/pre-phase-b-start-permit.json"
)
DEFAULT_CANARY_RELEASES_ROOT = Path("/opt/muncho-canary-releases")
PRE_PHASE_B_START_PERMIT_MAX_SECONDS = 180
_MAX_PERMIT_BYTES = 64 * 1024
# The signed release manifest enumerates the full built artifact tree.
_MAX_RELEASE_MANIFEST_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SCOPE_VALUES = frozenset({"native_observation", "activation"})
_PERMIT_KEYS = frozenset({
    "schema",
    "revision",
    "artifact_root",
    "artifact_sha256",
    "release_manifest_file_sha256",
    "bootstrap_module_sha256",
    "writer_config_path",
    "writer_config_sha256",
    "writer_uid",
    "writer_gid",
    "boot_id_sha256",
    "scope",
    "plan_sha256",
    "owner_approval_receipt_sha256",
    "owner_approval_expires_at_unix",
    "external_iam_receipt_sha256",
    "created_at_unix",
    "expires_at_unix",
    "receipt_sha256",
})


class PrePhaseBStartPermitError(RuntimeError):
    """The exact pre-Phase-B startup permit is missing or invalid."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return value


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return path


def _boot_id_sha256() -> str:
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_bytes()
        value = raw.decode("ascii", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    if (
        len(raw) > 128
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return _sha256(value.encode("ascii"))


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        try:
            item = os.lstat(current)
        except OSError:
            raise PrePhaseBStartPermitError(
                "pre_phase_b_start_permit_invalid"
            ) from None
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or item.st_uid != 0
            or stat.S_IMODE(item.st_mode) & 0o022
        ):
            raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
        if current == current.parent:
            return
        current = current.parent


def _read_exact_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    maximum_bytes: int,
) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    _validate_parent_chain(path.parent)
    try:
        before = os.lstat(path)
    except OSError:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) != expected_mode
        or not 0 < before.st_size <= maximum_bytes
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != expected_mode
            or opened.st_size != before.st_size
        ):
            raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if not raw or len(raw) > maximum_bytes or len(raw) != before.st_size:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return raw


def _validate_exact_file_metadata(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    maximum_bytes: int,
) -> None:
    """Validate an intentionally unreadable root-owned release leaf.

    The release manifest is mode 0400 by contract.  The root activation
    coordinator reads and hashes it before publishing the permit; the writer
    can only re-attest that the same root-controlled leaf remains immutable.
    """

    if not path.is_absolute() or ".." in path.parts:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    _validate_parent_chain(path.parent)
    try:
        item = os.lstat(path)
    except OSError:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or item.st_uid != expected_uid
        or item.st_gid != expected_gid
        or stat.S_IMODE(item.st_mode) != expected_mode
        or not 0 < item.st_size <= maximum_bytes
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")


def _strict_mapping(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in items:
            if name in value:
                raise ValueError("duplicate")
            value[name] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    if not isinstance(value, dict) or frozenset(value) != _PERMIT_KEYS:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    canonical = _canonical_bytes(value)
    if raw not in {canonical, canonical + b"\n"}:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return value


def validate_pre_phase_b_start_permit_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _PERMIT_KEYS:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    result = dict(value)
    if (
        result.get("schema") != PRE_PHASE_B_START_PERMIT_SCHEMA
        or not isinstance(result.get("revision"), str)
        or _REVISION_RE.fullmatch(result["revision"]) is None
        or result.get("scope") not in _SCOPE_VALUES
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    artifact_root = _absolute_path(result.get("artifact_root"))
    config_path = _absolute_path(result.get("writer_config_path"))
    if artifact_root != DEFAULT_CANARY_RELEASES_ROOT / result["revision"]:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    if config_path != Path("/etc/muncho-canonical-writer/writer.json"):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    for name in (
        "artifact_sha256",
        "release_manifest_file_sha256",
        "bootstrap_module_sha256",
        "writer_config_sha256",
        "boot_id_sha256",
        "plan_sha256",
        "owner_approval_receipt_sha256",
        "external_iam_receipt_sha256",
    ):
        _digest(result.get(name))
    for name in ("writer_uid", "writer_gid"):
        if type(result.get(name)) is not int or result[name] <= 0:
            raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    created = result.get("created_at_unix")
    expires = result.get("expires_at_unix")
    owner_expires = result.get("owner_approval_expires_at_unix")
    if (
        type(created) is not int
        or type(expires) is not int
        or type(owner_expires) is not int
        or created < 0
        or not created < expires
        or expires > owner_expires
        or expires - created > PRE_PHASE_B_START_PERMIT_MAX_SECONDS
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    receipt = _digest(result.get("receipt_sha256"))
    unsigned = {name: item for name, item in result.items() if name != "receipt_sha256"}
    if receipt != _sha256(_canonical_bytes(unsigned)):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return result


def build_pre_phase_b_start_permit(
    *,
    revision: str,
    artifact_root: str,
    artifact_sha256: str,
    release_manifest_file_sha256: str,
    writer_config_path: str,
    writer_config_sha256: str,
    writer_uid: int,
    writer_gid: int,
    boot_id_sha256: str,
    scope: str,
    plan_sha256: str,
    owner_approval_receipt_sha256: str,
    owner_approval_expires_at_unix: int,
    external_iam_receipt_sha256: str,
    now_unix: int | None = None,
) -> dict[str, Any]:
    root = _absolute_path(artifact_root)
    config = _absolute_path(writer_config_path)
    current = int(time.time()) if now_unix is None else now_unix
    if (
        type(current) is not int
        or current < 0
        or type(owner_approval_expires_at_unix) is not int
        or owner_approval_expires_at_unix <= current
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    bootstrap_candidates = tuple(
        root.glob(
            "venv/lib/python*/site-packages/gateway/canonical_writer_bootstrap.py"
        )
    )
    if len(bootstrap_candidates) != 1:
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    bootstrap = bootstrap_candidates[0]
    bootstrap_raw = _read_exact_file(
        bootstrap,
        expected_uid=0,
        expected_gid=0,
        expected_mode=0o444,
        maximum_bytes=2 * 1024 * 1024,
    )
    manifest_raw = _read_exact_file(
        root / "release-manifest.json",
        expected_uid=0,
        expected_gid=0,
        expected_mode=0o400,
        maximum_bytes=_MAX_RELEASE_MANIFEST_BYTES,
    )
    config_raw = _read_exact_file(
        config,
        expected_uid=0,
        expected_gid=writer_gid,
        expected_mode=0o440,
        maximum_bytes=2 * 1024 * 1024,
    )
    if (
        _sha256(manifest_raw) != release_manifest_file_sha256
        or _sha256(config_raw) != writer_config_sha256
        or _boot_id_sha256() != boot_id_sha256
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    expires_at_unix = min(
        current + PRE_PHASE_B_START_PERMIT_MAX_SECONDS,
        owner_approval_expires_at_unix,
    )
    unsigned = {
        "schema": PRE_PHASE_B_START_PERMIT_SCHEMA,
        "revision": revision,
        "artifact_root": str(root),
        "artifact_sha256": artifact_sha256,
        "release_manifest_file_sha256": release_manifest_file_sha256,
        "bootstrap_module_sha256": _sha256(bootstrap_raw),
        "writer_config_path": str(config),
        "writer_config_sha256": writer_config_sha256,
        "writer_uid": writer_uid,
        "writer_gid": writer_gid,
        "boot_id_sha256": boot_id_sha256,
        "scope": scope,
        "plan_sha256": plan_sha256,
        "owner_approval_receipt_sha256": owner_approval_receipt_sha256,
        "owner_approval_expires_at_unix": owner_approval_expires_at_unix,
        "external_iam_receipt_sha256": external_iam_receipt_sha256,
        "created_at_unix": current,
        "expires_at_unix": expires_at_unix,
    }
    value = {**unsigned, "receipt_sha256": _sha256(_canonical_bytes(unsigned))}
    return validate_pre_phase_b_start_permit_mapping(value)


def read_pre_phase_b_start_permit(
    path: Path = DEFAULT_PRE_PHASE_B_START_PERMIT_PATH,
    *,
    expected_writer_gid: int,
) -> dict[str, Any]:
    raw = _read_exact_file(
        path,
        expected_uid=0,
        expected_gid=expected_writer_gid,
        expected_mode=0o440,
        maximum_bytes=_MAX_PERMIT_BYTES,
    )
    return validate_pre_phase_b_start_permit_mapping(_strict_mapping(raw))


def validate_installed_pre_phase_b_start_permit(
    *,
    config_path: str,
    writer_uid: int,
    writer_gid: int,
    module_file: str,
    now_unix: int | None = None,
    path: Path = DEFAULT_PRE_PHASE_B_START_PERMIT_PATH,
) -> dict[str, Any]:
    value = read_pre_phase_b_start_permit(path, expected_writer_gid=writer_gid)
    current = int(time.time()) if now_unix is None else now_unix
    effective_uid = getattr(os, "geteuid", None)
    effective_gid = getattr(os, "getegid", None)
    if (
        type(current) is not int
        or not value["created_at_unix"] <= current < value["expires_at_unix"]
        or effective_uid is None
        or effective_gid is None
        or effective_uid() != writer_uid
        or effective_gid() != writer_gid
        or value["writer_uid"] != writer_uid
        or value["writer_gid"] != writer_gid
        or value["boot_id_sha256"] != _boot_id_sha256()
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    root = Path(value["artifact_root"])
    module = Path(module_file)
    try:
        resolved_module = module.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        relative = resolved_module.relative_to(resolved_root)
    except (OSError, ValueError):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid") from None
    if (
        relative.parts[:2] != ("venv", "lib")
        or relative.parts[-3:]
        != ("site-packages", "gateway", "canonical_writer_bootstrap.py")
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    module_raw = _read_exact_file(
        resolved_module,
        expected_uid=0,
        expected_gid=0,
        expected_mode=0o444,
        maximum_bytes=2 * 1024 * 1024,
    )
    _validate_exact_file_metadata(
        resolved_root / "release-manifest.json",
        expected_uid=0,
        expected_gid=0,
        expected_mode=0o400,
        maximum_bytes=_MAX_RELEASE_MANIFEST_BYTES,
    )
    config = _absolute_path(config_path)
    config_raw = _read_exact_file(
        config,
        expected_uid=0,
        expected_gid=writer_gid,
        expected_mode=0o440,
        maximum_bytes=2 * 1024 * 1024,
    )
    if (
        str(config) != value["writer_config_path"]
        or _sha256(config_raw) != value["writer_config_sha256"]
        or _sha256(module_raw) != value["bootstrap_module_sha256"]
        or resolved_root.name != value["revision"]
    ):
        raise PrePhaseBStartPermitError("pre_phase_b_start_permit_invalid")
    return value


def canonical_pre_phase_b_start_permit_bytes(value: Mapping[str, Any]) -> bytes:
    validated = validate_pre_phase_b_start_permit_mapping(value)
    return _canonical_bytes(validated) + b"\n"
