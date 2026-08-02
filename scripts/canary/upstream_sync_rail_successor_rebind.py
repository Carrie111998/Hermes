#!/usr/bin/env python3
"""Crash-safe successor rebinding for the production dual-sync rail.

The original activation edge remains a one-time absent-or-exact installer.
This module is a separate, schema-bound transaction for the later case where
the four installed rail units still reference immutable releases that have
already been retired.  It accepts no prose or model classification input.
Every decision is made from exact unit names, revisions, byte digests, and
systemd state.

The transaction stops only the two rail timers.  The two oneshot services
must already be inactive.  It archives all four exact predecessor unit files,
atomically replaces each file with the target package byte, reloads systemd,
enables and starts the timers, and runs one synchronous catch-up.  A durable
start/archive journal permits exact predecessor/target mixtures to recover
forward after a crash.  Foreign bytes or failed postconditions restore the
archived predecessor files and leave a fail-closed rollback receipt.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import os
import re
import stat
import struct
import sys
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence

from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import upstream_sync_rail_cutover as activation
from scripts.canary.production_cutover_activation_lock import (
    authority_activation_lock,
)
from scripts.canary import production_release_builder_runtime as release_builder
from scripts.canary import production_release_update_stage0 as release_stage0


AUTHORITY_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-authority.v1"
PREFLIGHT_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-preflight.v1"
STARTED_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-started.v1"
ARCHIVE_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-archive.v1"
TERMINAL_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-terminal.v1"
OWNER_REQUEST_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-owner-request.v3"
OWNER_RESULT_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-owner-result.v3"
ROLLBACK_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-rollback.v1"
ROLLBACK_INTENT_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-rollback-intent.v1"
OPERATION = "dual-upstream-sync-successor-rebind"

UNIT_NAMES = activation.UNIT_NAMES
TIMER_NAMES = activation.TIMER_NAMES
SERVICE_NAMES = (rail.SYNC_SERVICE_UNIT, rail.REPORT_SERVICE_UNIT)

SYSTEMD_ROOT = Path("/etc/systemd/system")
STAGED_ROOT = rail.PACKAGE_ROOT
AUTHORITY_PATH = STAGED_ROOT / "successor-rebind-authority.json"
PREFLIGHT_PATH = STAGED_ROOT / "successor-rebind-preflight.json"
EVIDENCE_ROOT = Path(
    "/var/lib/muncho-production-legacy-cutover/dual-upstream-sync-successor-rebind"
)
RUNTIME_RELATIVE = Path("scripts/canary/upstream_sync_rail_successor_rebind.py")
FOUNDATION_V4_WRAPPER = Path("/usr/libexec/muncho-release-foundation-exec-v4")
FOUNDATION_V4_WRAPPER_SHA256 = (
    "69a93dba1b6177ef015fb61aa8b65fef5b8ca9173f7a76848867236ba17c746b"
)
PREEXEC_VERIFIER_SHA256 = (
    "4cb6d6924a86393776a723597a5110c19fbf6d71f7f5bd37550b29dc407030aa"
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OWNER_FRAME_MAGIC = b"MSR2"
_OWNER_FRAME_MAX_BYTES = 16 * 1024
_UNIT_DIGEST_FIELDS = frozenset(UNIT_NAMES)
_OWNER_RUNTIME_IDENTITY_FIELDS = (
    "source_tree_oid",
    "stage_c_builder_terminal_receipt_sha256",
    "foundation_wrapper_sha256",
    "preexec_verifier_sha256",
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
)
_ENABLED_STATES = frozenset({
    "alias",
    "bad",
    "disabled",
    "enabled",
    "enabled-runtime",
    "generated",
    "indirect",
    "linked",
    "linked-runtime",
    "masked",
    "masked-runtime",
    "static",
    "transient",
})


class UpstreamSyncRailSuccessorRebindError(RuntimeError):
    """Stable, secret-free successor-rebind failure."""

    def __init__(self, code: str, subject: str | None = None) -> None:
        self.code = code
        self.subject = subject
        super().__init__(code if subject is None else f"{code}:{subject}")


def _fail(code: str, subject: str | None = None) -> NoReturn:
    raise UpstreamSyncRailSuccessorRebindError(code, subject)


@dataclass(frozen=True)
class UnitState:
    """Exact systemd and stable-open fragment observation for one rail unit."""

    unit: str
    loaded: bool
    fragment_path: str | None
    fragment_sha256: str | None
    enabled_state: str
    active_state: str
    assert_result: str
    result: str
    exec_main_status: int | None


class RebindHost(Protocol):
    """Private systemd boundary used by production and temp-root tests."""

    def observe(self, unit: str, *, systemd_root: Path) -> UnitState: ...

    def mutate(self, *arguments: str) -> None: ...


class Stage0Verifier(Protocol):
    """Canonical fixed-root Stage-0 verifier boundary."""

    def __call__(
        self,
        *,
        expected_predecessor_activation_receipt_sha256: str,
    ) -> release_stage0.VerifiedLaunchBundle: ...


def _receipt(unsigned: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(unsigned))
    return {
        **payload,
        "receipt_sha256": activation._sha256(  # noqa: SLF001
            activation._canonical(payload)  # noqa: SLF001
        ),
    }


def build_owner_request(
    *,
    target_revision: str,
    target_package_manifest_sha256: str,
    predecessor_revision: str,
    predecessor_activation_receipt_sha256: str,
    stage_c_host_artifact_manifest_sha256: str,
    stage_c_release_update_publication_sha256: str,
    source_tree_oid: str,
    stage_c_builder_terminal_receipt_sha256: str,
    rebind_runtime_sha256: str,
    foundation_wrapper_sha256: str,
    controller_owner_runtime_manifest_sha256: str,
    controller_owner_runtime_attestation_sha256: str,
    controller_owner_runtime_tree_sha256: str,
    controller_owner_runtime_interpreter_sha256: str,
    remote_owner_runtime_publication_sha256: str,
    remote_owner_runtime_manifest_sha256: str,
    remote_owner_runtime_attestation_sha256: str,
    remote_owner_runtime_tree_sha256: str,
    remote_owner_runtime_interpreter_sha256: str,
    remote_owner_runtime_staging_publication_sha256: str,
    remote_owner_runtime_staging_manifest_sha256: str,
    remote_owner_runtime_staging_attestation_sha256: str,
    remote_owner_runtime_staging_tree_sha256: str,
    remote_owner_runtime_staging_interpreter_sha256: str,
    remote_owner_runtime_staging_pyvenv_cfg_sha256: str,
    remote_owner_runtime_builder_receipt_sha256: str,
    remote_owner_runtime_wheel_sha256: str,
    preexec_verifier_sha256: str,
) -> dict[str, Any]:
    """Build the sole public owner input from exact reviewed identities."""

    unsigned = {
        "schema": OWNER_REQUEST_SCHEMA,
        "operation": OPERATION,
        "target_revision": target_revision,
        "target_package_manifest_sha256": target_package_manifest_sha256,
        "predecessor_revision": predecessor_revision,
        "predecessor_activation_receipt_sha256": (
            predecessor_activation_receipt_sha256
        ),
        "stage_c_host_artifact_manifest_sha256": (
            stage_c_host_artifact_manifest_sha256
        ),
        "stage_c_release_update_publication_sha256": (
            stage_c_release_update_publication_sha256
        ),
        "source_tree_oid": source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            stage_c_builder_terminal_receipt_sha256
        ),
        "rebind_runtime_sha256": rebind_runtime_sha256,
        "foundation_wrapper_sha256": foundation_wrapper_sha256,
        "controller_owner_runtime_manifest_sha256": (
            controller_owner_runtime_manifest_sha256
        ),
        "controller_owner_runtime_attestation_sha256": (
            controller_owner_runtime_attestation_sha256
        ),
        "controller_owner_runtime_tree_sha256": (controller_owner_runtime_tree_sha256),
        "controller_owner_runtime_interpreter_sha256": (
            controller_owner_runtime_interpreter_sha256
        ),
        "remote_owner_runtime_publication_sha256": (
            remote_owner_runtime_publication_sha256
        ),
        "remote_owner_runtime_manifest_sha256": remote_owner_runtime_manifest_sha256,
        "remote_owner_runtime_attestation_sha256": (
            remote_owner_runtime_attestation_sha256
        ),
        "remote_owner_runtime_tree_sha256": remote_owner_runtime_tree_sha256,
        "remote_owner_runtime_interpreter_sha256": (
            remote_owner_runtime_interpreter_sha256
        ),
        "remote_owner_runtime_staging_publication_sha256": (
            remote_owner_runtime_staging_publication_sha256
        ),
        "remote_owner_runtime_staging_manifest_sha256": (
            remote_owner_runtime_staging_manifest_sha256
        ),
        "remote_owner_runtime_staging_attestation_sha256": (
            remote_owner_runtime_staging_attestation_sha256
        ),
        "remote_owner_runtime_staging_tree_sha256": (
            remote_owner_runtime_staging_tree_sha256
        ),
        "remote_owner_runtime_staging_interpreter_sha256": (
            remote_owner_runtime_staging_interpreter_sha256
        ),
        "remote_owner_runtime_staging_pyvenv_cfg_sha256": (
            remote_owner_runtime_staging_pyvenv_cfg_sha256
        ),
        "remote_owner_runtime_builder_receipt_sha256": (
            remote_owner_runtime_builder_receipt_sha256
        ),
        "remote_owner_runtime_wheel_sha256": remote_owner_runtime_wheel_sha256,
        "preexec_verifier_sha256": preexec_verifier_sha256,
        "caller_selected_paths_allowed": False,
        "caller_selected_commands_allowed": False,
        "caller_selected_targets_allowed": False,
        "manual_json_allowed": False,
        "semantic_decisions_allowed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request = {
        **unsigned,
        "request_sha256": activation._sha256(  # noqa: SLF001
            activation._canonical(unsigned)  # noqa: SLF001
        ),
    }
    return validate_owner_request(request)


def validate_owner_request(value: Any) -> dict[str, Any]:
    fields = {
        "schema",
        "operation",
        "target_revision",
        "target_package_manifest_sha256",
        "predecessor_revision",
        "predecessor_activation_receipt_sha256",
        "stage_c_host_artifact_manifest_sha256",
        "stage_c_release_update_publication_sha256",
        "source_tree_oid",
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
        "caller_selected_paths_allowed",
        "caller_selected_commands_allowed",
        "caller_selected_targets_allowed",
        "manual_json_allowed",
        "semantic_decisions_allowed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "request_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("upstream_sync_successor_owner_request_invalid")
    request = copy.deepcopy(dict(value))
    if (
        request.get("schema") != OWNER_REQUEST_SCHEMA
        or request.get("operation") != OPERATION
        or any(
            _SHA40.fullmatch(str(request.get(name, ""))) is None
            for name in (
                "target_revision",
                "predecessor_revision",
                "source_tree_oid",
            )
        )
        or request["target_revision"] == request["predecessor_revision"]
        or request["foundation_wrapper_sha256"] != FOUNDATION_V4_WRAPPER_SHA256
        or request["preexec_verifier_sha256"] != PREEXEC_VERIFIER_SHA256
        or any(
            _SHA256.fullmatch(str(request.get(name, ""))) is None
            for name in (
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
                "request_sha256",
            )
        )
        or any(
            request.get(name) is not False
            for name in (
                "caller_selected_paths_allowed",
                "caller_selected_commands_allowed",
                "caller_selected_targets_allowed",
                "manual_json_allowed",
                "semantic_decisions_allowed",
                "secret_material_recorded",
                "secret_digest_recorded",
            )
        )
        or request["request_sha256"]
        != activation._sha256(  # noqa: SLF001
            activation._canonical(  # noqa: SLF001
                {
                    name: item
                    for name, item in request.items()
                    if name != "request_sha256"
                }
            )
        )
    ):
        _fail("upstream_sync_successor_owner_request_invalid")
    return request


def encode_owner_request(value: Mapping[str, Any]) -> bytes:
    request = validate_owner_request(value)
    payload = activation._canonical(request)  # noqa: SLF001
    if not payload or len(payload) > _OWNER_FRAME_MAX_BYTES:
        _fail("upstream_sync_successor_owner_frame_invalid")
    return _OWNER_FRAME_MAGIC + struct.pack(">I", len(payload)) + payload


def decode_owner_request(frame: bytes) -> dict[str, Any]:
    if (
        not isinstance(frame, bytes)
        or len(frame) < 9
        or len(frame) > _OWNER_FRAME_MAX_BYTES + 8
        or frame[:4] != _OWNER_FRAME_MAGIC
    ):
        _fail("upstream_sync_successor_owner_frame_invalid")
    length = struct.unpack(">I", frame[4:8])[0]
    if length != len(frame) - 8 or not 0 < length <= _OWNER_FRAME_MAX_BYTES:
        _fail("upstream_sync_successor_owner_frame_invalid")
    try:
        value = activation._decode(frame[8:])  # noqa: SLF001
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_owner_frame_invalid"
        ) from exc
    return validate_owner_request(value)


def _unit_digest_map(value: Any, *, code: str) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _UNIT_DIGEST_FIELDS
        or any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in value.values()
        )
    ):
        _fail(code)
    return {name: str(value[name]) for name in UNIT_NAMES}


def _release_references(raw: bytes) -> frozenset[str]:
    """Extract only exact content-addressed production release roots."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        _fail("upstream_sync_successor_unit_invalid")
    pattern = re.compile(
        re.escape(str(rail.RELEASES_ROOT))
        + r"/hermes-agent-[0-9a-f]{12}(?=/|[^A-Za-z0-9_.-]|$)"
    )
    return frozenset(match.group(0) for match in pattern.finditer(text))


