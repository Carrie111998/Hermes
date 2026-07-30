#!/usr/bin/env python3
"""Owner-bound activation edge for the dual upstream-sync systemd rail.

This module is the privileged half of
``ops.muncho.runtime.upstream_sync_job_rail``.  The rail package remains inert:
it only stages four digest-bound unit files.  This edge consumes those exact
bytes plus three immutable authorities:

* a first-catch-up receipt whose candidate revision is explicitly frozen;
* an exact passkey-v2 action bundle with a signed single-use owner-gate
  authorization receipt and pinned Ed25519 receipt key; and
* a canonical activation authority binding the release, package, legacy cron
  definition, and this runtime's digest.

The activation order is intentionally one way:

1. durably record the exact authorized activation transaction;
2. install the four reviewed unit files;
3. enable and start the two new timers;
4. prove both timers loaded the exact packaged fragments and are active;
5. atomically preserve the exact legacy Hermes cron record as disabled and
   paused, proving the scheduler's exact claim predicates are false; and
6. only then disable the exact legacy collector timer (when present).

No prompt, title, command, report prose, or job name is inspected to choose an
action.  The only selected legacy record is the opaque exact job ID
``06ef64d72891``.  An interrupted run after timer activation recovers forward;
it never moves an open upstream candidate and never merges or deploys.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gateway import production_cron_continuity_review as cron_review
from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import passkey_v2_upstream_sync as owner_gate
from scripts.canary.production_cutover_activation_lock import (
    authority_activation_lock,
)


FIRST_CATCH_UP_SCHEMA = "muncho-upstream-first-catch-up-receipt.v1"
AUTHORITY_SCHEMA = "muncho-dual-upstream-sync-activation-authority.v1"
PREFLIGHT_SCHEMA = "muncho-dual-upstream-sync-activation-preflight.v1"
ACTIVATION_STARTED_SCHEMA = (
    "muncho-dual-upstream-sync-activation-started.v1"
)
TIMERS_ACTIVE_SCHEMA = "muncho-dual-upstream-sync-timers-active.v1"
TERMINAL_SCHEMA = "muncho-dual-upstream-sync-activation-terminal.v1"
ROLLBACK_SCHEMA = "muncho-dual-upstream-sync-inert-rollback.v1"

OPERATION_ID = owner_gate.OPERATION
FORK_REPOSITORY = "lomliev/hermes-agent"
FORK_BRANCH = "main"
UPSTREAM_REPOSITORY = "NousResearch/hermes-agent"
LEGACY_CRON_JOB_ID = "06ef64d72891"
LEGACY_COLLECTOR_TIMER_UNIT = (
    f"muncho-cron-{LEGACY_CRON_JOB_ID}.timer"
)

STAGED_ROOT = rail.PACKAGE_ROOT
FIRST_CATCH_UP_PATH = STAGED_ROOT / "first-catch-up-receipt.json"
OWNER_AUTHORIZATION_PATH = STAGED_ROOT / "owner-authorization.receipt"
OWNER_RECEIPT_PUBLIC_KEY_PATH = Path(
    "/etc/muncho-owner-gate/public/authority-receipt-public.pem"
)
AUTHORITY_PATH = STAGED_ROOT / "activation-authority.json"
PREFLIGHT_PATH = STAGED_ROOT / "activation-preflight.json"
EVIDENCE_ROOT = Path(
    "/var/lib/muncho-production-legacy-cutover/"
    "dual-upstream-sync-activation"
)
JOBS_PATH = Path(
    "/opt/adventico-ai-platform/hermes-home/cron/jobs.json"
)
SYSTEMD_ROOT = Path("/etc/systemd/system")
STAGED_TRUST_ROOT = Path("/var/lib/muncho-production-legacy-cutover")
RELEASE_TRUST_ROOT = rail.RELEASES_ROOT
OWNER_GATE_TRUST_ROOT = Path("/etc/muncho-owner-gate")
SYSTEMCTL = Path("/usr/bin/systemctl")
CANDIDATE_STATE_PATHS = (
    Path(
        "/opt/adventico-ai-platform/canonical-brain/state/private/"
        "upstream_sync_monitor/auto-sync-pr-state.json"
    ),
    rail.STATE_ROOT / "muncho-state/auto-sync-pr-state.json",
    rail.STATE_ROOT / "skyai-state/skyai-sync-candidate-state.json",
)
CUTOVER_RUNTIME_RELATIVE = Path(
    "scripts/canary/upstream_sync_rail_cutover.py"
)

UNIT_NAMES = (
    rail.SYNC_SERVICE_UNIT,
    rail.SYNC_TIMER_UNIT,
    rail.REPORT_SERVICE_UNIT,
    rail.REPORT_TIMER_UNIT,
)
TIMER_NAMES = (rail.SYNC_TIMER_UNIT, rail.REPORT_TIMER_UNIT)

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_OWNER_AUTHORIZATION_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_KEY_BYTES = 16 * 1024
LOCK_TIMEOUT_SECONDS = 10.0

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_LEGACY_TIMER_PRESTATES = frozenset(
    {
        "absent",
        "disabled_inactive",
        "enabled_inactive",
        "enabled_active",
    }
)


class UpstreamSyncRailCutoverError(RuntimeError):
    """Stable, secret-free activation failure."""


@dataclass(frozen=True)
class PackageContext:
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, bytes]
    release: Path
    sender_release: Path


@dataclass(frozen=True)
class TimerObservation:
    unit: str
    fragment_path: str | None
    fragment_sha256: str | None
    loaded: bool
    enabled: bool
    active: bool

    @property
    def state(self) -> str:
        if not self.loaded:
            return "absent"
        return (
            ("enabled" if self.enabled else "disabled")
            + "_"
            + ("active" if self.active else "inactive")
        )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_json_invalid"
        ) from exc
    if not 0 < len(raw) <= MAX_JSON_BYTES:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_json_size_invalid"
        )
    return raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decode(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_cutover_json_duplicate_key"
                )
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError()
            ),
        )
    except UpstreamSyncRailCutoverError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_json_invalid"
        ) from exc
    if not isinstance(value, Mapping) or _canonical(value) != raw:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_json_not_canonical"
        )
    return value


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


def _read_regular(
    path: Path,
    *,
    maximum: int,
    modes: frozenset[int] | None = None,
    root_owned: bool = False,
) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
            or modes is not None
            and stat.S_IMODE(before.st_mode) not in modes
            or root_owned
            and (before.st_uid != 0 or before.st_gid != 0)
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_cutover_file_identity_invalid"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        reached = path.lstat()
    except UpstreamSyncRailCutoverError:
        raise
    except OSError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_file_unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raw = b"".join(chunks)
    if (
        len(raw) != before.st_size
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or _identity(before) != _identity(reached)
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_file_changed"
        )
    return raw, before


def _read_canonical_json(
    path: Path,
    *,
    root_owned: bool,
    modes: frozenset[int] = frozenset({0o400, 0o440, 0o444, 0o600, 0o640}),
) -> Mapping[str, Any]:
    raw, _metadata = _read_regular(
        path,
        maximum=MAX_JSON_BYTES,
        modes=modes,
        root_owned=root_owned,
    )
    if not raw.endswith(b"\n"):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_json_framing_invalid"
        )
    return _decode(raw[:-1])


def _validate_trusted_parent_chain(
    path: Path,
    *,
    boundary: Path,
    root_owned: bool,
) -> None:
    """Reject symlinked or writable parents inside one exact trust root."""

    selected = Path(os.path.abspath(os.path.normpath(str(path))))
    trusted = Path(os.path.abspath(os.path.normpath(str(boundary))))
    try:
        relative = selected.relative_to(trusted)
    except ValueError:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_parent_boundary_invalid"
        ) from None
    current = trusted
    chain = [trusted]
    for component in relative.parent.parts:
        if component in {"", "."}:
            continue
        current = current / component
        chain.append(current)
    try:
        for parent in chain:
            metadata = parent.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or root_owned
                and (metadata.st_uid != 0 or metadata.st_gid != 0)
            ):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_cutover_parent_chain_untrusted"
                )
    except UpstreamSyncRailCutoverError:
        raise
    except OSError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cutover_parent_chain_untrusted"
        ) from exc


def _decode_receipt_public_key(raw: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, ValueError):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_receipt_public_key_invalid"
        ) from None
    if not isinstance(key, Ed25519PublicKey):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_receipt_public_key_invalid"
        )
    return key


def _load_receipt_public_key(
    path: Path,
    *,
    root_owned: bool,
) -> tuple[Ed25519PublicKey, bytes]:
    raw, _metadata = _read_regular(
        path,
        maximum=MAX_PUBLIC_KEY_BYTES,
        modes=frozenset({0o400, 0o440, 0o444}),
        root_owned=root_owned,
    )
    return _decode_receipt_public_key(raw), raw


def _receipt(unsigned: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(unsigned))
    return {**payload, "receipt_sha256": _sha256(_canonical(payload))}


def build_first_catch_up_receipt(
    *,
    candidate_upstream_sha: str,
    fork_main_before_sha: str,
    fork_main_after_sha: str,
    observed_upstream_tail_sha: str,
    release_checks_sha256: str,
    completed_at: str,
) -> dict[str, Any]:
    """Build exact factual evidence that the initial large catch-up landed."""

    if (
        any(
            not isinstance(value, str) or _SHA40.fullmatch(value) is None
            for value in (
                candidate_upstream_sha,
                fork_main_before_sha,
                fork_main_after_sha,
                observed_upstream_tail_sha,
            )
        )
        or not isinstance(release_checks_sha256, str)
        or _SHA256.fullmatch(release_checks_sha256) is None
        or not isinstance(completed_at, str)
        or _UTC.fullmatch(completed_at) is None
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_first_catch_up_invalid"
        )
    return _receipt(
        {
            "schema": FIRST_CATCH_UP_SCHEMA,
            "fork_repository": FORK_REPOSITORY,
            "fork_branch": FORK_BRANCH,
            "upstream_repository": UPSTREAM_REPOSITORY,
            "candidate_upstream_sha": candidate_upstream_sha,
            "fork_main_before_sha": fork_main_before_sha,
            "fork_main_after_sha": fork_main_after_sha,
            "observed_upstream_tail_sha": observed_upstream_tail_sha,
            "release_checks_sha256": release_checks_sha256,
            "candidate_ref_frozen": True,
            "later_upstream_is_tail_drift": True,
            "tail_drift_rebinds_candidate": False,
            "merged_to_fork_main": True,
            "completed_at": completed_at,
            "secret_material_recorded": False,
        }
    )


def validate_first_catch_up_receipt(
    value: Mapping[str, Any],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "fork_repository",
        "fork_branch",
        "upstream_repository",
        "candidate_upstream_sha",
        "fork_main_before_sha",
        "fork_main_after_sha",
        "observed_upstream_tail_sha",
        "release_checks_sha256",
        "candidate_ref_frozen",
        "later_upstream_is_tail_drift",
        "tail_drift_rebinds_candidate",
        "merged_to_fork_main",
        "completed_at",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != FIRST_CATCH_UP_SCHEMA
        or value.get("fork_repository") != FORK_REPOSITORY
        or value.get("fork_branch") != FORK_BRANCH
        or value.get("upstream_repository") != UPSTREAM_REPOSITORY
        or any(
            not isinstance(value.get(name), str)
            or _SHA40.fullmatch(value[name]) is None
            for name in (
                "candidate_upstream_sha",
                "fork_main_before_sha",
                "fork_main_after_sha",
                "observed_upstream_tail_sha",
            )
        )
        or not isinstance(value.get("release_checks_sha256"), str)
        or _SHA256.fullmatch(value["release_checks_sha256"]) is None
        or not isinstance(value.get("completed_at"), str)
        or _UTC.fullmatch(value["completed_at"]) is None
        or value.get("candidate_ref_frozen") is not True
        or value.get("later_upstream_is_tail_drift") is not True
        or value.get("tail_drift_rebinds_candidate") is not False
        or value.get("merged_to_fork_main") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("receipt_sha256") != expected_sha256
        or _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
        != expected_sha256
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_first_catch_up_invalid"
        )
    return copy.deepcopy(dict(value))


def _static_definition_sha256(job: Mapping[str, Any]) -> str:
    try:
        definition = cron_review._static_definition(job)
    except cron_review.ProductionCronContinuityReviewError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_definition_invalid"
        ) from exc
    return _sha256(_canonical(definition))


def _legacy_job(payload: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cron_store_invalid"
        )
    matches = [
        (index, item)
        for index, item in enumerate(jobs)
        if isinstance(item, Mapping)
        and item.get("id") == LEGACY_CRON_JOB_ID
    ]
    if len(matches) != 1:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_identity_invalid"
        )
    return matches[0]


def _retired_job(job: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(job))
    result["enabled"] = False
    result["state"] = "paused"
    result["next_run_at"] = None
    result["fire_claim"] = None
    result["run_claim"] = None
    return result


def _legacy_scheduler_claimable(job: Mapping[str, Any]) -> bool:
    """Mirror the scheduler's two exact persisted claim predicates."""

    return (
        job.get("enabled", True) is not False
        and job.get("state") != "paused"
    )


