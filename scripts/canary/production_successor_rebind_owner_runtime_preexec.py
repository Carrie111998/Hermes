#!/usr/bin/env python3
"""Stdlib-only pre-exec proof for the fixed successor-rebind owner runtime."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Never, Sequence


MANIFEST_SCHEMA = "muncho-production-owner-runtime-manifest.v1"
ATTESTATION_SCHEMA = "muncho-production-owner-runtime-attestation.v1"
MANIFEST_NAME = "production-owner-runtime-manifest.json"
RUNTIME_BASE = Path("/usr/lib/muncho-successor-rebind-runtime")
PYTHON_VERSION = "3.11.15"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST = 32 * 1024 * 1024
_MAX_ENTRIES = 200_000
_MAX_BYTES = 4 * 1024 * 1024 * 1024
_LAUNCH_ENVELOPE_MAX_BYTES = 64 * 1024
_LAUNCH_AUTHORITY_SCHEMA = (
    "muncho-dual-upstream-sync-successor-rebind-launch-authority.v1"
)
_LAUNCH_ENVELOPE_SCHEMA = (
    "muncho-dual-upstream-sync-successor-rebind-launch-envelope.v1"
)
_OWNER_REQUEST_SCHEMA = (
    "muncho-dual-upstream-sync-successor-rebind-owner-request.v3"
)
_OWNER_OPERATION = "dual-upstream-sync-successor-rebind"
_OWNER_REQUEST_REVISION_FIELDS = frozenset({
    "target_revision",
    "predecessor_revision",
    "source_tree_oid",
})
_OWNER_REQUEST_DIGEST_FIELDS = frozenset({
    "target_package_manifest_sha256",
    "predecessor_activation_receipt_sha256",
    "stage_c_host_artifact_manifest_sha256",
    "stage_c_release_update_publication_sha256",
    "stage_c_builder_terminal_receipt_sha256",
    "rebind_runtime_sha256",
    "foundation_wrapper_sha256",
    "controller_owner_runtime_manifest_sha256",
    "controller_owner_runtime_attestation_sha256",
    "controller_owner_runtime_tree_sha256",
    "controller_owner_runtime_interpreter_sha256",
    "remote_owner_runtime_publication_sha256",
    "remote_owner_runtime_manifest_sha256",
    "remote_owner_runtime_attestation_sha256",
    "remote_owner_runtime_tree_sha256",
    "remote_owner_runtime_interpreter_sha256",
    "remote_owner_runtime_staging_publication_sha256",
    "remote_owner_runtime_staging_manifest_sha256",
    "remote_owner_runtime_staging_attestation_sha256",
    "remote_owner_runtime_staging_tree_sha256",
    "remote_owner_runtime_staging_interpreter_sha256",
    "remote_owner_runtime_staging_pyvenv_cfg_sha256",
    "remote_owner_runtime_builder_receipt_sha256",
    "remote_owner_runtime_wheel_sha256",
    "preexec_verifier_sha256",
    "successor_runtime_foundation_wrapper_sha256",
    "successor_runtime_foundation_launcher_sha256",
    "successor_runtime_controller_manifest_file_sha256",
    "request_sha256",
})
_OWNER_REQUEST_POLICY_FIELDS = frozenset({
    "caller_selected_paths_allowed",
    "caller_selected_commands_allowed",
    "caller_selected_targets_allowed",
    "manual_json_allowed",
    "semantic_decisions_allowed",
    "secret_material_recorded",
    "secret_digest_recorded",
})
_OWNER_REQUEST_FIELDS = frozenset({"schema", "operation"}).union(
    _OWNER_REQUEST_REVISION_FIELDS,
    _OWNER_REQUEST_DIGEST_FIELDS,
    _OWNER_REQUEST_POLICY_FIELDS,
)
_CONTROLLER_RELEASES_ROOT = Path(
    "/opt/adventico-ai-platform/hermes-agent-releases"
)
_MANIFEST_FIELDS = frozenset({
    "schema",
    "revision",
    "artifact_root",
    "python_version",
    "interpreter",
    "pyvenv_cfg",
    "site_packages",
    "sys_path",
    "required_modules",
    "entries",
    "entry_count",
    "tree_bytes",
    "tree_sha256",
    "root_uid",
    "root_gid",
    "root_mode",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})


class SuccessorRuntimePreExecError(RuntimeError):
    """Stable pre-exec proof failure."""


def _fail(code: str, _cause: BaseException | None = None) -> Never:
    raise SuccessorRuntimePreExecError(code) from None


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("successor_runtime_preexec_json_invalid", exc)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decode_launch_envelope(
    raw: bytes,
    *,
    revision: str,
    expected_launch_authority_sha256: str,
) -> bytes:
    try:
        envelope = json.loads(raw[:-1].decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_runtime_preexec_launch_envelope_invalid", exc)
    fields = {
        "schema",
        "request_frame_hex",
        "launch_authority",
        "secret_material_recorded",
        "secret_digest_recorded",
        "envelope_sha256",
    }
    unsigned = (
        {
            name: item
            for name, item in envelope.items()
            if name != "envelope_sha256"
        }
        if isinstance(envelope, Mapping)
        else {}
    )
    if (
        not raw.endswith(b"\n")
        or len(raw) > _LAUNCH_ENVELOPE_MAX_BYTES
        or not isinstance(envelope, Mapping)
        or set(envelope) != fields
        or envelope.get("schema") != _LAUNCH_ENVELOPE_SCHEMA
        or envelope.get("secret_material_recorded") is not False
        or envelope.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(envelope.get("envelope_sha256", ""))) is None
        or envelope.get("envelope_sha256") != _sha(_canonical(unsigned))
        or raw != _canonical(envelope) + b"\n"
    ):
        _fail("successor_runtime_preexec_launch_envelope_invalid")
    authority = envelope["launch_authority"]
    authority_fields = {
        "schema",
        "operation",
        "request_sha256",
        "target_revision",
        "predecessor_revision",
        "predecessor_activation_receipt_sha256",
        "predecessor_trust_sha256",
        "stage_c_host_artifact_manifest_sha256",
        "stage_c_release_update_publication_sha256",
        "stage_c_builder_terminal_receipt_sha256",
        "candidate_seal_receipt_sha256",
        "whole_tree_manifest_sha256",
        "input_internal_identities_sha256",
        "release_root",
        "source_tree_oid",
        "secret_material_recorded",
        "secret_digest_recorded",
        "launch_authority_sha256",
    }
    authority_unsigned = (
        {
            name: item
            for name, item in authority.items()
            if name != "launch_authority_sha256"
        }
        if isinstance(authority, Mapping)
        else {}
    )
    if (
        not isinstance(authority, Mapping)
        or set(authority) != authority_fields
        or authority.get("schema") != _LAUNCH_AUTHORITY_SCHEMA
        or authority.get("operation")
        != "dual-upstream-sync-successor-rebind"
        or authority.get("target_revision") != revision
        or any(
            _REVISION.fullmatch(str(authority.get(name, ""))) is None
            for name in (
                "target_revision",
                "predecessor_revision",
                "source_tree_oid",
            )
        )
        or authority.get("target_revision") == authority.get("predecessor_revision")
        or any(
            _SHA256.fullmatch(str(authority.get(name, ""))) is None
            for name in (
                "request_sha256",
                "predecessor_activation_receipt_sha256",
                "predecessor_trust_sha256",
                "stage_c_host_artifact_manifest_sha256",
                "stage_c_release_update_publication_sha256",
                "stage_c_builder_terminal_receipt_sha256",
                "candidate_seal_receipt_sha256",
                "whole_tree_manifest_sha256",
                "input_internal_identities_sha256",
                "launch_authority_sha256",
            )
        )
        or authority.get("release_root")
        != str(
            _CONTROLLER_RELEASES_ROOT
            / f"hermes-agent-{revision[:12]}"
        )
        or authority.get("launch_authority_sha256")
        != expected_launch_authority_sha256
        or authority.get("launch_authority_sha256")
        != _sha(_canonical(authority_unsigned))
        or authority.get("secret_material_recorded") is not False
        or authority.get("secret_digest_recorded") is not False
    ):
        _fail("successor_runtime_preexec_launch_authority_invalid")
    try:
        request_frame = bytes.fromhex(str(envelope["request_frame_hex"]))
    except ValueError as exc:
        _fail("successor_runtime_preexec_owner_frame_invalid", exc)
    if (
        len(request_frame) < 9
        or len(request_frame) > 16 * 1024 + 8
        or request_frame[:4] != b"MSR2"
        or int.from_bytes(request_frame[4:8], "big") != len(request_frame) - 8
    ):
        _fail("successor_runtime_preexec_owner_frame_invalid")
    try:
        request = json.loads(request_frame[8:].decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_runtime_preexec_owner_frame_invalid", exc)
    request_unsigned = (
        {
            name: item
            for name, item in request.items()
            if name != "request_sha256"
        }
        if isinstance(request, Mapping)
        else {}
    )
    if (
        not isinstance(request, Mapping)
        or set(request) != _OWNER_REQUEST_FIELDS
        or request_frame[8:] != _canonical(request)
        or request.get("schema") != _OWNER_REQUEST_SCHEMA
        or request.get("operation") != _OWNER_OPERATION
        or any(
            _REVISION.fullmatch(str(request.get(name, ""))) is None
            for name in _OWNER_REQUEST_REVISION_FIELDS
        )
        or request.get("target_revision") == request.get("predecessor_revision")
        or any(
            _SHA256.fullmatch(str(request.get(name, ""))) is None
            for name in _OWNER_REQUEST_DIGEST_FIELDS
        )
        or any(
            request.get(name) is not False
            for name in _OWNER_REQUEST_POLICY_FIELDS
        )
        or request.get("target_revision") != revision
        or request.get("request_sha256") != _sha(_canonical(request_unsigned))
        or request.get("request_sha256") != authority.get("request_sha256")
        or request.get("predecessor_revision")
        != authority.get("predecessor_revision")
        or request.get("predecessor_activation_receipt_sha256")
        != authority.get("predecessor_activation_receipt_sha256")
        or request.get("stage_c_host_artifact_manifest_sha256")
        != authority.get("stage_c_host_artifact_manifest_sha256")
        or request.get("stage_c_release_update_publication_sha256")
        != authority.get("stage_c_release_update_publication_sha256")
        or request.get("stage_c_builder_terminal_receipt_sha256")
        != authority.get("stage_c_builder_terminal_receipt_sha256")
        or request.get("source_tree_oid") != authority.get("source_tree_oid")
    ):
        _fail("successor_runtime_preexec_owner_frame_invalid")
    return request_frame


def _install_sealed_stdin(raw: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.memfd_create(
            "muncho-successor-owner-request",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("successor_runtime_preexec_owner_frame_invalid")
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(
            descriptor,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.dup2(descriptor, 0, inheritable=True)
    except (AttributeError, OSError) as exc:
        _fail("successor_runtime_preexec_owner_frame_invalid", exc)
    finally:
        if descriptor not in {None, 0}:
            os.close(descriptor)


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _hash_regular(
    path: Path,
    *,
    maximum: int = _MAX_BYTES,
    uid: int = 0,
    gid: int = 0,
) -> tuple[str, os.stat_result]:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            _fail("successor_runtime_preexec_tree_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except SuccessorRuntimePreExecError:
        raise
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or remaining != 0
    ):
        _fail("successor_runtime_preexec_tree_changed")
    return digest.hexdigest(), before


def _directory(
    path: Path,
    *,
    mode: int | None = None,
    uid: int = 0,
    gid: int = 0,
    writable_allowed: bool = False,
) -> os.stat_result:
    try:
        item = os.lstat(path)
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or (not writable_allowed and stat.S_IMODE(item.st_mode) & 0o222)
        or (mode is not None and stat.S_IMODE(item.st_mode) != mode)
    ):
        _fail("successor_runtime_preexec_tree_invalid")
    return item


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _entry(
    path: Path,
    root: Path,
    *,
    uid: int,
    gid: int,
) -> tuple[dict[str, Any], int]:
    try:
        item = os.lstat(path)
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    relative = path.relative_to(root).as_posix()
    common = {
        "path": relative,
        "mode": f"{stat.S_IMODE(item.st_mode):04o}",
        "uid": item.st_uid,
        "gid": item.st_gid,
    }
    if item.st_uid != uid or item.st_gid != gid:
        _fail("successor_runtime_preexec_tree_invalid")
    if stat.S_ISDIR(item.st_mode):
        if stat.S_IMODE(item.st_mode) & 0o222:
            _fail("successor_runtime_preexec_tree_invalid")
        return {**common, "kind": "directory"}, 0
    if stat.S_ISREG(item.st_mode):
        digest, checked = _hash_regular(path, uid=uid, gid=gid)
        return {
            **common,
            "kind": "file",
            "size": checked.st_size,
            "sha256": digest,
        }, checked.st_size
    if stat.S_ISLNK(item.st_mode):
        try:
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            _fail("successor_runtime_preexec_tree_invalid", exc)
        if not target or not _within(resolved, root):
            _fail("successor_runtime_preexec_tree_invalid")
        return {**common, "kind": "symlink", "target": target}, 0
    _fail("successor_runtime_preexec_tree_invalid")


def _collect(
    root: Path,
    *,
    uid: int,
    gid: int,
) -> tuple[list[dict[str, Any]], int]:
    _directory(root, mode=0o555, uid=uid, gid=gid)
    entries: list[dict[str, Any]] = []
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for name in (*directories, *files):
            path = Path(current) / name
            if path == root / MANIFEST_NAME:
                continue
            value, size = _entry(path, root, uid=uid, gid=gid)
            entries.append(value)
            total += size
            if len(entries) > _MAX_ENTRIES or total > _MAX_BYTES:
                _fail("successor_runtime_preexec_tree_oversized")
    entries.sort(key=lambda item: item["path"])
    if len({item["path"] for item in entries}) != len(entries):
        _fail("successor_runtime_preexec_tree_invalid")
    return entries, total


def _manifest(root: Path, *, uid: int, gid: int) -> Mapping[str, Any]:
    path = root / MANIFEST_NAME
    digest, item = _hash_regular(
        path,
        maximum=_MAX_MANIFEST,
        uid=uid,
        gid=gid,
    )
    if stat.S_IMODE(item.st_mode) != 0o444 or item.st_size == 0:
        _fail("successor_runtime_preexec_manifest_invalid")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_runtime_preexec_manifest_invalid", exc)
    if not isinstance(value, Mapping) or raw != _canonical(value) + b"\n":
        _fail("successor_runtime_preexec_manifest_invalid")
    unsigned = {name: item for name, item in value.items() if name != "manifest_sha256"}
    if (
        set(value) != _MANIFEST_FIELDS
        or value.get("manifest_sha256") != _sha(_canonical(unsigned))
        or digest != _sha(raw)
    ):
        _fail("successor_runtime_preexec_manifest_invalid")
    return value


def verify(
    *,
    revision: str,
    expected_manifest_sha256: str,
    expected_tree_sha256: str,
    expected_interpreter_sha256: str,
    expected_attestation_sha256: str,
    runtime_base: Path = RUNTIME_BASE,
    uid: int = 0,
    gid: int = 0,
    runtime_base_mode: int = 0o755,
    physical_root: Path | None = None,
) -> Path:
    if _REVISION.fullmatch(revision or "") is None or any(
        _SHA256.fullmatch(value or "") is None
        for value in (
            expected_manifest_sha256,
            expected_tree_sha256,
            expected_interpreter_sha256,
            expected_attestation_sha256,
        )
    ):
        _fail("successor_runtime_preexec_identity_invalid")
    _directory(
        runtime_base,
        mode=runtime_base_mode,
        uid=uid,
        gid=gid,
        writable_allowed=True,
    )
    logical_root = runtime_base / revision
    root = physical_root or logical_root
    manifest = _manifest(root, uid=uid, gid=gid)
    entries, total = _collect(root, uid=uid, gid=gid)
    interpreter = root / "venv/bin/python"
    interpreter_sha256, interpreter_state = _hash_regular(
        interpreter,
        uid=uid,
        gid=gid,
    )
    pyvenv = root / "venv/pyvenv.cfg"
    pyvenv_sha256, pyvenv_state = _hash_regular(
        pyvenv,
        uid=uid,
        gid=gid,
    )
    site_packages = root / "venv/lib/python3.11/site-packages"
    logical_site_packages = logical_root / "venv/lib/python3.11/site-packages"
    _directory(site_packages, uid=uid, gid=gid)
    if list(site_packages.glob("*.pth")) or list(site_packages.glob("*.egg-link")):
        _fail("successor_runtime_preexec_dynamic_site_path_forbidden")
    for direct_url in site_packages.glob("*.dist-info/direct_url.json"):
        try:
            value = json.loads(direct_url.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail("successor_runtime_preexec_direct_url_invalid", exc)
        directory = value.get("dir_info") if isinstance(value, Mapping) else None
        url = value.get("url") if isinstance(value, Mapping) else None
        if (isinstance(directory, Mapping) and directory.get("editable") is True) or (
            isinstance(url, str)
            and url.startswith("file://")
            and not _within(Path(url.removeprefix("file://")), logical_root)
        ):
            _fail("successor_runtime_preexec_direct_url_invalid")
    required = manifest.get("required_modules")
    sys_path = manifest.get("sys_path")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("revision") != revision
        or manifest.get("artifact_root") != str(logical_root)
        or manifest.get("python_version") != PYTHON_VERSION
        or manifest.get("site_packages") != str(logical_site_packages)
        or manifest.get("root_uid") != uid
        or manifest.get("root_gid") != gid
        or manifest.get("root_mode") != "0555"
        or manifest.get("entries") != entries
        or manifest.get("entry_count") != len(entries)
        or manifest.get("tree_bytes") != total
        or manifest.get("tree_sha256") != _sha(_canonical(entries))
        or manifest.get("tree_sha256") != expected_tree_sha256
        or manifest.get("manifest_sha256") != expected_manifest_sha256
        or not isinstance(required, Mapping)
        or not required
        or not isinstance(sys_path, list)
        or not sys_path
        or len(sys_path) != len(set(sys_path))
        or str(logical_site_packages) not in sys_path
        or manifest.get("secret_material_recorded") is not False
        or manifest.get("secret_digest_recorded") is not False
    ):
        _fail("successor_runtime_preexec_manifest_invalid")
    interpreter_record = manifest.get("interpreter")
    pyvenv_record = manifest.get("pyvenv_cfg")
    if (
        not isinstance(interpreter_record, Mapping)
        or interpreter_record.get("path") != str(logical_root / "venv/bin/python")
        or interpreter_record.get("realpath") != str(logical_root / "venv/bin/python")
        or interpreter_record.get("mode") != "0555"
        or stat.S_IMODE(interpreter_state.st_mode) != 0o555
        or interpreter_record.get("size") != interpreter_state.st_size
        or interpreter_record.get("sha256") != interpreter_sha256
        or interpreter_sha256 != expected_interpreter_sha256
        or not isinstance(pyvenv_record, Mapping)
        or pyvenv_record.get("path") != str(logical_root / "venv/pyvenv.cfg")
        or pyvenv_record.get("mode") != "0444"
        or stat.S_IMODE(pyvenv_state.st_mode) != 0o444
        or pyvenv_record.get("size") != pyvenv_state.st_size
        or pyvenv_record.get("sha256") != pyvenv_sha256
    ):
        _fail("successor_runtime_preexec_interpreter_invalid")
    entry_by_path = {item["path"]: item for item in entries}
    for record in required.values():
        if (
            not isinstance(record, Mapping)
            or set(record) != {"origin", "relative_path", "sha256"}
            or record.get("origin")
            != str(logical_root / str(record.get("relative_path")))
            or entry_by_path.get(record.get("relative_path"), {}).get("sha256")
            != record.get("sha256")
        ):
            _fail("successor_runtime_preexec_required_module_invalid")
    if any(
        not isinstance(item, str)
        or not os.path.isabs(item)
        or not _within(Path(item), logical_root)
        for item in sys_path
    ):
        _fail("successor_runtime_preexec_sys_path_invalid")
    attestation_unsigned = {
        "schema": ATTESTATION_SCHEMA,
        "revision": revision,
        "manifest_sha256": manifest["manifest_sha256"],
        "tree_sha256": manifest["tree_sha256"],
        "interpreter_sha256": interpreter_sha256,
        "pyvenv_cfg_sha256": pyvenv_sha256,
        "sys_path_sha256": _sha(_canonical(sys_path)),
        "required_modules_sha256": _sha(_canonical(required)),
        "module_origins_release_local": True,
        "ambient_python_environment_present": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if _sha(_canonical(attestation_unsigned)) != expected_attestation_sha256:
        _fail("successor_runtime_preexec_attestation_invalid")
    return interpreter


def verify_staged(
    *,
    root: Path,
    revision: str,
    expected_manifest_sha256: str,
    expected_tree_sha256: str,
    expected_interpreter_sha256: str,
    expected_attestation_sha256: str,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    """Verify builder-owned staging without executing its interpreter."""

    if root != root.parent / revision:
        _fail("successor_runtime_preexec_identity_invalid")
    verify(
        revision=revision,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_tree_sha256=expected_tree_sha256,
        expected_interpreter_sha256=expected_interpreter_sha256,
        expected_attestation_sha256=expected_attestation_sha256,
        runtime_base=root.parent,
        uid=uid,
        gid=gid,
        runtime_base_mode=0o700,
    )
    return dict(_manifest(root, uid=uid, gid=gid))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 6:
        _fail("successor_runtime_preexec_argv_invalid")
    (
        revision,
        manifest_sha,
        tree_sha,
        interpreter_sha,
        attestation_sha,
        launch_authority_sha256,
    ) = arguments
    if _SHA256.fullmatch(launch_authority_sha256 or "") is None:
        _fail("successor_runtime_preexec_argv_invalid")
    envelope_raw = sys.stdin.buffer.read(_LAUNCH_ENVELOPE_MAX_BYTES + 1)
    request_frame = _decode_launch_envelope(
        envelope_raw,
        revision=revision,
        expected_launch_authority_sha256=launch_authority_sha256,
    )
    interpreter = verify(
        revision=revision,
        expected_manifest_sha256=manifest_sha,
        expected_tree_sha256=tree_sha,
        expected_interpreter_sha256=interpreter_sha,
        expected_attestation_sha256=attestation_sha,
    )
    environment = {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    _install_sealed_stdin(request_frame)
    os.execve(
        interpreter,
        (
            str(interpreter),
            "-I",
            "-B",
            "-m",
            "gateway.production_owner_runtime",
            "--revision",
            revision,
            "run",
            "--",
            "upstream-sync-successor-owner-apply",
            "--revision",
            revision,
            "--launch-authority-sha256",
            launch_authority_sha256,
        ),
        environment,
    )
    _fail("successor_runtime_preexec_exec_returned")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SuccessorRuntimePreExecError:
        print(
            '{"error_code":"successor_runtime_preexec_failed","ok":false}',
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = [
    "RUNTIME_BASE",
    "SuccessorRuntimePreExecError",
    "main",
    "verify",
    "verify_staged",
]