def _expected_missing_roots(
    predecessor_revision: str,
    predecessor_sender_release_root: str,
) -> tuple[str, ...]:
    predecessor_root = str(rail.release_root(predecessor_revision))
    if _SHA40.fullmatch(predecessor_revision or "") is None:
        _fail("upstream_sync_successor_revision_invalid")
    valid_roots = _release_references(
        f"{predecessor_sender_release_root}\n".encode("ascii", errors="strict")
    )
    if valid_roots != frozenset({predecessor_sender_release_root}):
        _fail("upstream_sync_successor_revision_invalid")
    return tuple(sorted({predecessor_root, predecessor_sender_release_root}))


def _derive_predecessor_sender_release_root(
    *,
    predecessor_revision: str,
    predecessor_units: Mapping[str, bytes],
) -> str:
    report_raw = predecessor_units.get(rail.REPORT_SERVICE_UNIT)
    if not isinstance(report_raw, bytes):
        _fail("upstream_sync_successor_predecessor_unit_invalid")
    predecessor_root = str(rail.release_root(predecessor_revision))
    references = _release_references(report_raw)
    if predecessor_root not in references or len(references) not in {1, 2}:
        _fail("upstream_sync_successor_predecessor_refs_invalid")
    sender_roots = references - {predecessor_root}
    return next(iter(sender_roots), predecessor_root)


def _validate_predecessor_unit_contracts(
    *,
    predecessor_revision: str,
    predecessor_sender_release_root: str,
    predecessor_units: Mapping[str, bytes],
) -> None:
    report_raw = predecessor_units.get(rail.REPORT_SERVICE_UNIT)
    if not isinstance(report_raw, bytes):
        _fail("upstream_sync_successor_predecessor_unit_invalid")
    match = re.search(
        rb"(?:^| )--sender-python-sha256 ([0-9a-f]{64})(?: |\n|$)",
        report_raw,
    )
    if match is None or len(match.groups()) != 1:
        _fail("upstream_sync_successor_predecessor_unit_invalid")
    try:
        rail.validate_sync_service(
            predecessor_units[rail.SYNC_SERVICE_UNIT],
            revision=predecessor_revision,
            release=rail.release_root(predecessor_revision),
        )
        rail.validate_sync_timer(predecessor_units[rail.SYNC_TIMER_UNIT])
        rail.validate_report_service(
            report_raw,
            release=rail.release_root(predecessor_revision),
            sender_release=Path(predecessor_sender_release_root),
            sender_python_sha256=match.group(1).decode("ascii", errors="strict"),
        )
        rail.validate_report_timer(predecessor_units[rail.REPORT_TIMER_UNIT])
    except (KeyError, UnicodeError, rail.DualSyncRailError) as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_predecessor_unit_invalid"
        ) from exc


def build_authority(
    *,
    package: activation.PackageContext,
    predecessor_revision: str,
    predecessor_units: Mapping[str, bytes],
    stage_c_host_artifact_manifest_sha256: str,
    stage_c_release_update_publication_sha256: str,
    rebind_runtime_sha256: str,
) -> dict[str, Any]:
    """Build the canonical v1 migration authority from exact public facts."""

    if (
        not isinstance(package, activation.PackageContext)
        or package.manifest.get("release_revision")
        != package.manifest.get("sender_revision")
        or _SHA40.fullmatch(str(package.manifest.get("release_revision", ""))) is None
        or any(
            _SHA256.fullmatch(value or "") is None
            for value in (
                stage_c_host_artifact_manifest_sha256,
                stage_c_release_update_publication_sha256,
                rebind_runtime_sha256,
            )
        )
        or not isinstance(predecessor_units, Mapping)
        or set(predecessor_units) != _UNIT_DIGEST_FIELDS
        or any(
            not isinstance(raw, bytes) or not raw for raw in predecessor_units.values()
        )
    ):
        _fail("upstream_sync_successor_authority_invalid")
    predecessor_sender_release_root = _derive_predecessor_sender_release_root(
        predecessor_revision=predecessor_revision,
        predecessor_units=predecessor_units,
    )
    missing_roots = _expected_missing_roots(
        predecessor_revision,
        predecessor_sender_release_root,
    )
    _validate_predecessor_unit_contracts(
        predecessor_revision=predecessor_revision,
        predecessor_sender_release_root=predecessor_sender_release_root,
        predecessor_units=predecessor_units,
    )
    observed_refs = frozenset(
        ref for raw in predecessor_units.values() for ref in _release_references(raw)
    )
    if observed_refs != frozenset(missing_roots):
        _fail("upstream_sync_successor_predecessor_refs_invalid")
    predecessor_digests = {
        name: activation._sha256(predecessor_units[name])  # noqa: SLF001
        for name in UNIT_NAMES
    }
    target_digests = _unit_digest_map(
        package.manifest.get("artifacts"),
        code="upstream_sync_successor_target_digests_invalid",
    )
    if any(predecessor_digests[name] == target_digests[name] for name in SERVICE_NAMES):
        _fail("upstream_sync_successor_target_not_successor")
    target_revision = str(package.manifest["release_revision"])
    unsigned = {
        "schema": AUTHORITY_SCHEMA,
        "operation": OPERATION,
        "predecessor_revision": predecessor_revision,
        "predecessor_sender_release_root": predecessor_sender_release_root,
        "predecessor_unit_digests": predecessor_digests,
        "predecessor_missing_release_roots": list(missing_roots),
        "target_revision": target_revision,
        "target_release_root": str(rail.release_root(target_revision)),
        "target_package_manifest_sha256": package.manifest["manifest_sha256"],
        "target_unit_digests": target_digests,
        "stage_c_host_artifact_manifest_sha256": (
            stage_c_host_artifact_manifest_sha256
        ),
        "stage_c_release_update_publication_sha256": (
            stage_c_release_update_publication_sha256
        ),
        "rebind_runtime_sha256": rebind_runtime_sha256,
        "unit_names": list(UNIT_NAMES),
        "timer_units": list(TIMER_NAMES),
        "service_units": list(SERVICE_NAMES),
        "systemd_stop_units": list(TIMER_NAMES),
        "predecessor_release_refs_must_be_missing": True,
        "target_sender_equals_target": True,
        "atomic_archive_and_replace_required": True,
        "forward_recovery_enabled": True,
        "rollback_on_deviation": True,
        "auto_merge_or_deploy_enabled": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "authority_sha256": activation._sha256(  # noqa: SLF001
            activation._canonical(unsigned)  # noqa: SLF001
        ),
    }


def validate_authority(
    value: Mapping[str, Any],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "operation",
        "predecessor_revision",
        "predecessor_sender_release_root",
        "predecessor_unit_digests",
        "predecessor_missing_release_roots",
        "target_revision",
        "target_release_root",
        "target_package_manifest_sha256",
        "target_unit_digests",
        "stage_c_host_artifact_manifest_sha256",
        "stage_c_release_update_publication_sha256",
        "rebind_runtime_sha256",
        "unit_names",
        "timer_units",
        "service_units",
        "systemd_stop_units",
        "predecessor_release_refs_must_be_missing",
        "target_sender_equals_target",
        "atomic_archive_and_replace_required",
        "forward_recovery_enabled",
        "rollback_on_deviation",
        "auto_merge_or_deploy_enabled",
        "secret_material_recorded",
        "secret_digest_recorded",
        "authority_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("upstream_sync_successor_authority_invalid")
    predecessor = str(value.get("predecessor_revision", ""))
    predecessor_sender_release_root = str(
        value.get("predecessor_sender_release_root", "")
    )
    target = str(value.get("target_revision", ""))
    digests = _unit_digest_map(
        value.get("predecessor_unit_digests"),
        code="upstream_sync_successor_authority_invalid",
    )
    target_digests = _unit_digest_map(
        value.get("target_unit_digests"),
        code="upstream_sync_successor_authority_invalid",
    )
    expected_missing = _expected_missing_roots(
        predecessor,
        predecessor_sender_release_root,
    )
    if (
        set(value) != fields
        or value.get("schema") != AUTHORITY_SCHEMA
        or value.get("operation") != OPERATION
        or _SHA40.fullmatch(target) is None
        or target == predecessor
        or str(rail.release_root(target)) == predecessor_sender_release_root
        or value.get("predecessor_missing_release_roots") != list(expected_missing)
        or value.get("target_release_root") != str(rail.release_root(target))
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "target_package_manifest_sha256",
                "stage_c_host_artifact_manifest_sha256",
                "stage_c_release_update_publication_sha256",
                "rebind_runtime_sha256",
                "authority_sha256",
            )
        )
        or value.get("unit_names") != list(UNIT_NAMES)
        or value.get("timer_units") != list(TIMER_NAMES)
        or value.get("service_units") != list(SERVICE_NAMES)
        or value.get("systemd_stop_units") != list(TIMER_NAMES)
        or any(
            value.get(name) is not expected
            for name, expected in (
                ("predecessor_release_refs_must_be_missing", True),
                ("target_sender_equals_target", True),
                ("atomic_archive_and_replace_required", True),
                ("forward_recovery_enabled", True),
                ("rollback_on_deviation", True),
                ("auto_merge_or_deploy_enabled", False),
                ("secret_material_recorded", False),
                ("secret_digest_recorded", False),
            )
        )
        or any(digests[name] == target_digests[name] for name in SERVICE_NAMES)
        or value.get("authority_sha256") != expected_sha256
        or activation._sha256(  # noqa: SLF001
            activation._canonical(  # noqa: SLF001
                {key: item for key, item in value.items() if key != "authority_sha256"}
            )
        )
        != expected_sha256
    ):
        _fail("upstream_sync_successor_authority_invalid")
    return copy.deepcopy(dict(value))


def _load_context(
    *,
    expected_authority_sha256: str,
    staged_root: Path,
    authority_path: Path,
    runtime_path: Path,
    root_owned: bool,
    release_trust_root: Path,
) -> tuple[dict[str, Any], activation.PackageContext]:
    authority = validate_authority(
        activation._read_canonical_json(  # noqa: SLF001
            authority_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        ),
        expected_sha256=expected_authority_sha256,
    )
    package = activation._validate_package_context(  # noqa: SLF001
        staged_root=staged_root,
        release_revision=authority["target_revision"],
        sender_revision=authority["target_revision"],
        expected_manifest_sha256=authority["target_package_manifest_sha256"],
        root_owned=root_owned,
        staged_trust_root=(activation.STAGED_TRUST_ROOT if root_owned else staged_root),
        release_trust_root=release_trust_root,
    )
    if (
        package.manifest.get("artifacts") != authority["target_unit_digests"]
        or package.manifest.get("release_revision") != authority["target_revision"]
        or package.manifest.get("sender_revision") != authority["target_revision"]
    ):
        _fail("upstream_sync_successor_package_lineage_invalid")
    try:
        runtime_raw, _metadata = activation._read_regular(  # noqa: SLF001
            runtime_path,
            maximum=2 * 1024 * 1024,
            root_owned=root_owned,
        )
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_runtime_invalid"
        ) from exc
    if activation._sha256(runtime_raw) != authority["rebind_runtime_sha256"]:  # noqa: SLF001
        _fail("upstream_sync_successor_runtime_invalid")
    return authority, package


@dataclass
class _HeldStagedInputs(AbstractContextManager["_HeldStagedInputs"]):
    """Exact staged authority/preflight inodes held across mutation."""

    authority: release_builder.HeldRegularFile
    preflight: release_builder.HeldRegularFile

    def assert_stable(self) -> None:
        try:
            self.authority.assert_stable()
            self.preflight.assert_stable()
        except release_builder.ProductionReleaseBuilderError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_stage_identity_drifted"
            ) from exc

    def close(self) -> None:
        self.preflight.close()
        self.authority.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _hold_staged_inputs(
    *,
    authority_path: Path,
    authority: Mapping[str, Any],
    preflight_path: Path,
    preflight: Mapping[str, Any],
    root_owned: bool,
) -> _HeldStagedInputs:
    expected_uid = 0 if root_owned else activation._effective_uid()  # noqa: SLF001
    expected_gid = 0 if root_owned else activation._effective_gid()  # noqa: SLF001
    authority_held: release_builder.HeldRegularFile | None = None
    try:
        authority_held = release_builder.open_held_regular(
            authority_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=frozenset({0o444}),
            maximum_bytes=activation.MAX_JSON_BYTES,
            expected_sha256=hashlib.sha256(
                activation._canonical(authority) + b"\n"  # noqa: SLF001
            ).hexdigest(),
        )
        preflight_held = release_builder.open_held_regular(
            preflight_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=frozenset({0o444}),
            maximum_bytes=activation.MAX_JSON_BYTES,
            expected_sha256=hashlib.sha256(
                activation._canonical(preflight) + b"\n"  # noqa: SLF001
            ).hexdigest(),
        )
        held = _HeldStagedInputs(
            authority=authority_held,
            preflight=preflight_held,
        )
        try:
            held.assert_stable()
        except UpstreamSyncRailSuccessorRebindError:
            held.close()
            raise
        return held
    except release_builder.ProductionReleaseBuilderError as exc:
        if authority_held is not None:
            authority_held.close()
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_identity_invalid"
        ) from exc