def _validate_package_context(
    *,
    staged_root: Path,
    release_revision: str,
    sender_revision: str,
    expected_manifest_sha256: str,
    root_owned: bool,
    staged_trust_root: Path,
    release_trust_root: Path,
) -> PackageContext:
    _validate_trusted_parent_chain(
        staged_root / "manifest.json",
        boundary=staged_trust_root,
        root_owned=root_owned,
    )
    manifest = _read_canonical_json(
        staged_root / "manifest.json",
        root_owned=root_owned,
    )
    try:
        checked = rail.validate_manifest(
            manifest,
            revision=release_revision,
            sender_revision=sender_revision,
        )
    except rail.DualSyncRailError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_package_manifest_invalid"
        ) from exc
    if checked.get("manifest_sha256") != expected_manifest_sha256:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_package_manifest_invalid"
        )
    recorded_artifacts = checked.get("artifacts")
    if (
        not isinstance(recorded_artifacts, Mapping)
        or set(recorded_artifacts) != set(UNIT_NAMES)
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in recorded_artifacts.values()
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_package_manifest_invalid"
        )
    artifacts: dict[str, bytes] = {}
    for name in UNIT_NAMES:
        _validate_trusted_parent_chain(
            staged_root / name,
            boundary=staged_trust_root,
            root_owned=root_owned,
        )
        raw, _metadata = _read_regular(
            staged_root / name,
            maximum=2 * 1024 * 1024,
            modes=frozenset({0o444}),
            root_owned=root_owned,
        )
        if _sha256(raw) != recorded_artifacts[name]:
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_package_artifact_invalid"
            )
        artifacts[name] = raw
    release = rail.release_root(release_revision)
    sender_release = rail.release_root(sender_revision)
    for revision, root in (
        (release_revision, release),
        (sender_revision, sender_release),
    ):
        _validate_trusted_parent_chain(
            root / rail.SOURCE_MARKER_RELATIVE,
            boundary=release_trust_root,
            root_owned=root_owned,
        )
        marker, _metadata = _read_regular(
            root / rail.SOURCE_MARKER_RELATIVE,
            maximum=128,
            root_owned=root_owned,
        )
        if marker != rail.exact_revision_marker(revision):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_release_marker_invalid"
            )
    source_digests = checked.get("source_digests")
    expected_source_paths = rail.source_paths(release)
    if (
        not isinstance(source_digests, Mapping)
        or set(source_digests) != set(expected_source_paths)
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_package_manifest_invalid"
        )
    for name, path in expected_source_paths.items():
        _validate_trusted_parent_chain(
            path,
            boundary=release_trust_root,
            root_owned=root_owned,
        )
        raw, _metadata = _read_regular(
            path,
            maximum=128 * 1024 * 1024,
            root_owned=root_owned,
        )
        if _sha256(raw) != source_digests[name]:
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_release_source_drifted"
            )
    sender_python = sender_release / ".venv/bin/python"
    _validate_trusted_parent_chain(
        sender_python,
        boundary=release_trust_root,
        root_owned=root_owned,
    )
    sender_target = sender_python.resolve(strict=True)
    _validate_trusted_parent_chain(
        sender_target,
        boundary=release_trust_root,
        root_owned=root_owned,
    )
    raw, _metadata = _read_regular(
        sender_target,
        maximum=128 * 1024 * 1024,
        root_owned=root_owned,
    )
    if _sha256(raw) != checked["sender_interpreter_sha256"]:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_sender_interpreter_drifted"
        )
    try:
        rail.validate_sync_service(
            artifacts[rail.SYNC_SERVICE_UNIT],
            revision=release_revision,
            release=release,
        )
        rail.validate_sync_timer(artifacts[rail.SYNC_TIMER_UNIT])
        rail.validate_report_service(
            artifacts[rail.REPORT_SERVICE_UNIT],
            release=release,
            sender_release=sender_release,
            sender_python_sha256=checked["sender_interpreter_sha256"],
        )
        rail.validate_report_timer(artifacts[rail.REPORT_TIMER_UNIT])
    except rail.DualSyncRailError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_package_artifact_invalid"
        ) from exc
    return PackageContext(
        manifest=checked,
        artifacts=artifacts,
        release=release,
        sender_release=sender_release,
    )


def build_activation_authority(
    *,
    package: PackageContext,
    first_catch_up_receipt: Mapping[str, Any],
    owner_authorization: Mapping[str, Any],
    owner_receipt_public_key_pem: bytes,
    authorization_now_unix: int,
    jobs_store: Mapping[str, Any],
    legacy_timer: TimerObservation,
    activation_runtime_sha256: str,
) -> dict[str, Any]:
    """Build the canonical digest-bound document an owner gate may stage."""

    catch_up = validate_first_catch_up_receipt(
        first_catch_up_receipt,
        expected_sha256=str(first_catch_up_receipt.get("receipt_sha256")),
    )
    if (
        not isinstance(owner_authorization, Mapping)
        or not isinstance(owner_receipt_public_key_pem, bytes)
        or not 0 < len(owner_receipt_public_key_pem) <= MAX_PUBLIC_KEY_BYTES
        or type(authorization_now_unix) is not int
        or authorization_now_unix <= 0
        or _SHA256.fullmatch(activation_runtime_sha256 or "") is None
        or legacy_timer.unit != LEGACY_COLLECTOR_TIMER_UNIT
        or legacy_timer.state not in _LEGACY_TIMER_PRESTATES
        or legacy_timer.state == "absent"
        and (
            legacy_timer.fragment_path is not None
            or legacy_timer.fragment_sha256 is not None
        )
        or legacy_timer.state != "absent"
        and (
            legacy_timer.fragment_path
            != str(SYSTEMD_ROOT / LEGACY_COLLECTOR_TIMER_UNIT)
            or not isinstance(legacy_timer.fragment_sha256, str)
            or _SHA256.fullmatch(legacy_timer.fragment_sha256) is None
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_authority_invalid"
    )
    _index, job = _legacy_job(jobs_store)
    if job.get("fire_claim") is not None or job.get("run_claim") is not None:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_not_quiescent"
        )
    retired = _retired_job(job)
    try:
        activation_plan = owner_gate.build_activation_plan(
            release_revision=package.manifest["release_revision"],
            sender_revision=package.manifest["sender_revision"],
            package_manifest_sha256=package.manifest["manifest_sha256"],
            activation_runtime_sha256=activation_runtime_sha256,
            first_catch_up_receipt_sha256=catch_up["receipt_sha256"],
            candidate_upstream_sha=catch_up["candidate_upstream_sha"],
            fork_main_after_sha=catch_up["fork_main_after_sha"],
            unit_digests={
                name: package.manifest["artifacts"][name]
                for name in UNIT_NAMES
            },
            legacy_cron_source_definition_sha256=(
                _static_definition_sha256(job)
            ),
            legacy_cron_retired_definition_sha256=(
                _static_definition_sha256(retired)
            ),
            legacy_collector_timer_prestate=legacy_timer.state,
            legacy_collector_timer_fragment_path=(
                legacy_timer.fragment_path
            ),
            legacy_collector_timer_fragment_sha256=(
                legacy_timer.fragment_sha256
            ),
        )
        receipt_public_key = _decode_receipt_public_key(
            owner_receipt_public_key_pem
        )
        authorization = owner_gate.validate_authorization_bundle(
            owner_authorization,
            activation_plan=activation_plan,
            receipt_public_key=receipt_public_key,
            now_unix=authorization_now_unix,
        )
    except owner_gate.UpstreamSyncPasskeyError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_authorization_invalid"
        ) from exc
    authorization_receipt = authorization["authorization_receipt"]
    owner_authorization_raw = _canonical(authorization) + b"\n"
    unsigned = {
        "schema": AUTHORITY_SCHEMA,
        "operation_id": OPERATION_ID,
        "release_revision": package.manifest["release_revision"],
        "sender_revision": package.manifest["sender_revision"],
        "package_manifest_sha256": package.manifest["manifest_sha256"],
        "activation_runtime_sha256": activation_runtime_sha256,
        "first_catch_up_receipt_sha256": catch_up["receipt_sha256"],
        "candidate_upstream_sha": catch_up["candidate_upstream_sha"],
        "fork_main_after_sha": catch_up["fork_main_after_sha"],
        "activation_plan_sha256": activation_plan[
            "activation_plan_sha256"
        ],
        "owner_gate_authorization_file_sha256": _sha256(
            owner_authorization_raw
        ),
        "owner_gate_authorization_bundle_sha256": authorization[
            "bundle_sha256"
        ],
        "owner_gate_authorization_receipt_sha256": (
            authorization_receipt["receipt_sha256"]
        ),
        "owner_gate_consume_attempt_id": authorization_receipt[
            "consume_attempt_id"
        ],
        "owner_gate_execution_window_expires_at_unix": (
            authorization_receipt["execution_window_expires_at_unix"]
        ),
        "owner_gate_receipt_public_key_id": authorization_receipt[
            "receipt_public_key_id"
        ],
        "owner_gate_receipt_public_key_file_sha256": _sha256(
            owner_receipt_public_key_pem
        ),
        "unit_digests": {
            name: package.manifest["artifacts"][name]
            for name in UNIT_NAMES
        },
        "timer_units": list(TIMER_NAMES),
        "legacy_cron_job_id": LEGACY_CRON_JOB_ID,
        "legacy_cron_source_definition_sha256": (
            _static_definition_sha256(job)
        ),
        "legacy_cron_retired_definition_sha256": (
            _static_definition_sha256(retired)
        ),
        "legacy_collector_timer_unit": LEGACY_COLLECTOR_TIMER_UNIT,
        "legacy_collector_timer_prestate": legacy_timer.state,
        "legacy_collector_timer_fragment_path": legacy_timer.fragment_path,
        "legacy_collector_timer_fragment_sha256": (
            legacy_timer.fragment_sha256
        ),
        "new_candidate_may_replace_open_candidate": False,
        "later_upstream_is_tail_drift": True,
        "auto_merge_or_deploy_enabled": False,
        "retire_legacy_only_after_new_timers_active": True,
        "secret_material_recorded": False,
    }
    return {
        **unsigned,
        "authority_sha256": _sha256(_canonical(unsigned)),
    }


