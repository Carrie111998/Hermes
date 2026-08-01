#!/usr/bin/env python3
"""Root-only promotion of one offline-built production release candidate.

The builder terminal receipt and the root candidate-seal receipt are separate
authorities.  This phase first validates the stopped builder, every root input,
and the complete builder-owned candidate.  It then copies the candidate into a
deterministic hidden directory on the release filesystem, verifies the copy,
renames it without replacement, and invokes the root publication primitive.

Crash behavior is intentionally narrow:

* an exact hidden staging tree or exact renamed builder-owned tree is resumed;
* an exact already-published release is idempotent;
* every partial or conflicting state is left in place and requires explicit
  root cleanup before another attempt.

No target release file is imported or executed.  The phase has no Git, shell,
package-index, or network path.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import resource
import stat
import subprocess
import sys
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, NoReturn, Sequence

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as builder


PROMOTION_RESULT_SCHEMA = "muncho-production-release-candidate-promotion.v1"
PRODUCTION_RELEASE_PARENT = Path(
    "/opt/adventico-ai-platform/hermes-agent-releases"
)
PRODUCTION_BUILDER_UNIT_FRAGMENT = Path(
    "/etc/systemd/system/muncho-release-builder@.service"
)
PRODUCTION_BUILDER_WRAPPER = Path(
    "/usr/libexec/muncho-release-builder-phase"
)
PRODUCTION_PROMOTION_INTERLOCK = Path(
    "/run/lock/muncho-release-builder-promotion.lock"
)
PRODUCTION_BUILDER_UNIT_FRAGMENT_SHA256 = (
    "6964bc051d08a9024bc95703ddd4910804369b9e4596f1056703abde4bce6eb7"
)
PRODUCTION_BUILDER_WRAPPER_SHA256 = (
    "a4a5d5631335284b05d9ffdac2abb3c9a0e2a666854630fec656fb5ec4e8ff2e"
)
PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT = Path(
    "/etc/systemd/system/muncho-release-builder-v2@.service"
)
PRODUCTION_REVISION_BUILDER_WRAPPER = Path(
    "/usr/libexec/muncho-release-foundation-exec-v2"
)
PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT_SHA256 = (
    "821bc34ffbdce9ff1d2c4631277eb154feaaf7bf8f57fc7824f19b4119589ab4"
)
PRODUCTION_REVISION_BUILDER_WRAPPER_SHA256 = (
    "e4f869d9621e1b66654cb46421b365a5b2754b5bb1cd9da2b947886b47fa88b1"
)
PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT = Path(
    "/etc/systemd/system/muncho-release-builder-v3@.service"
)
PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER = Path(
    "/usr/libexec/muncho-release-foundation-exec-v3"
)
PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT_SHA256 = (
    "1a2e1a99b76ce7f841d4db418d7337b812dca90d17de5d56d92dff944c75f338"
)
PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER_SHA256 = (
    "e4f869d9621e1b66654cb46421b365a5b2754b5bb1cd9da2b947886b47fa88b1"
)
SYSTEMCTL = Path("/usr/bin/systemctl")
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SYSTEMCTL_BYTES = 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RESULT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "release_root",
    "builder_terminal_receipt_sha256",
    "candidate_seal_receipt_sha256",
    "candidate_seal_receipt_file_sha256",
    "whole_tree_manifest_sha256",
    "whole_tree_manifest_file_sha256",
    "process_free_evidence_sha256",
    "completed",
    "secret_material_recorded",
    "secret_digest_recorded",
    "result_sha256",
})
_ROOT_DIRECTORY_MODES = frozenset({0o555, 0o700, 0o750, 0o755})
_ROOT_FILE_MODES = frozenset({0o400, 0o440, 0o444})
_ROOT_EXECUTABLE_MODES = frozenset({0o500, 0o550, 0o555, 0o755})
_BUILDER_DIRECTORY_MODES = frozenset({0o555})
_BUILDER_FILE_MODES = frozenset({0o444, 0o555})
_INPUT_DESCRIPTOR_HEADROOM = 64
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_SYSTEMD_PROPERTY_NAMES = (
    "Id",
    "FragmentPath",
    "DropInPaths",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainPID",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "InvocationID",
    "ControlGroup",
)
_PRODUCTION_BASE_IDENTITY_NAMES = (
    "ai-platform-brain",
    "muncho-canonical-writer",
    "muncho-projector",
    "muncho-discord-egress",
    "muncho-discord-connector",
    "muncho-mac-ops-edge",
    "muncho-capability-browser",
    "muncho-worker",
)
_PRODUCTION_OPERATIONAL_EDGE_DOMAINS = (
    "adventico_email",
    "bitrix",
    "canonical",
    "github",
    "infrastructure",
    "skyvision_backup",
    "skyvision_db",
    "skyvision_email",
    "skyvision_gitlab",
    "skyvision_panel",
    "skyvision_seo",
)
_PRODUCTION_RUNTIME_USER_NAMES = (
    _PRODUCTION_BASE_IDENTITY_NAMES
    + tuple(
        f"muncho-edge-{domain}"
        for domain in _PRODUCTION_OPERATIONAL_EDGE_DOMAINS
    )
)
_PRODUCTION_RUNTIME_GROUP_NAMES = (
    _PRODUCTION_RUNTIME_USER_NAMES
    + ("muncho-writer-client", "muncho-worker-clients")
    + tuple(
        f"muncho-edge-{domain}-c"
        for domain in _PRODUCTION_OPERATIONAL_EDGE_DOMAINS
    )
)
_EXPECTED_RUNTIME_UID_COUNT = 19
_EXPECTED_RUNTIME_GID_COUNT = 32
_PRODUCTION_RUNTIME_UID_BY_NAME = {
    "ai-platform-brain": 999,
    "muncho-canonical-writer": 2000,
    "muncho-projector": 2004,
    "muncho-discord-egress": 2002,
    "muncho-discord-connector": 2001,
    "muncho-mac-ops-edge": 2003,
    "muncho-capability-browser": 2006,
    "muncho-worker": 2007,
    **{
        f"muncho-edge-{domain}": 2100 + index
        for index, domain in enumerate(
            _PRODUCTION_OPERATIONAL_EDGE_DOMAINS
        )
    },
}
_PRODUCTION_RUNTIME_GID_BY_NAME = {
    "ai-platform-brain": 994,
    "muncho-canonical-writer": 2000,
    "muncho-projector": 2004,
    "muncho-discord-egress": 2002,
    "muncho-discord-connector": 2001,
    "muncho-mac-ops-edge": 2003,
    "muncho-capability-browser": 2006,
    "muncho-worker": 2007,
    "muncho-writer-client": 2005,
    "muncho-worker-clients": 2008,
    **{
        f"muncho-edge-{domain}": 2100 + index
        for index, domain in enumerate(
            _PRODUCTION_OPERATIONAL_EDGE_DOMAINS
        )
    },
    **{
        f"muncho-edge-{domain}-c": 2200 + index
        for index, domain in enumerate(
            _PRODUCTION_OPERATIONAL_EDGE_DOMAINS
        )
    },
}


class ProductionReleaseCandidatePromoterError(RuntimeError):
    """Stable, secret-free failure at the root promotion boundary."""


def _fail(code: str, exc: BaseException | None = None) -> NoReturn:
    del exc
    raise ProductionReleaseCandidatePromoterError(code) from None


def _read_posix_identity(name: Literal["geteuid", "getegid"]) -> int:
    reader = getattr(os, name, None)
    if not callable(reader):
        _fail("candidate_promoter_posix_identity_unavailable")
    try:
        value = reader()
    except (OSError, TypeError, ValueError) as exc:
        _fail("candidate_promoter_posix_identity_unavailable", exc)
    if type(value) is not int or value < 0:
        _fail("candidate_promoter_posix_identity_unavailable")
    return value


def canonical_bytes(value: Any) -> bytes:
    return phase.canonical_bytes(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class PromoterRoots:
    job_root: Path
    release_parent: Path
    builder_unit_fragment: Path
    builder_wrapper: Path
    promotion_interlock: Path
    builder_unit_prefix: str = "muncho-release-builder@"
    cgroup_root: Path = Path("/sys/fs/cgroup")
    proc_root: Path = Path("/proc")


@dataclass(frozen=True)
class _PromotionBinding:
    request_schema: str
    request_purpose: str | None
    terminal_receipt_schema: str
    terminal_receipt_purpose: str | None
    entrypoint_relative_path: str


_RELEASE_UPDATER_PROMOTION_BINDING = _PromotionBinding(
    request_schema=phase.REQUEST_SCHEMA,
    request_purpose=None,
    terminal_receipt_schema=phase.TERMINAL_RECEIPT_SCHEMA,
    terminal_receipt_purpose=None,
    entrypoint_relative_path=phase.ENTRYPOINT_RELATIVE_PATH,
)
_UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING = _PromotionBinding(
    request_schema=phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
    request_purpose=phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
    terminal_receipt_schema=(
        phase.UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA
    ),
    terminal_receipt_purpose=phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
    entrypoint_relative_path=(
        phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    ),
)


def production_roots() -> PromoterRoots:
    return PromoterRoots(
        job_root=phase.PRODUCTION_JOB_ROOT,
        release_parent=PRODUCTION_RELEASE_PARENT,
        builder_unit_fragment=PRODUCTION_BUILDER_UNIT_FRAGMENT,
        builder_wrapper=PRODUCTION_BUILDER_WRAPPER,
        promotion_interlock=PRODUCTION_PROMOTION_INTERLOCK,
    )


def production_revision_roots() -> PromoterRoots:
    return PromoterRoots(
        job_root=phase.PRODUCTION_JOB_ROOT,
        release_parent=PRODUCTION_RELEASE_PARENT,
        builder_unit_fragment=PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT,
        builder_wrapper=PRODUCTION_REVISION_BUILDER_WRAPPER,
        promotion_interlock=PRODUCTION_PROMOTION_INTERLOCK,
        builder_unit_prefix="muncho-release-builder-v2@",
    )


def production_latched_revision_roots() -> PromoterRoots:
    return PromoterRoots(
        job_root=phase.PRODUCTION_JOB_ROOT,
        release_parent=PRODUCTION_RELEASE_PARENT,
        builder_unit_fragment=(
            PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT
        ),
        builder_wrapper=PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER,
        promotion_interlock=PRODUCTION_PROMOTION_INTERLOCK,
        builder_unit_prefix="muncho-release-builder-v3@",
    )


@dataclass(frozen=True)
class _InputBundle:
    request: Mapping[str, Any]
    source_manifest: Mapping[str, Any]
    runtime_manifest: Mapping[str, Any]
    request_file_sha256: str
    source_file_sha256: str
    runtime_file_sha256: str
    uv_sha256: str
    python_sha256: str
    input_root: Path
    output_root: Path
    candidate_root: Path
    held_directories: tuple[
        tuple[phase.HeldDirectory, tuple[str, ...]],
        ...,
    ]
    held_files: tuple[builder.HeldRegularFile, ...]
    xattr_reader: Callable[[int], Sequence[str | bytes]]

    def assert_stable(self) -> None:
        for directory, expected_names in self.held_directories:
            if directory.names() != expected_names:
                _fail("candidate_promoter_root_input_changed")
            _assert_no_xattrs(
                directory.descriptor,
                xattr_reader=self.xattr_reader,
            )
        for held in self.held_files:
            try:
                held.assert_stable()
                _assert_no_xattrs(
                    held.descriptor,
                    xattr_reader=self.xattr_reader,
                )
                if (
                    builder._hash_descriptor(
                        held.descriptor,
                        size=held.identity.size,
                    )
                    != held.sha256
                ):
                    _fail("candidate_promoter_root_input_changed")
            except builder.ProductionReleaseBuilderError as exc:
                _fail("candidate_promoter_root_input_changed", exc)


@dataclass(frozen=True)
class _CandidateBundle:
    payload_manifest: Mapping[str, Any]
    terminal_receipt: Mapping[str, Any]
    terminal_file_sha256: str
    records: tuple[Mapping[str, Any], ...]


def _decode_document(raw: bytes) -> Mapping[str, Any]:
    if (
        not isinstance(raw, bytes)
        or not 1 < len(raw) <= MAX_JSON_BYTES
        or not raw.endswith(b"\n")
        or b"\n" in raw[:-1]
    ):
        _fail("candidate_promoter_document_invalid")

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
        _fail("candidate_promoter_document_invalid", exc)
    if (
        not isinstance(value, Mapping)
        or raw != canonical_bytes(value) + b"\n"
    ):
        _fail("candidate_promoter_document_invalid")
    return dict(value)


def _read_held(held: builder.HeldRegularFile) -> bytes:
    try:
        raw = os.pread(held.descriptor, held.identity.size, 0)
    except OSError as exc:
        _fail("candidate_promoter_file_unavailable", exc)
    if len(raw) != held.identity.size:
        _fail("candidate_promoter_file_changed")
    held.assert_stable()
    return raw


def _validate_roots(
    roots: PromoterRoots,
    *,
    production: bool,
) -> PromoterRoots:
    if not isinstance(roots, PromoterRoots):
        _fail("candidate_promoter_roots_invalid")
    try:
        normalized = PromoterRoots(
            job_root=Path(roots.job_root),
            release_parent=Path(roots.release_parent),
            builder_unit_fragment=Path(roots.builder_unit_fragment),
            builder_wrapper=Path(roots.builder_wrapper),
            promotion_interlock=Path(roots.promotion_interlock),
            builder_unit_prefix=str(roots.builder_unit_prefix),
            cgroup_root=Path(roots.cgroup_root),
            proc_root=Path(roots.proc_root),
        )
    except (TypeError, ValueError) as exc:
        _fail("candidate_promoter_roots_invalid", exc)
    paths = (
        normalized.job_root,
        normalized.release_parent,
        normalized.builder_unit_fragment,
        normalized.builder_wrapper,
        normalized.promotion_interlock,
        normalized.cgroup_root,
        normalized.proc_root,
    )
    if any(
        not path.is_absolute()
        or "\x00" in str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        for path in paths
    ) or normalized.builder_unit_prefix not in {
        "muncho-release-builder@",
        "muncho-release-builder-v2@",
        "muncho-release-builder-v3@",
    }:
        _fail("candidate_promoter_roots_invalid")
    if (
        production
        and normalized
        not in {
            production_roots(),
            production_revision_roots(),
            production_latched_revision_roots(),
        }
        or not production
        and normalized
        in {
            production_roots(),
            production_revision_roots(),
            production_latched_revision_roots(),
        }
    ):
        _fail("candidate_promoter_roots_invalid")
    return normalized


def _assert_no_xattrs(
    descriptor: int,
    *,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> None:
    try:
        names = xattr_reader(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        _fail("candidate_promoter_xattr_inspection_unavailable", exc)
    if (
        not isinstance(names, (list, tuple))
        or any(
            not isinstance(name, (str, bytes)) or not name
            for name in names
        )
    ):
        _fail("candidate_promoter_xattr_inspection_unavailable")
    if names:
        _fail("candidate_promoter_xattrs_or_acl_present")


def _reserve_input_descriptor_capacity(
    *,
    source_blob_count: int,
    runtime_wheel_count: int,
) -> None:
    """Keep every verified root input inode held without exhausting nofile."""

    if (
        type(source_blob_count) is not int
        or not 0 < source_blob_count <= phase.MAX_SOURCE_BLOBS
        or type(runtime_wheel_count) is not int
        or not 0 < runtime_wheel_count <= phase.MAX_WHEELS
    ):
        _fail("candidate_promoter_descriptor_capacity_invalid")
    required = (
        source_blob_count
        + runtime_wheel_count
        + _INPUT_DESCRIPTOR_HEADROOM
    )
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        _fail("candidate_promoter_descriptor_capacity_unavailable", exc)
    if (
        type(soft) is not int
        or type(hard) is not int
        or (soft < 0 and soft != resource.RLIM_INFINITY)
        or (hard < 0 and hard != resource.RLIM_INFINITY)
    ):
        _fail("candidate_promoter_descriptor_capacity_unavailable")
    if hard != resource.RLIM_INFINITY and hard < required:
        _fail("candidate_promoter_descriptor_capacity_insufficient")
    if soft == resource.RLIM_INFINITY or soft >= required:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (required, hard))
        confirmed_soft, confirmed_hard = resource.getrlimit(
            resource.RLIMIT_NOFILE
        )
    except (OSError, ValueError) as exc:
        _fail("candidate_promoter_descriptor_capacity_unavailable", exc)
    if (
        type(confirmed_soft) is not int
        or type(confirmed_hard) is not int
        or (
            confirmed_soft != resource.RLIM_INFINITY
            and confirmed_soft < required
        )
        or confirmed_hard != hard
    ):
        _fail("candidate_promoter_descriptor_capacity_unavailable")


def _open_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    xattr_reader: Callable[[int], Sequence[str | bytes]] | None = None,
) -> phase.HeldDirectory:
    held: phase.HeldDirectory | None = None
    try:
        held = phase._open_directory(
            path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=allowed_modes,
        )
        if xattr_reader is not None:
            _assert_no_xattrs(
                held.descriptor,
                xattr_reader=xattr_reader,
            )
        return held
    except ProductionReleaseCandidatePromoterError:
        if held is not None:
            held.close()
        raise
    except (
        phase.ProductionReleaseBuilderPhaseError,
        OSError,
        RuntimeError,
    ) as exc:
        if held is not None:
            held.close()
        _fail("candidate_promoter_directory_invalid", exc)


def _open_root_file(
    stack: ExitStack,
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    maximum_bytes: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
    expected_sha256: str | None = None,
    require_nonempty: bool = True,
) -> builder.HeldRegularFile:
    try:
        held = stack.enter_context(
            builder.open_held_regular(
                path,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=allowed_modes,
                maximum_bytes=maximum_bytes,
                expected_sha256=expected_sha256,
                require_nonempty=require_nonempty,
            )
        )
        _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
        return held
    except builder.ProductionReleaseBuilderError as exc:
        _fail("candidate_promoter_root_input_invalid", exc)


def _derive_production_release_identities(
    *,
    user_lookup: Callable[[str], Any] = pwd.getpwnam,
    user_id_lookup: Callable[[int], Any] = pwd.getpwuid,
    group_lookup: Callable[[str], Any] = grp.getgrnam,
    group_id_lookup: Callable[[int], Any] = grp.getgrgid,
) -> builder.ReleaseIdentities:
    """Reserve the exact cutover identity catalog before host activation."""

    if (
        set(_PRODUCTION_RUNTIME_UID_BY_NAME)
        != set(_PRODUCTION_RUNTIME_USER_NAMES)
        or set(_PRODUCTION_RUNTIME_GID_BY_NAME)
        != set(_PRODUCTION_RUNTIME_GROUP_NAMES)
        or len(_PRODUCTION_RUNTIME_UID_BY_NAME)
        != _EXPECTED_RUNTIME_UID_COUNT
        or len(_PRODUCTION_RUNTIME_GID_BY_NAME)
        != _EXPECTED_RUNTIME_GID_COUNT
        or len(set(_PRODUCTION_RUNTIME_UID_BY_NAME.values()))
        != _EXPECTED_RUNTIME_UID_COUNT
        or len(set(_PRODUCTION_RUNTIME_GID_BY_NAME.values()))
        != _EXPECTED_RUNTIME_GID_COUNT
    ):
        _fail("candidate_promoter_identity_contract_invalid")
    try:
        for name in _PRODUCTION_RUNTIME_USER_NAMES:
            expected_uid = _PRODUCTION_RUNTIME_UID_BY_NAME[name]
            expected_gid = _PRODUCTION_RUNTIME_GID_BY_NAME[name]
            try:
                item = user_lookup(name)
            except KeyError:
                try:
                    user_id_lookup(expected_uid)
                except KeyError:
                    continue
                _fail("candidate_promoter_identity_contract_invalid")
            if (
                item.pw_name != name
                or item.pw_uid != expected_uid
                or item.pw_gid != expected_gid
            ):
                _fail("candidate_promoter_identity_contract_invalid")
        for name in _PRODUCTION_RUNTIME_GROUP_NAMES:
            expected_gid = _PRODUCTION_RUNTIME_GID_BY_NAME[name]
            try:
                item = group_lookup(name)
            except KeyError:
                try:
                    group_id_lookup(expected_gid)
                except KeyError:
                    continue
                _fail("candidate_promoter_identity_contract_invalid")
            if item.gr_name != name or item.gr_gid != expected_gid:
                _fail("candidate_promoter_identity_contract_invalid")
    except ProductionReleaseCandidatePromoterError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        _fail("candidate_promoter_identity_contract_invalid", exc)
    identities = builder.ReleaseIdentities(
        builder_uid=phase.BUILDER_UID,
        builder_gid=phase.BUILDER_GID,
        reserved_runtime_uids=tuple(
            sorted(_PRODUCTION_RUNTIME_UID_BY_NAME.values())
        ),
        reserved_runtime_gids=tuple(
            sorted(_PRODUCTION_RUNTIME_GID_BY_NAME.values())
        )
    )
    try:
        return builder.validate_release_identities(
            identities,
            require_effective_root=True,
        )
    except builder.ProductionReleaseBuilderError as exc:
        _fail("candidate_promoter_identity_contract_invalid", exc)


def _systemctl_show(unit: str) -> Mapping[str, str]:
    """Collect one fixed, complete systemd observation without a shell."""

    argv = (
        str(SYSTEMCTL),
        "show",
        "--no-pager",
        *(f"--property={name}" for name in _SYSTEMD_PROPERTY_NAMES),
        "--",
        unit,
    )
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env={
                "HOME": "/",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("candidate_promoter_systemd_observation_failed", exc)
    raw = completed.stdout
    if (
        completed.returncode != 0
        or not isinstance(raw, bytes)
        or not 0 < len(raw) <= MAX_SYSTEMCTL_BYTES
        or not raw.endswith(b"\n")
        or b"\x00" in raw
    ):
        _fail("candidate_promoter_systemd_observation_failed")
    result: dict[str, str] = {}
    try:
        for line in raw.decode("utf-8", errors="strict").splitlines():
            name, separator, value = line.partition("=")
            if (
                separator != "="
                or name not in _SYSTEMD_PROPERTY_NAMES
                or name in result
            ):
                _fail("candidate_promoter_systemd_observation_invalid")
            result[name] = value
    except UnicodeError as exc:
        _fail("candidate_promoter_systemd_observation_invalid", exc)
    if set(result) != set(_SYSTEMD_PROPERTY_NAMES):
        _fail("candidate_promoter_systemd_observation_invalid")
    return result


@dataclass
class _HeldPromotionInterlock(
    AbstractContextManager["_HeldPromotionInterlock"]
):
    held: builder.HeldRegularFile
    xattr_reader: Callable[[int], Sequence[str | bytes]]
    _locked: bool = False

    def __enter__(self) -> _HeldPromotionInterlock:
        try:
            fcntl.flock(self.held.descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            self.held.close()
            _fail("candidate_promoter_interlock_unavailable", exc)
        self._locked = True
        try:
            self.assert_stable()
        except ProductionReleaseCandidatePromoterError:
            self.__exit__(None, None, None)
            raise
        except (
            builder.ProductionReleaseBuilderError,
            OSError,
            RuntimeError,
        ) as exc:
            self.__exit__(None, None, None)
            _fail("candidate_promoter_interlock_changed", exc)
        return self

    def assert_stable(self) -> None:
        self.held.assert_stable()
        _assert_no_xattrs(
            self.held.descriptor,
            xattr_reader=self.xattr_reader,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        try:
            if self._locked:
                fcntl.flock(self.held.descriptor, fcntl.LOCK_UN)
        finally:
            self._locked = False
            self.held.close()


def _promotion_interlock(
    path: Path,
    *,
    authority_uid: int,
    builder_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> _HeldPromotionInterlock:
    held: builder.HeldRegularFile | None = None
    try:
        held = builder.open_held_regular(
            path,
            expected_uid=authority_uid,
            expected_gid=builder_gid,
            allowed_modes=frozenset({0o440}),
            maximum_bytes=1,
            require_nonempty=False,
        )
        _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
    except ProductionReleaseCandidatePromoterError:
        if held is not None:
            held.close()
        raise
    except (
        builder.ProductionReleaseBuilderError,
        OSError,
        RuntimeError,
    ) as exc:
        if held is not None:
            held.close()
        _fail("candidate_promoter_interlock_invalid", exc)
    assert held is not None
    return _HeldPromotionInterlock(
        held=held,
        xattr_reader=xattr_reader,
    )


def _load_inputs(
    stack: ExitStack,
    *,
    revision: str,
    roots: PromoterRoots,
    binding: _PromotionBinding,
    authority_uid: int,
    authority_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> _InputBundle:
    input_root = roots.job_root / revision / "input"
    output_root = roots.job_root / revision / "output"
    input_directory = stack.enter_context(
        _open_directory(
            input_root,
            expected_uid=authority_uid,
            expected_gid=authority_gid,
            allowed_modes=_ROOT_DIRECTORY_MODES,
            xattr_reader=xattr_reader,
        )
    )
    if input_directory.names() != tuple(sorted(phase._INPUT_ROOT_NAMES)):
        _fail("candidate_promoter_input_set_invalid")
    request_file = _open_root_file(
        stack,
        input_root / phase.REQUEST_NAME,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_FILE_MODES,
        maximum_bytes=MAX_JSON_BYTES,
        xattr_reader=xattr_reader,
    )
    request = phase.validate_request(
        _decode_document(_read_held(request_file)),
        expected_job_id=revision,
    )
    if (
        request.get("schema") != binding.request_schema
        or request.get("entrypoint_relative_path")
        != binding.entrypoint_relative_path
        or (
            binding.request_purpose is None
            and "purpose" in request
        )
        or (
            binding.request_purpose is not None
            and request.get("purpose") != binding.request_purpose
        )
    ):
        _fail("candidate_promoter_request_purpose_invalid")
    source_file = _open_root_file(
        stack,
        input_root / phase.SOURCE_MANIFEST_NAME,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_FILE_MODES,
        maximum_bytes=MAX_JSON_BYTES,
        expected_sha256=str(request["source_v3_manifest_sha256"]),
        xattr_reader=xattr_reader,
    )
    source_manifest = phase.validate_source_manifest(
        _decode_document(_read_held(source_file)),
        request=request,
    )
    runtime_file = _open_root_file(
        stack,
        input_root / phase.RUNTIME_MANIFEST_NAME,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_FILE_MODES,
        maximum_bytes=MAX_JSON_BYTES,
        expected_sha256=str(
            request["runtime_dependency_manifest_sha256"]
        ),
        xattr_reader=xattr_reader,
    )
    runtime_manifest = phase.validate_runtime_manifest(
        _decode_document(_read_held(runtime_file)),
        request=request,
    )
    tree_file = _open_root_file(
        stack,
        input_root / phase.TREE_LISTING_NAME,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_FILE_MODES,
        maximum_bytes=builder.MAX_GIT_TREE_BYTES,
        expected_sha256=str(source_manifest["tree_listing_sha256"]),
        xattr_reader=xattr_reader,
    )
    if tree_file.identity.size != source_manifest["tree_listing_size"]:
        _fail("candidate_promoter_source_input_invalid")
    entries = builder.parse_git_tree(
        _read_held(tree_file),
        object_format=str(source_manifest["object_format"]),
    )
    if (
        len(entries) != source_manifest["tree_entry_count"]
        or builder._reconstruct_git_tree_oid(entries)
        != request["source_tree_oid"]
    ):
        _fail("candidate_promoter_source_input_invalid")

    blob_directory = stack.enter_context(
        _open_directory(
            input_root / phase.SOURCE_BLOB_DIRECTORY_NAME,
            expected_uid=authority_uid,
            expected_gid=authority_gid,
            allowed_modes=_ROOT_DIRECTORY_MODES,
            xattr_reader=xattr_reader,
        )
    )
    blobs = {
        str(item["object_id"]): dict(item)
        for item in source_manifest["blobs"]
    }
    _reserve_input_descriptor_capacity(
        source_blob_count=len(blobs),
        runtime_wheel_count=len(runtime_manifest["wheels"]),
    )
    if (
        set(blobs) != {entry.object_id for entry in entries}
        or blob_directory.names()
        != tuple(sorted(str(item["filename"]) for item in blobs.values()))
    ):
        _fail("candidate_promoter_source_input_invalid")
    object_format = str(source_manifest["object_format"])
    blob_files: list[builder.HeldRegularFile] = []
    for object_id in sorted(blobs):
        item = blobs[object_id]
        held = _open_root_file(
            stack,
            input_root
            / phase.SOURCE_BLOB_DIRECTORY_NAME
            / str(item["filename"]),
            expected_uid=authority_uid,
            expected_gid=authority_gid,
            allowed_modes=_ROOT_FILE_MODES,
            maximum_bytes=builder.MAX_BLOB_BYTES,
            expected_sha256=str(item["sha256"]),
            require_nonempty=False,
            xattr_reader=xattr_reader,
        )
        blob_files.append(held)
        if (
            held.identity.size != item["size"]
            or builder._git_blob_oid(
                held.descriptor,
                size=held.identity.size,
                object_format=object_format,
            )
            != object_id
        ):
            _fail("candidate_promoter_source_input_invalid")
        held.assert_stable()
        _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)

    wheel_directory = stack.enter_context(
        _open_directory(
            input_root / phase.RUNTIME_WHEEL_DIRECTORY_NAME,
            expected_uid=authority_uid,
            expected_gid=authority_gid,
            allowed_modes=_ROOT_DIRECTORY_MODES,
            xattr_reader=xattr_reader,
        )
    )
    wheel_names = tuple(
        sorted(str(item["filename"]) for item in runtime_manifest["wheels"])
    )
    if wheel_directory.names() != wheel_names:
        _fail("candidate_promoter_runtime_input_invalid")
    wheel_files: list[builder.HeldRegularFile] = []
    for item in runtime_manifest["wheels"]:
        held = _open_root_file(
            stack,
            input_root
            / phase.RUNTIME_WHEEL_DIRECTORY_NAME
            / str(item["filename"]),
            expected_uid=authority_uid,
            expected_gid=authority_gid,
            allowed_modes=_ROOT_FILE_MODES,
            maximum_bytes=builder.MAX_WHEEL_BYTES,
            expected_sha256=str(item["sha256"]),
            xattr_reader=xattr_reader,
        )
        wheel_files.append(held)
        if held.identity.size != item["size"]:
            _fail("candidate_promoter_runtime_input_invalid")

    uv_file = _open_root_file(
        stack,
        input_root / phase.UV_NAME,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_EXECUTABLE_MODES,
        maximum_bytes=phase.MAX_UV_BYTES,
        expected_sha256=str(request["uv_sha256"]),
        xattr_reader=xattr_reader,
    )
    python_file = _open_root_file(
        stack,
        Path(str(request["python_executable_path"])),
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_ROOT_EXECUTABLE_MODES,
        maximum_bytes=phase.MAX_PYTHON_BYTES,
        expected_sha256=str(request["python_executable_sha256"]),
        xattr_reader=xattr_reader,
    )
    if (
        uv_file.identity.size != request["uv_size"]
        or python_file.identity.size != request["python_executable_size"]
    ):
        _fail("candidate_promoter_input_binding_invalid")
    if input_directory.names() != tuple(sorted(phase._INPUT_ROOT_NAMES)):
        _fail("candidate_promoter_input_set_invalid")
    input_directory.assert_stable()
    _assert_no_xattrs(
        input_directory.descriptor,
        xattr_reader=xattr_reader,
    )
    bundle = _InputBundle(
        request=dict(request),
        source_manifest=dict(source_manifest),
        runtime_manifest=dict(runtime_manifest),
        request_file_sha256=request_file.sha256,
        source_file_sha256=source_file.sha256,
        runtime_file_sha256=runtime_file.sha256,
        uv_sha256=uv_file.sha256,
        python_sha256=python_file.sha256,
        input_root=input_root,
        output_root=output_root,
        candidate_root=output_root / phase.CANDIDATE_NAME,
        held_directories=(
            (
                input_directory,
                tuple(sorted(phase._INPUT_ROOT_NAMES)),
            ),
            (
                blob_directory,
                tuple(
                    sorted(
                        str(item["filename"])
                        for item in blobs.values()
                    )
                ),
            ),
            (wheel_directory, wheel_names),
        ),
        held_files=(
            request_file,
            source_file,
            runtime_file,
            tree_file,
            *blob_files,
            *wheel_files,
            uv_file,
            python_file,
        ),
        xattr_reader=xattr_reader,
    )
    bundle.assert_stable()
    return bundle


def _record_for_file(
    *,
    path: str,
    mode: int,
    size: int,
    digest: str,
) -> Mapping[str, Any]:
    return {
        "path": path,
        "kind": "file",
        "mode": f"{mode:04o}",
        "uid": phase.BUILDER_UID,
        "gid": phase.BUILDER_GID,
        "size": size,
        "sha256": digest,
        "xattrs": [],
    }


def _record_for_directory(path: str) -> Mapping[str, Any]:
    return {
        "path": path,
        "kind": "directory",
        "mode": "0555",
        "uid": phase.BUILDER_UID,
        "gid": phase.BUILDER_GID,
        "xattrs": [],
    }


def _scan_candidate_tree(
    root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> tuple[Mapping[str, Any], ...]:
    directory = _open_directory(
        root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_modes=_BUILDER_DIRECTORY_MODES,
    )
    records: list[Mapping[str, Any]] = []
    root_device = directory.identity.device

    def visit(descriptor: int, relative: PurePosixPath) -> None:
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as exc:
            _fail("candidate_promoter_candidate_unavailable", exc)
        for name in names:
            phase._validate_component(name)
            try:
                before = builder.FileIdentity.from_stat(
                    os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                )
            except OSError as exc:
                _fail("candidate_promoter_candidate_unavailable", exc)
            path = (
                name
                if str(relative) == "."
                else (relative / name).as_posix()
            )
            if before.device != root_device:
                _fail("candidate_promoter_mount_crossing")
            if stat.S_ISDIR(before.mode) and not stat.S_ISLNK(before.mode):
                child: int | None = None
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                        | os.O_DIRECTORY,
                        dir_fd=descriptor,
                    )
                    opened = builder.FileIdentity.from_stat(os.fstat(child))
                    if (
                        before != opened
                        or opened.uid != expected_uid
                        or opened.gid != expected_gid
                        or stat.S_IMODE(opened.mode) != 0o555
                    ):
                        _fail("candidate_promoter_candidate_invalid")
                    visit(child, PurePosixPath(path))
                    final = builder.FileIdentity.from_stat(os.fstat(child))
                    reachable = builder.FileIdentity.from_stat(
                        os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    )
                    if final != reachable or final != opened:
                        _fail("candidate_promoter_candidate_changed")
                finally:
                    if child is not None:
                        os.close(child)
                records.append(_record_for_directory(path))
            elif stat.S_ISREG(before.mode) and not stat.S_ISLNK(before.mode):
                child = None
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    opened = builder.FileIdentity.from_stat(os.fstat(child))
                    mode = stat.S_IMODE(opened.mode)
                    if (
                        before != opened
                        or opened.uid != expected_uid
                        or opened.gid != expected_gid
                        or opened.links != 1
                        or mode not in _BUILDER_FILE_MODES
                    ):
                        _fail("candidate_promoter_candidate_invalid")
                    _assert_no_xattrs(child, xattr_reader=xattr_reader)
                    digest = builder._hash_descriptor(
                        child, size=opened.size
                    )
                    final = builder.FileIdentity.from_stat(os.fstat(child))
                    reachable = builder.FileIdentity.from_stat(
                        os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    )
                    _assert_no_xattrs(child, xattr_reader=xattr_reader)
                    if (
                        opened != final
                        or opened != reachable
                        or builder._hash_descriptor(
                            child, size=opened.size
                        )
                        != digest
                    ):
                        _fail("candidate_promoter_candidate_changed")
                finally:
                    if child is not None:
                        os.close(child)
                records.append(
                    _record_for_file(
                        path=path,
                        mode=mode,
                        size=before.size,
                        digest=digest,
                    )
                )
            else:
                _fail("candidate_promoter_special_entry")
        try:
            if tuple(sorted(os.listdir(descriptor))) != names:
                _fail("candidate_promoter_candidate_changed")
        except OSError as exc:
            _fail("candidate_promoter_candidate_changed", exc)
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)

    try:
        visit(directory.descriptor, PurePosixPath("."))
        directory.assert_stable()
    finally:
        directory.close()
    return tuple(sorted(records, key=lambda item: str(item["path"])))


def _validate_candidate_documents(
    *,
    payload: Mapping[str, Any],
    payload_file_sha256: str,
    terminal: Mapping[str, Any],
    expected_terminal_receipt_sha256: str,
    inputs: _InputBundle,
    binding: _PromotionBinding,
) -> None:
    if (
        terminal.get("schema") != binding.terminal_receipt_schema
        or (
            binding.terminal_receipt_purpose is None
            and "purpose" in terminal
        )
        or (
            binding.terminal_receipt_purpose is not None
            and terminal.get("purpose")
            != binding.terminal_receipt_purpose
        )
        or terminal["receipt_sha256"] != expected_terminal_receipt_sha256
        or terminal["release_revision"]
        != inputs.request["release_revision"]
        or terminal["source_tree_oid"] != inputs.request["source_tree_oid"]
        or terminal["builder_request_sha256"] != inputs.request_file_sha256
        or terminal["builder_request_identity_sha256"]
        != inputs.request["request_sha256"]
        or terminal["source_v3_manifest_sha256"] != inputs.source_file_sha256
        or terminal["source_v3_manifest_identity_sha256"]
        != inputs.source_manifest["manifest_sha256"]
        or terminal["runtime_dependency_manifest_sha256"]
        != inputs.runtime_file_sha256
        or terminal["runtime_dependency_manifest_identity_sha256"]
        != inputs.runtime_manifest["manifest_sha256"]
        or terminal["uv_sha256"] != inputs.uv_sha256
        or terminal["python_executable_sha256"] != inputs.python_sha256
        or terminal["payload_manifest_sha256"] != payload["manifest_sha256"]
        or terminal["payload_manifest_file_sha256"] != payload_file_sha256
        or terminal["payload_tree_sha256"] != payload["payload_tree_sha256"]
        or terminal["entrypoint_relative_path"]
        != inputs.request["entrypoint_relative_path"]
        or payload["release_revision"] != inputs.request["release_revision"]
        or payload["source_tree_oid"] != inputs.request["source_tree_oid"]
        or terminal["command_environment_sha256"]
        != sha256_bytes(canonical_bytes(phase._command_environment()))
    ):
        _fail("candidate_promoter_candidate_binding_invalid")
    entries = {
        str(item["path"]): item for item in payload["payload_entries"]
    }
    interpreter = entries.get(phase.INTERPRETER_RELATIVE_PATH)
    entrypoint = entries.get(binding.entrypoint_relative_path)
    retained_names = {
        f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
        for item in inputs.runtime_manifest["wheels"]
    }
    actual_retained = {
        path
        for path, item in entries.items()
        if path.startswith(f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/")
        and item.get("kind") == "file"
    }
    if (
        not isinstance(interpreter, Mapping)
        or not isinstance(entrypoint, Mapping)
        or interpreter.get("kind") != "file"
        or entrypoint.get("kind") != "file"
        or interpreter.get("sha256") != terminal["interpreter_sha256"]
        or entrypoint.get("sha256") != terminal["entrypoint_sha256"]
        or actual_retained != retained_names
        or any(
            entries[
                f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
            ].get("sha256")
            != item["sha256"]
            or entries[
                f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
            ].get("size")
            != item["size"]
            for item in inputs.runtime_manifest["wheels"]
        )
    ):
        _fail("candidate_promoter_candidate_binding_invalid")


def _load_and_validate_candidate(
    stack: ExitStack,
    *,
    root: Path,
    inputs: _InputBundle,
    binding: _PromotionBinding,
    expected_terminal_receipt_sha256: str,
    expected_uid: int,
    expected_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
    hold_documents: bool,
) -> _CandidateBundle:
    document_stack = stack if hold_documents else ExitStack()
    try:
        payload_file = document_stack.enter_context(
            builder.open_held_regular(
                root / phase.PAYLOAD_MANIFEST_NAME,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=frozenset({0o444}),
                maximum_bytes=MAX_JSON_BYTES,
            )
        )
        terminal_file = document_stack.enter_context(
            builder.open_held_regular(
                root / phase.TERMINAL_RECEIPT_NAME,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=frozenset({0o444}),
                maximum_bytes=MAX_JSON_BYTES,
            )
        )
        _assert_no_xattrs(
            payload_file.descriptor, xattr_reader=xattr_reader
        )
        _assert_no_xattrs(
            terminal_file.descriptor, xattr_reader=xattr_reader
        )
        payload = phase.validate_payload_manifest(
            _decode_document(_read_held(payload_file))
        )
        terminal = phase.validate_terminal_receipt(
            _decode_document(_read_held(terminal_file))
        )
        _validate_candidate_documents(
            payload=payload,
            payload_file_sha256=payload_file.sha256,
            terminal=terminal,
            expected_terminal_receipt_sha256=(
                expected_terminal_receipt_sha256
            ),
            inputs=inputs,
            binding=binding,
        )
        records = _scan_candidate_tree(
            root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            xattr_reader=xattr_reader,
        )
        actual = {str(item["path"]): item for item in records}
        expected_payload = {
            str(item["path"]): dict(item)
            for item in payload["payload_entries"]
        }
        expected_paths = set(expected_payload) | {
            phase.PAYLOAD_MANIFEST_NAME,
            phase.TERMINAL_RECEIPT_NAME,
        }
        if (
            set(actual) != expected_paths
            or any(actual[path] != item for path, item in expected_payload.items())
            or actual[phase.PAYLOAD_MANIFEST_NAME]
            != _record_for_file(
                path=phase.PAYLOAD_MANIFEST_NAME,
                mode=0o444,
                size=payload_file.identity.size,
                digest=payload_file.sha256,
            )
            or actual[phase.TERMINAL_RECEIPT_NAME]
            != _record_for_file(
                path=phase.TERMINAL_RECEIPT_NAME,
                mode=0o444,
                size=terminal_file.identity.size,
                digest=terminal_file.sha256,
            )
        ):
            _fail("candidate_promoter_candidate_tree_invalid")
        interpreter = actual.get(phase.INTERPRETER_RELATIVE_PATH)
        entrypoint = actual.get(binding.entrypoint_relative_path)
        if (
            not isinstance(interpreter, Mapping)
            or not isinstance(entrypoint, Mapping)
            or interpreter.get("kind") != "file"
            or entrypoint.get("kind") != "file"
            or interpreter.get("sha256") != terminal["interpreter_sha256"]
            or entrypoint.get("sha256") != terminal["entrypoint_sha256"]
        ):
            _fail("candidate_promoter_executable_binding_invalid")
        retained_names = {
            f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
            for item in inputs.runtime_manifest["wheels"]
        }
        actual_retained = {
            path
            for path in actual
            if path.startswith(
                f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/"
            )
            and actual[path]["kind"] == "file"
        }
        if actual_retained != retained_names or any(
            actual[
                f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
            ].get("sha256")
            != item["sha256"]
            or actual[
                f"{phase.RETAINED_WHEEL_DIRECTORY_NAME}/{item['filename']}"
            ].get("size")
            != item["size"]
            for item in inputs.runtime_manifest["wheels"]
        ):
            _fail("candidate_promoter_retained_wheels_invalid")
        return _CandidateBundle(
            payload_manifest=dict(payload),
            terminal_receipt=dict(terminal),
            terminal_file_sha256=terminal_file.sha256,
            records=records,
        )
    except ProductionReleaseCandidatePromoterError:
        raise
    except (
        builder.ProductionReleaseBuilderError,
        phase.ProductionReleaseBuilderPhaseError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _fail("candidate_promoter_candidate_invalid", exc)
    finally:
        if not hold_documents:
            document_stack.close()


def _copy_descriptor(
    source: int,
    destination: int,
    *,
    size: int,
) -> str:
    digest = hashlib.sha256()
    offset = 0
    try:
        while offset < size:
            chunk = os.pread(
                source, min(1024 * 1024, size - offset), offset
            )
            if not chunk:
                _fail("candidate_promoter_source_changed")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    _fail("candidate_promoter_copy_failed")
                view = view[written:]
            offset += len(chunk)
        if os.pread(source, 1, size):
            _fail("candidate_promoter_source_changed")
    except OSError as exc:
        _fail("candidate_promoter_copy_failed", exc)
    return digest.hexdigest()


def _copy_candidate_to_hidden(
    source: Path,
    hidden: Path,
    *,
    source_uid: int,
    source_gid: int,
    staging_uid: int,
    staging_gid: int,
    release_parent_uid: int,
    release_parent_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
    checkpoint: Callable[[str], None] | None,
) -> None:
    phase._validate_component(hidden.name)
    parent = _open_directory(
        hidden.parent,
        expected_uid=release_parent_uid,
        expected_gid=release_parent_gid,
        allowed_modes=_ROOT_DIRECTORY_MODES,
        xattr_reader=xattr_reader,
    )
    source_directory = _open_directory(
        source,
        expected_uid=source_uid,
        expected_gid=source_gid,
        allowed_modes=_BUILDER_DIRECTORY_MODES,
        xattr_reader=xattr_reader,
    )
    destination: int | None = None
    try:
        try:
            os.mkdir(hidden.name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            _fail("candidate_promoter_staging_conflict")
        except OSError as exc:
            _fail("candidate_promoter_staging_create_failed", exc)
        destination = os.open(
            hidden.name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_DIRECTORY,
            dir_fd=parent.descriptor,
        )
        os.fchown(destination, staging_uid, staging_gid)
        destination_root = builder.FileIdentity.from_stat(
            os.fstat(destination)
        )
        if destination_root.device != parent.identity.device:
            _fail("candidate_promoter_staging_mount_invalid")
        if checkpoint is not None:
            checkpoint("hidden_staging_created")

        def copy_directory(
            source_fd: int,
            destination_fd: int,
            *,
            source_device: int,
            destination_device: int,
        ) -> None:
            _assert_no_xattrs(source_fd, xattr_reader=xattr_reader)
            _assert_no_xattrs(destination_fd, xattr_reader=xattr_reader)
            try:
                names = tuple(sorted(os.listdir(source_fd)))
            except OSError as exc:
                _fail("candidate_promoter_source_unavailable", exc)
            for name in names:
                phase._validate_component(name)
                source_state = builder.FileIdentity.from_stat(
                    os.stat(
                        name,
                        dir_fd=source_fd,
                        follow_symlinks=False,
                    )
                )
                if source_state.device != source_device:
                    _fail("candidate_promoter_mount_crossing")
                if stat.S_ISDIR(
                    source_state.mode
                ) and not stat.S_ISLNK(source_state.mode):
                    source_child: int | None = None
                    destination_child: int | None = None
                    try:
                        source_child = os.open(
                            name,
                            os.O_RDONLY
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW
                            | os.O_DIRECTORY,
                            dir_fd=source_fd,
                        )
                        opened_source = builder.FileIdentity.from_stat(
                            os.fstat(source_child)
                        )
                        if (
                            opened_source != source_state
                            or opened_source.uid != source_uid
                            or opened_source.gid != source_gid
                            or stat.S_IMODE(opened_source.mode) != 0o555
                        ):
                            _fail("candidate_promoter_source_invalid")
                        os.mkdir(
                            name, 0o700, dir_fd=destination_fd
                        )
                        destination_child = os.open(
                            name,
                            os.O_RDONLY
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW
                            | os.O_DIRECTORY,
                            dir_fd=destination_fd,
                        )
                        os.fchown(
                            destination_child, staging_uid, staging_gid
                        )
                        destination_state = builder.FileIdentity.from_stat(
                            os.fstat(destination_child)
                        )
                        if destination_state.device != destination_device:
                            _fail(
                                "candidate_promoter_staging_mount_invalid"
                            )
                        copy_directory(
                            source_child,
                            destination_child,
                            source_device=source_device,
                            destination_device=destination_device,
                        )
                        os.fchmod(destination_child, 0o555)
                        os.fsync(destination_child)
                        _assert_no_xattrs(
                            destination_child,
                            xattr_reader=xattr_reader,
                        )
                    except FileExistsError:
                        _fail("candidate_promoter_staging_conflict")
                    except OSError as exc:
                        _fail("candidate_promoter_copy_failed", exc)
                    finally:
                        if source_child is not None:
                            os.close(source_child)
                        if destination_child is not None:
                            os.close(destination_child)
                elif stat.S_ISREG(
                    source_state.mode
                ) and not stat.S_ISLNK(source_state.mode):
                    source_child = None
                    destination_child = None
                    try:
                        source_child = os.open(
                            name,
                            os.O_RDONLY
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW,
                            dir_fd=source_fd,
                        )
                        opened_source = builder.FileIdentity.from_stat(
                            os.fstat(source_child)
                        )
                        mode = stat.S_IMODE(opened_source.mode)
                        if (
                            opened_source != source_state
                            or opened_source.uid != source_uid
                            or opened_source.gid != source_gid
                            or opened_source.links != 1
                            or mode not in _BUILDER_FILE_MODES
                        ):
                            _fail("candidate_promoter_source_invalid")
                        _assert_no_xattrs(
                            source_child, xattr_reader=xattr_reader
                        )
                        destination_child = os.open(
                            name,
                            os.O_RDWR
                            | os.O_CREAT
                            | os.O_EXCL
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=destination_fd,
                        )
                        digest = _copy_descriptor(
                            source_child,
                            destination_child,
                            size=opened_source.size,
                        )
                        if (
                            digest
                            != builder._hash_descriptor(
                                source_child,
                                size=opened_source.size,
                            )
                        ):
                            _fail("candidate_promoter_source_changed")
                        os.fchown(
                            destination_child, staging_uid, staging_gid
                        )
                        os.fchmod(destination_child, mode)
                        os.fsync(destination_child)
                        destination_state = (
                            builder.FileIdentity.from_stat(
                                os.fstat(destination_child)
                            )
                        )
                        _assert_no_xattrs(
                            destination_child,
                            xattr_reader=xattr_reader,
                        )
                        if (
                            destination_state.device
                            != destination_device
                            or destination_state.links != 1
                            or destination_state.uid != staging_uid
                            or destination_state.gid != staging_gid
                            or stat.S_IMODE(destination_state.mode) != mode
                            or destination_state.size
                            != opened_source.size
                            or builder._hash_descriptor(
                                destination_child,
                                size=destination_state.size,
                            )
                            != digest
                        ):
                            _fail("candidate_promoter_copy_failed")
                    except FileExistsError:
                        _fail("candidate_promoter_staging_conflict")
                    except OSError as exc:
                        _fail("candidate_promoter_copy_failed", exc)
                    finally:
                        if source_child is not None:
                            os.close(source_child)
                        if destination_child is not None:
                            os.close(destination_child)
                else:
                    _fail("candidate_promoter_special_entry")
            try:
                if tuple(sorted(os.listdir(source_fd))) != names:
                    _fail("candidate_promoter_source_changed")
            except OSError as exc:
                _fail("candidate_promoter_source_changed", exc)
            _assert_no_xattrs(source_fd, xattr_reader=xattr_reader)

        copy_directory(
            source_directory.descriptor,
            destination,
            source_device=source_directory.identity.device,
            destination_device=destination_root.device,
        )
        os.fchmod(destination, 0o555)
        os.fsync(destination)
        os.fsync(parent.descriptor)
        _assert_no_xattrs(destination, xattr_reader=xattr_reader)
        source_directory.assert_stable()
        if checkpoint is not None:
            checkpoint("hidden_staging_fsynced")
    finally:
        if destination is not None:
            os.close(destination)
        source_directory.close()
        parent.close()


def _rename_no_replace_linux(source: Path, destination: Path) -> None:
    if not sys.platform.startswith("linux"):
        _fail("candidate_promoter_rename_noreplace_unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        _fail("candidate_promoter_rename_noreplace_unavailable", exc)
    if result != 0:
        observed = ctypes.get_errno()
        if observed in {errno.EEXIST, errno.ENOTEMPTY}:
            _fail("candidate_promoter_destination_exists")
        _fail("candidate_promoter_rename_failed")


def _rename_no_replace_test(source: Path, destination: Path) -> None:
    if destination.exists():
        _fail("candidate_promoter_destination_exists")
    try:
        os.rename(source, destination)
    except OSError as exc:
        _fail("candidate_promoter_rename_failed", exc)


def _prepare_staging_modes_for_root_publisher(
    root: Path,
    *,
    records: Sequence[Mapping[str, Any]],
    staging_uid: int,
    staging_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> None:
    """Convert only sealed executables to the publisher's staging mode.

    The builder terminal contract records executable payload files as 0555.
    The root publisher deliberately accepts builder-owned files only in its
    writable staging-mode set, where an executable is 0755, and seals it back
    to 0555 while changing ownership.  Promotion therefore proves the exact
    0555 builder candidate first, then performs this single bounded mode
    transition immediately before publication.

    A crash after this point is intentionally not resumable: the renamed tree
    no longer matches the exact builder terminal contract and explicit root
    cleanup is required.
    """

    expected = {
        str(item.get("path")): dict(item)
        for item in records
        if isinstance(item, Mapping)
    }
    if (
        len(expected) != len(records)
        or not expected
        or any(
            item.get("kind") not in {"file", "directory"}
            or item.get("mode") not in {"0444", "0555"}
            for item in expected.values()
        )
    ):
        _fail("candidate_promoter_candidate_tree_invalid")
    expected_executables = {
        path
        for path, item in expected.items()
        if item["kind"] == "file" and item["mode"] == "0555"
    }
    if not expected_executables:
        _fail("candidate_promoter_executable_binding_invalid")

    directory = _open_directory(
        root,
        expected_uid=staging_uid,
        expected_gid=staging_gid,
        allowed_modes=_BUILDER_DIRECTORY_MODES,
    )
    root_device = directory.identity.device
    visited: set[str] = set()
    changed: set[str] = set()

    def visit(descriptor: int, relative: PurePosixPath) -> None:
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as exc:
            _fail("candidate_promoter_candidate_unavailable", exc)
        for name in names:
            phase._validate_component(name)
            path = (
                name
                if str(relative) == "."
                else (relative / name).as_posix()
            )
            item = expected.get(path)
            if item is None or path in visited:
                _fail("candidate_promoter_candidate_tree_invalid")
            visited.add(path)
            try:
                before = builder.FileIdentity.from_stat(
                    os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                )
            except OSError as exc:
                _fail("candidate_promoter_candidate_unavailable", exc)
            if (
                before.device != root_device
                or before.uid != staging_uid
                or before.gid != staging_gid
            ):
                _fail("candidate_promoter_candidate_invalid")
            if item["kind"] == "directory":
                child: int | None = None
                try:
                    if (
                        not stat.S_ISDIR(before.mode)
                        or stat.S_ISLNK(before.mode)
                        or stat.S_IMODE(before.mode) != 0o555
                    ):
                        _fail("candidate_promoter_candidate_tree_invalid")
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                        | os.O_DIRECTORY,
                        dir_fd=descriptor,
                    )
                    opened = builder.FileIdentity.from_stat(os.fstat(child))
                    if opened != before:
                        _fail("candidate_promoter_candidate_changed")
                    visit(child, PurePosixPath(path))
                    os.fsync(child)
                    final = builder.FileIdentity.from_stat(os.fstat(child))
                    reachable = builder.FileIdentity.from_stat(
                        os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    )
                    if final != opened or reachable != opened:
                        _fail("candidate_promoter_candidate_changed")
                except OSError as exc:
                    _fail("candidate_promoter_candidate_unavailable", exc)
                finally:
                    if child is not None:
                        os.close(child)
                continue

            child = None
            try:
                if (
                    item["kind"] != "file"
                    or not stat.S_ISREG(before.mode)
                    or stat.S_ISLNK(before.mode)
                    or before.links != 1
                    or stat.S_IMODE(before.mode) != int(item["mode"], 8)
                    or before.size != item.get("size")
                ):
                    _fail("candidate_promoter_candidate_tree_invalid")
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                opened = builder.FileIdentity.from_stat(os.fstat(child))
                if opened != before:
                    _fail("candidate_promoter_candidate_changed")
                _assert_no_xattrs(child, xattr_reader=xattr_reader)
                digest = builder._hash_descriptor(
                    child, size=opened.size
                )
                if digest != item.get("sha256"):
                    _fail("candidate_promoter_candidate_tree_invalid")
                target_mode = (
                    0o755
                    if stat.S_IMODE(opened.mode) == 0o555
                    else 0o444
                )
                if target_mode == 0o755:
                    os.fchmod(child, target_mode)
                    os.fsync(child)
                    changed.add(path)
                final = builder.FileIdentity.from_stat(os.fstat(child))
                reachable = builder.FileIdentity.from_stat(
                    os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                )
                if (
                    final.device != opened.device
                    or final.inode != opened.inode
                    or final.uid != opened.uid
                    or final.gid != opened.gid
                    or final.links != opened.links
                    or final.size != opened.size
                    or stat.S_IMODE(final.mode) != target_mode
                    or reachable != final
                    or builder._hash_descriptor(
                        child, size=final.size
                    )
                    != digest
                ):
                    _fail("candidate_promoter_candidate_changed")
                _assert_no_xattrs(child, xattr_reader=xattr_reader)
            except OSError as exc:
                _fail("candidate_promoter_candidate_unavailable", exc)
            finally:
                if child is not None:
                    os.close(child)
        try:
            if tuple(sorted(os.listdir(descriptor))) != names:
                _fail("candidate_promoter_candidate_changed")
            os.fsync(descriptor)
        except OSError as exc:
            _fail("candidate_promoter_candidate_changed", exc)
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)

    try:
        visit(directory.descriptor, PurePosixPath("."))
        if visited != set(expected) or changed != expected_executables:
            _fail("candidate_promoter_candidate_tree_invalid")
        directory.assert_stable()
    finally:
        directory.close()


def _process_free_evidence(
    *,
    revision: str,
    roots: PromoterRoots,
    systemd_reader: Callable[[str], Mapping[str, Any]],
    expected_fragment_sha256: str,
    expected_wrapper_sha256: str,
    authority_uid: int,
    authority_gid: int,
    process_uid: Callable[[Path, os.stat_result], int | None] | None,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> Mapping[str, Any]:
    unit = f"{roots.builder_unit_prefix}{revision}.service"
    control_group = f"/system.slice/{unit}"
    try:
        systemd_properties = systemd_reader(unit)
        return builder.validate_process_free_evidence(
            systemd_properties,
            expected_unit=unit,
            expected_fragment=roots.builder_unit_fragment,
            expected_fragment_sha256=expected_fragment_sha256,
            expected_wrapper=roots.builder_wrapper,
            expected_wrapper_sha256=expected_wrapper_sha256,
            expected_control_group=control_group,
            builder_uid=phase.BUILDER_UID,
            builder_gid=phase.BUILDER_GID,
            cgroup_root=roots.cgroup_root,
            proc_root=roots.proc_root,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
            process_uid=process_uid,
            xattr_reader=xattr_reader,
        )
    except ProductionReleaseCandidatePromoterError:
        raise
    except (
        builder.ProductionReleaseBuilderError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _fail("candidate_promoter_builder_not_process_free", exc)


def _manifest_identities(
    identities: builder.ReleaseIdentities,
) -> Mapping[str, Any]:
    return {
        "release_owner": {
            "uid": identities.root_uid,
            "gid": identities.root_gid,
        },
        "builder_identity": {
            "user": phase.BUILDER_USER,
            "group": phase.BUILDER_GROUP,
            "uid": identities.builder_uid,
            "gid": identities.builder_gid,
        },
        "reserved_runtime_uids": list(identities.reserved_runtime_uids),
        "reserved_runtime_gids": list(identities.reserved_runtime_gids),
    }


def _read_published_document(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> tuple[Mapping[str, Any], str]:
    try:
        with builder.open_held_regular(
            path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=frozenset({0o444}),
            maximum_bytes=builder.MAX_RECORD_BYTES,
        ) as held:
            _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
            document = _decode_document(_read_held(held))
            _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
            return document, held.sha256
    except builder.ProductionReleaseBuilderError as exc:
        _fail("candidate_promoter_published_record_invalid", exc)


def _published_result(
    final: Path,
    *,
    revision: str,
    expected_terminal_receipt_sha256: str,
    inputs: _InputBundle,
    binding: _PromotionBinding,
    identities: builder.ReleaseIdentities,
    expected_uid: int,
    expected_gid: int,
    production: bool,
    xattr_reader: Callable[[int], Sequence[str | bytes]],
) -> Mapping[str, Any]:
    try:
        candidate_seal = (
            builder.verify_published_release(final, revision=revision)
            if production
            else builder._verify_published_release_filesystem(
                final,
                revision=revision,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                require_logical_owner=False,
                _xattr_reader=xattr_reader,
            )
        )
    except builder.ProductionReleaseBuilderError as exc:
        _fail("candidate_promoter_published_release_invalid", exc)
    manifest, manifest_file_sha256 = _read_published_document(
        final / builder.MANIFEST_NAME,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        xattr_reader=xattr_reader,
    )
    seal_document, seal_file_sha256 = _read_published_document(
        final / builder.RECEIPT_NAME,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        xattr_reader=xattr_reader,
    )
    payload_document, payload_file_sha256 = _read_published_document(
        final / phase.PAYLOAD_MANIFEST_NAME,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        xattr_reader=xattr_reader,
    )
    terminal, _terminal_file_sha256 = _read_published_document(
        final / phase.TERMINAL_RECEIPT_NAME,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        xattr_reader=xattr_reader,
    )
    try:
        payload = phase.validate_payload_manifest(payload_document)
        terminal = phase.validate_terminal_receipt(terminal)
        _validate_candidate_documents(
            payload=payload,
            payload_file_sha256=payload_file_sha256,
            terminal=terminal,
            expected_terminal_receipt_sha256=(
                expected_terminal_receipt_sha256
            ),
            inputs=inputs,
            binding=binding,
        )
    except phase.ProductionReleaseBuilderPhaseError as exc:
        _fail("candidate_promoter_published_binding_invalid", exc)
    if (
        seal_document != candidate_seal
        or manifest.get("manifest_sha256")
        != candidate_seal.get("manifest_sha256")
        or manifest.get("identities") != _manifest_identities(identities)
        or candidate_seal.get("receipt_sha256")
        == terminal.get("receipt_sha256")
    ):
        _fail("candidate_promoter_published_binding_invalid")
    unsigned = {
        "schema": PROMOTION_RESULT_SCHEMA,
        "release_revision": revision,
        "release_root": str(final),
        "builder_terminal_receipt_sha256": terminal["receipt_sha256"],
        "candidate_seal_receipt_sha256": candidate_seal[
            "receipt_sha256"
        ],
        "candidate_seal_receipt_file_sha256": seal_file_sha256,
        "whole_tree_manifest_sha256": manifest["manifest_sha256"],
        "whole_tree_manifest_file_sha256": manifest_file_sha256,
        "process_free_evidence_sha256": candidate_seal[
            "process_free_evidence_sha256"
        ],
        "completed": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "result_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }


def validate_promotion_result(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RESULT_FIELDS:
        _fail("candidate_promoter_result_invalid")
    raw = dict(value)
    digest = raw.pop("result_sha256")
    if (
        value.get("schema") != PROMOTION_RESULT_SCHEMA
        or _REVISION.fullmatch(
            str(value.get("release_revision", ""))
        )
        is None
        or not Path(str(value.get("release_root", ""))).is_absolute()
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "builder_terminal_receipt_sha256",
                "candidate_seal_receipt_sha256",
                "candidate_seal_receipt_file_sha256",
                "whole_tree_manifest_sha256",
                "whole_tree_manifest_file_sha256",
                "process_free_evidence_sha256",
            )
        )
        or value.get("builder_terminal_receipt_sha256")
        == value.get("candidate_seal_receipt_sha256")
        or value.get("completed") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(digest)) is None
        or digest != sha256_bytes(canonical_bytes(raw))
    ):
        _fail("candidate_promoter_result_invalid")
    return dict(value)


def _promote_candidate_for_test(
    *,
    revision: str,
    expected_builder_terminal_receipt_sha256: str,
    roots: PromoterRoots,
    binding: _PromotionBinding,
    production: bool = True,
    checkpoint: Callable[[str], None] | None = None,
    rename_no_replace: Callable[[Path, Path], None] | None = None,
    test_authority_uid: int | None = None,
    test_authority_gid: int | None = None,
    test_interlock_gid: int | None = None,
    test_source_builder_uid: int | None = None,
    test_source_builder_gid: int | None = None,
    test_staging_uid: int | None = None,
    test_staging_gid: int | None = None,
    test_publication_uid: int | None = None,
    test_publication_gid: int | None = None,
    test_xattr_reader: Callable[[int], Sequence[str | bytes]]
    | None = None,
    test_process_uid: Callable[[Path, os.stat_result], int | None]
    | None = None,
    test_identities: builder.ReleaseIdentities | None = None,
    test_systemd_reader: Callable[[str], Mapping[str, Any]] | None = None,
    test_fragment_sha256: str | None = None,
    test_wrapper_sha256: str | None = None,
) -> Mapping[str, Any]:
    """Promote one exact builder candidate into a sealed release."""

    if (
        type(production) is not bool
        or _REVISION.fullmatch(str(revision)) is None
        or _SHA256.fullmatch(
            str(expected_builder_terminal_receipt_sha256)
        )
        is None
    ):
        _fail("candidate_promoter_contract_invalid")
    normalized_roots = _validate_roots(roots, production=production)
    if production:
        if (
            not sys.platform.startswith("linux")
            or _read_posix_identity("geteuid") != 0
            or any(
                item is not None
                for item in (
                    rename_no_replace,
                    test_authority_uid,
                    test_authority_gid,
                    test_interlock_gid,
                    test_source_builder_uid,
                    test_source_builder_gid,
                    test_staging_uid,
                    test_staging_gid,
                    test_publication_uid,
                    test_publication_gid,
                    test_xattr_reader,
                    test_process_uid,
                    test_identities,
                    test_systemd_reader,
                    test_fragment_sha256,
                    test_wrapper_sha256,
                )
            )
        ):
            _fail("candidate_promoter_root_authority_required")
        authority_uid = authority_gid = 0
        interlock_gid = phase.BUILDER_GID
        source_builder_uid = staging_uid = phase.BUILDER_UID
        source_builder_gid = staging_gid = phase.BUILDER_GID
        publication_uid = publication_gid = 0
        xattr_reader = builder._read_descriptor_xattrs
        rename = _rename_no_replace_linux
        validated_identities = _derive_production_release_identities()
        systemd_reader = _systemctl_show
        if normalized_roots == production_latched_revision_roots():
            fragment_sha256 = (
                PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT_SHA256
            )
            wrapper_sha256 = (
                PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER_SHA256
            )
        elif normalized_roots == production_revision_roots():
            fragment_sha256 = PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT_SHA256
            wrapper_sha256 = PRODUCTION_REVISION_BUILDER_WRAPPER_SHA256
        else:
            fragment_sha256 = PRODUCTION_BUILDER_UNIT_FRAGMENT_SHA256
            wrapper_sha256 = PRODUCTION_BUILDER_WRAPPER_SHA256
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
        interlock_gid = (
            _read_posix_identity("getegid")
            if test_interlock_gid is None
            else test_interlock_gid
        )
        source_builder_uid = (
            _read_posix_identity("geteuid")
            if test_source_builder_uid is None
            else test_source_builder_uid
        )
        source_builder_gid = (
            _read_posix_identity("getegid")
            if test_source_builder_gid is None
            else test_source_builder_gid
        )
        staging_uid = (
            _read_posix_identity("geteuid")
            if test_staging_uid is None
            else test_staging_uid
        )
        staging_gid = (
            _read_posix_identity("getegid")
            if test_staging_gid is None
            else test_staging_gid
        )
        publication_uid = (
            _read_posix_identity("geteuid")
            if test_publication_uid is None
            else test_publication_uid
        )
        publication_gid = (
            _read_posix_identity("getegid")
            if test_publication_gid is None
            else test_publication_gid
        )
        xattr_reader = (
            (lambda _descriptor: ())
            if test_xattr_reader is None
            else test_xattr_reader
        )
        rename = (
            _rename_no_replace_test
            if rename_no_replace is None
            else rename_no_replace
        )
        if (
            test_identities is None
            or test_systemd_reader is None
            or _SHA256.fullmatch(str(test_fragment_sha256)) is None
            or _SHA256.fullmatch(str(test_wrapper_sha256)) is None
        ):
            _fail("candidate_promoter_contract_invalid")
        systemd_reader = test_systemd_reader
        fragment_sha256 = str(test_fragment_sha256)
        wrapper_sha256 = str(test_wrapper_sha256)
        try:
            validated_identities = builder.validate_release_identities(
                test_identities,
            )
        except builder.ProductionReleaseBuilderError as exc:
            _fail("candidate_promoter_identity_contract_invalid", exc)
    if any(
        type(item) is not int or item < 0
        for item in (
            authority_uid,
            authority_gid,
            interlock_gid,
            source_builder_uid,
            source_builder_gid,
            staging_uid,
            staging_gid,
            publication_uid,
            publication_gid,
        )
    ):
        _fail("candidate_promoter_contract_invalid")
    stack = ExitStack()
    try:
        inputs = _load_inputs(
            stack,
            revision=revision,
            roots=normalized_roots,
            binding=binding,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
            xattr_reader=xattr_reader,
        )
        final = (
            normalized_roots.release_parent
            / f"hermes-agent-{revision[:12]}"
        )
        hidden = (
            normalized_roots.release_parent
            / f".muncho-release-staging-{revision}"
        )
        release_parent = stack.enter_context(
            _open_directory(
                normalized_roots.release_parent,
                expected_uid=publication_uid,
                expected_gid=publication_gid,
                allowed_modes=_ROOT_DIRECTORY_MODES,
                xattr_reader=xattr_reader,
            )
        )
        names = set(release_parent.names())
        final_exists = final.name in names
        hidden_exists = hidden.name in names
        if hidden_exists and final_exists:
            _fail("candidate_promoter_cleanup_required")
        if final_exists:
            try:
                inputs.assert_stable()
                result = _published_result(
                    final,
                    revision=revision,
                    expected_terminal_receipt_sha256=(
                        expected_builder_terminal_receipt_sha256
                    ),
                    inputs=inputs,
                    binding=binding,
                    identities=validated_identities,
                    expected_uid=publication_uid,
                    expected_gid=publication_gid,
                    production=production,
                    xattr_reader=xattr_reader,
                )
                inputs.assert_stable()
            except ProductionReleaseCandidatePromoterError:
                pass
            else:
                return validate_promotion_result(result)

        initial_evidence = _process_free_evidence(
            revision=revision,
            roots=normalized_roots,
            systemd_reader=systemd_reader,
            expected_fragment_sha256=fragment_sha256,
            expected_wrapper_sha256=wrapper_sha256,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
            process_uid=test_process_uid,
            xattr_reader=xattr_reader,
        )
        if checkpoint is not None:
            checkpoint("builder_process_free_initial")
        source_candidate = _load_and_validate_candidate(
            stack,
            root=inputs.candidate_root,
            inputs=inputs,
            binding=binding,
            expected_terminal_receipt_sha256=(
                expected_builder_terminal_receipt_sha256
            ),
            expected_uid=source_builder_uid,
            expected_gid=source_builder_gid,
            xattr_reader=xattr_reader,
            hold_documents=True,
        )
        if final_exists:
            try:
                _load_and_validate_candidate(
                    stack,
                    root=final,
                    inputs=inputs,
                    binding=binding,
                    expected_terminal_receipt_sha256=(
                        expected_builder_terminal_receipt_sha256
                    ),
                    expected_uid=staging_uid,
                    expected_gid=staging_gid,
                    xattr_reader=xattr_reader,
                    hold_documents=False,
                )
            except ProductionReleaseCandidatePromoterError:
                _fail("candidate_promoter_cleanup_required")
        else:
            if hidden_exists:
                try:
                    _load_and_validate_candidate(
                        stack,
                        root=hidden,
                        inputs=inputs,
                        binding=binding,
                        expected_terminal_receipt_sha256=(
                            expected_builder_terminal_receipt_sha256
                        ),
                        expected_uid=staging_uid,
                        expected_gid=staging_gid,
                        xattr_reader=xattr_reader,
                        hold_documents=False,
                    )
                except ProductionReleaseCandidatePromoterError:
                    _fail("candidate_promoter_cleanup_required")
            else:
                _copy_candidate_to_hidden(
                    inputs.candidate_root,
                    hidden,
                    source_uid=source_builder_uid,
                    source_gid=source_builder_gid,
                    staging_uid=staging_uid,
                    staging_gid=staging_gid,
                    release_parent_uid=publication_uid,
                    release_parent_gid=publication_gid,
                    xattr_reader=xattr_reader,
                    checkpoint=checkpoint,
                )
                _load_and_validate_candidate(
                    stack,
                    root=hidden,
                    inputs=inputs,
                    binding=binding,
                    expected_terminal_receipt_sha256=(
                        expected_builder_terminal_receipt_sha256
                    ),
                    expected_uid=staging_uid,
                    expected_gid=staging_gid,
                    xattr_reader=xattr_reader,
                    hold_documents=False,
                )
            rename(hidden, final)
            os.fsync(release_parent.descriptor)
            if checkpoint is not None:
                checkpoint("renamed_final_no_replace")

        renamed_candidate = _load_and_validate_candidate(
            stack,
            root=final,
            inputs=inputs,
            binding=binding,
            expected_terminal_receipt_sha256=(
                expected_builder_terminal_receipt_sha256
            ),
            expected_uid=staging_uid,
            expected_gid=staging_gid,
            xattr_reader=xattr_reader,
            hold_documents=False,
        )
        _prepare_staging_modes_for_root_publisher(
            final,
            records=renamed_candidate.records,
            staging_uid=staging_uid,
            staging_gid=staging_gid,
            xattr_reader=xattr_reader,
        )
        os.fsync(release_parent.descriptor)
        if checkpoint is not None:
            checkpoint("root_publisher_modes_prepared")
        def publication_checkpoint(name: str) -> None:
            if checkpoint is not None:
                checkpoint(f"root_publication_{name}")

        with _promotion_interlock(
            normalized_roots.promotion_interlock,
            authority_uid=authority_uid,
            builder_gid=interlock_gid,
            xattr_reader=xattr_reader,
        ) as interlock:
            if checkpoint is not None:
                checkpoint("promotion_interlock_acquired")
            inputs.assert_stable()
            final_evidence = _process_free_evidence(
                revision=revision,
                roots=normalized_roots,
                systemd_reader=systemd_reader,
                expected_fragment_sha256=fragment_sha256,
                expected_wrapper_sha256=wrapper_sha256,
                authority_uid=authority_uid,
                authority_gid=authority_gid,
                process_uid=test_process_uid,
                xattr_reader=xattr_reader,
            )
            if checkpoint is not None:
                checkpoint("builder_process_free_final")
            try:
                evidence_set = builder.build_process_free_evidence_set(
                    initial_evidence,
                    final_evidence,
                    builder_uid=validated_identities.builder_uid,
                    builder_gid=validated_identities.builder_gid,
                )
            except builder.ProductionReleaseBuilderError as exc:
                _fail("candidate_promoter_builder_evidence_changed", exc)
            interlock.assert_stable()
            try:
                if production:
                    builder._publish_root_owned_release(
                        final,
                        revision=revision,
                        identities=validated_identities,
                        process_free_evidence=evidence_set,
                    )
                else:
                    builder._publish_release_filesystem(
                        final,
                        revision=revision,
                        identities=validated_identities,
                        process_free_evidence=evidence_set,
                        staging_uid=staging_uid,
                        staging_gid=staging_gid,
                        publication_uid=publication_uid,
                        publication_gid=publication_gid,
                        checkpoint=publication_checkpoint,
                        _xattr_reader=xattr_reader,
                    )
            except builder.ProductionReleaseBuilderError as exc:
                _fail("candidate_promoter_root_publication_failed", exc)
            interlock.assert_stable()
            result = _published_result(
                final,
                revision=revision,
                expected_terminal_receipt_sha256=(
                    source_candidate.terminal_receipt["receipt_sha256"]
                ),
                inputs=inputs,
                binding=binding,
                identities=validated_identities,
                expected_uid=publication_uid,
                expected_gid=publication_gid,
                production=production,
                xattr_reader=xattr_reader,
            )
            inputs.assert_stable()
            interlock.assert_stable()
        if checkpoint is not None:
            checkpoint("completed")
        return validate_promotion_result(result)
    except ProductionReleaseCandidatePromoterError:
        raise
    except (
        builder.ProductionReleaseBuilderError,
        phase.ProductionReleaseBuilderPhaseError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        _fail("candidate_promoter_failed", exc)
    finally:
        stack.close()


def promote_candidate(
    *,
    revision: str,
    expected_builder_terminal_receipt_sha256: str,
) -> Mapping[str, Any]:
    """Promote one production candidate through fixed root-only authority."""

    return _promote_candidate_for_test(
        revision=revision,
        expected_builder_terminal_receipt_sha256=(
            expected_builder_terminal_receipt_sha256
        ),
        roots=production_roots(),
        binding=_RELEASE_UPDATER_PROMOTION_BINDING,
        production=True,
    )


def promote_rotation_stager_candidate(
    *,
    revision: str,
    expected_builder_terminal_receipt_sha256: str,
) -> Mapping[str, Any]:
    """Publish one exact unit-input rotation stager without activating it."""

    return _promote_candidate_for_test(
        revision=revision,
        expected_builder_terminal_receipt_sha256=(
            expected_builder_terminal_receipt_sha256
        ),
        roots=production_latched_revision_roots(),
        binding=_UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING,
        production=True,
    )


__all__ = [
    "PROMOTION_RESULT_SCHEMA",
    "PRODUCTION_BUILDER_UNIT_FRAGMENT",
    "PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT",
    "PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER",
    "PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT",
    "PRODUCTION_REVISION_BUILDER_WRAPPER",
    "PRODUCTION_RELEASE_PARENT",
    "ProductionReleaseCandidatePromoterError",
    "canonical_bytes",
    "promote_candidate",
    "promote_rotation_stager_candidate",
    "production_latched_revision_roots",
    "production_revision_roots",
    "sha256_bytes",
    "validate_promotion_result",
]