@dataclass
class _HeldUnitSet(AbstractContextManager["_HeldUnitSet"]):
    files: Mapping[str, release_builder.HeldRegularFile]

    def assert_stable(self) -> None:
        try:
            for name in UNIT_NAMES:
                self.files[name].assert_stable()
        except (KeyError, release_builder.ProductionReleaseBuilderError) as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_unit_identity_drifted"
            ) from exc

    def close(self) -> None:
        for held in reversed(tuple(self.files.values())):
            held.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _hold_unit_set(
    *,
    systemd_root: Path,
    expected_digests: Mapping[str, str],
    root_owned: bool,
) -> _HeldUnitSet:
    if set(expected_digests) != _UNIT_DIGEST_FIELDS:
        _fail("upstream_sync_successor_unit_identity_invalid")
    expected_uid = 0 if root_owned else activation._effective_uid()  # noqa: SLF001
    expected_gid = 0 if root_owned else activation._effective_gid()  # noqa: SLF001
    held: dict[str, release_builder.HeldRegularFile] = {}
    try:
        for name in UNIT_NAMES:
            held[name] = release_builder.open_held_regular(
                systemd_root / name,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=frozenset({0o644}),
                maximum_bytes=2 * 1024 * 1024,
                expected_sha256=expected_digests[name],
            )
        result = _HeldUnitSet(files=held)
        try:
            result.assert_stable()
        except UpstreamSyncRailSuccessorRebindError:
            result.close()
            raise
        return result
    except release_builder.ProductionReleaseBuilderError as exc:
        for opened in reversed(tuple(held.values())):
            opened.close()
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_unit_identity_invalid"
        ) from exc


def _unit_bytes(
    *,
    systemd_root: Path,
    root_owned: bool,
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for name in UNIT_NAMES:
        try:
            raw, metadata = activation._read_regular(  # noqa: SLF001
                systemd_root / name,
                maximum=2 * 1024 * 1024,
                modes=frozenset({0o644}),
                root_owned=root_owned,
            )
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_unit_unavailable",
                name,
            ) from exc
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            _fail("upstream_sync_successor_unit_mode_invalid", name)
        result[name] = raw
    return result


def _digest_bytes(units: Mapping[str, bytes]) -> dict[str, str]:
    return {
        name: activation._sha256(units[name])  # noqa: SLF001
        for name in UNIT_NAMES
    }


def _prove_missing_predecessor_refs(authority: Mapping[str, Any]) -> None:
    for raw_path in authority["predecessor_missing_release_roots"]:
        path = Path(raw_path)
        if os.path.lexists(path):
            _fail("upstream_sync_successor_predecessor_release_present")


def _validate_predecessor_units(
    units: Mapping[str, bytes],
    authority: Mapping[str, Any],
) -> None:
    if _digest_bytes(units) != authority["predecessor_unit_digests"]:
        _fail("upstream_sync_successor_predecessor_unit_drifted")
    references = frozenset(
        ref for raw in units.values() for ref in _release_references(raw)
    )
    if references != frozenset(authority["predecessor_missing_release_roots"]):
        _fail("upstream_sync_successor_predecessor_refs_invalid")


class _SystemdHost:
    """Fixed production systemd implementation."""

    def __init__(self, *, root_owned: bool = True) -> None:
        self._root_owned = root_owned

    @staticmethod
    def _property(unit: str, name: str) -> str:
        try:
            raw = activation._systemctl_property(unit, name)  # noqa: SLF001
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_systemd_observation_invalid"
            ) from exc
        if not raw.endswith(b"\n"):
            _fail("upstream_sync_successor_systemd_observation_invalid")
        try:
            value = raw[:-1].decode("utf-8", errors="strict")
        except UnicodeError:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        if "\n" in value:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        return value

    def observe(self, unit: str, *, systemd_root: Path) -> UnitState:
        if unit not in UNIT_NAMES:
            _fail("upstream_sync_successor_unit_identity_invalid")
        load = self._property(unit, "LoadState")
        if load != "loaded":
            return UnitState(
                unit=unit,
                loaded=False,
                fragment_path=None,
                fragment_sha256=None,
                enabled_state="not-found",
                active_state="inactive",
                assert_result="",
                result="",
                exec_main_status=None,
            )
        fragment = self._property(unit, "FragmentPath")
        if fragment != str(systemd_root / unit):
            _fail("upstream_sync_successor_systemd_fragment_invalid", unit)
        try:
            raw, metadata = activation._read_regular(  # noqa: SLF001
                Path(fragment),
                maximum=2 * 1024 * 1024,
                modes=frozenset({0o644}),
                root_owned=self._root_owned,
            )
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_systemd_fragment_invalid",
                unit,
            ) from exc
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            _fail("upstream_sync_successor_unit_mode_invalid", unit)
        try:
            enabled_code, enabled_raw = activation._systemctl_capture(  # noqa: SLF001
                "is-enabled", unit
            )
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_systemd_observation_invalid"
            ) from exc
        if enabled_code not in {0, 1} or not enabled_raw.endswith(b"\n"):
            _fail("upstream_sync_successor_systemd_observation_invalid")
        try:
            enabled_state = enabled_raw[:-1].decode("ascii", errors="strict")
        except UnicodeError:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        if "\n" in enabled_state or enabled_state not in _ENABLED_STATES:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        active_value = self._property(unit, "ActiveState")
        if active_value not in {"active", "inactive", "failed"}:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        status_raw = self._property(unit, "ExecMainStatus")
        try:
            status = int(status_raw) if status_raw else None
        except ValueError:
            _fail("upstream_sync_successor_systemd_observation_invalid")
        return UnitState(
            unit=unit,
            loaded=True,
            fragment_path=fragment,
            fragment_sha256=activation._sha256(raw),  # noqa: SLF001
            enabled_state=enabled_state,
            active_state=active_value,
            assert_result=self._property(unit, "AssertResult"),
            result=self._property(unit, "Result"),
            exec_main_status=status,
        )

    @staticmethod
    def mutate(*arguments: str) -> None:
        try:
            activation._systemctl_mutate(*arguments)  # noqa: SLF001
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_systemd_mutation_failed"
            ) from exc


@dataclass(frozen=True)
class _GuardedHost:
    delegate: RebindHost
    assert_stable: Callable[[], None]

    def observe(self, unit: str, *, systemd_root: Path) -> UnitState:
        self.assert_stable()
        observed = self.delegate.observe(unit, systemd_root=systemd_root)
        self.assert_stable()
        return observed

    def mutate(self, *arguments: str) -> None:
        self.assert_stable()
        self.delegate.mutate(*arguments)
        self.assert_stable()


def _observe_all(host: RebindHost, *, systemd_root: Path) -> dict[str, UnitState]:
    return {name: host.observe(name, systemd_root=systemd_root) for name in UNIT_NAMES}


def _validate_loaded_digests(
    observed: Mapping[str, UnitState],
    *,
    expected_digests: Mapping[str, str],
    systemd_root: Path,
) -> None:
    if set(observed) != _UNIT_DIGEST_FIELDS:
        _fail("upstream_sync_successor_systemd_observation_invalid")
    for name in UNIT_NAMES:
        item = observed[name]
        if (
            item.unit != name
            or not item.loaded
            or item.fragment_path != str(systemd_root / name)
            or item.fragment_sha256 != expected_digests[name]
        ):
            _fail("upstream_sync_successor_systemd_digest_unconfirmed", name)


def _timer_state_map(
    observed: Mapping[str, UnitState],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in TIMER_NAMES:
        item = observed[name]
        if item.enabled_state not in {
            "enabled",
            "disabled",
        } or item.active_state not in {
            "active",
            "inactive",
        }:
            _fail("upstream_sync_successor_timer_prestate_unsupported", name)
        result[name] = {
            "enabled_state": item.enabled_state,
            "active_state": item.active_state,
        }
    return result


def _validate_fresh_predecessor_state(
    observed: Mapping[str, UnitState],
    *,
    authority: Mapping[str, Any],
    preflight: Mapping[str, Any],
    systemd_root: Path,
) -> None:
    _validate_loaded_digests(
        observed,
        expected_digests=authority["predecessor_unit_digests"],
        systemd_root=systemd_root,
    )
    if any(observed[name].active_state != "inactive" for name in SERVICE_NAMES):
        _fail("upstream_sync_successor_service_prestate_drifted")
    if _timer_state_map(observed) != preflight["timer_prestates"]:
        _fail("upstream_sync_successor_timer_prestate_drifted")


def _build_preflight_receipt(
    *,
    authority: Mapping[str, Any],
    package: activation.PackageContext,
    systemd_root: Path,
    root_owned: bool,
    host: RebindHost,
) -> dict[str, Any]:
    """Observe the complete fresh predecessor state without mutating it."""

    units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    _validate_predecessor_units(units, authority)
    _prove_missing_predecessor_refs(authority)
    observed = _observe_all(host, systemd_root=systemd_root)
    _validate_loaded_digests(
        observed,
        expected_digests=authority["predecessor_unit_digests"],
        systemd_root=systemd_root,
    )
    if any(observed[name].active_state != "inactive" for name in SERVICE_NAMES):
        _fail("upstream_sync_successor_service_busy")
    timer_prestates = _timer_state_map(observed)
    return _receipt({
        "schema": PREFLIGHT_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "target_package_manifest_sha256": package.manifest["manifest_sha256"],
        "predecessor_unit_digests": dict(authority["predecessor_unit_digests"]),
        "predecessor_missing_release_roots": list(
            authority["predecessor_missing_release_roots"]
        ),
        "target_revision": authority["target_revision"],
        "target_unit_digests": dict(authority["target_unit_digests"]),
        "timer_prestates": timer_prestates,
        "service_units_inactive": True,
        "missing_release_refs_proven": True,
        "unit_files_exact": True,
        "runtime_mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    })


def preflight(
    *,
    expected_authority_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    runtime_path: Path | None = None,
    systemd_root: Path = SYSTEMD_ROOT,
    root_owned: bool = True,
    release_trust_root: Path | None = None,
    host: RebindHost | None = None,
) -> dict[str, Any]:
    provisional = validate_authority(
        activation._read_canonical_json(  # noqa: SLF001
            authority_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        ),
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["target_revision"]) / RUNTIME_RELATIVE
    )
    authority, package = _load_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=selected_runtime,
        root_owned=root_owned,
        release_trust_root=(release_trust_root or rail.RELEASES_ROOT),
    )
    selected_host = host or _SystemdHost(root_owned=root_owned)
    return _build_preflight_receipt(
        authority=authority,
        package=package,
        systemd_root=systemd_root,
        root_owned=root_owned,
        host=selected_host,
    )


def _validate_preflight(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    expected_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "authority_sha256",
        "target_package_manifest_sha256",
        "predecessor_unit_digests",
        "predecessor_missing_release_roots",
        "target_revision",
        "target_unit_digests",
        "timer_prestates",
        "service_units_inactive",
        "missing_release_refs_proven",
        "unit_files_exact",
        "runtime_mutation_performed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    prestates = value.get("timer_prestates")
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PREFLIGHT_SCHEMA
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("target_package_manifest_sha256")
        != authority["target_package_manifest_sha256"]
        or value.get("predecessor_unit_digests")
        != authority["predecessor_unit_digests"]
        or value.get("predecessor_missing_release_roots")
        != authority["predecessor_missing_release_roots"]
        or value.get("target_revision") != authority["target_revision"]
        or value.get("target_unit_digests") != authority["target_unit_digests"]
        or not isinstance(prestates, Mapping)
        or set(prestates) != set(TIMER_NAMES)
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"enabled_state", "active_state"}
            or item["enabled_state"] not in {"enabled", "disabled"}
            or item["active_state"] not in {"active", "inactive"}
            for item in prestates.values()
        )
        or any(
            value.get(name) is not expected
            for name, expected in (
                ("service_units_inactive", True),
                ("missing_release_refs_proven", True),
                ("unit_files_exact", True),
                ("runtime_mutation_performed", False),
                ("secret_material_recorded", False),
                ("secret_digest_recorded", False),
            )
        )
        or value.get("receipt_sha256") != expected_sha256
        or activation._sha256(  # noqa: SLF001
            activation._canonical(  # noqa: SLF001
                {key: item for key, item in value.items() if key != "receipt_sha256"}
            )
        )
        != expected_sha256
    ):
        _fail("upstream_sync_successor_preflight_invalid")
    return copy.deepcopy(dict(value))


def _validate_stage0_bundle(
    *,
    request: Mapping[str, Any],
    bundle: release_stage0.VerifiedLaunchBundle,
    release_trust_root: Path,
) -> None:
    """Bind the complete predecessor-authorized Stage-C launch bundle."""

    try:
        bundle.assert_stable()
        publication = bundle.publication
        plan = publication["plan"]
        host_manifest = bundle.input_documents["host_artifact_manifest_sha256"]
        host_identity = bundle.input_internal_identities[
            "host_artifact_manifest_sha256"
        ]
        expected_release_root = release_trust_root / (
            f"hermes-agent-{request['target_revision'][:12]}"
        )
        if (
            publication.get("release_revision") != request["target_revision"]
            or plan.get("release_revision") != request["target_revision"]
            or publication.get("publication_sha256")
            != request["stage_c_release_update_publication_sha256"]
            or plan.get("host_artifact_manifest_sha256")
            != request["stage_c_host_artifact_manifest_sha256"]
            or host_manifest.get("manifest_sha256")
            != request["stage_c_host_artifact_manifest_sha256"]
            or host_identity != request["stage_c_host_artifact_manifest_sha256"]
            or bundle.predecessor_trust.get("activation_receipt_sha256")
            != request["predecessor_activation_receipt_sha256"]
            or bundle.predecessor_trust.get("release_revision")
            != request["predecessor_revision"]
            or publication.get("predecessor_revision")
            != request["predecessor_revision"]
            or plan.get("predecessor_revision") != request["predecessor_revision"]
            or plan.get("predecessor_activation_receipt_sha256")
            != request["predecessor_activation_receipt_sha256"]
            or plan.get("source_tree_oid") != request["source_tree_oid"]
            or plan.get("builder_terminal_receipt_sha256")
            != request["stage_c_builder_terminal_receipt_sha256"]
            or bundle.builder_receipt.get("source_tree_oid")
            != request["source_tree_oid"]
            or bundle.builder_receipt.get("receipt_sha256")
            != request["stage_c_builder_terminal_receipt_sha256"]
            or bundle.release_root != expected_release_root
            or bundle.builder_manifest.get("release_revision")
            != request["target_revision"]
            or bundle.builder_receipt.get("release_revision")
            != request["target_revision"]
        ):
            _fail("upstream_sync_successor_stage0_binding_invalid")
    except UpstreamSyncRailSuccessorRebindError:
        raise
    except (
        KeyError,
        TypeError,
        release_stage0.ProductionReleaseUpdateStage0Error,
    ) as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage0_binding_invalid"
        ) from exc