def validate_activation_authority(
    value: Mapping[str, Any],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "operation_id",
        "release_revision",
        "sender_revision",
        "package_manifest_sha256",
        "activation_runtime_sha256",
        "first_catch_up_receipt_sha256",
        "candidate_upstream_sha",
        "fork_main_after_sha",
        "activation_plan_sha256",
        "owner_gate_authorization_file_sha256",
        "owner_gate_authorization_bundle_sha256",
        "owner_gate_authorization_receipt_sha256",
        "owner_gate_consume_attempt_id",
        "owner_gate_execution_window_expires_at_unix",
        "owner_gate_receipt_public_key_id",
        "owner_gate_receipt_public_key_file_sha256",
        "unit_digests",
        "timer_units",
        "legacy_cron_job_id",
        "legacy_cron_source_definition_sha256",
        "legacy_cron_retired_definition_sha256",
        "legacy_collector_timer_unit",
        "legacy_collector_timer_prestate",
        "legacy_collector_timer_fragment_path",
        "legacy_collector_timer_fragment_sha256",
        "new_candidate_may_replace_open_candidate",
        "later_upstream_is_tail_drift",
        "auto_merge_or_deploy_enabled",
        "retire_legacy_only_after_new_timers_active",
        "secret_material_recorded",
        "authority_sha256",
    }
    unit_digests = value.get("unit_digests")
    legacy_prestate = value.get("legacy_collector_timer_prestate")
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != AUTHORITY_SCHEMA
        or value.get("operation_id") != OPERATION_ID
        or any(
            not isinstance(value.get(name), str)
            or _SHA40.fullmatch(value[name]) is None
            for name in (
                "release_revision",
                "sender_revision",
                "candidate_upstream_sha",
                "fork_main_after_sha",
            )
        )
        or any(
            not isinstance(value.get(name), str)
            or _SHA256.fullmatch(value[name]) is None
            for name in (
                "package_manifest_sha256",
                "activation_runtime_sha256",
                "first_catch_up_receipt_sha256",
                "activation_plan_sha256",
                "owner_gate_authorization_file_sha256",
                "owner_gate_authorization_bundle_sha256",
                "owner_gate_authorization_receipt_sha256",
                "owner_gate_consume_attempt_id",
                "owner_gate_receipt_public_key_id",
                "owner_gate_receipt_public_key_file_sha256",
                "legacy_cron_source_definition_sha256",
                "legacy_cron_retired_definition_sha256",
                "authority_sha256",
            )
        )
        or type(value.get("owner_gate_execution_window_expires_at_unix"))
        is not int
        or value["owner_gate_execution_window_expires_at_unix"] <= 0
        or not isinstance(unit_digests, Mapping)
        or set(unit_digests) != set(UNIT_NAMES)
        or any(
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            for digest in unit_digests.values()
        )
        or value.get("timer_units") != list(TIMER_NAMES)
        or value.get("legacy_cron_job_id") != LEGACY_CRON_JOB_ID
        or value.get("legacy_collector_timer_unit")
        != LEGACY_COLLECTOR_TIMER_UNIT
        or legacy_prestate not in _LEGACY_TIMER_PRESTATES
        or legacy_prestate == "absent"
        and (
            value.get("legacy_collector_timer_fragment_path") is not None
            or value.get("legacy_collector_timer_fragment_sha256") is not None
        )
        or legacy_prestate != "absent"
        and (
            value.get("legacy_collector_timer_fragment_path")
            != str(SYSTEMD_ROOT / LEGACY_COLLECTOR_TIMER_UNIT)
            or not isinstance(
                value.get("legacy_collector_timer_fragment_sha256"),
                str,
            )
            or _SHA256.fullmatch(
                value["legacy_collector_timer_fragment_sha256"]
            )
            is None
        )
        or value.get("new_candidate_may_replace_open_candidate") is not False
        or value.get("later_upstream_is_tail_drift") is not True
        or value.get("auto_merge_or_deploy_enabled") is not False
        or value.get("retire_legacy_only_after_new_timers_active") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("authority_sha256") != expected_sha256
        or _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "authority_sha256"
                }
            )
        )
        != expected_sha256
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_authority_invalid"
        )
    try:
        plan = owner_gate.build_activation_plan(
            release_revision=value["release_revision"],
            sender_revision=value["sender_revision"],
            package_manifest_sha256=value["package_manifest_sha256"],
            activation_runtime_sha256=value[
                "activation_runtime_sha256"
            ],
            first_catch_up_receipt_sha256=value[
                "first_catch_up_receipt_sha256"
            ],
            candidate_upstream_sha=value["candidate_upstream_sha"],
            fork_main_after_sha=value["fork_main_after_sha"],
            unit_digests=value["unit_digests"],
            legacy_cron_source_definition_sha256=value[
                "legacy_cron_source_definition_sha256"
            ],
            legacy_cron_retired_definition_sha256=value[
                "legacy_cron_retired_definition_sha256"
            ],
            legacy_collector_timer_prestate=value[
                "legacy_collector_timer_prestate"
            ],
            legacy_collector_timer_fragment_path=value[
                "legacy_collector_timer_fragment_path"
            ],
            legacy_collector_timer_fragment_sha256=value[
                "legacy_collector_timer_fragment_sha256"
            ],
        )
    except owner_gate.UpstreamSyncPasskeyError:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_authority_invalid"
        ) from None
    if (
        plan["activation_plan_sha256"]
        != value["activation_plan_sha256"]
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_authority_invalid"
        )
    return copy.deepcopy(dict(value))


def _systemctl_capture(*arguments: str) -> tuple[int, bytes]:
    try:
        result = subprocess.run(
            [str(SYSTEMCTL), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_unavailable"
        ) from exc
    return result.returncode, result.stdout


def _systemctl_mutate(*arguments: str) -> None:
    code, output = _systemctl_capture(*arguments)
    if code != 0 or output not in {b"", b"\n"}:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_mutation_failed"
        )


def _systemctl_property(unit: str, property_name: str) -> bytes:
    code, output = _systemctl_capture(
        "show",
        unit,
        f"--property={property_name}",
        "--value",
    )
    if code != 0:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_failed"
        )
    return output


def observe_timer(unit: str) -> TimerObservation:
    """Return an exact structured systemd observation for one fixed timer."""

    if unit not in {*TIMER_NAMES, LEGACY_COLLECTOR_TIMER_UNIT}:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_timer_identity_invalid"
        )
    load = _systemctl_property(unit, "LoadState")
    fragment_raw = _systemctl_property(unit, "FragmentPath")
    enabled_result = _systemctl_capture("is-enabled", unit)
    active_result = _systemctl_capture("is-active", unit)
    if load == b"not-found\n":
        if (
            fragment_raw != b"\n"
            or enabled_result not in {
                (1, b"not-found\n"),
                (1, b"disabled\n"),
            }
            or active_result not in {
                (3, b"inactive\n"),
                (3, b"unknown\n"),
            }
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_systemd_observation_invalid"
            )
        return TimerObservation(
            unit=unit,
            fragment_path=None,
            fragment_sha256=None,
            loaded=False,
            enabled=False,
            active=False,
        )
    if load != b"loaded\n":
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_invalid"
        )
    try:
        fragment = fragment_raw[:-1].decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_invalid"
        ) from exc
    if (
        not fragment_raw.endswith(b"\n")
        or not fragment
        or "\n" in fragment
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_invalid"
        )
    raw, _metadata = _read_regular(
        Path(fragment),
        maximum=2 * 1024 * 1024,
    )
    if enabled_result == (0, b"enabled\n"):
        enabled = True
    elif enabled_result == (1, b"disabled\n"):
        enabled = False
    else:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_invalid"
        )
    if active_result == (0, b"active\n"):
        active = True
    elif active_result == (3, b"inactive\n"):
        active = False
    else:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_systemd_observation_invalid"
        )
    return TimerObservation(
        unit=unit,
        fragment_path=fragment,
        fragment_sha256=_sha256(raw),
        loaded=True,
        enabled=enabled,
        active=active,
    )


