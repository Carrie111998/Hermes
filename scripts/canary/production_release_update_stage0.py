#!/usr/bin/env python3
"""Predecessor-trusted, data-only verifier for one production release update.

Stage 0 is intentionally incapable of executing or importing the candidate
release.  It opens every authority and plan-bound input as a single-link,
root-owned regular file, validates the predecessor-pinned owner signature,
verifies the complete sealed release, and returns a context-managed bundle of
held descriptors.  A later stage may consume those descriptors only after
dropping the root authority appropriate to that stage.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence

from scripts.canary import production_release_builder_phase as builder_phase
from scripts.canary import production_release_builder_runtime as builder
from scripts.canary import production_release_update_contract as contract
from scripts.canary import production_release_update_inputs as update_inputs


PRODUCTION_AUTHORITY_ROOT = Path(
    "/var/lib/muncho-production-release-update/authority"
)
PRODUCTION_INPUT_ROOT = Path(
    "/var/lib/muncho-production-release-update/inputs"
)
PRODUCTION_EXTERNAL_PIN_PATH = Path(
    "/etc/muncho/release-update/predecessor-trust.sha256"
)
PRODUCTION_RELEASE_ROOT_PARENT = Path(
    "/opt/adventico-ai-platform/hermes-agent-releases"
)

PREDECESSOR_TRUST_NAME = "predecessor-trust.json"
UPDATE_PUBLICATION_NAME = "release-update-publication.json"

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_ENTRYPOINT_BYTES = 8 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ROOT_DIRECTORY_MODES = frozenset({0o555, 0o700, 0o750, 0o755})
_ROOT_JSON_MODES = frozenset({0o444})
_ROOT_EXECUTABLE_MODES = frozenset({0o555})
_ROOT_ENTRYPOINT_MODES = frozenset({0o444, 0o555})

_JSON_INPUTS = {
    "source_v3_manifest_sha256": "source-v3-manifest.json",
    "builder_request_sha256": "builder-request.json",
    "builder_terminal_receipt_sha256": "builder-terminal-receipt.json",
    "candidate_seal_receipt_sha256": "candidate-seal-receipt.json",
    "runtime_dependency_manifest_sha256": (
        "runtime-dependency-manifest.json"
    ),
    "host_inventory_sha256": "host-inventory.json",
    "release_consumer_set_sha256": "release-consumer-set.json",
    "host_artifact_manifest_sha256": "host-artifact-manifest.json",
    "cron_artifact_index_sha256": "cron-artifact-index.json",
    "alias_artifact_index_sha256": "alias-artifact-index.json",
    "successor_unit_input_publication_sha256": (
        "successor-unit-input-publication.json"
    ),
    "activation_plan_sha256": "activation-plan.json",
    "rollback_plan_sha256": "rollback-plan.json",
}
_FILE_DIGEST_JSON_INPUTS = frozenset(
    {
        "source_v3_manifest_sha256",
        "builder_request_sha256",
        "runtime_dependency_manifest_sha256",
    }
)
_SEMANTIC_JSON_INPUTS = frozenset(
    {
        "host_inventory_sha256",
        "release_consumer_set_sha256",
        "host_artifact_manifest_sha256",
        "cron_artifact_index_sha256",
        "alias_artifact_index_sha256",
        "successor_unit_input_publication_sha256",
        "activation_plan_sha256",
        "rollback_plan_sha256",
    }
)
_INTERNAL_IDENTITY_FIELDS = {
    "builder_terminal_receipt_sha256": "receipt_sha256",
    "candidate_seal_receipt_sha256": "receipt_sha256",
    "host_inventory_sha256": "receipt_sha256",
    "release_consumer_set_sha256": "consumer_set_sha256",
    "host_artifact_manifest_sha256": "manifest_sha256",
    "cron_artifact_index_sha256": "artifact_index_sha256",
    "alias_artifact_index_sha256": "package_sha256",
    "successor_unit_input_publication_sha256": "publication_sha256",
    "activation_plan_sha256": "activation_plan_sha256",
    "rollback_plan_sha256": "rollback_plan_sha256",
}
_BINARY_INPUTS = {"uv_sha256": "uv"}
_XattrReader = Callable[[int], Sequence[str | bytes]]


class ProductionReleaseUpdateStage0Error(RuntimeError):
    """Stable, secret-free failure at the stage-0 trust boundary."""


def _fail(code: str) -> NoReturn:
    raise ProductionReleaseUpdateStage0Error(code) from None


def _posix_effective_uid(*, failure_code: str) -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        _fail(failure_code)
    try:
        value = getter()
    except (OSError, TypeError, ValueError):
        _fail(failure_code)
    if type(value) is not int or value < 0:
        _fail(failure_code)
    return value


def _posix_effective_gid(*, failure_code: str) -> int:
    getter = getattr(os, "getegid", None)
    if not callable(getter):
        _fail(failure_code)
    try:
        value = getter()
    except (OSError, TypeError, ValueError):
        _fail(failure_code)
    if type(value) is not int or value < 0:
        _fail(failure_code)
    return value


@dataclass(frozen=True)
class Stage0Roots:
    """Fixed roots used by stage 0.

    Production accepts only :func:`production_roots`.  Tests may inject
    canonical absolute roots while running with ``production=False``.
    """

    authority_root: Path
    input_root: Path
    external_pin_path: Path
    release_root_parent: Path


def production_roots() -> Stage0Roots:
    return Stage0Roots(
        authority_root=PRODUCTION_AUTHORITY_ROOT,
        input_root=PRODUCTION_INPUT_ROOT,
        external_pin_path=PRODUCTION_EXTERNAL_PIN_PATH,
        release_root_parent=PRODUCTION_RELEASE_ROOT_PARENT,
    )


@dataclass
class _HeldDirectory(AbstractContextManager["_HeldDirectory"]):
    path: Path
    descriptor: int
    identity: builder.FileIdentity
    xattr_reader: _XattrReader
    _closed: bool = False

    def assert_stable(self) -> None:
        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        try:
            current = builder.FileIdentity.from_stat(
                os.fstat(self.descriptor)
            )
            reachable = builder.FileIdentity.from_stat(os.lstat(self.path))
        except OSError:
            _fail("release_update_stage0_directory_drift")
        if current != self.identity or reachable != self.identity:
            _fail("release_update_stage0_directory_drift")
        _assert_no_extended_metadata(
            self.descriptor,
            xattr_reader=self.xattr_reader,
        )

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


@dataclass
class VerifiedLaunchBundle(
    AbstractContextManager["VerifiedLaunchBundle"]
):
    """Held, revalidatable inputs for a later non-root launch stage."""

    predecessor_trust: Mapping[str, Any]
    publication: Mapping[str, Any]
    builder_manifest: Mapping[str, Any]
    builder_receipt: Mapping[str, Any]
    input_documents: Mapping[str, Mapping[str, Any]]
    input_internal_identities: Mapping[str, str]
    fixed_v4_inputs: Mapping[str, Any]
    held_files: Mapping[str, builder.HeldRegularFile]
    held_directories: tuple[_HeldDirectory, ...]
    release_root: Path
    expected_uid: int
    expected_gid: int
    production: bool
    xattr_reader: _XattrReader
    _stack: ExitStack
    _closed: bool = False

    @property
    def interpreter_descriptor(self) -> int:
        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        return self.held_files["interpreter"].descriptor

    @property
    def entrypoint_descriptor(self) -> int:
        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        return self.held_files["entrypoint"].descriptor

    @property
    def interpreter_identity(self) -> builder.FileIdentity:
        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        return self.held_files["interpreter"].identity

    @property
    def entrypoint_identity(self) -> builder.FileIdentity:
        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        return self.held_files["entrypoint"].identity

    def assert_stable(self) -> None:
        """Revalidate path bindings and the complete sealed release."""

        if self._closed:
            _fail("release_update_stage0_bundle_closed")
        try:
            for directory in self.held_directories:
                directory.assert_stable()
            for held in self.held_files.values():
                held.assert_stable()
                _assert_no_extended_metadata(
                    held.descriptor,
                    xattr_reader=self.xattr_reader,
                )
            receipt = _verify_release(
                self.release_root,
                revision=str(self.publication["release_revision"]),
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
                production=self.production,
            )
            if receipt != self.builder_receipt:
                _fail("release_update_stage0_release_drift")
            for directory in self.held_directories:
                directory.assert_stable()
            for held in self.held_files.values():
                held.assert_stable()
                _assert_no_extended_metadata(
                    held.descriptor,
                    xattr_reader=self.xattr_reader,
                )
        except ProductionReleaseUpdateStage0Error:
            raise
        except (
            builder.ProductionReleaseBuilderError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ):
            _fail("release_update_stage0_release_drift")

    def close(self) -> None:
        if not self._closed:
            self._stack.close()
            self._closed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _open_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    xattr_reader: _XattrReader,
) -> _HeldDirectory:
    descriptor: int | None = None
    try:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_CLOEXEC")
        ):
            _fail("release_update_stage0_directory_invalid")
        before = builder.FileIdentity.from_stat(os.lstat(path))
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_DIRECTORY,
        )
        opened = builder.FileIdentity.from_stat(os.fstat(descriptor))
        after = builder.FileIdentity.from_stat(os.lstat(path))
        mode = stat.S_IMODE(opened.mode)
        if (
            before != opened
            or opened != after
            or not stat.S_ISDIR(opened.mode)
            or stat.S_ISLNK(opened.mode)
            or opened.uid != expected_uid
            or opened.gid != expected_gid
            or mode not in allowed_modes
            or mode & 0o022
        ):
            _fail("release_update_stage0_directory_invalid")
        _assert_no_extended_metadata(
            descriptor,
            xattr_reader=xattr_reader,
        )
        return _HeldDirectory(path, descriptor, opened, xattr_reader)
    except ProductionReleaseUpdateStage0Error:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError):
        if descriptor is not None:
            os.close(descriptor)
        _fail("release_update_stage0_directory_invalid")


def _read_descriptor(held: builder.HeldRegularFile) -> bytes:
    try:
        raw = os.pread(held.descriptor, held.identity.size, 0)
        if len(raw) != held.identity.size:
            _fail("release_update_stage0_file_invalid")
        held.assert_stable()
        return raw
    except ProductionReleaseUpdateStage0Error:
        raise
    except (builder.ProductionReleaseBuilderError, OSError):
        _fail("release_update_stage0_file_invalid")


def _assert_no_extended_metadata(
    descriptor: int,
    *,
    xattr_reader: _XattrReader,
) -> None:
    try:
        builder._assert_no_xattrs(  # noqa: SLF001
            descriptor,
            xattr_reader=xattr_reader,
        )
    except builder.ProductionReleaseBuilderError:
        _fail("release_update_stage0_extended_metadata_invalid")


def _canonical_mapping(held: builder.HeldRegularFile) -> Mapping[str, Any]:
    raw = _read_descriptor(held)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if not isinstance(name, str) or name in result:
                raise ValueError("duplicate")
            result[name] = value
        return result

    try:
        value = json.loads(
            raw[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("constant")
            ),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        _fail("release_update_stage0_json_invalid")
    if (
        not isinstance(value, Mapping)
        or not raw.endswith(b"\n")
        or b"\n" in raw[:-1]
        or raw != contract.canonical_bytes(value) + b"\n"
    ):
        _fail("release_update_stage0_json_invalid")
    return dict(value)


def _open_file(
    stack: ExitStack,
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    maximum_bytes: int,
    xattr_reader: _XattrReader,
    expected_sha256: str | None = None,
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
            )
        )
        _assert_no_extended_metadata(
            held.descriptor,
            xattr_reader=xattr_reader,
        )
        return held
    except (builder.ProductionReleaseBuilderError, OSError, RuntimeError):
        _fail("release_update_stage0_file_invalid")


def _validate_roots(
    roots: Stage0Roots,
    *,
    production: bool,
) -> Stage0Roots:
    if not isinstance(roots, Stage0Roots):
        _fail("release_update_stage0_roots_invalid")
    try:
        normalized = Stage0Roots(
            authority_root=Path(roots.authority_root),
            input_root=Path(roots.input_root),
            external_pin_path=Path(roots.external_pin_path),
            release_root_parent=Path(roots.release_root_parent),
        )
    except (TypeError, ValueError):
        _fail("release_update_stage0_roots_invalid")
    paths = (
        normalized.authority_root,
        normalized.input_root,
        normalized.external_pin_path,
        normalized.release_root_parent,
    )
    if any(
        not path.is_absolute()
        or "\x00" in str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        for path in paths
    ):
        _fail("release_update_stage0_roots_invalid")
    if (
        (production and normalized != production_roots())
        or (not production and normalized == production_roots())
    ):
        _fail("release_update_stage0_roots_invalid")
    return normalized


def _validate_manifest_identities(
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    identities = manifest.get("identities")
    if (
        not isinstance(identities, Mapping)
        or identities
        != {
            "release_owner": plan["release_owner"],
            "builder_identity": plan["builder_identity"],
            "reserved_runtime_uids": plan["reserved_runtime_uids"],
            "reserved_runtime_gids": plan["reserved_runtime_gids"],
        }
    ):
        _fail("release_update_stage0_release_binding_invalid")


def _validate_builder_input_documents(
    documents: Mapping[str, Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate and cross-bind the offline builder's complete authority."""

    try:
        revision = str(plan["release_revision"])
        request = builder_phase.validate_request(
            documents["builder_request_sha256"],
            expected_job_id=revision,
        )
        source = builder_phase.validate_source_manifest(
            documents["source_v3_manifest_sha256"],
            request=request,
        )
        runtime_manifest = builder_phase.validate_runtime_manifest(
            documents["runtime_dependency_manifest_sha256"],
            request=request,
        )
        terminal = builder_phase.validate_terminal_receipt(
            documents["builder_terminal_receipt_sha256"]
        )
    except (
        builder_phase.ProductionReleaseBuilderPhaseError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _fail("release_update_stage0_builder_binding_invalid")
    if (
        request["release_revision"] != revision
        or request["source_tree_oid"] != plan["source_tree_oid"]
        or request["source_v3_manifest_sha256"]
        != plan["source_v3_manifest_sha256"]
        or request["runtime_dependency_manifest_sha256"]
        != plan["runtime_dependency_manifest_sha256"]
        or request["uv_sha256"] != plan["uv_sha256"]
        or request["interpreter_relative_path"]
        != plan["interpreter_relative_path"]
        or request["entrypoint_relative_path"]
        != plan["entrypoint_relative_path"]
        or request["python_executable_sha256"]
        != plan["interpreter_sha256"]
        or request["builder_identity"] != plan["builder_identity"]
        or source["release_revision"] != revision
        or source["source_tree_oid"] != plan["source_tree_oid"]
        or runtime_manifest["release_revision"] != revision
        or terminal["release_revision"] != revision
        or terminal["source_tree_oid"] != plan["source_tree_oid"]
        or terminal["receipt_sha256"]
        != plan["builder_terminal_receipt_sha256"]
        or terminal["builder_request_sha256"]
        != plan["builder_request_sha256"]
        or terminal["builder_request_identity_sha256"]
        != request["request_sha256"]
        or terminal["source_v3_manifest_sha256"]
        != plan["source_v3_manifest_sha256"]
        or terminal["source_v3_manifest_identity_sha256"]
        != source["manifest_sha256"]
        or terminal["runtime_dependency_manifest_sha256"]
        != plan["runtime_dependency_manifest_sha256"]
        or terminal["runtime_dependency_manifest_identity_sha256"]
        != runtime_manifest["manifest_sha256"]
        or terminal["uv_sha256"] != plan["uv_sha256"]
        or terminal["python_executable_sha256"]
        != plan["interpreter_sha256"]
        or terminal["interpreter_relative_path"]
        != plan["interpreter_relative_path"]
        or terminal["interpreter_sha256"] != plan["interpreter_sha256"]
        or terminal["entrypoint_relative_path"]
        != plan["entrypoint_relative_path"]
        or terminal["entrypoint_sha256"] != plan["entrypoint_sha256"]
        or terminal["builder_identity"] != plan["builder_identity"]
    ):
        _fail("release_update_stage0_builder_binding_invalid")
    return terminal


def _validate_semantic_input_documents(
    documents: Mapping[str, Mapping[str, Any]],
    *,
    publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_trust_sha256: str,
    now_unix: int,
) -> update_inputs.ValidatedStage0Inputs:
    try:
        semantic_documents = {
            name: documents[name] for name in _SEMANTIC_JSON_INPUTS
        }
        return update_inputs.validate_stage0_inputs(
            semantic_documents,
            publication,
            trusted_predecessor,
            expected_trust_sha256,
            now_unix,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        update_inputs.ProductionReleaseUpdateInputsError,
    ):
        _fail("release_update_stage0_semantic_binding_invalid")


def _expected_json_file_sha256(
    digest_field: str,
    plan: Mapping[str, Any],
) -> str | None:
    """Return a physical digest only for fields whose contract is file bytes."""

    if digest_field in _FILE_DIGEST_JSON_INPUTS:
        return str(plan[digest_field])
    if digest_field in _INTERNAL_IDENTITY_FIELDS:
        return None
    _fail("release_update_stage0_input_classification_invalid")


def _verify_release(
    release_root: Path,
    *,
    revision: str,
    expected_uid: int,
    expected_gid: int,
    production: bool,
) -> Mapping[str, Any]:
    if production:
        return builder.verify_published_release(
            release_root,
            revision=revision,
        )
    return builder._verify_published_release_filesystem(
        release_root,
        revision=revision,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        require_logical_owner=False,
        _xattr_reader=lambda _descriptor: (),
    )


def _verify_stage0(
    *,
    roots: Stage0Roots,
    expected_predecessor_activation_receipt_sha256: str,
    production: bool = True,
    now_unix: int | None = None,
    test_expected_uid: int | None = None,
    test_expected_gid: int | None = None,
    test_xattr_reader: _XattrReader | None = None,
) -> VerifiedLaunchBundle:
    """Verify authority, exact artifacts, and the sealed release.

    ``now_unix`` and test identities are accepted only outside production.
    Production always requires Linux, effective root, the fixed roots above,
    and the wall clock observed inside this function.
    """

    if type(production) is not bool:
        _fail("release_update_stage0_contract_invalid")
    normalized_roots = _validate_roots(roots, production=production)
    if production:
        if (
            not sys.platform.startswith("linux")
            or _posix_effective_uid(
                failure_code=(
                    "release_update_stage0_root_authority_required"
                )
            )
            != 0
            or now_unix is not None
            or test_expected_uid is not None
            or test_expected_gid is not None
            or test_xattr_reader is not None
        ):
            _fail("release_update_stage0_root_authority_required")
        expected_uid = 0
        expected_gid = 0
        observed_now = int(time.time())
    else:
        if now_unix is None or type(now_unix) is not int or now_unix <= 0:
            _fail("release_update_stage0_time_invalid")
        expected_uid = (
            _posix_effective_uid(
                failure_code="release_update_stage0_contract_invalid"
            )
            if test_expected_uid is None
            else test_expected_uid
        )
        expected_gid = (
            _posix_effective_gid(
                failure_code="release_update_stage0_contract_invalid"
            )
            if test_expected_gid is None
            else test_expected_gid
        )
        observed_now = now_unix
    xattr_reader = (
        builder._read_descriptor_xattrs  # noqa: SLF001
        if production
        else (
            (lambda _descriptor: ())
            if test_xattr_reader is None
            else test_xattr_reader
        )
    )
    if (
        type(expected_uid) is not int
        or type(expected_gid) is not int
        or min(expected_uid, expected_gid) < 0
        or _SHA256.fullmatch(
            str(expected_predecessor_activation_receipt_sha256)
        )
        is None
    ):
        _fail("release_update_stage0_contract_invalid")

    stack = ExitStack()
    try:
        directories: list[_HeldDirectory] = []
        seen_directories: set[Path] = set()
        for directory_path in (
            normalized_roots.authority_root,
            normalized_roots.input_root,
            normalized_roots.external_pin_path.parent,
            normalized_roots.release_root_parent,
        ):
            if directory_path not in seen_directories:
                directory = stack.enter_context(
                    _open_directory(
                        directory_path,
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                        allowed_modes=_ROOT_DIRECTORY_MODES,
                        xattr_reader=xattr_reader,
                    )
                )
                directories.append(directory)
                seen_directories.add(directory_path)

        held_files: dict[str, builder.HeldRegularFile] = {}
        trust_file = _open_file(
            stack,
            normalized_roots.authority_root / PREDECESSOR_TRUST_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_JSON_MODES,
            maximum_bytes=MAX_JSON_BYTES,
            xattr_reader=xattr_reader,
        )
        held_files["predecessor_trust"] = trust_file
        trust_value = _canonical_mapping(trust_file)

        pin_file = _open_file(
            stack,
            normalized_roots.external_pin_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_JSON_MODES,
            maximum_bytes=65,
            xattr_reader=xattr_reader,
        )
        held_files["external_pin"] = pin_file
        pin_raw = _read_descriptor(pin_file)
        try:
            expected_trust_sha256 = pin_raw[:-1].decode(
                "ascii", errors="strict"
            )
        except UnicodeError:
            _fail("release_update_stage0_external_pin_invalid")
        if (
            len(pin_raw) != 65
            or not pin_raw.endswith(b"\n")
            or _SHA256.fullmatch(expected_trust_sha256) is None
        ):
            _fail("release_update_stage0_external_pin_invalid")
        trusted = contract.validate_predecessor_trust(
            trust_value,
            expected_trust_sha256=expected_trust_sha256,
        )

        publication_file = _open_file(
            stack,
            normalized_roots.authority_root / UPDATE_PUBLICATION_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_JSON_MODES,
            maximum_bytes=MAX_JSON_BYTES,
            xattr_reader=xattr_reader,
        )
        held_files["update_publication"] = publication_file
        publication_value = _canonical_mapping(publication_file)
        publication = contract.validate_publication(
            publication_value,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=expected_trust_sha256,
            now_unix=observed_now,
        )
        plan = publication["plan"]
        if (
            trusted["activation_receipt_sha256"]
            != expected_predecessor_activation_receipt_sha256
            or plan["predecessor_activation_receipt_sha256"]
            != expected_predecessor_activation_receipt_sha256
        ):
            _fail("release_update_stage0_predecessor_cas_mismatch")

        revision = str(publication["release_revision"])
        if _REVISION.fullmatch(revision) is None:
            _fail("release_update_stage0_release_binding_invalid")
        release_root = (
            normalized_roots.release_root_parent
            / f"hermes-agent-{revision[:12]}"
        )
        if production and str(release_root) != plan["release_root"]:
            _fail("release_update_stage0_release_binding_invalid")

        release_directory = stack.enter_context(
            _open_directory(
                release_root,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=frozenset({0o555}),
                xattr_reader=xattr_reader,
            )
        )
        directories.append(release_directory)

        input_documents: dict[str, Mapping[str, Any]] = {}
        for digest_field, filename in _JSON_INPUTS.items():
            held = _open_file(
                stack,
                normalized_roots.input_root / filename,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=_ROOT_JSON_MODES,
                maximum_bytes=MAX_JSON_BYTES,
                xattr_reader=xattr_reader,
                expected_sha256=_expected_json_file_sha256(
                    digest_field,
                    plan,
                ),
            )
            held_files[digest_field] = held
            input_documents[digest_field] = _canonical_mapping(held)
        for digest_field, filename in _BINARY_INPUTS.items():
            held_files[digest_field] = _open_file(
                stack,
                normalized_roots.input_root / filename,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=_ROOT_EXECUTABLE_MODES,
                maximum_bytes=MAX_EXECUTABLE_BYTES,
                xattr_reader=xattr_reader,
                expected_sha256=str(plan[digest_field]),
            )
        terminal = _validate_builder_input_documents(input_documents, plan)
        input_documents["builder_terminal_receipt_sha256"] = terminal
        semantic_inputs = _validate_semantic_input_documents(
            input_documents,
            publication=publication,
            trusted_predecessor=trusted,
            expected_trust_sha256=expected_trust_sha256,
            now_unix=observed_now,
        )
        input_documents.update(semantic_inputs.documents)

        manifest_file = _open_file(
            stack,
            release_root / builder.MANIFEST_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_JSON_MODES,
            maximum_bytes=builder.MAX_RECORD_BYTES,
            xattr_reader=xattr_reader,
        )
        receipt_file = _open_file(
            stack,
            release_root / builder.RECEIPT_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_JSON_MODES,
            maximum_bytes=builder.MAX_RECORD_BYTES,
            xattr_reader=xattr_reader,
        )
        held_files["builder_manifest"] = manifest_file
        held_files["builder_receipt"] = receipt_file
        manifest = _canonical_mapping(manifest_file)
        receipt_document = _canonical_mapping(receipt_file)

        interpreter = _open_file(
            stack,
            release_root / str(plan["interpreter_relative_path"]),
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_EXECUTABLE_MODES,
            maximum_bytes=MAX_EXECUTABLE_BYTES,
            xattr_reader=xattr_reader,
            expected_sha256=str(plan["interpreter_sha256"]),
        )
        entrypoint = _open_file(
            stack,
            release_root / str(plan["entrypoint_relative_path"]),
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=_ROOT_ENTRYPOINT_MODES,
            maximum_bytes=MAX_ENTRYPOINT_BYTES,
            xattr_reader=xattr_reader,
            expected_sha256=str(plan["entrypoint_sha256"]),
        )
        held_files["interpreter"] = interpreter
        held_files["entrypoint"] = entrypoint

        verified_receipt = _verify_release(
            release_root,
            revision=revision,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            production=production,
        )
        if (
            verified_receipt != receipt_document
            or receipt_document
            != input_documents["candidate_seal_receipt_sha256"]
            or receipt_document.get("receipt_sha256")
            != plan["candidate_seal_receipt_sha256"]
            or manifest.get("manifest_sha256")
            != plan["whole_tree_manifest_sha256"]
            or receipt_document.get("manifest_sha256")
            != plan["whole_tree_manifest_sha256"]
            or manifest.get("release_revision") != revision
            or receipt_document.get("release_revision") != revision
            or (
                production
                and (
                    manifest.get("release_root") != plan["release_root"]
                    or receipt_document.get("release_root")
                    != plan["release_root"]
                )
            )
        ):
            _fail("release_update_stage0_release_binding_invalid")
        _validate_manifest_identities(manifest, plan)
        input_documents["candidate_seal_receipt_sha256"] = dict(
            receipt_document
        )
        internal_identities = {
            "builder_terminal_receipt_sha256": terminal["receipt_sha256"],
            "candidate_seal_receipt_sha256": receipt_document[
                "receipt_sha256"
            ],
            **semantic_inputs.identities,
        }
        if (
            set(internal_identities) != set(_INTERNAL_IDENTITY_FIELDS)
            or any(
                plan[name] != identity
                for name, identity in internal_identities.items()
            )
        ):
            _fail("release_update_stage0_internal_identity_invalid")

        bundle = VerifiedLaunchBundle(
            predecessor_trust=dict(trusted),
            publication=dict(publication),
            builder_manifest=dict(manifest),
            builder_receipt=dict(verified_receipt),
            input_documents=dict(input_documents),
            input_internal_identities=dict(internal_identities),
            fixed_v4_inputs=dict(semantic_inputs.fixed_v4_inputs),
            held_files=dict(held_files),
            held_directories=tuple(directories),
            release_root=release_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            production=production,
            xattr_reader=xattr_reader,
            _stack=stack.pop_all(),
        )
        bundle.assert_stable()
        return bundle
    except ProductionReleaseUpdateStage0Error:
        stack.close()
        raise
    except (
        builder.ProductionReleaseBuilderError,
        contract.ProductionReleaseUpdateContractError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        stack.close()
        _fail("release_update_stage0_verification_failed")


def verify_stage0(
    *,
    expected_predecessor_activation_receipt_sha256: str,
) -> VerifiedLaunchBundle:
    """Verify production authority using only fixed roots and the real clock."""

    return _verify_stage0(
        roots=production_roots(),
        expected_predecessor_activation_receipt_sha256=(
            expected_predecessor_activation_receipt_sha256
        ),
        production=True,
    )


def _verify_stage0_for_test(
    *,
    roots: Stage0Roots,
    expected_predecessor_activation_receipt_sha256: str,
    now_unix: int,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    xattr_reader: _XattrReader | None = None,
) -> VerifiedLaunchBundle:
    """Exercise the verifier without production roots or root authority."""

    return _verify_stage0(
        roots=roots,
        expected_predecessor_activation_receipt_sha256=(
            expected_predecessor_activation_receipt_sha256
        ),
        production=False,
        now_unix=now_unix,
        test_expected_uid=expected_uid,
        test_expected_gid=expected_gid,
        test_xattr_reader=xattr_reader,
    )


__all__ = [
    "PRODUCTION_AUTHORITY_ROOT",
    "PRODUCTION_EXTERNAL_PIN_PATH",
    "PRODUCTION_INPUT_ROOT",
    "PRODUCTION_RELEASE_ROOT_PARENT",
    "ProductionReleaseUpdateStage0Error",
    "Stage0Roots",
    "VerifiedLaunchBundle",
    "production_roots",
    "verify_stage0",
]