def _stage_pending_path(path: Path, digest: str) -> Path:
    if _SHA256.fullmatch(digest or "") is None:
        _fail("upstream_sync_successor_stage_identity_invalid")
    return path.with_name(f".{path.name}.{digest}.stage")


def _read_staged_exact(
    path: Path,
    *,
    raw: bytes,
    root_owned: bool,
) -> release_builder.FileIdentity:
    try:
        observed, metadata = activation._read_regular(  # noqa: SLF001
            path,
            maximum=activation.MAX_JSON_BYTES,
            modes=frozenset({0o444}),
            root_owned=root_owned,
        )
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_invalid"
        ) from exc
    if observed != raw:
        _fail("upstream_sync_successor_stage_drifted")
    return release_builder.FileIdentity.from_stat(metadata)


def _read_pending_transaction_inode(
    *,
    target: Path,
    pending: Path,
    raw: bytes,
    root_owned: bool,
) -> None:
    """Verify the sole two-link staging inode through stable open identities."""

    descriptor: int | None = None
    try:
        target_before = target.lstat()
        pending_before = pending.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(pending, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        target_reached = target.lstat()
        pending_reached = pending.lstat()
    except OSError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_hardlink_invalid"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    identities = {
        activation._identity(item)  # noqa: SLF001
        for item in (
            target_before,
            pending_before,
            opened,
            after,
            target_reached,
            pending_reached,
        )
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 2
        or not 0 < opened.st_size <= activation.MAX_JSON_BYTES
        or stat.S_IMODE(opened.st_mode) != 0o444
        or root_owned
        and (opened.st_uid != 0 or opened.st_gid != 0)
        or b"".join(chunks) != raw
    ):
        _fail("upstream_sync_successor_stage_hardlink_invalid")


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_invalid"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stage_create_only(
    value: Mapping[str, Any],
    *,
    path: Path,
    root_owned: bool,
) -> release_builder.FileIdentity:
    """Publish exact immutable bytes without an overwrite-capable edge.

    The deterministic pending inode makes crashes before or after ``link(2)``
    recoverable.  A foreign target, foreign pending file, symlink, or unrelated
    hard link is never removed or replaced.
    """

    raw = activation._canonical(value) + b"\n"  # noqa: SLF001
    digest = activation._sha256(raw)  # noqa: SLF001
    pending = _stage_pending_path(path, digest)
    try:
        activation._validate_trusted_parent_chain(  # noqa: SLF001
            path,
            boundary=path.parent,
            root_owned=root_owned,
        )
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_parent_invalid"
        ) from exc

    target_exists = os.path.lexists(path)
    pending_exists = os.path.lexists(pending)
    if target_exists:
        if pending_exists:
            _read_pending_transaction_inode(
                target=path,
                pending=pending,
                raw=raw,
                root_owned=root_owned,
            )
            try:
                pending.unlink()
            except OSError as exc:
                raise UpstreamSyncRailSuccessorRebindError(
                    "upstream_sync_successor_stage_invalid"
                ) from exc
            _fsync_directory(path.parent)
        return _read_staged_exact(path, raw=raw, root_owned=root_owned)

    if pending_exists:
        _read_staged_exact(pending, raw=raw, root_owned=root_owned)
    else:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(pending, flags, 0o444)
            os.fchmod(descriptor, 0o444)
            if root_owned and activation._effective_uid() == 0:  # noqa: SLF001
                os.fchown(descriptor, 0, 0)
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("upstream_sync_successor_stage_invalid")
                view = view[written:]
            os.fsync(descriptor)
        except UpstreamSyncRailSuccessorRebindError:
            raise
        except OSError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_stage_invalid"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        _read_staged_exact(pending, raw=raw, root_owned=root_owned)
    try:
        os.link(pending, path, follow_symlinks=False)
    except FileExistsError:
        _fail("upstream_sync_successor_stage_raced")
    except OSError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_invalid"
        ) from exc
    try:
        pending.unlink()
    except OSError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_invalid"
        ) from exc
    _fsync_directory(path.parent)
    return _read_staged_exact(path, raw=raw, root_owned=root_owned)


def _remove_just_created_stage(
    value: Mapping[str, Any],
    *,
    path: Path,
    identity: release_builder.FileIdentity,
    root_owned: bool,
) -> None:
    """Quarantine, rebind, then remove only this owner's exact inode."""

    raw = activation._canonical(value) + b"\n"  # noqa: SLF001
    quarantine_root: Path | None = None
    quarantined: Path | None = None
    try:
        observed, metadata = activation._read_regular(  # noqa: SLF001
            path,
            maximum=activation.MAX_JSON_BYTES,
            modes=frozenset({0o444}),
            root_owned=root_owned,
        )
        if (
            release_builder.FileIdentity.from_stat(metadata) != identity
            or observed != raw
        ):
            _fail("upstream_sync_successor_stage_cleanup_failed")
        quarantine_root = Path(
            tempfile.mkdtemp(
                prefix=f".{path.name}.cleanup-quarantine.",
                dir=path.parent,
            )
        )
        os.chmod(quarantine_root, 0o700)
        if root_owned and activation._effective_uid() == 0:  # noqa: SLF001
            os.chown(quarantine_root, 0, 0)
        quarantine_metadata = quarantine_root.lstat()
        if (
            not stat.S_ISDIR(quarantine_metadata.st_mode)
            or stat.S_IMODE(quarantine_metadata.st_mode) != 0o700
            or root_owned
            and (quarantine_metadata.st_uid != 0 or quarantine_metadata.st_gid != 0)
        ):
            _fail("upstream_sync_successor_stage_cleanup_failed")
        quarantined = quarantine_root / "candidate"
        _rename_noreplace(path, quarantined)
        _fsync_directory(path.parent)
        _fsync_directory(quarantine_root)
        try:
            moved_raw, moved_metadata = activation._read_regular(  # noqa: SLF001
                quarantined,
                maximum=activation.MAX_JSON_BYTES,
                modes=frozenset({0o444}),
                root_owned=root_owned,
            )
            moved_identity = release_builder.FileIdentity.from_stat(moved_metadata)
            moved_is_exact = (
                moved_identity.device == identity.device
                and moved_identity.inode == identity.inode
                and moved_identity.mode == identity.mode
                and moved_identity.uid == identity.uid
                and moved_identity.gid == identity.gid
                and moved_identity.links == identity.links
                and moved_identity.size == identity.size
                and moved_identity.modified_ns == identity.modified_ns
                and moved_raw == raw
            )
        except activation.UpstreamSyncRailCutoverError:
            moved_is_exact = False
        if not moved_is_exact:
            _rename_noreplace(quarantined, path)
            _fsync_directory(path.parent)
            _fsync_directory(quarantine_root)
            quarantine_root.rmdir()
            _fsync_directory(path.parent)
            _fail("upstream_sync_successor_stage_cleanup_failed")
        quarantined.unlink()
        _fsync_directory(quarantine_root)
        quarantine_root.rmdir()
        _fsync_directory(path.parent)
    except UpstreamSyncRailSuccessorRebindError:
        raise
    except (OSError, activation.UpstreamSyncRailCutoverError) as exc:
        if (
            quarantine_root is not None
            and quarantined is not None
            and not os.path.lexists(quarantined)
        ):
            try:
                quarantine_root.rmdir()
                _fsync_directory(path.parent)
            except OSError:
                pass
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage_cleanup_failed"
        ) from exc


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically move one pathname only when the destination is absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    target_raw = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOSYS,
                "atomic no-replace rename is unavailable",
            ) from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_raw, target_raw, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOSYS,
                "atomic no-replace rename is unavailable",
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_raw, -100, target_raw, 1)  # RENAME_NOREPLACE
    else:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace rename is unavailable",
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(source))


def _stage_create_only_guarded(
    value: Mapping[str, Any],
    *,
    path: Path,
    root_owned: bool,
    stability_guard: Callable[[], None],
) -> release_builder.FileIdentity:
    """Create one fixed artifact with immediate Stage-0 pre/post guards."""

    stability_guard()
    if os.path.lexists(path):
        _fail("upstream_sync_successor_stage_raced")
    identity = _stage_create_only(value, path=path, root_owned=root_owned)
    try:
        stability_guard()
    except release_stage0.ProductionReleaseUpdateStage0Error:
        _remove_just_created_stage(
            value,
            path=path,
            identity=identity,
            root_owned=root_owned,
        )
        raise
    return identity


def _transaction_path(
    authority_sha256: str,
    name: str,
    *,
    evidence_root: Path,
) -> Path:
    if _SHA256.fullmatch(authority_sha256 or "") is None or name not in {
        "started.json",
        "archive.json",
        "terminal.json",
        "rollback-intent.json",
        "rollback.json",
    }:
        _fail("upstream_sync_successor_evidence_identity_invalid")
    return evidence_root / authority_sha256 / name


def _archive_root(authority_sha256: str, *, evidence_root: Path) -> Path:
    if _SHA256.fullmatch(authority_sha256 or "") is None:
        _fail("upstream_sync_successor_evidence_identity_invalid")
    return evidence_root / authority_sha256 / "archive"


def _publish(
    value: Mapping[str, Any],
    *,
    path: Path,
    root_owned: bool,
) -> dict[str, Any]:
    try:
        return activation._publish_evidence(  # noqa: SLF001
            value,
            path=path,
            root_owned=root_owned,
        )
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_evidence_invalid"
        ) from exc


def _started_receipt(
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
) -> dict[str, Any]:
    return _receipt({
        "schema": STARTED_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_sha256,
        "predecessor_unit_digests": dict(authority["predecessor_unit_digests"]),
        "target_unit_digests": dict(authority["target_unit_digests"]),
        "timer_units": list(TIMER_NAMES),
        "service_units": list(SERVICE_NAMES),
        "stop_scope": list(TIMER_NAMES),
        "archive_before_replace": True,
        "forward_recovery_only_after_start": True,
        "runtime_mutation_performed": False,
        "secret_material_recorded": False,
    })


def _validate_started(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
) -> dict[str, Any]:
    expected = _started_receipt(
        authority=authority,
        preflight_sha256=preflight_sha256,
    )
    if value != expected:
        _fail("upstream_sync_successor_started_receipt_invalid")
    return copy.deepcopy(dict(value))


def _rollback_intent_receipt(
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    cause: str,
) -> dict[str, Any]:
    if not isinstance(cause, str) or re.fullmatch(r"[a-z0-9_]{1,120}", cause) is None:
        _fail("upstream_sync_successor_rollback_intent_invalid")
    return _receipt({
        "schema": ROLLBACK_INTENT_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_sha256,
        "cause": cause,
        "stop_scope": list(TIMER_NAMES),
        "rollback_must_resume_before_forward_recovery": True,
        "forward_mutation_may_have_occurred": True,
        "rollback_mutation_performed": False,
        "secret_material_recorded": False,
    })


def _ensure_rollback_intent(
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    evidence_root: Path,
    root_owned: bool,
    cause: str,
) -> dict[str, Any]:
    expected = _rollback_intent_receipt(
        authority=authority,
        preflight_sha256=preflight_sha256,
        cause=cause,
    )
    path = _transaction_path(
        authority["authority_sha256"],
        "rollback-intent.json",
        evidence_root=evidence_root,
    )
    if path.exists() or path.is_symlink():
        observed = activation._read_canonical_json(  # noqa: SLF001
            path,
            root_owned=root_owned,
            modes=frozenset({0o600}),
        )
        if observed != expected:
            _fail("upstream_sync_successor_rollback_intent_invalid")
        return copy.deepcopy(dict(observed))
    return _publish(expected, path=path, root_owned=root_owned)