def _validate_legacy_timer(
    observed: TimerObservation,
    authority: Mapping[str, Any],
    *,
    allow_retired: bool,
) -> str:
    expected_state = authority["legacy_collector_timer_prestate"]
    if (
        observed.unit != LEGACY_COLLECTOR_TIMER_UNIT
        or observed.fragment_path
        != authority["legacy_collector_timer_fragment_path"]
        or observed.fragment_sha256
        != authority["legacy_collector_timer_fragment_sha256"]
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_timer_drifted"
        )
    if observed.state == expected_state:
        if (
            allow_retired
            and expected_state in {"absent", "disabled_inactive"}
        ):
            return "retired"
        return "source"
    if (
        allow_retired
        and expected_state != "absent"
        and observed.state == "disabled_inactive"
    ):
        return "retired"
    raise UpstreamSyncRailCutoverError(
        "upstream_sync_legacy_timer_drifted"
    )


def _runtime_context(
    *,
    expected_authority_sha256: str,
    staged_root: Path,
    authority_path: Path,
    catch_up_path: Path,
    owner_authorization_path: Path,
    owner_receipt_public_key_path: Path,
    runtime_path: Path,
    root_owned: bool,
    authorization_now_unix: int | None,
    authorization_must_be_current: bool,
) -> tuple[dict[str, Any], PackageContext, dict[str, Any]]:
    staged_trust_root = (
        STAGED_TRUST_ROOT if root_owned else staged_root
    )
    release_trust_root = (
        RELEASE_TRUST_ROOT if root_owned else rail.RELEASES_ROOT
    )
    runtime_trust_root = (
        RELEASE_TRUST_ROOT if root_owned else runtime_path.parent
    )
    owner_gate_trust_root = (
        OWNER_GATE_TRUST_ROOT
        if root_owned
        else owner_receipt_public_key_path.parent
    )
    for path in (
        authority_path,
        catch_up_path,
        owner_authorization_path,
    ):
        _validate_trusted_parent_chain(
            path,
            boundary=staged_trust_root,
            root_owned=root_owned,
        )
    _validate_trusted_parent_chain(
        runtime_path,
        boundary=runtime_trust_root,
        root_owned=root_owned,
    )
    _validate_trusted_parent_chain(
        owner_receipt_public_key_path,
        boundary=owner_gate_trust_root,
        root_owned=root_owned,
    )
    authority = validate_activation_authority(
        _read_canonical_json(
            authority_path,
            root_owned=root_owned,
        ),
        expected_sha256=expected_authority_sha256,
    )
    runtime_raw, _metadata = _read_regular(
        runtime_path,
        maximum=8 * 1024 * 1024,
        root_owned=root_owned,
    )
    if _sha256(runtime_raw) != authority["activation_runtime_sha256"]:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_runtime_drifted"
        )
    catch_up = validate_first_catch_up_receipt(
        _read_canonical_json(catch_up_path, root_owned=root_owned),
        expected_sha256=authority["first_catch_up_receipt_sha256"],
    )
    owner_raw, _metadata = _read_regular(
        owner_authorization_path,
        maximum=MAX_OWNER_AUTHORIZATION_BYTES,
        modes=frozenset({0o400, 0o440, 0o444}),
        root_owned=root_owned,
    )
    if not owner_raw.endswith(b"\n"):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_authorization_framing_invalid"
        )
    owner_authorization = _decode(owner_raw[:-1])
    receipt_public_key, receipt_public_key_raw = (
        _load_receipt_public_key(
            owner_receipt_public_key_path,
            root_owned=root_owned,
        )
    )
    if (
        _sha256(owner_raw)
        != authority["owner_gate_authorization_file_sha256"]
        or _sha256(receipt_public_key_raw)
        != authority["owner_gate_receipt_public_key_file_sha256"]
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_authorization_drifted"
        )
    package = _validate_package_context(
        staged_root=staged_root,
        release_revision=authority["release_revision"],
        sender_revision=authority["sender_revision"],
        expected_manifest_sha256=authority["package_manifest_sha256"],
        root_owned=root_owned,
        staged_trust_root=staged_trust_root,
        release_trust_root=release_trust_root,
    )
    try:
        activation_plan = owner_gate.build_activation_plan(
            release_revision=authority["release_revision"],
            sender_revision=authority["sender_revision"],
            package_manifest_sha256=authority[
                "package_manifest_sha256"
            ],
            activation_runtime_sha256=authority[
                "activation_runtime_sha256"
            ],
            first_catch_up_receipt_sha256=authority[
                "first_catch_up_receipt_sha256"
            ],
            candidate_upstream_sha=authority[
                "candidate_upstream_sha"
            ],
            fork_main_after_sha=authority["fork_main_after_sha"],
            unit_digests=authority["unit_digests"],
            legacy_cron_source_definition_sha256=authority[
                "legacy_cron_source_definition_sha256"
            ],
            legacy_cron_retired_definition_sha256=authority[
                "legacy_cron_retired_definition_sha256"
            ],
            legacy_collector_timer_prestate=authority[
                "legacy_collector_timer_prestate"
            ],
            legacy_collector_timer_fragment_path=authority[
                "legacy_collector_timer_fragment_path"
            ],
            legacy_collector_timer_fragment_sha256=authority[
                "legacy_collector_timer_fragment_sha256"
            ],
        )
        receipt = owner_authorization.get("authorization_receipt")
        selected_now = authorization_now_unix
        if not authorization_must_be_current:
            selected_now = (
                receipt.get("consumed_at_unix")
                if isinstance(receipt, Mapping)
                else None
            )
        if selected_now is None:
            selected_now = int(time.time())
        authorization = owner_gate.validate_authorization_bundle(
            owner_authorization,
            activation_plan=activation_plan,
            receipt_public_key=receipt_public_key,
            now_unix=selected_now,
        )
    except owner_gate.UpstreamSyncPasskeyError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_owner_authorization_invalid"
        ) from exc
    authorization_receipt = authorization["authorization_receipt"]
    if (
        package.manifest["artifacts"] != authority["unit_digests"]
        or catch_up["candidate_upstream_sha"]
        != authority["candidate_upstream_sha"]
        or catch_up["fork_main_after_sha"]
        != authority["fork_main_after_sha"]
        or activation_plan["activation_plan_sha256"]
        != authority["activation_plan_sha256"]
        or authorization["bundle_sha256"]
        != authority["owner_gate_authorization_bundle_sha256"]
        or authorization_receipt["receipt_sha256"]
        != authority["owner_gate_authorization_receipt_sha256"]
        or authorization_receipt["consume_attempt_id"]
        != authority["owner_gate_consume_attempt_id"]
        or authorization_receipt["execution_window_expires_at_unix"]
        != authority["owner_gate_execution_window_expires_at_unix"]
        or authorization_receipt["receipt_public_key_id"]
        != authority["owner_gate_receipt_public_key_id"]
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_lineage_invalid"
        )
    return authority, package, catch_up


def _parse_jobs(raw: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError()
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cron_store_invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cron_store_invalid"
        )
    return value


def _validate_legacy_job(
    payload: Mapping[str, Any],
    authority: Mapping[str, Any],
    *,
    allow_retired: bool,
) -> tuple[str, int, Mapping[str, Any]]:
    index, job = _legacy_job(payload)
    definition = _static_definition_sha256(job)
    if (
        definition
        == authority["legacy_cron_source_definition_sha256"]
    ):
        state = "source"
    elif (
        allow_retired
        and definition
        == authority["legacy_cron_retired_definition_sha256"]
    ):
        state = "retired"
    else:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_definition_drifted"
        )
    if job.get("fire_claim") is not None or job.get("run_claim") is not None:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_not_quiescent"
        )
    if state == "retired" and (
        job.get("enabled") is not False
        or job.get("state") != "paused"
        or job.get("next_run_at") is not None
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_retirement_invalid"
        )
    return state, index, job


def _validate_unit_targets(
    package: PackageContext,
    *,
    systemd_root: Path,
    root_owned: bool,
) -> dict[str, str]:
    prestates: dict[str, str] = {}
    for name, expected in package.artifacts.items():
        path = systemd_root / name
        if not path.exists() and not path.is_symlink():
            prestates[name] = "absent"
            continue
        raw, metadata = _read_regular(
            path,
            maximum=2 * 1024 * 1024,
            modes=frozenset({0o644}),
            root_owned=root_owned,
        )
        if raw != expected or stat.S_IMODE(metadata.st_mode) != 0o644:
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_systemd_target_drifted"
            )
        prestates[name] = "exact"
    return prestates


def _validate_new_timer_preflight_observation(
    observed: TimerObservation,
    *,
    package: PackageContext,
    systemd_root: Path,
) -> str:
    if observed.unit not in TIMER_NAMES or observed.active:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_new_timer_prematurely_active"
        )
    if not observed.loaded:
        if (
            observed.fragment_path is not None
            or observed.fragment_sha256 is not None
            or observed.enabled
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_new_timer_prestate_invalid"
            )
        return "absent"
    if (
        observed.fragment_path != str(systemd_root / observed.unit)
        or observed.fragment_sha256
        != package.manifest["artifacts"][observed.unit]
        or observed.state not in {"disabled_inactive", "enabled_inactive"}
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_new_timer_prestate_invalid"
        )
    return observed.state


def _assert_candidate_authorities_absent(
    candidate_state_paths: Sequence[Path],
) -> None:
    """Block activation when any old or new candidate authority already exists."""

    if (
        not candidate_state_paths
        or any(not isinstance(path, Path) for path in candidate_state_paths)
        or len({str(path) for path in candidate_state_paths})
        != len(candidate_state_paths)
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_candidate_state_paths_invalid"
        )
    for pointer in candidate_state_paths:
        ledger = pointer.with_name(f"{pointer.name}.ledger")
        if (
            os.path.lexists(pointer)
            or os.path.lexists(ledger)
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_candidate_state_requires_reconciliation"
            )


def preflight(
    *,
    expected_authority_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    catch_up_path: Path = FIRST_CATCH_UP_PATH,
    owner_authorization_path: Path = OWNER_AUTHORIZATION_PATH,
    owner_receipt_public_key_path: Path = OWNER_RECEIPT_PUBLIC_KEY_PATH,
    runtime_path: Path | None = None,
    jobs_path: Path = JOBS_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    root_owned: bool = True,
    authorization_now_unix: int | None = None,
    timer_observer: Callable[[str], TimerObservation] = observe_timer,
    candidate_state_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    authority_value = _read_canonical_json(
        authority_path,
        root_owned=root_owned,
    )
    provisional = validate_activation_authority(
        authority_value,
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["release_revision"])
        / CUTOVER_RUNTIME_RELATIVE
    )
    authority, package, _catch_up = _runtime_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        catch_up_path=catch_up_path,
        owner_authorization_path=owner_authorization_path,
        owner_receipt_public_key_path=owner_receipt_public_key_path,
        runtime_path=selected_runtime,
        root_owned=root_owned,
        authorization_now_unix=authorization_now_unix,
        authorization_must_be_current=True,
    )
    jobs_raw, _jobs_metadata = _read_regular(
        jobs_path,
        maximum=MAX_JSON_BYTES,
    )
    jobs = _parse_jobs(jobs_raw)
    state, _index, _job = _validate_legacy_job(
        jobs,
        authority,
        allow_retired=False,
    )
    unit_target_prestates = _validate_unit_targets(
        package,
        systemd_root=systemd_root,
        root_owned=root_owned,
    )
    new_timers = [timer_observer(name) for name in TIMER_NAMES]
    new_timer_prestates = {
        item.unit: _validate_new_timer_preflight_observation(
            item,
            package=package,
            systemd_root=systemd_root,
        )
        for item in new_timers
    }
    if any(
        new_timer_prestates[name] != "absent"
        and unit_target_prestates[name] != "exact"
        for name in TIMER_NAMES
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_new_timer_prestate_invalid"
        )
    legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
    _validate_legacy_timer(legacy, authority, allow_retired=False)
    _assert_candidate_authorities_absent(
        CANDIDATE_STATE_PATHS
        if candidate_state_paths is None
        else candidate_state_paths
    )
    return _receipt(
        {
            "schema": PREFLIGHT_SCHEMA,
            "created_at": _now(),
            "authority_sha256": authority["authority_sha256"],
            "package_manifest_sha256": package.manifest["manifest_sha256"],
            "first_catch_up_receipt_sha256": (
                authority["first_catch_up_receipt_sha256"]
            ),
            "owner_gate_authorization_receipt_sha256": (
                authority["owner_gate_authorization_receipt_sha256"]
            ),
            "legacy_cron_state": state,
            "legacy_cron_definition_sha256": (
                authority["legacy_cron_source_definition_sha256"]
            ),
            "legacy_collector_timer_state": legacy.state,
            "new_timer_count": len(new_timers),
            "new_timers_active": False,
            "new_timer_prestates": new_timer_prestates,
            "unit_target_prestates": unit_target_prestates,
            "unit_targets_exact_or_absent": True,
            "candidate_authorities_absent": True,
            "runtime_mutation_performed": False,
            "legacy_retirement_performed": False,
            "secret_material_recorded": False,
        }
    )


def _validate_preflight(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    expected_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "created_at",
        "authority_sha256",
        "package_manifest_sha256",
        "first_catch_up_receipt_sha256",
        "owner_gate_authorization_receipt_sha256",
        "legacy_cron_state",
        "legacy_cron_definition_sha256",
        "legacy_collector_timer_state",
        "new_timer_count",
        "new_timers_active",
        "new_timer_prestates",
        "unit_target_prestates",
        "unit_targets_exact_or_absent",
        "candidate_authorities_absent",
        "runtime_mutation_performed",
        "legacy_retirement_performed",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PREFLIGHT_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("package_manifest_sha256")
        != authority["package_manifest_sha256"]
        or value.get("first_catch_up_receipt_sha256")
        != authority["first_catch_up_receipt_sha256"]
        or value.get("owner_gate_authorization_receipt_sha256")
        != authority["owner_gate_authorization_receipt_sha256"]
        or value.get("legacy_cron_state") != "source"
        or value.get("legacy_cron_definition_sha256")
        != authority["legacy_cron_source_definition_sha256"]
        or value.get("legacy_collector_timer_state")
        != authority["legacy_collector_timer_prestate"]
        or value.get("new_timer_count") != len(TIMER_NAMES)
        or value.get("new_timers_active") is not False
        or not isinstance(value.get("new_timer_prestates"), Mapping)
        or set(value["new_timer_prestates"]) != set(TIMER_NAMES)
        or any(
            state
            not in {"absent", "disabled_inactive", "enabled_inactive"}
            for state in value["new_timer_prestates"].values()
        )
        or not isinstance(value.get("unit_target_prestates"), Mapping)
        or set(value["unit_target_prestates"]) != set(UNIT_NAMES)
        or any(
            state not in {"absent", "exact"}
            for state in value["unit_target_prestates"].values()
        )
        or any(
            value["new_timer_prestates"][name] != "absent"
            and value["unit_target_prestates"][name] != "exact"
            for name in TIMER_NAMES
        )
        or value.get("unit_targets_exact_or_absent") is not True
        or value.get("candidate_authorities_absent") is not True
        or value.get("runtime_mutation_performed") is not False
        or value.get("legacy_retirement_performed") is not False
        or value.get("secret_material_recorded") is not False
        or value.get("receipt_sha256") != expected_sha256
        or _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
        != expected_sha256
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_preflight_invalid"
        )
    return copy.deepcopy(dict(value))


def _build_activation_started_receipt(
    *,
    authority: Mapping[str, Any],
    expected_preflight_sha256: str,
) -> dict[str, Any]:
    """Freeze the exact forward-only transaction before its first mutation."""

    return _receipt(
        {
            "schema": ACTIVATION_STARTED_SCHEMA,
            "created_at": _now(),
            "authority_sha256": authority["authority_sha256"],
            "preflight_receipt_sha256": expected_preflight_sha256,
            "package_manifest_sha256": authority[
                "package_manifest_sha256"
            ],
            "activation_plan_sha256": authority[
                "activation_plan_sha256"
            ],
            "owner_gate_authorization_bundle_sha256": authority[
                "owner_gate_authorization_bundle_sha256"
            ],
            "owner_gate_authorization_receipt_sha256": authority[
                "owner_gate_authorization_receipt_sha256"
            ],
            "owner_gate_consume_attempt_id": authority[
                "owner_gate_consume_attempt_id"
            ],
            "owner_gate_execution_window_expires_at_unix": authority[
                "owner_gate_execution_window_expires_at_unix"
            ],
            "unit_digests": dict(authority["unit_digests"]),
            "timer_units": list(TIMER_NAMES),
            "legacy_cron_job_id": LEGACY_CRON_JOB_ID,
            "legacy_collector_timer_unit": LEGACY_COLLECTOR_TIMER_UNIT,
            "runtime_mutation_performed": False,
            "legacy_retirement_performed": False,
            "forward_recovery_only": True,
            "secret_material_recorded": False,
        }
    )


def _validate_activation_started_receipt(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    expected_preflight_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "created_at",
        "authority_sha256",
        "preflight_receipt_sha256",
        "package_manifest_sha256",
        "activation_plan_sha256",
        "owner_gate_authorization_bundle_sha256",
        "owner_gate_authorization_receipt_sha256",
        "owner_gate_consume_attempt_id",
        "owner_gate_execution_window_expires_at_unix",
        "unit_digests",
        "timer_units",
        "legacy_cron_job_id",
        "legacy_collector_timer_unit",
        "runtime_mutation_performed",
        "legacy_retirement_performed",
        "forward_recovery_only",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != ACTIVATION_STARTED_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("preflight_receipt_sha256")
        != expected_preflight_sha256
        or value.get("package_manifest_sha256")
        != authority["package_manifest_sha256"]
        or value.get("activation_plan_sha256")
        != authority["activation_plan_sha256"]
        or value.get("owner_gate_authorization_bundle_sha256")
        != authority["owner_gate_authorization_bundle_sha256"]
        or value.get("owner_gate_authorization_receipt_sha256")
        != authority["owner_gate_authorization_receipt_sha256"]
        or value.get("owner_gate_consume_attempt_id")
        != authority["owner_gate_consume_attempt_id"]
        or value.get("owner_gate_execution_window_expires_at_unix")
        != authority["owner_gate_execution_window_expires_at_unix"]
        or value.get("unit_digests") != authority["unit_digests"]
        or value.get("timer_units") != list(TIMER_NAMES)
        or value.get("legacy_cron_job_id") != LEGACY_CRON_JOB_ID
        or value.get("legacy_collector_timer_unit")
        != LEGACY_COLLECTOR_TIMER_UNIT
        or value.get("runtime_mutation_performed") is not False
        or value.get("legacy_retirement_performed") is not False
        or value.get("forward_recovery_only") is not True
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_started_receipt_invalid"
        )
    return copy.deepcopy(dict(value))


def _validate_timers_active_receipt(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    expected_preflight_sha256: str,
    activation_started_sha256: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "created_at",
        "authority_sha256",
        "preflight_receipt_sha256",
        "activation_started_receipt_sha256",
        "package_manifest_sha256",
        "timer_units",
        "timer_fragment_sha256",
        "timers_enabled",
        "timers_active",
        "legacy_retirement_performed",
        "forward_recovery_only",
        "secret_material_recorded",
        "receipt_sha256",
    }
    fragments = value.get("timer_fragment_sha256")
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != TIMERS_ACTIVE_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("preflight_receipt_sha256")
        != expected_preflight_sha256
        or value.get("activation_started_receipt_sha256")
        != activation_started_sha256
        or value.get("package_manifest_sha256")
        != authority["package_manifest_sha256"]
        or value.get("timer_units") != list(TIMER_NAMES)
        or not isinstance(fragments, Mapping)
        or fragments
        != {
            name: authority["unit_digests"][name]
            for name in TIMER_NAMES
        }
        or value.get("timers_enabled") is not True
        or value.get("timers_active") is not True
        or value.get("legacy_retirement_performed") is not False
        or value.get("forward_recovery_only") is not True
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_timers_active_receipt_invalid"
        )
    return copy.deepcopy(dict(value))