def _archive_units(
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    stability_guard: Callable[[], None],
) -> dict[str, Any]:
    stability_guard()
    directory = _archive_root(
        authority["authority_sha256"],
        evidence_root=evidence_root,
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stability_guard()
    units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    digests = _digest_bytes(units)
    for name in UNIT_NAMES:
        archive_path = directory / name
        if archive_path.exists() or archive_path.is_symlink():
            try:
                observed, _metadata = activation._read_regular(  # noqa: SLF001
                    archive_path,
                    maximum=2 * 1024 * 1024,
                    modes=frozenset({0o600}),
                    root_owned=root_owned,
                )
            except activation.UpstreamSyncRailCutoverError as exc:
                raise UpstreamSyncRailSuccessorRebindError(
                    "upstream_sync_successor_archive_invalid",
                    name,
                ) from exc
            if (
                activation._sha256(observed)  # noqa: SLF001
                != authority["predecessor_unit_digests"][name]
            ):
                _fail("upstream_sync_successor_archive_invalid", name)
            continue
        if digests[name] != authority["predecessor_unit_digests"][name]:
            _fail("upstream_sync_successor_archive_source_invalid", name)
        stability_guard()
        activation._atomic_write(  # noqa: SLF001
            archive_path,
            units[name],
            mode=0o600,
            uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
            gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
        )
        stability_guard()
    receipt = _receipt({
        "schema": ARCHIVE_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_sha256,
        "archived_unit_digests": dict(authority["predecessor_unit_digests"]),
        "archived_unit_names": list(UNIT_NAMES),
        "archive_complete_before_replace": True,
        "secret_material_recorded": False,
    })
    stability_guard()
    published = _publish(
        receipt,
        path=_transaction_path(
            authority["authority_sha256"],
            "archive.json",
            evidence_root=evidence_root,
        ),
        root_owned=root_owned,
    )
    stability_guard()
    return published


def _validate_archive(
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    evidence_root: Path,
    root_owned: bool,
) -> dict[str, bytes]:
    receipt_path = _transaction_path(
        authority["authority_sha256"],
        "archive.json",
        evidence_root=evidence_root,
    )
    expected = _receipt({
        "schema": ARCHIVE_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_sha256,
        "archived_unit_digests": dict(authority["predecessor_unit_digests"]),
        "archived_unit_names": list(UNIT_NAMES),
        "archive_complete_before_replace": True,
        "secret_material_recorded": False,
    })
    observed_receipt = activation._read_canonical_json(  # noqa: SLF001
        receipt_path,
        root_owned=root_owned,
        modes=frozenset({0o600}),
    )
    if observed_receipt != expected:
        _fail("upstream_sync_successor_archive_receipt_invalid")
    directory = _archive_root(
        authority["authority_sha256"],
        evidence_root=evidence_root,
    )
    archived: dict[str, bytes] = {}
    for name in UNIT_NAMES:
        try:
            raw, _metadata = activation._read_regular(  # noqa: SLF001
                directory / name,
                maximum=2 * 1024 * 1024,
                modes=frozenset({0o600}),
                root_owned=root_owned,
            )
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_archive_invalid",
                name,
            ) from exc
        if (
            activation._sha256(raw)  # noqa: SLF001
            != authority["predecessor_unit_digests"][name]
        ):
            _fail("upstream_sync_successor_archive_invalid", name)
        archived[name] = raw
    _validate_predecessor_units(archived, authority)
    return archived


def _install_target_units(
    *,
    package: activation.PackageContext,
    authority: Mapping[str, Any],
    systemd_root: Path,
    root_owned: bool,
    progress_hook: Callable[[str, str | None], None] | None,
    stability_guard: Callable[[], None],
) -> bool:
    stability_guard()
    units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    digests = _digest_bytes(units)
    changed_units = tuple(
        name
        for name in UNIT_NAMES
        if authority["predecessor_unit_digests"][name]
        != authority["target_unit_digests"][name]
    )
    recovered = any(
        digests[name] == authority["target_unit_digests"][name]
        for name in changed_units
    )
    for name in UNIT_NAMES:
        if digests[name] not in {
            authority["predecessor_unit_digests"][name],
            authority["target_unit_digests"][name],
        }:
            _fail("upstream_sync_successor_foreign_unit_drift", name)
        if digests[name] == authority["target_unit_digests"][name]:
            continue
        stability_guard()
        activation._atomic_write(  # noqa: SLF001
            systemd_root / name,
            package.artifacts[name],
            mode=0o644,
            uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
            gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
        )
        stability_guard()
        if progress_hook is not None:
            progress_hook("unit_replaced", name)
    final = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    if _digest_bytes(final) != authority["target_unit_digests"]:
        _fail("upstream_sync_successor_target_install_unconfirmed")
    return recovered


def _prove_target(
    *,
    host: RebindHost,
    authority: Mapping[str, Any],
    systemd_root: Path,
) -> dict[str, UnitState]:
    observed = _observe_all(host, systemd_root=systemd_root)
    _validate_loaded_digests(
        observed,
        expected_digests=authority["target_unit_digests"],
        systemd_root=systemd_root,
    )
    for name in UNIT_NAMES:
        if observed[name].assert_result != "yes":
            _fail("upstream_sync_successor_assert_unconfirmed", name)
    for name in TIMER_NAMES:
        if (
            observed[name].enabled_state != "enabled"
            or observed[name].active_state != "active"
        ):
            _fail("upstream_sync_successor_timer_unconfirmed", name)
    for name in SERVICE_NAMES:
        if (
            observed[name].active_state != "inactive"
            or observed[name].result != "success"
            or observed[name].exec_main_status != 0
        ):
            _fail("upstream_sync_successor_catch_up_unconfirmed", name)
    return observed


def _restore_predecessor(
    *,
    host: RebindHost,
    authority: Mapping[str, Any],
    preflight: Mapping[str, Any],
    preflight_sha256: str,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    cause: str,
    progress_hook: Callable[[str, str | None], None] | None,
    stability_guard: Callable[[], None],
) -> dict[str, Any]:
    stability_guard()
    rollback_intent = _ensure_rollback_intent(
        authority=authority,
        preflight_sha256=preflight_sha256,
        evidence_root=evidence_root,
        root_owned=root_owned,
        cause=cause,
    )
    stability_guard()
    host.mutate("stop", *TIMER_NAMES)
    quiescent = _observe_all(host, systemd_root=systemd_root)
    if any(quiescent[name].active_state != "inactive" for name in UNIT_NAMES):
        _fail("upstream_sync_successor_rollback_quiescence_unconfirmed")
    archive_path = _transaction_path(
        authority["authority_sha256"],
        "archive.json",
        evidence_root=evidence_root,
    )
    archive_used = archive_path.exists() or archive_path.is_symlink()
    if archive_used:
        archived = _validate_archive(
            authority=authority,
            preflight_sha256=preflight_sha256,
            evidence_root=evidence_root,
            root_owned=root_owned,
        )
        for name in UNIT_NAMES:
            stability_guard()
            activation._atomic_write(  # noqa: SLF001
                systemd_root / name,
                archived[name],
                mode=0o644,
                uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
                gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
            )
            stability_guard()
            if progress_hook is not None:
                progress_hook("rollback_unit_restored", name)
    else:
        # A failure may occur after the durable start receipt but before the
        # archive receipt.  In that phase no replacement is permitted yet;
        # only exact predecessor bytes are a valid rollback source.
        current = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
        _validate_predecessor_units(current, authority)
    host.mutate("daemon-reload")
    prestates = preflight["timer_prestates"]
    for name in TIMER_NAMES:
        if prestates[name]["enabled_state"] == "enabled":
            host.mutate("enable", name)
        else:
            host.mutate("disable", name)
    for name in TIMER_NAMES:
        if prestates[name]["active_state"] == "active":
            host.mutate("start", name)
    units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    if _digest_bytes(units) != authority["predecessor_unit_digests"]:
        _fail("upstream_sync_successor_rollback_unconfirmed")
    observed = _observe_all(host, systemd_root=systemd_root)
    _validate_loaded_digests(
        observed,
        expected_digests=authority["predecessor_unit_digests"],
        systemd_root=systemd_root,
    )
    for name in TIMER_NAMES:
        if (
            observed[name].enabled_state != prestates[name]["enabled_state"]
            or observed[name].active_state != prestates[name]["active_state"]
        ):
            _fail("upstream_sync_successor_rollback_unconfirmed")
    if any(observed[name].active_state != "inactive" for name in SERVICE_NAMES):
        _fail("upstream_sync_successor_rollback_unconfirmed")
    rollback = _receipt({
        "schema": ROLLBACK_SCHEMA,
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_sha256,
        "rollback_intent_receipt_sha256": rollback_intent["receipt_sha256"],
        "restored_unit_digests": dict(authority["predecessor_unit_digests"]),
        "restored_timer_prestates": copy.deepcopy(dict(prestates)),
        "stopped_units": list(TIMER_NAMES),
        "archive_used": archive_used,
        "cause": cause,
        "rollback_complete": True,
        "target_active": False,
        "secret_material_recorded": False,
    })
    stability_guard()
    published = _publish(
        rollback,
        path=_transaction_path(
            authority["authority_sha256"],
            "rollback.json",
            evidence_root=evidence_root,
        ),
        root_owned=root_owned,
    )
    stability_guard()
    return published