def _atomic_write(
    path: Path,
    raw: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, mode)
        if os.geteuid() == 0:
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextmanager
def _cron_jobs_lock(
    jobs_path: Path,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    lock_path = jobs_path.parent / ".jobs.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_cron_lock_invalid"
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise UpstreamSyncRailCutoverError(
                        "upstream_sync_cron_lock_unavailable"
                    )
                time.sleep(0.05)
        yield
    except UpstreamSyncRailCutoverError:
        raise
    except OSError as exc:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_cron_lock_unavailable"
        ) from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)


def _evidence_path(
    authority_sha256: str,
    name: str,
    *,
    evidence_root: Path,
) -> Path:
    if _SHA256.fullmatch(authority_sha256 or "") is None:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_authority_identity_invalid"
        )
    if name not in {
        "activation-started.json",
        "timers-active.json",
        "terminal.json",
        "inert-rollback.json",
    }:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_evidence_identity_invalid"
        )
    return evidence_root / authority_sha256 / name


def _publish_evidence(
    value: Mapping[str, Any],
    *,
    path: Path,
    root_owned: bool,
) -> dict[str, Any]:
    raw = _canonical(value) + b"\n"
    if path.exists() or path.is_symlink():
        observed, _metadata = _read_regular(
            path,
            maximum=MAX_JSON_BYTES,
            modes=frozenset({0o600}),
            root_owned=root_owned,
        )
        if observed != raw:
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_evidence_drifted"
            )
        return copy.deepcopy(dict(value))
    _atomic_write(
        path,
        raw,
        mode=0o600,
        uid=0 if root_owned else os.geteuid(),
        gid=0 if root_owned else os.getegid(),
    )
    observed, _metadata = _read_regular(
        path,
        maximum=MAX_JSON_BYTES,
        modes=frozenset({0o600}),
        root_owned=root_owned,
    )
    if observed != raw:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_evidence_write_unconfirmed"
        )
    return copy.deepcopy(dict(value))


def _install_units(
    package: PackageContext,
    *,
    systemd_root: Path,
    root_owned: bool,
) -> None:
    prestates = _validate_unit_targets(
        package,
        systemd_root=systemd_root,
        root_owned=root_owned,
    )
    for name, raw in package.artifacts.items():
        if prestates[name] == "absent":
            _atomic_write(
                systemd_root / name,
                raw,
                mode=0o644,
                uid=0,
                gid=0,
            )
    _validate_unit_targets(
        package,
        systemd_root=systemd_root,
        root_owned=root_owned,
    )
    _systemctl_mutate("daemon-reload")


def _prove_new_timers_active(
    package: PackageContext,
    *,
    timer_observer: Callable[[str], TimerObservation],
    systemd_root: Path,
) -> list[TimerObservation]:
    observed = [timer_observer(name) for name in TIMER_NAMES]
    for item in observed:
        expected_path = systemd_root / item.unit
        expected_digest = package.manifest["artifacts"][item.unit]
        if (
            not item.loaded
            or not item.enabled
            or not item.active
            or item.fragment_path != str(expected_path)
            or item.fragment_sha256 != expected_digest
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_new_timer_activation_unconfirmed"
            )
    return observed


def _retire_legacy_cron(
    *,
    jobs_path: Path,
    authority: Mapping[str, Any],
) -> tuple[str, str]:
    raw, metadata = _read_regular(
        jobs_path,
        maximum=MAX_JSON_BYTES,
    )
    payload = _parse_jobs(raw)
    state, index, job = _validate_legacy_job(
        payload,
        authority,
        allow_retired=True,
    )
    if state == "source":
        updated = copy.deepcopy(dict(payload))
        jobs = copy.deepcopy(list(updated["jobs"]))
        jobs[index] = _retired_job(job)
        updated["jobs"] = jobs
        value = (
            json.dumps(
                updated,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8", errors="strict")
            + b"\n"
        )
        _atomic_write(
            jobs_path,
            value,
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )
    final_raw, _final_metadata = _read_regular(
        jobs_path,
        maximum=MAX_JSON_BYTES,
    )
    final_payload = _parse_jobs(final_raw)
    final_state, _index, final_job = _validate_legacy_job(
        final_payload,
        authority,
        allow_retired=True,
    )
    if final_state != "retired":
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_retirement_unconfirmed"
        )
    if (
        final_job.get("enabled") is not False
        or final_job.get("state") != "paused"
        or _legacy_scheduler_claimable(final_job)
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_legacy_cron_scheduler_claimable"
        )
    return final_state, _static_definition_sha256(final_job)


def activate(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    catch_up_path: Path = FIRST_CATCH_UP_PATH,
    owner_authorization_path: Path = OWNER_AUTHORIZATION_PATH,
    owner_receipt_public_key_path: Path = OWNER_RECEIPT_PUBLIC_KEY_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    runtime_path: Path | None = None,
    jobs_path: Path = JOBS_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    authorization_now_unix: int | None = None,
    timer_observer: Callable[[str], TimerObservation] = observe_timer,
    activation_lock_factory: Callable[[], Any] | None = None,
    candidate_state_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    if require_root and os.geteuid() != 0:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_root_required"
        )
    authority_value = _read_canonical_json(
        authority_path,
        root_owned=root_owned,
    )
    provisional = validate_activation_authority(
        authority_value,
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["release_revision"])
        / CUTOVER_RUNTIME_RELATIVE
    )
    activation_started_path = _evidence_path(
        provisional["authority_sha256"],
        "activation-started.json",
        evidence_root=evidence_root,
    )
    timers_path = _evidence_path(
        provisional["authority_sha256"],
        "timers-active.json",
        evidence_root=evidence_root,
    )
    terminal_path = _evidence_path(
        provisional["authority_sha256"],
        "terminal.json",
        evidence_root=evidence_root,
    )
    rollback_path = _evidence_path(
        provisional["authority_sha256"],
        "inert-rollback.json",
        evidence_root=evidence_root,
    )
    with authority_activation_lock(
        require_root=require_root,
        lock_factory=activation_lock_factory,
    ):
        with _cron_jobs_lock(jobs_path):
            if rollback_path.exists() or rollback_path.is_symlink():
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_inert_rollback_already_recorded"
                )
            activation_started_present = (
                activation_started_path.exists()
                or activation_started_path.is_symlink()
            )
            if not activation_started_present and (
                timers_path.exists()
                or timers_path.is_symlink()
                or terminal_path.exists()
                or terminal_path.is_symlink()
            ):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_activation_started_receipt_missing"
                )
            # Current owner authorization is required to publish the first
            # durable activation-started receipt. Once that exact receipt
            # exists, retries validate the same signed lineage at its original
            # consume time and may only complete this frozen transaction.
            authority, package, _catch_up = _runtime_context(
                expected_authority_sha256=expected_authority_sha256,
                staged_root=staged_root,
                authority_path=authority_path,
                catch_up_path=catch_up_path,
                owner_authorization_path=owner_authorization_path,
                owner_receipt_public_key_path=(
                    owner_receipt_public_key_path
                ),
                runtime_path=selected_runtime,
                root_owned=root_owned,
                authorization_now_unix=authorization_now_unix,
                authorization_must_be_current=(
                    not activation_started_present
                ),
            )
            _validate_preflight(
                _read_canonical_json(
                    preflight_path,
                    root_owned=root_owned,
                ),
                authority=authority,
                expected_sha256=expected_preflight_sha256,
            )
            if not activation_started_present:
                _assert_candidate_authorities_absent(
                    CANDIDATE_STATE_PATHS
                    if candidate_state_paths is None
                    else candidate_state_paths
                )
            if activation_started_present:
                activation_started = (
                    _validate_activation_started_receipt(
                        _read_canonical_json(
                            activation_started_path,
                            root_owned=root_owned,
                            modes=frozenset({0o600}),
                        ),
                        authority=authority,
                        expected_preflight_sha256=(
                            expected_preflight_sha256
                        ),
                    )
                )
            else:
                activation_started = _publish_evidence(
                    _build_activation_started_receipt(
                        authority=authority,
                        expected_preflight_sha256=(
                            expected_preflight_sha256
                        ),
                    ),
                    path=activation_started_path,
                    root_owned=root_owned,
                )
            if terminal_path.exists() or terminal_path.is_symlink():
                return verify(
                    expected_authority_sha256=expected_authority_sha256,
                    staged_root=staged_root,
                    authority_path=authority_path,
                    catch_up_path=catch_up_path,
                    owner_authorization_path=owner_authorization_path,
                    owner_receipt_public_key_path=(
                        owner_receipt_public_key_path
                    ),
                    preflight_path=preflight_path,
                    runtime_path=selected_runtime,
                    jobs_path=jobs_path,
                    systemd_root=systemd_root,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    authorization_now_unix=authorization_now_unix,
                    timer_observer=timer_observer,
                )
            if not timers_path.exists() and not timers_path.is_symlink():
                jobs_raw, _metadata = _read_regular(
                    jobs_path,
                    maximum=MAX_JSON_BYTES,
                )
                jobs = _parse_jobs(jobs_raw)
                _validate_legacy_job(
                    jobs,
                    authority,
                    allow_retired=False,
                )
                legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
                _validate_legacy_timer(
                    legacy,
                    authority,
                    allow_retired=False,
                )
                _install_units(
                    package,
                    systemd_root=systemd_root,
                    root_owned=root_owned,
                )
                for timer in TIMER_NAMES:
                    _systemctl_mutate("enable", "--now", timer)
                observed = _prove_new_timers_active(
                    package,
                    timer_observer=timer_observer,
                    systemd_root=systemd_root,
                )
                timers_receipt = _receipt(
                    {
                        "schema": TIMERS_ACTIVE_SCHEMA,
                        "created_at": _now(),
                        "authority_sha256": authority["authority_sha256"],
                        "preflight_receipt_sha256": (
                            expected_preflight_sha256
                        ),
                        "activation_started_receipt_sha256": (
                            activation_started["receipt_sha256"]
                        ),
                        "package_manifest_sha256": (
                            package.manifest["manifest_sha256"]
                        ),
                        "timer_units": [item.unit for item in observed],
                        "timer_fragment_sha256": {
                            item.unit: item.fragment_sha256
                            for item in observed
                        },
                        "timers_enabled": True,
                        "timers_active": True,
                        "legacy_retirement_performed": False,
                        "forward_recovery_only": True,
                        "secret_material_recorded": False,
                    }
                )
                _publish_evidence(
                    timers_receipt,
                    path=timers_path,
                    root_owned=root_owned,
                )
            else:
                _validate_timers_active_receipt(
                    _read_canonical_json(
                        timers_path,
                        root_owned=root_owned,
                        modes=frozenset({0o600}),
                    ),
                    authority=authority,
                    expected_preflight_sha256=(
                        expected_preflight_sha256
                    ),
                    activation_started_sha256=(
                        activation_started["receipt_sha256"]
                    ),
                )
                _prove_new_timers_active(
                    package,
                    timer_observer=timer_observer,
                    systemd_root=systemd_root,
                )

            cron_state, cron_definition = _retire_legacy_cron(
                jobs_path=jobs_path,
                authority=authority,
            )
            legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
            legacy_state = _validate_legacy_timer(
                legacy,
                authority,
                allow_retired=True,
            )
            if legacy_state == "source" and legacy.loaded:
                _systemctl_mutate(
                    "disable",
                    "--now",
                    LEGACY_COLLECTOR_TIMER_UNIT,
                )
                legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
                legacy_state = _validate_legacy_timer(
                    legacy,
                    authority,
                    allow_retired=True,
                )
                if legacy_state != "retired":
                    raise UpstreamSyncRailCutoverError(
                        "upstream_sync_legacy_timer_retirement_unconfirmed"
                    )
            _prove_new_timers_active(
                package,
                timer_observer=timer_observer,
                systemd_root=systemd_root,
            )
            terminal = _receipt(
                {
                    "schema": TERMINAL_SCHEMA,
                    "created_at": _now(),
                    "authority_sha256": authority["authority_sha256"],
                    "preflight_receipt_sha256": expected_preflight_sha256,
                    "activation_started_receipt_sha256": (
                        activation_started["receipt_sha256"]
                    ),
                    "timers_active_receipt_sha256": _read_canonical_json(
                        timers_path,
                        root_owned=root_owned,
                        modes=frozenset({0o600}),
                    )["receipt_sha256"],
                    "package_manifest_sha256": (
                        package.manifest["manifest_sha256"]
                    ),
                    "new_timer_count": len(TIMER_NAMES),
                    "new_timers_enabled": True,
                    "new_timers_active": True,
                    "legacy_cron_job_id": LEGACY_CRON_JOB_ID,
                    "legacy_cron_state": cron_state,
                    "legacy_cron_definition_sha256": cron_definition,
                    "legacy_scheduler_claimable": False,
                    "legacy_collector_timer_unit": (
                        LEGACY_COLLECTOR_TIMER_UNIT
                    ),
                    "legacy_collector_timer_state": (
                        "absent" if not legacy.loaded else legacy.state
                    ),
                    "legacy_retired_after_new_timers_active": True,
                    "new_candidate_may_replace_open_candidate": False,
                    "auto_merge_or_deploy_enabled": False,
                    "secret_material_recorded": False,
                }
            )
            return _publish_evidence(
                terminal,
                path=terminal_path,
                root_owned=root_owned,
            )


def _validate_rollback_receipt(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    removed_units: list[str],
    preserved_units: list[str],
    final_timer_states: Mapping[str, str],
) -> dict[str, Any]:
    fields = {
        "schema",
        "created_at",
        "authority_sha256",
        "preflight_receipt_sha256",
        "package_manifest_sha256",
        "removed_unit_files",
        "preserved_preexisting_unit_files",
        "new_timer_states",
        "new_timers_were_ever_active",
        "timers_active_receipt_present",
        "legacy_cron_state",
        "legacy_collector_timer_state",
        "legacy_retirement_performed",
        "forward_recovery_required",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != ROLLBACK_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("preflight_receipt_sha256") != preflight_sha256
        or value.get("package_manifest_sha256")
        != authority["package_manifest_sha256"]
        or value.get("removed_unit_files") != removed_units
        or value.get("preserved_preexisting_unit_files")
        != preserved_units
        or value.get("new_timer_states") != final_timer_states
        or value.get("new_timers_were_ever_active") is not False
        or value.get("timers_active_receipt_present") is not False
        or value.get("legacy_cron_state") != "source"
        or value.get("legacy_collector_timer_state")
        != authority["legacy_collector_timer_prestate"]
        or value.get("legacy_retirement_performed") is not False
        or value.get("forward_recovery_required") is not False
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_inert_rollback_receipt_invalid"
        )
    return copy.deepcopy(dict(value))