def _rebind(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    release_trust_root: Path,
    host: RebindHost,
    require_root: bool,
    activation_lock_factory: Callable[[], Any] | None,
    progress_hook: Callable[[str, str | None], None] | None,
    stability_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Hold exact staged inputs for the complete mutation transaction."""

    authority, _package = _load_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=runtime_path,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
    )
    preflight_value = activation._read_canonical_json(  # noqa: SLF001
        preflight_path,
        root_owned=root_owned,
        modes=frozenset({0o444}),
    )
    checked_preflight = _validate_preflight(
        preflight_value,
        authority=authority,
        expected_sha256=expected_preflight_sha256,
    )
    with _hold_staged_inputs(
        authority_path=authority_path,
        authority=authority,
        preflight_path=preflight_path,
        preflight=checked_preflight,
        root_owned=root_owned,
    ) as held:

        def assert_rollback_authority() -> None:
            held.assert_stable()

        def assert_forward_stable() -> None:
            assert_rollback_authority()
            if stability_guard is not None:
                stability_guard()

        assert_rollback_authority()
        result = _rebind_unheld(
            expected_authority_sha256=expected_authority_sha256,
            expected_preflight_sha256=expected_preflight_sha256,
            staged_root=staged_root,
            authority_path=authority_path,
            preflight_path=preflight_path,
            runtime_path=runtime_path,
            systemd_root=systemd_root,
            evidence_root=evidence_root,
            root_owned=root_owned,
            release_trust_root=release_trust_root,
            host=_GuardedHost(host, assert_forward_stable),
            rollback_host=_GuardedHost(host, assert_rollback_authority),
            require_root=require_root,
            activation_lock_factory=activation_lock_factory,
            progress_hook=progress_hook,
            stability_guard=assert_forward_stable,
            rollback_stability_guard=assert_rollback_authority,
        )
        assert_forward_stable()
        return result


def _rebind_unheld(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    release_trust_root: Path,
    host: RebindHost,
    rollback_host: RebindHost,
    require_root: bool,
    activation_lock_factory: Callable[[], Any] | None,
    progress_hook: Callable[[str, str | None], None] | None,
    stability_guard: Callable[[], None],
    rollback_stability_guard: Callable[[], None],
) -> dict[str, Any]:
    if require_root and activation._effective_uid() != 0:  # noqa: SLF001
        _fail("upstream_sync_successor_root_required")
    authority, package = _load_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=runtime_path,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
    )
    preflight_value = activation._read_canonical_json(  # noqa: SLF001
        preflight_path,
        root_owned=root_owned,
        modes=frozenset({0o444}),
    )
    checked_preflight = _validate_preflight(
        preflight_value,
        authority=authority,
        expected_sha256=expected_preflight_sha256,
    )
    started_path = _transaction_path(
        authority["authority_sha256"],
        "started.json",
        evidence_root=evidence_root,
    )
    archive_path = _transaction_path(
        authority["authority_sha256"],
        "archive.json",
        evidence_root=evidence_root,
    )
    terminal_path = _transaction_path(
        authority["authority_sha256"],
        "terminal.json",
        evidence_root=evidence_root,
    )
    rollback_path = _transaction_path(
        authority["authority_sha256"],
        "rollback.json",
        evidence_root=evidence_root,
    )
    rollback_intent_path = _transaction_path(
        authority["authority_sha256"],
        "rollback-intent.json",
        evidence_root=evidence_root,
    )
    with authority_activation_lock(
        require_root=require_root,
        lock_factory=activation_lock_factory,
    ):
        rollback_stability_guard()
        if rollback_path.exists() or rollback_path.is_symlink():
            _fail("upstream_sync_successor_already_rolled_back")
        if terminal_path.exists() or terminal_path.is_symlink():
            if rollback_intent_path.exists() or rollback_intent_path.is_symlink():
                _fail("upstream_sync_successor_terminal_conflicts_with_rollback")
            stability_guard()
            return _verify(
                expected_authority_sha256=expected_authority_sha256,
                expected_preflight_sha256=expected_preflight_sha256,
                staged_root=staged_root,
                authority_path=authority_path,
                preflight_path=preflight_path,
                runtime_path=runtime_path,
                systemd_root=systemd_root,
                evidence_root=evidence_root,
                root_owned=root_owned,
                release_trust_root=release_trust_root,
                host=host,
                stability_guard=stability_guard,
            )
        started_present = started_path.exists() or started_path.is_symlink()
        if not started_present and (archive_path.exists() or archive_path.is_symlink()):
            _fail("upstream_sync_successor_started_receipt_missing")
        if not started_present and (
            rollback_intent_path.exists() or rollback_intent_path.is_symlink()
        ):
            _fail("upstream_sync_successor_started_receipt_missing")
        if started_present:
            _validate_started(
                activation._read_canonical_json(  # noqa: SLF001
                    started_path,
                    root_owned=root_owned,
                    modes=frozenset({0o600}),
                ),
                authority=authority,
                preflight_sha256=expected_preflight_sha256,
            )
        else:
            stability_guard()
            units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
            _validate_predecessor_units(units, authority)
            _prove_missing_predecessor_refs(authority)
            observed = _observe_all(host, systemd_root=systemd_root)
            _validate_fresh_predecessor_state(
                observed,
                authority=authority,
                preflight=checked_preflight,
                systemd_root=systemd_root,
            )
            stability_guard()
            _publish(
                _started_receipt(
                    authority=authority,
                    preflight_sha256=expected_preflight_sha256,
                ),
                path=started_path,
                root_owned=root_owned,
            )
            stability_guard()
        if rollback_intent_path.exists() or rollback_intent_path.is_symlink():
            rollback_stability_guard()
            intent = activation._read_canonical_json(  # noqa: SLF001
                rollback_intent_path,
                root_owned=root_owned,
                modes=frozenset({0o600}),
            )
            cause = intent.get("cause") if isinstance(intent, Mapping) else None
            if not isinstance(cause, str):
                _fail("upstream_sync_successor_rollback_intent_invalid")
            rollback_stability_guard()
            _ensure_rollback_intent(
                authority=authority,
                preflight_sha256=expected_preflight_sha256,
                evidence_root=evidence_root,
                root_owned=root_owned,
                cause=cause,
            )
            rollback_stability_guard()
            try:
                _restore_predecessor(
                    host=rollback_host,
                    authority=authority,
                    preflight=checked_preflight,
                    preflight_sha256=expected_preflight_sha256,
                    systemd_root=systemd_root,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                    progress_hook=progress_hook,
                    stability_guard=rollback_stability_guard,
                )
            except Exception as rollback_exc:
                raise UpstreamSyncRailSuccessorRebindError(
                    "upstream_sync_successor_rollback_failed"
                ) from rollback_exc
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_failed_rolled_back",
                cause,
            )
        try:
            stability_guard()
            current = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
            current_digests = _digest_bytes(current)
            if any(
                current_digests[name]
                not in {
                    authority["predecessor_unit_digests"][name],
                    authority["target_unit_digests"][name],
                }
                for name in UNIT_NAMES
            ):
                _fail("upstream_sync_successor_foreign_unit_drift")
            with _hold_unit_set(
                systemd_root=systemd_root,
                expected_digests=current_digests,
                root_owned=root_owned,
            ) as held_current:

                def assert_current_stable() -> None:
                    stability_guard()
                    held_current.assert_stable()

                current_host = _GuardedHost(host, assert_current_stable)
                current_host.mutate("stop", *TIMER_NAMES)
                stopped = _observe_all(
                    current_host,
                    systemd_root=systemd_root,
                )
                if any(stopped[name].active_state != "inactive" for name in UNIT_NAMES):
                    _fail("upstream_sync_successor_quiescence_unconfirmed")
                if archive_path.exists() or archive_path.is_symlink():
                    _validate_archive(
                        authority=authority,
                        preflight_sha256=expected_preflight_sha256,
                        evidence_root=evidence_root,
                        root_owned=root_owned,
                    )
                    forward_recovery = True
                else:
                    _archive_units(
                        authority=authority,
                        preflight_sha256=expected_preflight_sha256,
                        systemd_root=systemd_root,
                        evidence_root=evidence_root,
                        root_owned=root_owned,
                        stability_guard=assert_current_stable,
                    )
                    forward_recovery = False
                assert_current_stable()
            forward_recovery = (
                _install_target_units(
                    package=package,
                    authority=authority,
                    systemd_root=systemd_root,
                    root_owned=root_owned,
                    progress_hook=progress_hook,
                    stability_guard=stability_guard,
                )
                or forward_recovery
            )
            with _hold_unit_set(
                systemd_root=systemd_root,
                expected_digests=authority["target_unit_digests"],
                root_owned=root_owned,
            ) as held_target:

                def assert_target_stable() -> None:
                    stability_guard()
                    held_target.assert_stable()

                target_host = _GuardedHost(host, assert_target_stable)
                target_host.mutate("daemon-reload")
                reloaded = _observe_all(
                    target_host,
                    systemd_root=systemd_root,
                )
                _validate_loaded_digests(
                    reloaded,
                    expected_digests=authority["target_unit_digests"],
                    systemd_root=systemd_root,
                )
                if any(
                    reloaded[name].active_state != "inactive" for name in UNIT_NAMES
                ):
                    _fail("upstream_sync_successor_reload_quiescence_invalid")
                target_host.mutate("enable", *TIMER_NAMES)
                # Both oneshot services run once before their timers are armed.
                target_host.mutate("start", *SERVICE_NAMES)
                target_host.mutate("start", *TIMER_NAMES)
                observed = _prove_target(
                    host=target_host,
                    authority=authority,
                    systemd_root=systemd_root,
                )
                assert_target_stable()
            terminal = _receipt({
                "schema": TERMINAL_SCHEMA,
                "authority_sha256": authority["authority_sha256"],
                "preflight_receipt_sha256": expected_preflight_sha256,
                "archive_receipt_sha256": activation._read_canonical_json(  # noqa: SLF001
                    archive_path,
                    root_owned=root_owned,
                    modes=frozenset({0o600}),
                )["receipt_sha256"],
                "target_revision": authority["target_revision"],
                "target_unit_digests": dict(authority["target_unit_digests"]),
                "timer_units": list(TIMER_NAMES),
                "service_units": list(SERVICE_NAMES),
                "timers_enabled": True,
                "timers_active": True,
                "assert_result": {
                    name: observed[name].assert_result for name in UNIT_NAMES
                },
                "catch_up_result": {
                    name: {
                        "result": observed[name].result,
                        "exec_main_status": observed[name].exec_main_status,
                    }
                    for name in SERVICE_NAMES
                },
                "forward_recovery_performed": forward_recovery,
                "rollback_performed": False,
                "stopped_units": list(TIMER_NAMES),
                "auto_merge_or_deploy_enabled": False,
                "secret_material_recorded": False,
            })
            stability_guard()
            published = _publish(
                terminal,
                path=terminal_path,
                root_owned=root_owned,
            )
            stability_guard()
            return published
        except (
            UpstreamSyncRailSuccessorRebindError,
            activation.UpstreamSyncRailCutoverError,
            release_stage0.ProductionReleaseUpdateStage0Error,
            OSError,
        ) as exc:
            if isinstance(exc, UpstreamSyncRailSuccessorRebindError):
                cause = exc.code
            elif isinstance(
                exc,
                release_stage0.ProductionReleaseUpdateStage0Error,
            ):
                cause = "upstream_sync_successor_stage0_drifted"
            else:
                cause = "upstream_sync_successor_runtime_mutation_failed"
            try:
                rollback_stability_guard()
                _ensure_rollback_intent(
                    authority=authority,
                    preflight_sha256=expected_preflight_sha256,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                )
                rollback_stability_guard()
                _restore_predecessor(
                    host=rollback_host,
                    authority=authority,
                    preflight=checked_preflight,
                    preflight_sha256=expected_preflight_sha256,
                    systemd_root=systemd_root,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                    progress_hook=progress_hook,
                    stability_guard=rollback_stability_guard,
                )
            except Exception as rollback_exc:
                raise UpstreamSyncRailSuccessorRebindError(
                    "upstream_sync_successor_rollback_failed"
                ) from rollback_exc
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_failed_rolled_back",
                cause,
            ) from exc


def rebind(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    runtime_path: Path | None = None,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    release_trust_root: Path | None = None,
    require_root: bool = True,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    provisional = validate_authority(
        activation._read_canonical_json(  # noqa: SLF001
            authority_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        ),
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["target_revision"]) / RUNTIME_RELATIVE
    )
    return _rebind(
        expected_authority_sha256=expected_authority_sha256,
        expected_preflight_sha256=expected_preflight_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        preflight_path=preflight_path,
        runtime_path=selected_runtime,
        systemd_root=systemd_root,
        evidence_root=evidence_root,
        root_owned=root_owned,
        release_trust_root=release_trust_root or rail.RELEASES_ROOT,
        host=_SystemdHost(root_owned=root_owned),
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
        progress_hook=None,
    )


def _authority_matches_owner_request(
    authority: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    if (
        authority.get("predecessor_revision") != request["predecessor_revision"]
        or authority.get("target_revision") != request["target_revision"]
        or authority.get("target_package_manifest_sha256")
        != request["target_package_manifest_sha256"]
        or authority.get("stage_c_host_artifact_manifest_sha256")
        != request["stage_c_host_artifact_manifest_sha256"]
        or authority.get("stage_c_release_update_publication_sha256")
        != request["stage_c_release_update_publication_sha256"]
        or authority.get("rebind_runtime_sha256") != request["rebind_runtime_sha256"]
    ):
        _fail("upstream_sync_successor_owner_authority_binding_invalid")


def _owner_result(
    *,
    request: Mapping[str, Any],
    authority: Mapping[str, Any],
    preflight_value: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    unsigned = {
        "schema": OWNER_RESULT_SCHEMA,
        "operation": OPERATION,
        "request_sha256": request["request_sha256"],
        "target_revision": request["target_revision"],
        **{name: request[name] for name in _OWNER_RUNTIME_IDENTITY_FIELDS},
        "authority_sha256": authority["authority_sha256"],
        "preflight_receipt_sha256": preflight_value["receipt_sha256"],
        "terminal_receipt_sha256": terminal["receipt_sha256"],
        "authority_staged_at_fixed_path": True,
        "preflight_staged_at_fixed_path": True,
        "terminal_verified": True,
        "caller_selected_paths_allowed": False,
        "caller_selected_commands_allowed": False,
        "caller_selected_targets_allowed": False,
        "manual_json_allowed": False,
        "semantic_decisions_allowed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return _receipt(unsigned)


def validate_owner_result(
    value: Any,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    expected_request = validate_owner_request(request)
    fields = {
        "schema",
        "operation",
        "request_sha256",
        "target_revision",
        *_OWNER_RUNTIME_IDENTITY_FIELDS,
        "authority_sha256",
        "preflight_receipt_sha256",
        "terminal_receipt_sha256",
        "authority_staged_at_fixed_path",
        "preflight_staged_at_fixed_path",
        "terminal_verified",
        "caller_selected_paths_allowed",
        "caller_selected_commands_allowed",
        "caller_selected_targets_allowed",
        "manual_json_allowed",
        "semantic_decisions_allowed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("upstream_sync_successor_owner_result_invalid")
    result = copy.deepcopy(dict(value))
    if (
        result.get("schema") != OWNER_RESULT_SCHEMA
        or result.get("operation") != OPERATION
        or result.get("request_sha256") != expected_request["request_sha256"]
        or result.get("target_revision") != expected_request["target_revision"]
        or any(
            result.get(name) != expected_request[name]
            for name in _OWNER_RUNTIME_IDENTITY_FIELDS
        )
        or any(
            _SHA256.fullmatch(str(result.get(name, ""))) is None
            for name in (
                "authority_sha256",
                "preflight_receipt_sha256",
                "terminal_receipt_sha256",
                "receipt_sha256",
            )
        )
        or any(
            result.get(name) is not True
            for name in (
                "authority_staged_at_fixed_path",
                "preflight_staged_at_fixed_path",
                "terminal_verified",
            )
        )
        or any(
            result.get(name) is not False
            for name in (
                "caller_selected_paths_allowed",
                "caller_selected_commands_allowed",
                "caller_selected_targets_allowed",
                "manual_json_allowed",
                "semantic_decisions_allowed",
                "secret_material_recorded",
                "secret_digest_recorded",
            )
        )
        or result["receipt_sha256"]
        != activation._sha256(  # noqa: SLF001
            activation._canonical(  # noqa: SLF001
                {
                    name: item
                    for name, item in result.items()
                    if name != "receipt_sha256"
                }
            )
        )
    ):
        _fail("upstream_sync_successor_owner_result_invalid")
    return result


def _owner_apply(
    request_value: Mapping[str, Any],
    *,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    release_trust_root: Path,
    root_owned: bool,
    host: RebindHost,
    require_root: bool,
    activation_lock_factory: Callable[[], Any] | None,
    stage0_verifier: Stage0Verifier = release_stage0.verify_stage0,
) -> dict[str, Any]:
    """Apply only while canonical Stage 0 keeps every trust input open."""

    request = validate_owner_request(request_value)
    try:
        verified = stage0_verifier(
            expected_predecessor_activation_receipt_sha256=request[
                "predecessor_activation_receipt_sha256"
            ]
        )
        with verified as bundle:
            _validate_stage0_bundle(
                request=request,
                bundle=bundle,
                release_trust_root=release_trust_root,
            )
            result = _owner_apply_verified(
                request,
                staged_root=staged_root,
                authority_path=authority_path,
                preflight_path=preflight_path,
                runtime_path=runtime_path,
                systemd_root=systemd_root,
                evidence_root=evidence_root,
                release_trust_root=release_trust_root,
                root_owned=root_owned,
                host=host,
                require_root=require_root,
                activation_lock_factory=activation_lock_factory,
                stability_guard=bundle.assert_stable,
            )
            bundle.assert_stable()
            return result
    except UpstreamSyncRailSuccessorRebindError:
        raise
    except release_stage0.ProductionReleaseUpdateStage0Error as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_stage0_invalid"
        ) from exc


def _owner_apply_verified(
    request_value: Mapping[str, Any],
    *,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    release_trust_root: Path,
    root_owned: bool,
    host: RebindHost,
    require_root: bool,
    activation_lock_factory: Callable[[], Any] | None,
    stability_guard: Callable[[], None],
) -> dict[str, Any]:
    """Author, stage, rebind, and verify one exact successor transaction."""

    request = validate_owner_request(request_value)
    if require_root and activation._effective_uid() != 0:  # noqa: SLF001
        _fail("upstream_sync_successor_root_required")
    if (
        authority_path != staged_root / AUTHORITY_PATH.name
        or preflight_path != staged_root / PREFLIGHT_PATH.name
    ):
        _fail("upstream_sync_successor_owner_fixed_path_invalid")
    authority_exists = os.path.lexists(authority_path)
    preflight_exists = os.path.lexists(preflight_path)
    if preflight_exists and not authority_exists:
        _fail("upstream_sync_successor_owner_partial_stage_invalid")
    stability_guard()
    try:
        activation._validate_trusted_parent_chain(  # noqa: SLF001
            runtime_path,
            boundary=release_trust_root,
            root_owned=root_owned,
        )
        runtime_raw, _runtime_metadata = activation._read_regular(  # noqa: SLF001
            runtime_path,
            maximum=2 * 1024 * 1024,
            root_owned=root_owned,
        )
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_runtime_invalid"
        ) from exc
    if activation._sha256(runtime_raw) != request["rebind_runtime_sha256"]:  # noqa: SLF001
        _fail("upstream_sync_successor_runtime_invalid")
    stability_guard()
    package = activation._validate_package_context(  # noqa: SLF001
        staged_root=staged_root,
        release_revision=request["target_revision"],
        sender_revision=request["target_revision"],
        expected_manifest_sha256=request["target_package_manifest_sha256"],
        root_owned=root_owned,
        staged_trust_root=(activation.STAGED_TRUST_ROOT if root_owned else staged_root),
        release_trust_root=release_trust_root,
    )

    authority: dict[str, Any]
    preflight_value: dict[str, Any]
    if authority_exists:
        provisional = activation._read_canonical_json(  # noqa: SLF001
            authority_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        )
        authority_digest = str(provisional.get("authority_sha256", ""))
        authority = validate_authority(
            provisional,
            expected_sha256=authority_digest,
        )
        _authority_matches_owner_request(authority, request)
        _load_context(
            expected_authority_sha256=authority_digest,
            staged_root=staged_root,
            authority_path=authority_path,
            runtime_path=runtime_path,
            root_owned=root_owned,
            release_trust_root=release_trust_root,
        )
        if preflight_exists:
            provisional_preflight = activation._read_canonical_json(  # noqa: SLF001
                preflight_path,
                root_owned=root_owned,
                modes=frozenset({0o444}),
            )
            preflight_digest = str(provisional_preflight.get("receipt_sha256", ""))
            preflight_value = _validate_preflight(
                provisional_preflight,
                authority=authority,
                expected_sha256=preflight_digest,
            )
        else:
            predecessor_units = _unit_bytes(
                systemd_root=systemd_root,
                root_owned=root_owned,
            )
            rebuilt = build_authority(
                package=package,
                predecessor_revision=request["predecessor_revision"],
                predecessor_units=predecessor_units,
                stage_c_host_artifact_manifest_sha256=request[
                    "stage_c_host_artifact_manifest_sha256"
                ],
                stage_c_release_update_publication_sha256=request[
                    "stage_c_release_update_publication_sha256"
                ],
                rebind_runtime_sha256=request["rebind_runtime_sha256"],
            )
            if rebuilt != authority:
                _fail("upstream_sync_successor_owner_authority_drifted")
            preflight_value = _build_preflight_receipt(
                authority=authority,
                package=package,
                systemd_root=systemd_root,
                root_owned=root_owned,
                host=host,
            )
            _stage_create_only_guarded(
                preflight_value,
                path=preflight_path,
                root_owned=root_owned,
                stability_guard=stability_guard,
            )
    else:
        predecessor_units = _unit_bytes(
            systemd_root=systemd_root,
            root_owned=root_owned,
        )
        authority = build_authority(
            package=package,
            predecessor_revision=request["predecessor_revision"],
            predecessor_units=predecessor_units,
            stage_c_host_artifact_manifest_sha256=request[
                "stage_c_host_artifact_manifest_sha256"
            ],
            stage_c_release_update_publication_sha256=request[
                "stage_c_release_update_publication_sha256"
            ],
            rebind_runtime_sha256=request["rebind_runtime_sha256"],
        )
        preflight_value = _build_preflight_receipt(
            authority=authority,
            package=package,
            systemd_root=systemd_root,
            root_owned=root_owned,
            host=host,
        )
        # No staged or runtime mutation is permitted until the complete exact
        # predecessor preflight above has passed in memory.
        created: list[tuple[Mapping[str, Any], Path, release_builder.FileIdentity]] = []
        try:
            authority_identity = _stage_create_only_guarded(
                authority,
                path=authority_path,
                root_owned=root_owned,
                stability_guard=stability_guard,
            )
            created.append((authority, authority_path, authority_identity))
            preflight_identity = _stage_create_only_guarded(
                preflight_value,
                path=preflight_path,
                root_owned=root_owned,
                stability_guard=stability_guard,
            )
            created.append((preflight_value, preflight_path, preflight_identity))
        except release_stage0.ProductionReleaseUpdateStage0Error:
            for created_value, created_path, created_identity in reversed(created):
                _remove_just_created_stage(
                    created_value,
                    path=created_path,
                    identity=created_identity,
                    root_owned=root_owned,
                )
            raise

    terminal = _rebind(
        expected_authority_sha256=authority["authority_sha256"],
        expected_preflight_sha256=preflight_value["receipt_sha256"],
        staged_root=staged_root,
        authority_path=authority_path,
        preflight_path=preflight_path,
        runtime_path=runtime_path,
        systemd_root=systemd_root,
        evidence_root=evidence_root,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
        host=host,
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
        progress_hook=None,
        stability_guard=stability_guard,
    )
    verified = _verify(
        expected_authority_sha256=authority["authority_sha256"],
        expected_preflight_sha256=preflight_value["receipt_sha256"],
        staged_root=staged_root,
        authority_path=authority_path,
        preflight_path=preflight_path,
        runtime_path=runtime_path,
        systemd_root=systemd_root,
        evidence_root=evidence_root,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
        host=host,
        stability_guard=stability_guard,
    )
    if terminal != verified:
        _fail("upstream_sync_successor_owner_terminal_drifted")
    stability_guard()
    return _owner_result(
        request=request,
        authority=authority,
        preflight_value=preflight_value,
        terminal=verified,
    )


def owner_apply(request: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_owner_request(request)
    runtime_path = rail.release_root(checked["target_revision"]) / RUNTIME_RELATIVE
    return _owner_apply(
        checked,
        staged_root=STAGED_ROOT,
        authority_path=AUTHORITY_PATH,
        preflight_path=PREFLIGHT_PATH,
        runtime_path=runtime_path,
        systemd_root=SYSTEMD_ROOT,
        evidence_root=EVIDENCE_ROOT,
        release_trust_root=rail.RELEASES_ROOT,
        root_owned=True,
        host=_SystemdHost(root_owned=True),
        require_root=True,
        activation_lock_factory=None,
    )


def owner_apply_framed_stdin() -> dict[str, Any]:
    """Consume the sole fixed framed owner request from process stdin."""

    if sys.stdin.isatty():
        _fail("upstream_sync_successor_owner_cli_invalid")
    frame = sys.stdin.buffer.read(_OWNER_FRAME_MAX_BYTES + 9)
    request = decode_owner_request(frame)
    from scripts.canary import production_successor_rebind_owner_runtime

    production_successor_rebind_owner_runtime.validate_active_installation(
        revision=request["target_revision"],
        expected_publication_sha256=request["remote_owner_runtime_publication_sha256"],
        expected_manifest_sha256=request["remote_owner_runtime_manifest_sha256"],
        expected_attestation_sha256=request["remote_owner_runtime_attestation_sha256"],
        expected_tree_sha256=request["remote_owner_runtime_tree_sha256"],
        expected_interpreter_sha256=request["remote_owner_runtime_interpreter_sha256"],
        expected_staging_publication_sha256=request[
            "remote_owner_runtime_staging_publication_sha256"
        ],
        expected_source_tree_oid=request["source_tree_oid"],
        expected_stage_c_builder_terminal_receipt_sha256=request[
            "stage_c_builder_terminal_receipt_sha256"
        ],
        expected_owner_runtime_builder_receipt_sha256=request[
            "remote_owner_runtime_builder_receipt_sha256"
        ],
        expected_owner_runtime_wheel_sha256=request[
            "remote_owner_runtime_wheel_sha256"
        ],
        expected_staging_manifest_sha256=request[
            "remote_owner_runtime_staging_manifest_sha256"
        ],
        expected_staging_attestation_sha256=request[
            "remote_owner_runtime_staging_attestation_sha256"
        ],
        expected_staging_tree_sha256=request[
            "remote_owner_runtime_staging_tree_sha256"
        ],
        expected_staging_interpreter_sha256=request[
            "remote_owner_runtime_staging_interpreter_sha256"
        ],
        expected_staging_pyvenv_cfg_sha256=request[
            "remote_owner_runtime_staging_pyvenv_cfg_sha256"
        ],
    )
    return owner_apply(request)


class OwnerRebindTransport(Protocol):
    """One fixed IAP edge; callers cannot provide argv, paths, or targets."""

    def invoke_successor_rebind(
        self,
        *,
        target_revision: str,
        request_frame: bytes,
    ) -> bytes: ...


class _ProductionOwnerRebindTransport:
    def __init__(self, request_value: Mapping[str, Any]) -> None:
        from gateway import production_owner_runtime
        from scripts.canary import production_cutover_owner_launcher as owner

        request = validate_owner_request(request_value)
        target_revision = request["target_revision"]
        try:
            attestation = production_owner_runtime.require_active_owner_runtime(
                target_revision
            )
        except production_owner_runtime.ProductionOwnerRuntimeError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_owner_runtime_not_active"
            ) from exc
        if any(
            attestation[name] != request[request_name]
            for name, request_name in (
                (
                    "manifest_sha256",
                    "controller_owner_runtime_manifest_sha256",
                ),
                (
                    "attestation_sha256",
                    "controller_owner_runtime_attestation_sha256",
                ),
                ("tree_sha256", "controller_owner_runtime_tree_sha256"),
                (
                    "interpreter_sha256",
                    "controller_owner_runtime_interpreter_sha256",
                ),
            )
        ):
            _fail("upstream_sync_successor_owner_runtime_identity_invalid")
        identity, trusted, configuration = (
            owner.build_production_cutover_owner_identity(target_revision)
        )
        self._target_revision = target_revision
        self._request_sha256 = request["request_sha256"]
        self._runtime_identities = (
            request["foundation_wrapper_sha256"],
            request["remote_owner_runtime_manifest_sha256"],
            request["remote_owner_runtime_tree_sha256"],
            request["remote_owner_runtime_interpreter_sha256"],
            request["remote_owner_runtime_attestation_sha256"],
            request["preexec_verifier_sha256"],
        )
        self._identity = identity
        self._transport = owner.ProductionCutoverTransport(
            identity,
            gcloud_executable=trusted,
            gcloud_configuration=configuration,
        )

    def invoke_successor_rebind(
        self,
        *,
        target_revision: str,
        request_frame: bytes,
    ) -> bytes:
        if target_revision != self._target_revision:
            _fail("upstream_sync_successor_owner_target_changed")
        request = decode_owner_request(request_frame)
        if request["request_sha256"] != self._request_sha256:
            _fail("upstream_sync_successor_owner_request_changed")
        account = self._identity.account_for_read_only_preflight()
        completed = self._transport._run_remote_input(  # noqa: SLF001
            (
                str(FOUNDATION_V4_WRAPPER),
                target_revision,
                "successor-rebind-owner-apply",
                *self._runtime_identities,
            ),
            account=account,
            input_bytes=request_frame,
            maximum_input_bytes=_OWNER_FRAME_MAX_BYTES + 8,
            maximum_output_bytes=64 * 1024,
            timeout_seconds=2_400.0,
        )
        return completed.stdout


def owner_run(
    request_value: Mapping[str, Any],
    *,
    transport: OwnerRebindTransport | None = None,
) -> dict[str, Any]:
    request = validate_owner_request(request_value)
    selected = transport or _ProductionOwnerRebindTransport(request)
    try:
        raw = selected.invoke_successor_rebind(
            target_revision=request["target_revision"],
            request_frame=encode_owner_request(request),
        )
    except UpstreamSyncRailSuccessorRebindError:
        raise
    except Exception as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_owner_transport_failed"
        ) from exc
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or len(raw) > 64 * 1024:
        _fail("upstream_sync_successor_owner_transport_failed")
    try:
        value = activation._decode(raw[:-1])  # noqa: SLF001
    except activation.UpstreamSyncRailCutoverError as exc:
        raise UpstreamSyncRailSuccessorRebindError(
            "upstream_sync_successor_owner_transport_failed"
        ) from exc
    return validate_owner_result(value, request=request)


def _verify(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    release_trust_root: Path,
    host: RebindHost,
    stability_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    authority, _package = _load_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=runtime_path,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
    )
    preflight = _validate_preflight(
        activation._read_canonical_json(  # noqa: SLF001
            preflight_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        ),
        authority=authority,
        expected_sha256=expected_preflight_sha256,
    )
    with _hold_staged_inputs(
        authority_path=authority_path,
        authority=authority,
        preflight_path=preflight_path,
        preflight=preflight,
        root_owned=root_owned,
    ) as held:

        def assert_stable() -> None:
            held.assert_stable()
            if stability_guard is not None:
                stability_guard()

        assert_stable()
        result = _verify_unheld(
            expected_authority_sha256=expected_authority_sha256,
            expected_preflight_sha256=expected_preflight_sha256,
            staged_root=staged_root,
            authority_path=authority_path,
            preflight_path=preflight_path,
            runtime_path=runtime_path,
            systemd_root=systemd_root,
            evidence_root=evidence_root,
            root_owned=root_owned,
            release_trust_root=release_trust_root,
            host=_GuardedHost(host, assert_stable),
        )
        assert_stable()
        return result


def _verify_unheld(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path,
    authority_path: Path,
    preflight_path: Path,
    runtime_path: Path,
    systemd_root: Path,
    evidence_root: Path,
    root_owned: bool,
    release_trust_root: Path,
    host: RebindHost,
) -> dict[str, Any]:
    authority, _package = _load_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=runtime_path,
        root_owned=root_owned,
        release_trust_root=release_trust_root,
    )
    _validate_preflight(
        activation._read_canonical_json(  # noqa: SLF001
            preflight_path,
            root_owned=root_owned,
            modes=frozenset({0o444}),
        ),
        authority=authority,
        expected_sha256=expected_preflight_sha256,
    )
    if _transaction_path(
        authority["authority_sha256"],
        "rollback.json",
        evidence_root=evidence_root,
    ).exists():
        _fail("upstream_sync_successor_already_rolled_back")
    if _transaction_path(
        authority["authority_sha256"],
        "rollback-intent.json",
        evidence_root=evidence_root,
    ).exists():
        _fail("upstream_sync_successor_rollback_in_progress")
    _validate_started(
        activation._read_canonical_json(  # noqa: SLF001
            _transaction_path(
                authority["authority_sha256"],
                "started.json",
                evidence_root=evidence_root,
            ),
            root_owned=root_owned,
            modes=frozenset({0o600}),
        ),
        authority=authority,
        preflight_sha256=expected_preflight_sha256,
    )
    _validate_archive(
        authority=authority,
        preflight_sha256=expected_preflight_sha256,
        evidence_root=evidence_root,
        root_owned=root_owned,
    )
    observed = _prove_target(
        host=host,
        authority=authority,
        systemd_root=systemd_root,
    )
    terminal = activation._read_canonical_json(  # noqa: SLF001
        _transaction_path(
            authority["authority_sha256"],
            "terminal.json",
            evidence_root=evidence_root,
        ),
        root_owned=root_owned,
        modes=frozenset({0o600}),
    )
    expected_fields = {
        "schema",
        "authority_sha256",
        "preflight_receipt_sha256",
        "archive_receipt_sha256",
        "target_revision",
        "target_unit_digests",
        "timer_units",
        "service_units",
        "timers_enabled",
        "timers_active",
        "assert_result",
        "catch_up_result",
        "forward_recovery_performed",
        "rollback_performed",
        "stopped_units",
        "auto_merge_or_deploy_enabled",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(terminal, Mapping)
        or set(terminal) != expected_fields
        or terminal.get("schema") != TERMINAL_SCHEMA
        or terminal.get("authority_sha256") != authority["authority_sha256"]
        or terminal.get("preflight_receipt_sha256") != expected_preflight_sha256
        or terminal.get("target_revision") != authority["target_revision"]
        or terminal.get("target_unit_digests") != authority["target_unit_digests"]
        or terminal.get("timer_units") != list(TIMER_NAMES)
        or terminal.get("service_units") != list(SERVICE_NAMES)
        or terminal.get("timers_enabled") is not True
        or terminal.get("timers_active") is not True
        or terminal.get("assert_result") != {name: "yes" for name in UNIT_NAMES}
        or terminal.get("catch_up_result")
        != {
            name: {
                "result": observed[name].result,
                "exec_main_status": observed[name].exec_main_status,
            }
            for name in SERVICE_NAMES
        }
        or type(terminal.get("forward_recovery_performed")) is not bool
        or terminal.get("rollback_performed") is not False
        or terminal.get("stopped_units") != list(TIMER_NAMES)
        or terminal.get("auto_merge_or_deploy_enabled") is not False
        or terminal.get("secret_material_recorded") is not False
        or terminal.get("receipt_sha256")
        != activation._sha256(  # noqa: SLF001
            activation._canonical(  # noqa: SLF001
                {key: item for key, item in terminal.items() if key != "receipt_sha256"}
            )
        )
    ):
        _fail("upstream_sync_successor_terminal_invalid")
    return copy.deepcopy(dict(terminal))


def verify(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    runtime_path: Path | None = None,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    release_trust_root: Path | None = None,
) -> dict[str, Any]:
    provisional = validate_authority(
        activation._read_canonical_json(  # noqa: SLF001
            authority_path,
            root_owned=root_owned,
        ),
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["target_revision"]) / RUNTIME_RELATIVE
    )
    return _verify(
        expected_authority_sha256=expected_authority_sha256,
        expected_preflight_sha256=expected_preflight_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        preflight_path=preflight_path,
        runtime_path=selected_runtime,
        systemd_root=systemd_root,
        evidence_root=evidence_root,
        root_owned=root_owned,
        release_trust_root=release_trust_root or rail.RELEASES_ROOT,
        host=_SystemdHost(root_owned=root_owned),
    )


def _write_stdout(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(activation._canonical(value) + b"\n")  # noqa: SLF001
    sys.stdout.buffer.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "owner-run",
            "owner-apply-fixed",
            "preflight",
            "rebind",
            "verify",
        ),
    )
    parser.add_argument("--expected-authority-sha256")
    parser.add_argument("--expected-preflight-sha256")
    parser.add_argument("--target-revision")
    parser.add_argument("--target-package-manifest-sha256")
    parser.add_argument("--predecessor-revision")
    parser.add_argument("--predecessor-activation-receipt-sha256")
    parser.add_argument("--stage-c-host-artifact-manifest-sha256")
    parser.add_argument("--stage-c-release-update-publication-sha256")
    parser.add_argument("--source-tree-oid")
    parser.add_argument("--stage-c-builder-terminal-receipt-sha256")
    parser.add_argument("--rebind-runtime-sha256")
    parser.add_argument("--foundation-wrapper-sha256")
    parser.add_argument("--controller-owner-runtime-manifest-sha256")
    parser.add_argument("--controller-owner-runtime-attestation-sha256")
    parser.add_argument("--controller-owner-runtime-tree-sha256")
    parser.add_argument("--controller-owner-runtime-interpreter-sha256")
    parser.add_argument("--remote-owner-runtime-publication-sha256")
    parser.add_argument("--remote-owner-runtime-manifest-sha256")
    parser.add_argument("--remote-owner-runtime-attestation-sha256")
    parser.add_argument("--remote-owner-runtime-tree-sha256")
    parser.add_argument("--remote-owner-runtime-interpreter-sha256")
    parser.add_argument("--remote-owner-runtime-staging-publication-sha256")
    parser.add_argument("--remote-owner-runtime-staging-manifest-sha256")
    parser.add_argument("--remote-owner-runtime-staging-attestation-sha256")
    parser.add_argument("--remote-owner-runtime-staging-tree-sha256")
    parser.add_argument("--remote-owner-runtime-staging-interpreter-sha256")
    parser.add_argument("--remote-owner-runtime-staging-pyvenv-cfg-sha256")
    parser.add_argument("--remote-owner-runtime-builder-receipt-sha256")
    parser.add_argument("--remote-owner-runtime-wheel-sha256")
    parser.add_argument("--preexec-verifier-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    owner_fields = (
        arguments.target_revision,
        arguments.target_package_manifest_sha256,
        arguments.predecessor_revision,
        arguments.predecessor_activation_receipt_sha256,
        arguments.stage_c_host_artifact_manifest_sha256,
        arguments.stage_c_release_update_publication_sha256,
        arguments.source_tree_oid,
        arguments.stage_c_builder_terminal_receipt_sha256,
        arguments.rebind_runtime_sha256,
        arguments.foundation_wrapper_sha256,
        arguments.controller_owner_runtime_manifest_sha256,
        arguments.controller_owner_runtime_attestation_sha256,
        arguments.controller_owner_runtime_tree_sha256,
        arguments.controller_owner_runtime_interpreter_sha256,
        arguments.remote_owner_runtime_publication_sha256,
        arguments.remote_owner_runtime_manifest_sha256,
        arguments.remote_owner_runtime_attestation_sha256,
        arguments.remote_owner_runtime_tree_sha256,
        arguments.remote_owner_runtime_interpreter_sha256,
        arguments.remote_owner_runtime_staging_publication_sha256,
        arguments.remote_owner_runtime_staging_manifest_sha256,
        arguments.remote_owner_runtime_staging_attestation_sha256,
        arguments.remote_owner_runtime_staging_tree_sha256,
        arguments.remote_owner_runtime_staging_interpreter_sha256,
        arguments.remote_owner_runtime_staging_pyvenv_cfg_sha256,
        arguments.remote_owner_runtime_builder_receipt_sha256,
        arguments.remote_owner_runtime_wheel_sha256,
        arguments.preexec_verifier_sha256,
    )
    if arguments.operation == "owner-run":
        if (
            arguments.expected_authority_sha256 is not None
            or arguments.expected_preflight_sha256 is not None
            or any(item is None for item in owner_fields)
        ):
            _fail("upstream_sync_successor_owner_cli_invalid")
        _write_stdout(
            owner_run(
                build_owner_request(
                    target_revision=arguments.target_revision,
                    target_package_manifest_sha256=(
                        arguments.target_package_manifest_sha256
                    ),
                    predecessor_revision=arguments.predecessor_revision,
                    predecessor_activation_receipt_sha256=(
                        arguments.predecessor_activation_receipt_sha256
                    ),
                    stage_c_host_artifact_manifest_sha256=(
                        arguments.stage_c_host_artifact_manifest_sha256
                    ),
                    stage_c_release_update_publication_sha256=(
                        arguments.stage_c_release_update_publication_sha256
                    ),
                    source_tree_oid=arguments.source_tree_oid,
                    stage_c_builder_terminal_receipt_sha256=(
                        arguments.stage_c_builder_terminal_receipt_sha256
                    ),
                    rebind_runtime_sha256=arguments.rebind_runtime_sha256,
                    foundation_wrapper_sha256=(arguments.foundation_wrapper_sha256),
                    controller_owner_runtime_manifest_sha256=(
                        arguments.controller_owner_runtime_manifest_sha256
                    ),
                    controller_owner_runtime_attestation_sha256=(
                        arguments.controller_owner_runtime_attestation_sha256
                    ),
                    controller_owner_runtime_tree_sha256=(
                        arguments.controller_owner_runtime_tree_sha256
                    ),
                    controller_owner_runtime_interpreter_sha256=(
                        arguments.controller_owner_runtime_interpreter_sha256
                    ),
                    remote_owner_runtime_publication_sha256=(
                        arguments.remote_owner_runtime_publication_sha256
                    ),
                    remote_owner_runtime_manifest_sha256=(
                        arguments.remote_owner_runtime_manifest_sha256
                    ),
                    remote_owner_runtime_attestation_sha256=(
                        arguments.remote_owner_runtime_attestation_sha256
                    ),
                    remote_owner_runtime_tree_sha256=(
                        arguments.remote_owner_runtime_tree_sha256
                    ),
                    remote_owner_runtime_interpreter_sha256=(
                        arguments.remote_owner_runtime_interpreter_sha256
                    ),
                    remote_owner_runtime_staging_publication_sha256=(
                        arguments.remote_owner_runtime_staging_publication_sha256
                    ),
                    remote_owner_runtime_staging_manifest_sha256=(
                        arguments.remote_owner_runtime_staging_manifest_sha256
                    ),
                    remote_owner_runtime_staging_attestation_sha256=(
                        arguments.remote_owner_runtime_staging_attestation_sha256
                    ),
                    remote_owner_runtime_staging_tree_sha256=(
                        arguments.remote_owner_runtime_staging_tree_sha256
                    ),
                    remote_owner_runtime_staging_interpreter_sha256=(
                        arguments.remote_owner_runtime_staging_interpreter_sha256
                    ),
                    remote_owner_runtime_staging_pyvenv_cfg_sha256=(
                        arguments.remote_owner_runtime_staging_pyvenv_cfg_sha256
                    ),
                    remote_owner_runtime_builder_receipt_sha256=(
                        arguments.remote_owner_runtime_builder_receipt_sha256
                    ),
                    remote_owner_runtime_wheel_sha256=(
                        arguments.remote_owner_runtime_wheel_sha256
                    ),
                    preexec_verifier_sha256=(arguments.preexec_verifier_sha256),
                )
            )
        )
        return 0
    if arguments.operation == "owner-apply-fixed":
        if (
            arguments.expected_authority_sha256 is not None
            or arguments.expected_preflight_sha256 is not None
            or any(item is not None for item in owner_fields)
            or sys.stdin.isatty()
        ):
            _fail("upstream_sync_successor_owner_cli_invalid")
        _write_stdout(owner_apply_framed_stdin())
        return 0
    if any(item is not None for item in owner_fields):
        _fail("upstream_sync_successor_owner_cli_invalid")
    if _SHA256.fullmatch(arguments.expected_authority_sha256 or "") is None:
        _fail("upstream_sync_successor_authority_identity_invalid")
    if arguments.operation == "preflight":
        _write_stdout(
            preflight(
                expected_authority_sha256=arguments.expected_authority_sha256,
            )
        )
        return 0
    if _SHA256.fullmatch(arguments.expected_preflight_sha256 or "") is None:
        _fail("upstream_sync_successor_preflight_identity_invalid")
    operation = rebind if arguments.operation == "rebind" else verify
    _write_stdout(
        operation(
            expected_authority_sha256=arguments.expected_authority_sha256,
            expected_preflight_sha256=arguments.expected_preflight_sha256,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpstreamSyncRailSuccessorRebindError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


__all__ = [
    "ARCHIVE_SCHEMA",
    "AUTHORITY_SCHEMA",
    "PREFLIGHT_SCHEMA",
    "ROLLBACK_SCHEMA",
    "ROLLBACK_INTENT_SCHEMA",
    "STARTED_SCHEMA",
    "TERMINAL_SCHEMA",
    "OWNER_REQUEST_SCHEMA",
    "OWNER_RESULT_SCHEMA",
    "UNIT_NAMES",
    "TIMER_NAMES",
    "SERVICE_NAMES",
    "UnitState",
    "UpstreamSyncRailSuccessorRebindError",
    "build_authority",
    "build_owner_request",
    "decode_owner_request",
    "encode_owner_request",
    "main",
    "owner_apply",
    "owner_apply_framed_stdin",
    "owner_run",
    "preflight",
    "rebind",
    "validate_authority",
    "validate_owner_request",
    "validate_owner_result",
    "verify",
]