def rollback_inert(
    *,
    expected_authority_sha256: str,
    expected_preflight_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    catch_up_path: Path = FIRST_CATCH_UP_PATH,
    owner_authorization_path: Path = OWNER_AUTHORIZATION_PATH,
    owner_receipt_public_key_path: Path = OWNER_RECEIPT_PUBLIC_KEY_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    runtime_path: Path | None = None,
    jobs_path: Path = JOBS_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    authorization_now_unix: int | None = None,
    timer_observer: Callable[[str], TimerObservation] = observe_timer,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Remove only newly installed inert bytes before activation is proven."""

    if require_root and os.geteuid() != 0:
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_root_required"
        )
    authority_value = _read_canonical_json(
        authority_path,
        root_owned=root_owned,
    )
    provisional = validate_activation_authority(
        authority_value,
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["release_revision"])
        / CUTOVER_RUNTIME_RELATIVE
    )
    authority, package, _catch_up = _runtime_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        catch_up_path=catch_up_path,
        owner_authorization_path=owner_authorization_path,
        owner_receipt_public_key_path=owner_receipt_public_key_path,
        runtime_path=selected_runtime,
        root_owned=root_owned,
        authorization_now_unix=authorization_now_unix,
        authorization_must_be_current=True,
    )
    staged_preflight = _validate_preflight(
        _read_canonical_json(
            preflight_path,
            root_owned=root_owned,
        ),
        authority=authority,
        expected_sha256=expected_preflight_sha256,
    )
    activation_started_path = _evidence_path(
        authority["authority_sha256"],
        "activation-started.json",
        evidence_root=evidence_root,
    )
    timers_path = _evidence_path(
        authority["authority_sha256"],
        "timers-active.json",
        evidence_root=evidence_root,
    )
    terminal_path = _evidence_path(
        authority["authority_sha256"],
        "terminal.json",
        evidence_root=evidence_root,
    )
    rollback_path = _evidence_path(
        authority["authority_sha256"],
        "inert-rollback.json",
        evidence_root=evidence_root,
    )
    removed_units = [
        name
        for name in UNIT_NAMES
        if staged_preflight["unit_target_prestates"][name] == "absent"
    ]
    preserved_units = [
        name
        for name in UNIT_NAMES
        if staged_preflight["unit_target_prestates"][name] == "exact"
    ]
    final_timer_states = {
        name: (
            "absent"
            if staged_preflight["unit_target_prestates"][name] == "absent"
            else (
                "enabled_inactive"
                if staged_preflight["new_timer_prestates"][name]
                == "enabled_inactive"
                else "disabled_inactive"
            )
        )
        for name in TIMER_NAMES
    }
    with authority_activation_lock(
        require_root=require_root,
        lock_factory=activation_lock_factory,
    ):
        with _cron_jobs_lock(jobs_path):
            if (
                activation_started_path.exists()
                or activation_started_path.is_symlink()
            ):
                _validate_activation_started_receipt(
                    _read_canonical_json(
                        activation_started_path,
                        root_owned=root_owned,
                        modes=frozenset({0o600}),
                    ),
                    authority=authority,
                    expected_preflight_sha256=(
                        expected_preflight_sha256
                    ),
                )
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_forward_recovery_only"
                )
            if (
                timers_path.exists()
                or timers_path.is_symlink()
                or terminal_path.exists()
                or terminal_path.is_symlink()
            ):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_forward_recovery_only"
                )
            existing_rollback = None
            if rollback_path.exists() or rollback_path.is_symlink():
                existing_rollback = _read_canonical_json(
                    rollback_path,
                    root_owned=root_owned,
                    modes=frozenset({0o600}),
                )
            jobs_raw, _metadata = _read_regular(
                jobs_path,
                maximum=MAX_JSON_BYTES,
            )
            jobs = _parse_jobs(jobs_raw)
            cron_state, _index, _job = _validate_legacy_job(
                jobs,
                authority,
                allow_retired=False,
            )
            legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
            _validate_legacy_timer(
                legacy,
                authority,
                allow_retired=False,
            )
            _systemctl_mutate("daemon-reload")
            current_timers = {
                name: timer_observer(name)
                for name in TIMER_NAMES
            }
            if any(item.active for item in current_timers.values()):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_forward_recovery_only"
                )
            for item in current_timers.values():
                if item.loaded:
                    _validate_new_timer_preflight_observation(
                        item,
                        package=package,
                        systemd_root=systemd_root,
                    )
            for name, item in current_timers.items():
                expected = final_timer_states[name]
                if expected != "enabled_inactive" and item.enabled:
                    _systemctl_mutate("disable", "--now", name)
                elif expected == "enabled_inactive" and not item.enabled:
                    if not item.loaded:
                        raise UpstreamSyncRailCutoverError(
                            "upstream_sync_inert_rollback_state_invalid"
                        )
                    _systemctl_mutate("enable", name)

            current_units = _validate_unit_targets(
                package,
                systemd_root=systemd_root,
                root_owned=root_owned,
            )
            for name in preserved_units:
                if current_units[name] != "exact":
                    raise UpstreamSyncRailCutoverError(
                        "upstream_sync_preexisting_unit_missing"
                    )
            for name in removed_units:
                if current_units[name] == "exact":
                    try:
                        os.unlink(systemd_root / name)
                    except OSError as exc:
                        raise UpstreamSyncRailCutoverError(
                            "upstream_sync_inert_unit_removal_failed"
                        ) from exc
            _systemctl_mutate("daemon-reload")
            final_observations = {
                name: timer_observer(name)
                for name in TIMER_NAMES
            }
            if any(
                item.state != final_timer_states[name]
                for name, item in final_observations.items()
            ):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_inert_rollback_state_invalid"
                )
            final_units = _validate_unit_targets(
                package,
                systemd_root=systemd_root,
                root_owned=root_owned,
            )
            if any(
                final_units[name] != "absent"
                for name in removed_units
            ) or any(
                final_units[name] != "exact"
                for name in preserved_units
            ):
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_inert_rollback_state_invalid"
                )
            final_jobs_raw, _metadata = _read_regular(
                jobs_path,
                maximum=MAX_JSON_BYTES,
            )
            final_cron_state, _index, _job = _validate_legacy_job(
                _parse_jobs(final_jobs_raw),
                authority,
                allow_retired=False,
            )
            final_legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
            _validate_legacy_timer(
                final_legacy,
                authority,
                allow_retired=False,
            )
            if cron_state != "source" or final_cron_state != "source":
                raise UpstreamSyncRailCutoverError(
                    "upstream_sync_legacy_retirement_during_rollback"
                )
            if existing_rollback is None:
                existing_rollback = _receipt(
                    {
                        "schema": ROLLBACK_SCHEMA,
                        "created_at": _now(),
                        "authority_sha256": authority["authority_sha256"],
                        "preflight_receipt_sha256": (
                            expected_preflight_sha256
                        ),
                        "package_manifest_sha256": (
                            authority["package_manifest_sha256"]
                        ),
                        "removed_unit_files": removed_units,
                        "preserved_preexisting_unit_files": preserved_units,
                        "new_timer_states": {
                            name: item.state
                            for name, item in final_observations.items()
                        },
                        "new_timers_were_ever_active": False,
                        "timers_active_receipt_present": False,
                        "legacy_cron_state": "source",
                        "legacy_collector_timer_state": (
                            authority["legacy_collector_timer_prestate"]
                        ),
                        "legacy_retirement_performed": False,
                        "forward_recovery_required": False,
                        "secret_material_recorded": False,
                    }
                )
                existing_rollback = _publish_evidence(
                    existing_rollback,
                    path=rollback_path,
                    root_owned=root_owned,
                )
            return _validate_rollback_receipt(
                existing_rollback,
                authority=authority,
                preflight_sha256=expected_preflight_sha256,
                removed_units=removed_units,
                preserved_units=preserved_units,
                final_timer_states=final_timer_states,
            )


def _validate_terminal_receipt(
    value: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    preflight_sha256: str,
    activation_started_sha256: str,
    timers_active_sha256: str,
    legacy_cron_definition_sha256: str,
    legacy_collector_timer_state: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "created_at",
        "authority_sha256",
        "preflight_receipt_sha256",
        "activation_started_receipt_sha256",
        "timers_active_receipt_sha256",
        "package_manifest_sha256",
        "new_timer_count",
        "new_timers_enabled",
        "new_timers_active",
        "legacy_cron_job_id",
        "legacy_cron_state",
        "legacy_cron_definition_sha256",
        "legacy_scheduler_claimable",
        "legacy_collector_timer_unit",
        "legacy_collector_timer_state",
        "legacy_retired_after_new_timers_active",
        "new_candidate_may_replace_open_candidate",
        "auto_merge_or_deploy_enabled",
        "secret_material_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != TERMINAL_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("preflight_receipt_sha256") != preflight_sha256
        or value.get("activation_started_receipt_sha256")
        != activation_started_sha256
        or value.get("timers_active_receipt_sha256")
        != timers_active_sha256
        or value.get("package_manifest_sha256")
        != authority["package_manifest_sha256"]
        or value.get("new_timer_count") != len(TIMER_NAMES)
        or value.get("new_timers_enabled") is not True
        or value.get("new_timers_active") is not True
        or value.get("legacy_cron_job_id") != LEGACY_CRON_JOB_ID
        or value.get("legacy_cron_state") != "retired"
        or value.get("legacy_cron_definition_sha256")
        != legacy_cron_definition_sha256
        or value.get("legacy_scheduler_claimable") is not False
        or value.get("legacy_collector_timer_unit")
        != LEGACY_COLLECTOR_TIMER_UNIT
        or value.get("legacy_collector_timer_state")
        != legacy_collector_timer_state
        or value.get("legacy_retired_after_new_timers_active") is not True
        or value.get("new_candidate_may_replace_open_candidate")
        is not False
        or value.get("auto_merge_or_deploy_enabled") is not False
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            _canonical(
                {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_sha256"
                }
            )
        )
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_terminal_invalid"
        )
    return copy.deepcopy(dict(value))


def verify(
    *,
    expected_authority_sha256: str,
    staged_root: Path = STAGED_ROOT,
    authority_path: Path = AUTHORITY_PATH,
    catch_up_path: Path = FIRST_CATCH_UP_PATH,
    owner_authorization_path: Path = OWNER_AUTHORIZATION_PATH,
    owner_receipt_public_key_path: Path = OWNER_RECEIPT_PUBLIC_KEY_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    runtime_path: Path | None = None,
    jobs_path: Path = JOBS_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    authorization_now_unix: int | None = None,
    timer_observer: Callable[[str], TimerObservation] = observe_timer,
) -> dict[str, Any]:
    authority_value = _read_canonical_json(
        authority_path,
        root_owned=root_owned,
    )
    provisional = validate_activation_authority(
        authority_value,
        expected_sha256=expected_authority_sha256,
    )
    selected_runtime = runtime_path or (
        rail.release_root(provisional["release_revision"])
        / CUTOVER_RUNTIME_RELATIVE
    )
    authority, package, _catch_up = _runtime_context(
        expected_authority_sha256=expected_authority_sha256,
        staged_root=staged_root,
        authority_path=authority_path,
        catch_up_path=catch_up_path,
        owner_authorization_path=owner_authorization_path,
        owner_receipt_public_key_path=owner_receipt_public_key_path,
        runtime_path=selected_runtime,
        root_owned=root_owned,
        authorization_now_unix=authorization_now_unix,
        authorization_must_be_current=False,
    )
    staged_preflight = _read_canonical_json(
        preflight_path,
        root_owned=root_owned,
    )
    preflight_sha256 = staged_preflight.get("receipt_sha256")
    if (
        not isinstance(preflight_sha256, str)
        or _SHA256.fullmatch(preflight_sha256) is None
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_preflight_invalid"
        )
    _validate_preflight(
        staged_preflight,
        authority=authority,
        expected_sha256=preflight_sha256,
    )
    activation_started = _validate_activation_started_receipt(
        _read_canonical_json(
            _evidence_path(
                authority["authority_sha256"],
                "activation-started.json",
                evidence_root=evidence_root,
            ),
            root_owned=root_owned,
            modes=frozenset({0o600}),
        ),
        authority=authority,
        expected_preflight_sha256=preflight_sha256,
    )
    _prove_new_timers_active(
        package,
        timer_observer=timer_observer,
        systemd_root=systemd_root,
    )
    jobs_raw, _metadata = _read_regular(
        jobs_path,
        maximum=MAX_JSON_BYTES,
    )
    cron_state, _index, job = _validate_legacy_job(
        _parse_jobs(jobs_raw),
        authority,
        allow_retired=True,
    )
    legacy = timer_observer(LEGACY_COLLECTOR_TIMER_UNIT)
    legacy_state = _validate_legacy_timer(
        legacy,
        authority,
        allow_retired=True,
    )
    if cron_state != "retired" or legacy_state != "retired":
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_activation_terminal_unconfirmed"
        )
    timers_active = _validate_timers_active_receipt(
        _read_canonical_json(
            _evidence_path(
                authority["authority_sha256"],
                "timers-active.json",
                evidence_root=evidence_root,
            ),
            root_owned=root_owned,
            modes=frozenset({0o600}),
        ),
        authority=authority,
        expected_preflight_sha256=preflight_sha256,
        activation_started_sha256=(
            activation_started["receipt_sha256"]
        ),
    )
    terminal = _validate_terminal_receipt(
        _read_canonical_json(
        _evidence_path(
            authority["authority_sha256"],
            "terminal.json",
            evidence_root=evidence_root,
        ),
            root_owned=root_owned,
            modes=frozenset({0o600}),
        ),
        authority=authority,
        preflight_sha256=preflight_sha256,
        activation_started_sha256=(
            activation_started["receipt_sha256"]
        ),
        timers_active_sha256=timers_active["receipt_sha256"],
        legacy_cron_definition_sha256=_static_definition_sha256(job),
        legacy_collector_timer_state=(
            "absent" if not legacy.loaded else "disabled_inactive"
        ),
    )
    return terminal


def _write_stdout(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(_canonical(value) + b"\n")
    sys.stdout.buffer.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("preflight", "activate", "rollback-inert", "verify"),
    )
    parser.add_argument("--expected-authority-sha256", required=True)
    parser.add_argument("--expected-preflight-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if (
        _SHA256.fullmatch(arguments.expected_authority_sha256 or "")
        is None
    ):
        raise UpstreamSyncRailCutoverError(
            "upstream_sync_authority_identity_invalid"
        )
    if arguments.operation == "preflight":
        _write_stdout(
            preflight(
                expected_authority_sha256=(
                    arguments.expected_authority_sha256
                )
            )
        )
        return 0
    if arguments.operation in {"activate", "rollback-inert"}:
        if (
            _SHA256.fullmatch(arguments.expected_preflight_sha256 or "")
            is None
        ):
            raise UpstreamSyncRailCutoverError(
                "upstream_sync_preflight_identity_invalid"
            )
        operation = (
            activate
            if arguments.operation == "activate"
            else rollback_inert
        )
        _write_stdout(
            operation(
                expected_authority_sha256=arguments.expected_authority_sha256,
                expected_preflight_sha256=arguments.expected_preflight_sha256,
            )
        )
        return 0
    if arguments.operation == "verify":
        _write_stdout(
            verify(
                expected_authority_sha256=(
                    arguments.expected_authority_sha256
                )
            )
        )
        return 0
    raise UpstreamSyncRailCutoverError(
        "upstream_sync_cutover_operation_invalid"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpstreamSyncRailCutoverError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


__all__ = [
    "ACTIVATION_STARTED_SCHEMA",
    "AUTHORITY_SCHEMA",
    "FIRST_CATCH_UP_SCHEMA",
    "LEGACY_COLLECTOR_TIMER_UNIT",
    "LEGACY_CRON_JOB_ID",
    "OPERATION_ID",
    "PREFLIGHT_SCHEMA",
    "PackageContext",
    "ROLLBACK_SCHEMA",
    "TERMINAL_SCHEMA",
    "TIMERS_ACTIVE_SCHEMA",
    "TimerObservation",
    "UpstreamSyncRailCutoverError",
    "activate",
    "build_activation_authority",
    "build_first_catch_up_receipt",
    "observe_timer",
    "preflight",
    "rollback_inert",
    "validate_activation_authority",
    "validate_first_catch_up_receipt",
    "verify",
]
