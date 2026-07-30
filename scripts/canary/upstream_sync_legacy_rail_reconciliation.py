#!/usr/bin/env python3
"""Exact, forward-only reconciliation of the one known legacy sync rail.

This is deliberately a one-time narrow edge, not a generic unit cleanup tool.
It recognizes only:

* the four files from one validated staged dual-rail package;
* the two timer units belonging to that package;
* Hermes cron job ``06ef64d72891``, which is observed but never changed; and
* the exact stale pointer for merged fork PR 95.

``plan`` and ``preflight`` are read-only.  ``reconcile`` requires the reviewed
self-hashes of both artifacts, acquires the shared authority lock and the cron
store lock, records durable evidence, disables only the two fixed timers,
archives and removes only exact reviewed bytes, and proves the cron store is
byte-for-byte unchanged.  An interrupted transaction recovers forward.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import upstream_sync_rail_cutover as cutover
from scripts.canary.production_cutover_activation_lock import (
    AuthorityActivationLockError,
    authority_activation_lock,
)


PLAN_SCHEMA = "muncho-legacy-dual-sync-reconciliation-plan.v1"
PREFLIGHT_SCHEMA = "muncho-legacy-dual-sync-reconciliation-preflight.v1"
STARTED_SCHEMA = "muncho-legacy-dual-sync-reconciliation-started.v1"
TERMINAL_SCHEMA = "muncho-legacy-dual-sync-reconciliation-terminal.v1"

STAGED_ROOT = rail.PACKAGE_ROOT
JOBS_PATH = cutover.JOBS_PATH
SYSTEMD_ROOT = cutover.SYSTEMD_ROOT
POINTER_PATH = Path(
    "/opt/adventico-ai-platform/canonical-brain/state/private/"
    "upstream_sync_monitor/auto-sync-pr-state.json"
)
PLAN_PATH = STAGED_ROOT / "legacy-rail-reconciliation-plan.json"
PREFLIGHT_PATH = STAGED_ROOT / "legacy-rail-reconciliation-preflight.json"
EVIDENCE_ROOT = Path(
    "/var/lib/muncho-production-legacy-cutover/dual-upstream-sync-legacy-reconciliation"
)
SYSTEMCTL = Path("/usr/bin/systemctl")

UNIT_NAMES = cutover.UNIT_NAMES
TIMER_NAMES = cutover.TIMER_NAMES
SERVICE_NAMES = (
    rail.SYNC_SERVICE_UNIT,
    rail.REPORT_SERVICE_UNIT,
)

EXACT_STALE_POINTER: Mapping[str, Any] = {
    "automation_owned": True,
    "branch": "codex/upstream-sync-auto-20260711-2100",
    "created_at_utc": "2026-07-11T21:00:24Z",
    "head": "44076d4a2be485b1e27a604759a396e01eb913e7",
    "pr_number": 95,
    "pr_url": "https://github.com/lomliev/hermes-agent/pull/95",
}
EXACT_MERGED_PR_EVIDENCE: Mapping[str, Any] = {
    "repository": "lomliev/hermes-agent",
    "pr_number": 95,
    "pr_url": "https://github.com/lomliev/hermes-agent/pull/95",
    "state": "MERGED",
    "head_ref_name": "codex/upstream-sync-auto-20260711-2100",
    "head_ref_oid": "44076d4a2be485b1e27a604759a396e01eb913e7",
    "base_ref_name": "main",
    "merge_commit_oid": "cbfbcde43a521a76fd4af0f3558e9745c68fe531",
    "merged_at": "2026-07-11T21:07:07Z",
    "same_repository": True,
}

MAX_JSON_BYTES = cutover.MAX_JSON_BYTES
MAX_UNIT_BYTES = 2 * 1024 * 1024
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class LegacyRailReconciliationError(RuntimeError):
    """Stable, secret-free legacy reconciliation failure."""


@dataclass(frozen=True)
class UnitObservation:
    unit: str
    loaded: bool
    active: bool
    unit_file_state: str
    main_pid: int
    fragment_path: str | None
    fragment_sha256: str | None

    @classmethod
    def absent(cls, unit: str) -> "UnitObservation":
        return cls(
            unit=unit,
            loaded=False,
            active=False,
            unit_file_state="not-found",
            main_pid=0,
            fragment_path=None,
            fragment_sha256=None,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "loaded": self.loaded,
            "active": self.active,
            "unit_file_state": self.unit_file_state,
            "main_pid": self.main_pid,
            "fragment_path": self.fragment_path,
            "fragment_sha256": self.fragment_sha256,
        }


@dataclass(frozen=True)
class LegacyPackage:
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, bytes]


def _now() -> str:
    return (
        datetime
        .now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LegacyRailReconciliationError("legacy_rail_json_invalid") from exc
    if not 0 < len(raw) <= MAX_JSON_BYTES:
        raise LegacyRailReconciliationError("legacy_rail_json_size_invalid")
    return raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _self_hashed(
    unsigned: Mapping[str, Any],
    *,
    field: str,
) -> dict[str, Any]:
    value = copy.deepcopy(dict(unsigned))
    return {**value, field: _sha256(canonical(value))}


def _receipt(unsigned: Mapping[str, Any]) -> dict[str, Any]:
    return _self_hashed(unsigned, field="receipt_sha256")


def _decode_json(raw: bytes, *, canonical_required: bool) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise LegacyRailReconciliationError("legacy_rail_json_duplicate_key")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except LegacyRailReconciliationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise LegacyRailReconciliationError("legacy_rail_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise LegacyRailReconciliationError("legacy_rail_json_invalid")
    if canonical_required and canonical(value) != raw:
        raise LegacyRailReconciliationError("legacy_rail_json_not_canonical")
    return value


def _read_regular(
    path: Path,
    *,
    maximum: int,
    modes: frozenset[int] | None = None,
    root_owned: bool,
) -> tuple[bytes, os.stat_result]:
    try:
        return cutover._read_regular(
            path,
            maximum=maximum,
            modes=modes,
            root_owned=root_owned,
        )
    except cutover.UpstreamSyncRailCutoverError as exc:
        raise LegacyRailReconciliationError(
            "legacy_rail_file_identity_invalid"
        ) from exc


def _read_canonical(
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
        raise LegacyRailReconciliationError("legacy_rail_json_framing_invalid")
    return _decode_json(raw[:-1], canonical_required=True)


def _load_package(
    staged_root: Path,
    *,
    root_owned: bool,
) -> LegacyPackage:
    manifest = _read_canonical(
        staged_root / "manifest.json",
        root_owned=root_owned,
    )
    revision = manifest.get("release_revision")
    sender_revision = manifest.get("sender_revision")
    if (
        not isinstance(revision, str)
        or _SHA40.fullmatch(revision) is None
        or not isinstance(sender_revision, str)
        or _SHA40.fullmatch(sender_revision) is None
    ):
        raise LegacyRailReconciliationError("legacy_rail_package_manifest_invalid")
    try:
        checked = rail.validate_manifest(
            manifest,
            revision=revision,
            sender_revision=sender_revision,
        )
    except rail.DualSyncRailError as exc:
        raise LegacyRailReconciliationError(
            "legacy_rail_package_manifest_invalid"
        ) from exc
    digests = checked.get("artifacts")
    if (
        not isinstance(digests, Mapping)
        or set(digests) != set(UNIT_NAMES)
        or any(
            not isinstance(digests.get(name), str)
            or _SHA256.fullmatch(digests[name]) is None
            for name in UNIT_NAMES
        )
    ):
        raise LegacyRailReconciliationError("legacy_rail_package_artifacts_invalid")
    artifacts: dict[str, bytes] = {}
    for name in UNIT_NAMES:
        raw, _metadata = _read_regular(
            staged_root / name,
            maximum=MAX_UNIT_BYTES,
            modes=frozenset({0o400, 0o440, 0o444}),
            root_owned=root_owned,
        )
        if _sha256(raw) != digests[name]:
            raise LegacyRailReconciliationError("legacy_rail_package_artifacts_invalid")
        artifacts[name] = raw
    return LegacyPackage(
        manifest=copy.deepcopy(dict(checked)),
        artifacts=artifacts,
    )


def _systemctl_capture(*arguments: str) -> tuple[int, bytes]:
    try:
        completed = subprocess.run(
            [str(SYSTEMCTL), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LegacyRailReconciliationError("legacy_rail_systemd_unavailable") from exc
    return completed.returncode, completed.stdout


def observe_unit(unit: str) -> UnitObservation:
    """Observe one of the four fixed units without interpreting free text."""

    if unit not in UNIT_NAMES:
        raise LegacyRailReconciliationError("legacy_rail_unit_identity_invalid")
    code, raw = _systemctl_capture(
        "show",
        "--no-pager",
        "--property=Id",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=UnitFileState",
        "--property=MainPID",
        "--property=FragmentPath",
        unit,
    )
    if code != 0:
        raise LegacyRailReconciliationError("legacy_rail_systemd_observation_failed")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
        values = dict(line.split("=", 1) for line in lines if line)
        if len(values) != len([line for line in lines if line]):
            raise ValueError
        if set(values) != {
            "Id",
            "LoadState",
            "ActiveState",
            "UnitFileState",
            "MainPID",
            "FragmentPath",
        }:
            raise ValueError
        if values["Id"] != unit:
            raise ValueError
        main_pid = int(values["MainPID"], 10)
        if main_pid < 0:
            raise ValueError
    except (UnicodeError, ValueError) as exc:
        raise LegacyRailReconciliationError(
            "legacy_rail_systemd_observation_invalid"
        ) from exc
    if values["LoadState"] == "not-found":
        if (
            values["ActiveState"] != "inactive"
            or values["UnitFileState"] not in {"", "disabled", "not-found"}
            or main_pid != 0
            or values["FragmentPath"]
        ):
            raise LegacyRailReconciliationError(
                "legacy_rail_systemd_observation_invalid"
            )
        return UnitObservation.absent(unit)
    if (
        values["LoadState"] != "loaded"
        or values["ActiveState"] not in {"active", "inactive"}
        or values["UnitFileState"] not in {"enabled", "disabled", "static"}
        or not values["FragmentPath"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_systemd_observation_invalid")
    fragment_raw, _metadata = _read_regular(
        Path(values["FragmentPath"]),
        maximum=MAX_UNIT_BYTES,
        modes=frozenset({0o644}),
        root_owned=True,
    )
    return UnitObservation(
        unit=unit,
        loaded=True,
        active=values["ActiveState"] == "active",
        unit_file_state=values["UnitFileState"],
        main_pid=main_pid,
        fragment_path=values["FragmentPath"],
        fragment_sha256=_sha256(fragment_raw),
    )


def systemctl_mutate(*arguments: str) -> None:
    """Apply only the fixed two-timer disable or a daemon reload."""

    allowed = arguments == ("daemon-reload",) or (
        len(arguments) == 3
        and arguments[:2] == ("disable", "--now")
        and arguments[2] in TIMER_NAMES
    )
    if not allowed:
        raise LegacyRailReconciliationError(
            "legacy_rail_systemd_mutation_scope_invalid"
        )
    code, output = _systemctl_capture(*arguments)
    if code != 0 or output not in {b"", b"\n"}:
        raise LegacyRailReconciliationError("legacy_rail_systemd_mutation_failed")


def _pointer(
    pointer_path: Path,
    *,
    root_owned: bool,
) -> tuple[bytes, Mapping[str, Any]]:
    raw, _metadata = _read_regular(
        pointer_path,
        maximum=MAX_JSON_BYTES,
        modes=frozenset({0o400, 0o440, 0o444, 0o600, 0o640, 0o644}),
        root_owned=root_owned,
    )
    value = _decode_json(
        raw[:-1] if raw.endswith(b"\n") else raw,
        canonical_required=False,
    )
    if dict(value) != dict(EXACT_STALE_POINTER):
        raise LegacyRailReconciliationError("legacy_rail_pointer_identity_invalid")
    ledger = pointer_path.with_name(f"{pointer_path.name}.ledger")
    if os.path.lexists(ledger):
        raise LegacyRailReconciliationError("legacy_rail_pointer_ledger_present")
    return raw, copy.deepcopy(dict(value))


def _cron(
    jobs_path: Path,
    *,
    root_owned: bool,
) -> tuple[bytes, str]:
    # Match the scheduler's existing continuity boundary: the fixed cron path
    # is protected by its own lock and may legitimately be owned by the
    # service account rather than root.  Its exact bytes are hash-bound and
    # rechecked under that lock; ``root_owned`` applies to privileged staged,
    # unit, pointer, plan, and evidence files.
    _ = root_owned
    raw, _metadata = _read_regular(
        jobs_path,
        maximum=MAX_JSON_BYTES,
        root_owned=False,
    )
    try:
        payload = cutover._parse_jobs(raw)
        _index, job = cutover._legacy_job(payload)
        definition_sha256 = cutover._static_definition_sha256(job)
    except cutover.UpstreamSyncRailCutoverError as exc:
        raise LegacyRailReconciliationError("legacy_rail_cron_invalid") from exc
    if (
        job.get("id") != cutover.LEGACY_CRON_JOB_ID
        or job.get("enabled") is not True
        or job.get("state") != "scheduled"
        or job.get("fire_claim") is not None
        or job.get("run_claim") is not None
    ):
        raise LegacyRailReconciliationError("legacy_rail_cron_not_quiescent")
    return raw, definition_sha256


def _live_unit_bytes(
    package: LegacyPackage,
    *,
    systemd_root: Path,
    root_owned: bool,
    allow_absent: bool,
) -> dict[str, bytes | None]:
    values: dict[str, bytes | None] = {}
    for name in UNIT_NAMES:
        path = systemd_root / name
        if not os.path.lexists(path):
            if not allow_absent:
                raise LegacyRailReconciliationError("legacy_rail_unit_file_missing")
            values[name] = None
            continue
        raw, metadata = _read_regular(
            path,
            maximum=MAX_UNIT_BYTES,
            modes=frozenset({0o644}),
            root_owned=root_owned,
        )
        if raw != package.artifacts[name] or stat.S_IMODE(metadata.st_mode) != 0o644:
            raise LegacyRailReconciliationError("legacy_rail_unit_file_drifted")
        values[name] = raw
    return values


def _observe_units(
    package: LegacyPackage,
    *,
    systemd_root: Path,
    unit_observer: Callable[[str], UnitObservation],
    state: str,
) -> dict[str, UnitObservation]:
    observed = {name: unit_observer(name) for name in UNIT_NAMES}
    if set(observed) != set(UNIT_NAMES):
        raise LegacyRailReconciliationError("legacy_rail_systemd_observation_invalid")
    for name, item in observed.items():
        if item.unit != name:
            raise LegacyRailReconciliationError(
                "legacy_rail_systemd_observation_invalid"
            )
        if state == "final":
            if item != UnitObservation.absent(name):
                raise LegacyRailReconciliationError(
                    "legacy_rail_final_unit_state_invalid"
                )
            continue
        if not item.loaded:
            if state == "recovery":
                continue
            raise LegacyRailReconciliationError("legacy_rail_unit_not_loaded")
        if (
            item.fragment_path != str(systemd_root / name)
            or item.fragment_sha256 != package.manifest["artifacts"][name]
        ):
            raise LegacyRailReconciliationError("legacy_rail_unit_observation_drifted")
        if name in SERVICE_NAMES:
            if item.active or item.main_pid != 0 or item.unit_file_state != "static":
                raise LegacyRailReconciliationError("legacy_rail_service_not_quiescent")
            continue
        if item.main_pid != 0:
            raise LegacyRailReconciliationError("legacy_rail_timer_prestate_invalid")
        if state == "initial":
            if not item.active or item.unit_file_state != "enabled":
                raise LegacyRailReconciliationError(
                    "legacy_rail_timer_prestate_invalid"
                )
        elif (item.active and item.unit_file_state != "enabled") or (
            not item.active and item.unit_file_state not in {"enabled", "disabled"}
        ):
            raise LegacyRailReconciliationError(
                "legacy_rail_timer_recovery_state_invalid"
            )
    return observed


def _plan_fields() -> set[str]:
    return {
        "schema",
        "created_at",
        "staged_package_root",
        "package_release_revision",
        "package_sender_revision",
        "package_manifest_sha256",
        "unit_file_paths",
        "unit_digests",
        "initial_unit_states",
        "timer_unit_names",
        "service_unit_names",
        "legacy_cron_path",
        "legacy_cron_job_id",
        "legacy_cron_jobs_sha256",
        "legacy_cron_static_definition_sha256",
        "stale_pointer_path",
        "stale_pointer_sha256",
        "stale_pointer",
        "merged_pr_evidence",
        "pointer_ledger_required_absent",
        "legacy_cron_preserved_exactly",
        "systemctl_mutation_scope",
        "removal_scope",
        "merge_or_deploy_performed",
        "forward_recovery_only",
        "secret_material_recorded",
        "plan_sha256",
    }


def validate_plan(
    value: Mapping[str, Any],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _plan_fields()
        or value.get("schema") != PLAN_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or not isinstance(value.get("package_release_revision"), str)
        or _SHA40.fullmatch(value["package_release_revision"]) is None
        or not isinstance(value.get("package_sender_revision"), str)
        or _SHA40.fullmatch(value["package_sender_revision"]) is None
        or not isinstance(value.get("package_manifest_sha256"), str)
        or _SHA256.fullmatch(value["package_manifest_sha256"]) is None
        or value.get("timer_unit_names") != list(TIMER_NAMES)
        or value.get("service_unit_names") != list(SERVICE_NAMES)
        or value.get("legacy_cron_job_id") != cutover.LEGACY_CRON_JOB_ID
        or not isinstance(value.get("legacy_cron_jobs_sha256"), str)
        or _SHA256.fullmatch(value["legacy_cron_jobs_sha256"]) is None
        or not isinstance(
            value.get("legacy_cron_static_definition_sha256"),
            str,
        )
        or _SHA256.fullmatch(value["legacy_cron_static_definition_sha256"]) is None
        or not isinstance(value.get("stale_pointer_sha256"), str)
        or _SHA256.fullmatch(value["stale_pointer_sha256"]) is None
        or value.get("stale_pointer") != EXACT_STALE_POINTER
        or value.get("merged_pr_evidence") != EXACT_MERGED_PR_EVIDENCE
        or value.get("pointer_ledger_required_absent") is not True
        or value.get("legacy_cron_preserved_exactly") is not True
        or value.get("systemctl_mutation_scope")
        != [["disable", "--now", name] for name in TIMER_NAMES] + [["daemon-reload"]]
        or value.get("merge_or_deploy_performed") is not False
        or value.get("forward_recovery_only") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("plan_sha256") != expected_sha256
        or _SHA256.fullmatch(expected_sha256 or "") is None
        or _sha256(
            canonical({
                key: item for key, item in value.items() if key != "plan_sha256"
            })
        )
        != expected_sha256
    ):
        raise LegacyRailReconciliationError("legacy_rail_plan_invalid")
    unit_paths = value.get("unit_file_paths")
    unit_digests = value.get("unit_digests")
    initial_states = value.get("initial_unit_states")
    if (
        not isinstance(unit_paths, Mapping)
        or set(unit_paths) != set(UNIT_NAMES)
        or not isinstance(unit_digests, Mapping)
        or set(unit_digests) != set(UNIT_NAMES)
        or any(
            not isinstance(unit_paths.get(name), str)
            or not unit_paths[name].endswith(f"/{name}")
            or not isinstance(unit_digests.get(name), str)
            or _SHA256.fullmatch(unit_digests[name]) is None
            for name in UNIT_NAMES
        )
        or not isinstance(initial_states, Mapping)
        or set(initial_states) != set(UNIT_NAMES)
        or value.get("removal_scope")
        != [unit_paths[name] for name in UNIT_NAMES] + [value.get("stale_pointer_path")]
        or any(
            initial_states.get(name)
            != UnitObservation(
                unit=name,
                loaded=True,
                active=name in TIMER_NAMES,
                unit_file_state=("enabled" if name in TIMER_NAMES else "static"),
                main_pid=0,
                fragment_path=unit_paths[name],
                fragment_sha256=unit_digests[name],
            ).as_json()
            for name in UNIT_NAMES
        )
    ):
        raise LegacyRailReconciliationError("legacy_rail_plan_invalid")
    return copy.deepcopy(dict(value))


def _validate_runtime_paths(
    plan: Mapping[str, Any],
    *,
    staged_root: Path,
    jobs_path: Path,
    pointer_path: Path,
    systemd_root: Path,
) -> None:
    if (
        plan["staged_package_root"] != str(staged_root)
        or plan["legacy_cron_path"] != str(jobs_path)
        or plan["stale_pointer_path"] != str(pointer_path)
        or plan["unit_file_paths"]
        != {name: str(systemd_root / name) for name in UNIT_NAMES}
    ):
        raise LegacyRailReconciliationError("legacy_rail_runtime_path_drifted")


def _initial_state(
    plan: Mapping[str, Any],
    *,
    staged_root: Path,
    jobs_path: Path,
    pointer_path: Path,
    systemd_root: Path,
    root_owned: bool,
    unit_observer: Callable[[str], UnitObservation],
) -> tuple[LegacyPackage, bytes, bytes]:
    _validate_runtime_paths(
        plan,
        staged_root=staged_root,
        jobs_path=jobs_path,
        pointer_path=pointer_path,
        systemd_root=systemd_root,
    )
    package = _load_package(staged_root, root_owned=root_owned)
    if (
        package.manifest["release_revision"] != plan["package_release_revision"]
        or package.manifest["sender_revision"] != plan["package_sender_revision"]
        or package.manifest["manifest_sha256"] != plan["package_manifest_sha256"]
        or package.manifest["artifacts"] != plan["unit_digests"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_package_drifted")
    _live_unit_bytes(
        package,
        systemd_root=systemd_root,
        root_owned=root_owned,
        allow_absent=False,
    )
    observed = _observe_units(
        package,
        systemd_root=systemd_root,
        unit_observer=unit_observer,
        state="initial",
    )
    if {name: item.as_json() for name, item in observed.items()} != plan[
        "initial_unit_states"
    ]:
        raise LegacyRailReconciliationError("legacy_rail_unit_state_drifted")
    jobs_raw, definition_sha256 = _cron(
        jobs_path,
        root_owned=root_owned,
    )
    if (
        _sha256(jobs_raw) != plan["legacy_cron_jobs_sha256"]
        or definition_sha256 != plan["legacy_cron_static_definition_sha256"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_cron_drifted")
    pointer_raw, pointer = _pointer(
        pointer_path,
        root_owned=root_owned,
    )
    if (
        pointer != plan["stale_pointer"]
        or _sha256(pointer_raw) != plan["stale_pointer_sha256"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_pointer_drifted")
    return package, jobs_raw, pointer_raw


@contextmanager
def _locks(
    *,
    jobs_path: Path,
    require_root: bool,
    activation_lock_factory: Callable[[], Any] | None,
) -> Iterator[None]:
    try:
        with authority_activation_lock(
            require_root=require_root,
            lock_factory=activation_lock_factory,
        ):
            with _cron_jobs_lock(jobs_path):
                yield
    except LegacyRailReconciliationError:
        raise
    except (
        AuthorityActivationLockError,
        cutover.UpstreamSyncRailCutoverError,
    ) as exc:
        raise LegacyRailReconciliationError("legacy_rail_lock_unavailable") from exc


_cron_jobs_lock = cutover._cron_jobs_lock


def build_plan(
    *,
    staged_root: Path = STAGED_ROOT,
    jobs_path: Path = JOBS_PATH,
    pointer_path: Path = POINTER_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    unit_observer: Callable[[str], UnitObservation] = observe_unit,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if require_root and cutover._effective_uid() != 0:
        raise LegacyRailReconciliationError("legacy_rail_root_required")
    with _locks(
        jobs_path=jobs_path,
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
    ):
        package = _load_package(staged_root, root_owned=root_owned)
        _live_unit_bytes(
            package,
            systemd_root=systemd_root,
            root_owned=root_owned,
            allow_absent=False,
        )
        observations = _observe_units(
            package,
            systemd_root=systemd_root,
            unit_observer=unit_observer,
            state="initial",
        )
        jobs_raw, definition_sha256 = _cron(
            jobs_path,
            root_owned=root_owned,
        )
        pointer_raw, pointer = _pointer(
            pointer_path,
            root_owned=root_owned,
        )
        unit_paths = {name: str(systemd_root / name) for name in UNIT_NAMES}
        return _self_hashed(
            {
                "schema": PLAN_SCHEMA,
                "created_at": _now(),
                "staged_package_root": str(staged_root),
                "package_release_revision": package.manifest["release_revision"],
                "package_sender_revision": package.manifest["sender_revision"],
                "package_manifest_sha256": package.manifest["manifest_sha256"],
                "unit_file_paths": unit_paths,
                "unit_digests": copy.deepcopy(dict(package.manifest["artifacts"])),
                "initial_unit_states": {
                    name: observations[name].as_json() for name in UNIT_NAMES
                },
                "timer_unit_names": list(TIMER_NAMES),
                "service_unit_names": list(SERVICE_NAMES),
                "legacy_cron_path": str(jobs_path),
                "legacy_cron_job_id": cutover.LEGACY_CRON_JOB_ID,
                "legacy_cron_jobs_sha256": _sha256(jobs_raw),
                "legacy_cron_static_definition_sha256": (definition_sha256),
                "stale_pointer_path": str(pointer_path),
                "stale_pointer_sha256": _sha256(pointer_raw),
                "stale_pointer": pointer,
                "merged_pr_evidence": copy.deepcopy(dict(EXACT_MERGED_PR_EVIDENCE)),
                "pointer_ledger_required_absent": True,
                "legacy_cron_preserved_exactly": True,
                "systemctl_mutation_scope": [
                    ["disable", "--now", name] for name in TIMER_NAMES
                ]
                + [["daemon-reload"]],
                "removal_scope": [unit_paths[name] for name in UNIT_NAMES]
                + [str(pointer_path)],
                "merge_or_deploy_performed": False,
                "forward_recovery_only": True,
                "secret_material_recorded": False,
            },
            field="plan_sha256",
        )


def _preflight_fields() -> set[str]:
    return {
        "schema",
        "created_at",
        "plan_sha256",
        "package_manifest_sha256",
        "legacy_cron_jobs_sha256",
        "stale_pointer_sha256",
        "unit_digests",
        "timer_unit_names",
        "legacy_cron_preserved_exactly",
        "exact_initial_state_confirmed",
        "runtime_mutation_performed",
        "secret_material_recorded",
        "receipt_sha256",
    }


def validate_preflight(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    expected_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _preflight_fields()
        or value.get("schema") != PREFLIGHT_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("plan_sha256") != plan["plan_sha256"]
        or value.get("package_manifest_sha256") != plan["package_manifest_sha256"]
        or value.get("legacy_cron_jobs_sha256") != plan["legacy_cron_jobs_sha256"]
        or value.get("stale_pointer_sha256") != plan["stale_pointer_sha256"]
        or value.get("unit_digests") != plan["unit_digests"]
        or value.get("timer_unit_names") != list(TIMER_NAMES)
        or value.get("legacy_cron_preserved_exactly") is not True
        or value.get("exact_initial_state_confirmed") is not True
        or value.get("runtime_mutation_performed") is not False
        or value.get("secret_material_recorded") is not False
        or value.get("receipt_sha256") != expected_sha256
        or _SHA256.fullmatch(expected_sha256 or "") is None
        or _sha256(
            canonical({
                key: item for key, item in value.items() if key != "receipt_sha256"
            })
        )
        != expected_sha256
    ):
        raise LegacyRailReconciliationError("legacy_rail_preflight_invalid")
    return copy.deepcopy(dict(value))


def preflight(
    *,
    expected_plan_sha256: str,
    plan_path: Path = PLAN_PATH,
    staged_root: Path = STAGED_ROOT,
    jobs_path: Path = JOBS_PATH,
    pointer_path: Path = POINTER_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    unit_observer: Callable[[str], UnitObservation] = observe_unit,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if require_root and cutover._effective_uid() != 0:
        raise LegacyRailReconciliationError("legacy_rail_root_required")
    plan = validate_plan(
        _read_canonical(plan_path, root_owned=root_owned),
        expected_sha256=expected_plan_sha256,
    )
    with _locks(
        jobs_path=jobs_path,
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
    ):
        _initial_state(
            plan,
            staged_root=staged_root,
            jobs_path=jobs_path,
            pointer_path=pointer_path,
            systemd_root=systemd_root,
            root_owned=root_owned,
            unit_observer=unit_observer,
        )
        return _receipt({
            "schema": PREFLIGHT_SCHEMA,
            "created_at": _now(),
            "plan_sha256": plan["plan_sha256"],
            "package_manifest_sha256": plan["package_manifest_sha256"],
            "legacy_cron_jobs_sha256": plan["legacy_cron_jobs_sha256"],
            "stale_pointer_sha256": plan["stale_pointer_sha256"],
            "unit_digests": copy.deepcopy(dict(plan["unit_digests"])),
            "timer_unit_names": list(TIMER_NAMES),
            "legacy_cron_preserved_exactly": True,
            "exact_initial_state_confirmed": True,
            "runtime_mutation_performed": False,
            "secret_material_recorded": False,
        })


def _evidence_directory(
    plan_sha256: str,
    *,
    evidence_root: Path,
) -> Path:
    if _SHA256.fullmatch(plan_sha256 or "") is None:
        raise LegacyRailReconciliationError("legacy_rail_plan_identity_invalid")
    return evidence_root / plan_sha256


def _publish_exact(
    path: Path,
    raw: bytes,
    *,
    root_owned: bool,
) -> None:
    if os.path.lexists(path):
        observed, _metadata = _read_regular(
            path,
            maximum=max(MAX_JSON_BYTES, len(raw)),
            modes=frozenset({0o600}),
            root_owned=root_owned,
        )
        if observed != raw:
            raise LegacyRailReconciliationError("legacy_rail_evidence_drifted")
        return
    try:
        cutover._atomic_write(
            path,
            raw,
            mode=0o600,
            uid=0 if root_owned else cutover._effective_uid(),
            gid=0 if root_owned else cutover._effective_gid(),
        )
    except (OSError, cutover.UpstreamSyncRailCutoverError) as exc:
        raise LegacyRailReconciliationError(
            "legacy_rail_evidence_write_failed"
        ) from exc
    observed, _metadata = _read_regular(
        path,
        maximum=max(MAX_JSON_BYTES, len(raw)),
        modes=frozenset({0o600}),
        root_owned=root_owned,
    )
    if observed != raw:
        raise LegacyRailReconciliationError("legacy_rail_evidence_write_unconfirmed")


def _publish_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    root_owned: bool,
) -> dict[str, Any]:
    _publish_exact(
        path,
        canonical(value) + b"\n",
        root_owned=root_owned,
    )
    return copy.deepcopy(dict(value))


def _started_fields() -> set[str]:
    return {
        "schema",
        "created_at",
        "plan_sha256",
        "preflight_receipt_sha256",
        "package_manifest_sha256",
        "archived_unit_sha256",
        "archived_pointer_sha256",
        "legacy_cron_jobs_sha256",
        "timer_unit_names",
        "legacy_cron_mutation_performed",
        "merge_or_deploy_performed",
        "forward_recovery_only",
        "secret_material_recorded",
        "receipt_sha256",
    }


def _validate_started(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _started_fields()
        or value.get("schema") != STARTED_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or _UTC.fullmatch(value["created_at"]) is None
        or value.get("plan_sha256") != plan["plan_sha256"]
        or value.get("preflight_receipt_sha256") != preflight["receipt_sha256"]
        or value.get("package_manifest_sha256") != plan["package_manifest_sha256"]
        or value.get("archived_unit_sha256") != plan["unit_digests"]
        or value.get("archived_pointer_sha256") != plan["stale_pointer_sha256"]
        or value.get("legacy_cron_jobs_sha256") != plan["legacy_cron_jobs_sha256"]
        or value.get("timer_unit_names") != list(TIMER_NAMES)
        or value.get("legacy_cron_mutation_performed") is not False
        or value.get("merge_or_deploy_performed") is not False
        or value.get("forward_recovery_only") is not True
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            canonical({
                key: item for key, item in value.items() if key != "receipt_sha256"
            })
        )
    ):
        raise LegacyRailReconciliationError("legacy_rail_started_receipt_invalid")
    return copy.deepcopy(dict(value))


def _archive_paths(
    plan: Mapping[str, Any],
    *,
    evidence_root: Path,
) -> tuple[dict[str, Path], Path]:
    root = _evidence_directory(
        plan["plan_sha256"],
        evidence_root=evidence_root,
    )
    units = {name: root / "archives" / name for name in UNIT_NAMES}
    return units, root / "archives" / "auto-sync-pr-state.json.raw"


def _validate_archives(
    plan: Mapping[str, Any],
    *,
    evidence_root: Path,
    root_owned: bool,
) -> tuple[dict[str, bytes], bytes]:
    unit_paths, pointer_path = _archive_paths(
        plan,
        evidence_root=evidence_root,
    )
    units: dict[str, bytes] = {}
    for name, path in unit_paths.items():
        raw, _metadata = _read_regular(
            path,
            maximum=MAX_UNIT_BYTES,
            modes=frozenset({0o600}),
            root_owned=root_owned,
        )
        if _sha256(raw) != plan["unit_digests"][name]:
            raise LegacyRailReconciliationError("legacy_rail_archive_invalid")
        units[name] = raw
    pointer_raw, _metadata = _read_regular(
        pointer_path,
        maximum=MAX_JSON_BYTES,
        modes=frozenset({0o600}),
        root_owned=root_owned,
    )
    if _sha256(pointer_raw) != plan["stale_pointer_sha256"]:
        raise LegacyRailReconciliationError("legacy_rail_archive_invalid")
    return units, pointer_raw


def _remove_exact(
    path: Path,
    expected: bytes,
    *,
    root_owned: bool,
    maximum: int,
) -> None:
    if not os.path.lexists(path):
        return
    raw, metadata = _read_regular(
        path,
        maximum=maximum,
        root_owned=root_owned,
    )
    try:
        reached = path.lstat()
    except OSError as exc:
        raise LegacyRailReconciliationError(
            "legacy_rail_removal_target_changed"
        ) from exc
    if raw != expected or cutover._identity(metadata) != cutover._identity(reached):
        raise LegacyRailReconciliationError("legacy_rail_removal_target_changed")
    try:
        os.unlink(path)
    except OSError as exc:
        raise LegacyRailReconciliationError("legacy_rail_exact_removal_failed") from exc


def _cron_unchanged(
    plan: Mapping[str, Any],
    *,
    jobs_path: Path,
    root_owned: bool,
) -> None:
    raw, definition = _cron(jobs_path, root_owned=root_owned)
    if (
        _sha256(raw) != plan["legacy_cron_jobs_sha256"]
        or definition != plan["legacy_cron_static_definition_sha256"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_cron_drifted")


def _terminal_fields() -> set[str]:
    return {
        "schema",
        "completed_at",
        "plan_sha256",
        "preflight_receipt_sha256",
        "started_receipt_sha256",
        "package_manifest_sha256",
        "disabled_timer_units",
        "removed_unit_files",
        "removed_pointer",
        "merged_pr_evidence",
        "legacy_cron_job_id",
        "legacy_cron_jobs_sha256",
        "legacy_cron_preserved_exactly",
        "unit_files_absent",
        "unit_states_absent",
        "stale_pointer_absent",
        "pointer_ledger_absent",
        "merge_or_deploy_performed",
        "forward_recovery_only",
        "secret_material_recorded",
        "receipt_sha256",
    }


def _validate_terminal(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    started: Mapping[str, Any],
    expected_preflight_sha256: str | None,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _terminal_fields()
        or value.get("schema") != TERMINAL_SCHEMA
        or not isinstance(value.get("completed_at"), str)
        or _UTC.fullmatch(value["completed_at"]) is None
        or value.get("plan_sha256") != plan["plan_sha256"]
        or (
            expected_preflight_sha256 is not None
            and value.get("preflight_receipt_sha256") != expected_preflight_sha256
        )
        or value.get("started_receipt_sha256") != started["receipt_sha256"]
        or value.get("package_manifest_sha256") != plan["package_manifest_sha256"]
        or value.get("disabled_timer_units") != list(TIMER_NAMES)
        or value.get("removed_unit_files") != list(UNIT_NAMES)
        or value.get("removed_pointer") != plan["stale_pointer"]
        or value.get("merged_pr_evidence") != EXACT_MERGED_PR_EVIDENCE
        or value.get("legacy_cron_job_id") != cutover.LEGACY_CRON_JOB_ID
        or value.get("legacy_cron_jobs_sha256") != plan["legacy_cron_jobs_sha256"]
        or value.get("legacy_cron_preserved_exactly") is not True
        or value.get("unit_files_absent") is not True
        or value.get("unit_states_absent") is not True
        or value.get("stale_pointer_absent") is not True
        or value.get("pointer_ledger_absent") is not True
        or value.get("merge_or_deploy_performed") is not False
        or value.get("forward_recovery_only") is not True
        or value.get("secret_material_recorded") is not False
        or not isinstance(value.get("receipt_sha256"), str)
        or _SHA256.fullmatch(value["receipt_sha256"]) is None
        or value.get("receipt_sha256")
        != _sha256(
            canonical({
                key: item for key, item in value.items() if key != "receipt_sha256"
            })
        )
    ):
        raise LegacyRailReconciliationError("legacy_rail_terminal_receipt_invalid")
    return copy.deepcopy(dict(value))


def _poststate(
    plan: Mapping[str, Any],
    *,
    staged_root: Path,
    jobs_path: Path,
    pointer_path: Path,
    systemd_root: Path,
    root_owned: bool,
    unit_observer: Callable[[str], UnitObservation],
) -> None:
    _validate_runtime_paths(
        plan,
        staged_root=staged_root,
        jobs_path=jobs_path,
        pointer_path=pointer_path,
        systemd_root=systemd_root,
    )
    package = _load_package(staged_root, root_owned=root_owned)
    if (
        package.manifest["manifest_sha256"] != plan["package_manifest_sha256"]
        or package.manifest["artifacts"] != plan["unit_digests"]
    ):
        raise LegacyRailReconciliationError("legacy_rail_package_drifted")
    if any(os.path.lexists(systemd_root / name) for name in UNIT_NAMES):
        raise LegacyRailReconciliationError("legacy_rail_unit_file_still_present")
    _observe_units(
        package,
        systemd_root=systemd_root,
        unit_observer=unit_observer,
        state="final",
    )
    if os.path.lexists(pointer_path) or os.path.lexists(
        pointer_path.with_name(f"{pointer_path.name}.ledger")
    ):
        raise LegacyRailReconciliationError("legacy_rail_pointer_still_present")
    _cron_unchanged(
        plan,
        jobs_path=jobs_path,
        root_owned=root_owned,
    )


def reconcile(
    *,
    expected_plan_sha256: str,
    expected_preflight_sha256: str,
    plan_path: Path = PLAN_PATH,
    preflight_path: Path = PREFLIGHT_PATH,
    staged_root: Path = STAGED_ROOT,
    jobs_path: Path = JOBS_PATH,
    pointer_path: Path = POINTER_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    unit_observer: Callable[[str], UnitObservation] = observe_unit,
    systemctl_mutator: Callable[..., None] = systemctl_mutate,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if require_root and cutover._effective_uid() != 0:
        raise LegacyRailReconciliationError("legacy_rail_root_required")
    plan = validate_plan(
        _read_canonical(plan_path, root_owned=root_owned),
        expected_sha256=expected_plan_sha256,
    )
    preflight_value = _read_canonical(
        preflight_path,
        root_owned=root_owned,
    )
    preflight_receipt = validate_preflight(
        preflight_value,
        plan=plan,
        expected_sha256=expected_preflight_sha256,
    )
    evidence = _evidence_directory(
        plan["plan_sha256"],
        evidence_root=evidence_root,
    )
    started_path = evidence / "reconciliation-started.json"
    terminal_path = evidence / "terminal.json"
    with _locks(
        jobs_path=jobs_path,
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
    ):
        plan = validate_plan(
            _read_canonical(plan_path, root_owned=root_owned),
            expected_sha256=expected_plan_sha256,
        )
        preflight_receipt = validate_preflight(
            _read_canonical(
                preflight_path,
                root_owned=root_owned,
            ),
            plan=plan,
            expected_sha256=expected_preflight_sha256,
        )
        started_present = os.path.lexists(started_path)
        terminal_present = os.path.lexists(terminal_path)
        if terminal_present and not started_present:
            raise LegacyRailReconciliationError("legacy_rail_started_receipt_missing")
        if started_present:
            started = _validate_started(
                _read_canonical(
                    started_path,
                    root_owned=root_owned,
                    modes=frozenset({0o600}),
                ),
                plan=plan,
                preflight=preflight_receipt,
            )
            archived_units, archived_pointer = _validate_archives(
                plan,
                evidence_root=evidence_root,
                root_owned=root_owned,
            )
            if terminal_present:
                terminal = _validate_terminal(
                    _read_canonical(
                        terminal_path,
                        root_owned=root_owned,
                        modes=frozenset({0o600}),
                    ),
                    plan=plan,
                    started=started,
                    expected_preflight_sha256=(expected_preflight_sha256),
                )
                _poststate(
                    plan,
                    staged_root=staged_root,
                    jobs_path=jobs_path,
                    pointer_path=pointer_path,
                    systemd_root=systemd_root,
                    root_owned=root_owned,
                    unit_observer=unit_observer,
                )
                return terminal
            # An interruption may have removed a fragment before daemon reload.
            systemctl_mutator("daemon-reload")
            _cron_unchanged(
                plan,
                jobs_path=jobs_path,
                root_owned=root_owned,
            )
            live_units = _live_unit_bytes(
                _load_package(staged_root, root_owned=root_owned),
                systemd_root=systemd_root,
                root_owned=root_owned,
                allow_absent=True,
            )
            for name, raw in live_units.items():
                if raw is not None and raw != archived_units[name]:
                    raise LegacyRailReconciliationError("legacy_rail_unit_file_drifted")
            if os.path.lexists(pointer_path):
                current_pointer, value = _pointer(
                    pointer_path,
                    root_owned=root_owned,
                )
                if (
                    current_pointer != archived_pointer
                    or value != plan["stale_pointer"]
                ):
                    raise LegacyRailReconciliationError("legacy_rail_pointer_drifted")
            elif os.path.lexists(pointer_path.with_name(f"{pointer_path.name}.ledger")):
                raise LegacyRailReconciliationError(
                    "legacy_rail_pointer_ledger_present"
                )
        else:
            package, jobs_raw, pointer_raw = _initial_state(
                plan,
                staged_root=staged_root,
                jobs_path=jobs_path,
                pointer_path=pointer_path,
                systemd_root=systemd_root,
                root_owned=root_owned,
                unit_observer=unit_observer,
            )
            unit_archive_paths, pointer_archive_path = _archive_paths(
                plan,
                evidence_root=evidence_root,
            )
            for name, archive_path in unit_archive_paths.items():
                _publish_exact(
                    archive_path,
                    package.artifacts[name],
                    root_owned=root_owned,
                )
            _publish_exact(
                pointer_archive_path,
                pointer_raw,
                root_owned=root_owned,
            )
            archived_units, archived_pointer = _validate_archives(
                plan,
                evidence_root=evidence_root,
                root_owned=root_owned,
            )
            if _sha256(jobs_raw) != plan["legacy_cron_jobs_sha256"]:
                raise LegacyRailReconciliationError("legacy_rail_cron_drifted")
            started = _publish_json(
                started_path,
                _receipt({
                    "schema": STARTED_SCHEMA,
                    "created_at": _now(),
                    "plan_sha256": plan["plan_sha256"],
                    "preflight_receipt_sha256": (preflight_receipt["receipt_sha256"]),
                    "package_manifest_sha256": plan["package_manifest_sha256"],
                    "archived_unit_sha256": copy.deepcopy(dict(plan["unit_digests"])),
                    "archived_pointer_sha256": plan["stale_pointer_sha256"],
                    "legacy_cron_jobs_sha256": plan["legacy_cron_jobs_sha256"],
                    "timer_unit_names": list(TIMER_NAMES),
                    "legacy_cron_mutation_performed": False,
                    "merge_or_deploy_performed": False,
                    "forward_recovery_only": True,
                    "secret_material_recorded": False,
                }),
                root_owned=root_owned,
            )
        package = _load_package(staged_root, root_owned=root_owned)
        current = _observe_units(
            package,
            systemd_root=systemd_root,
            unit_observer=unit_observer,
            state="recovery",
        )
        for name in TIMER_NAMES:
            item = current[name]
            if item.loaded and (item.active or item.unit_file_state != "disabled"):
                systemctl_mutator("disable", "--now", name)
        after_disable = {name: unit_observer(name) for name in TIMER_NAMES}
        for name, item in after_disable.items():
            if item.loaded and (
                item.active
                or item.unit_file_state != "disabled"
                or item.main_pid != 0
                or item.fragment_sha256 != plan["unit_digests"][name]
            ):
                raise LegacyRailReconciliationError(
                    "legacy_rail_timer_disable_unconfirmed"
                )
        for name in SERVICE_NAMES:
            item = unit_observer(name)
            if item.loaded and (item.active or item.main_pid != 0):
                raise LegacyRailReconciliationError("legacy_rail_service_not_quiescent")
        _cron_unchanged(
            plan,
            jobs_path=jobs_path,
            root_owned=root_owned,
        )
        for name in UNIT_NAMES:
            _remove_exact(
                systemd_root / name,
                archived_units[name],
                root_owned=root_owned,
                maximum=MAX_UNIT_BYTES,
            )
        _remove_exact(
            pointer_path,
            archived_pointer,
            root_owned=root_owned,
            maximum=MAX_JSON_BYTES,
        )
        systemctl_mutator("daemon-reload")
        _poststate(
            plan,
            staged_root=staged_root,
            jobs_path=jobs_path,
            pointer_path=pointer_path,
            systemd_root=systemd_root,
            root_owned=root_owned,
            unit_observer=unit_observer,
        )
        terminal = _receipt({
            "schema": TERMINAL_SCHEMA,
            "completed_at": _now(),
            "plan_sha256": plan["plan_sha256"],
            "preflight_receipt_sha256": (preflight_receipt["receipt_sha256"]),
            "started_receipt_sha256": started["receipt_sha256"],
            "package_manifest_sha256": plan["package_manifest_sha256"],
            "disabled_timer_units": list(TIMER_NAMES),
            "removed_unit_files": list(UNIT_NAMES),
            "removed_pointer": copy.deepcopy(dict(EXACT_STALE_POINTER)),
            "merged_pr_evidence": copy.deepcopy(dict(EXACT_MERGED_PR_EVIDENCE)),
            "legacy_cron_job_id": cutover.LEGACY_CRON_JOB_ID,
            "legacy_cron_jobs_sha256": plan["legacy_cron_jobs_sha256"],
            "legacy_cron_preserved_exactly": True,
            "unit_files_absent": True,
            "unit_states_absent": True,
            "stale_pointer_absent": True,
            "pointer_ledger_absent": True,
            "merge_or_deploy_performed": False,
            "forward_recovery_only": True,
            "secret_material_recorded": False,
        })
        return _publish_json(
            terminal_path,
            terminal,
            root_owned=root_owned,
        )


def verify(
    *,
    expected_plan_sha256: str,
    plan_path: Path = PLAN_PATH,
    staged_root: Path = STAGED_ROOT,
    jobs_path: Path = JOBS_PATH,
    pointer_path: Path = POINTER_PATH,
    systemd_root: Path = SYSTEMD_ROOT,
    evidence_root: Path = EVIDENCE_ROOT,
    root_owned: bool = True,
    require_root: bool = True,
    unit_observer: Callable[[str], UnitObservation] = observe_unit,
    activation_lock_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if require_root and cutover._effective_uid() != 0:
        raise LegacyRailReconciliationError("legacy_rail_root_required")
    plan = validate_plan(
        _read_canonical(plan_path, root_owned=root_owned),
        expected_sha256=expected_plan_sha256,
    )
    evidence = _evidence_directory(
        plan["plan_sha256"],
        evidence_root=evidence_root,
    )
    with _locks(
        jobs_path=jobs_path,
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
    ):
        started = _validate_started(
            _read_canonical(
                evidence / "reconciliation-started.json",
                root_owned=root_owned,
                modes=frozenset({0o600}),
            ),
            plan=plan,
            preflight={
                "receipt_sha256": _read_canonical(
                    evidence / "terminal.json",
                    root_owned=root_owned,
                    modes=frozenset({0o600}),
                )["preflight_receipt_sha256"]
            },
        )
        terminal = _validate_terminal(
            _read_canonical(
                evidence / "terminal.json",
                root_owned=root_owned,
                modes=frozenset({0o600}),
            ),
            plan=plan,
            started=started,
            expected_preflight_sha256=None,
        )
        _validate_archives(
            plan,
            evidence_root=evidence_root,
            root_owned=root_owned,
        )
        _poststate(
            plan,
            staged_root=staged_root,
            jobs_path=jobs_path,
            pointer_path=pointer_path,
            systemd_root=systemd_root,
            root_owned=root_owned,
            unit_observer=unit_observer,
        )
        return terminal


def _write_stdout(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical(value) + b"\n")
    sys.stdout.buffer.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("plan", "preflight", "reconcile", "verify"),
    )
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-preflight-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.operation == "plan":
        _write_stdout(build_plan())
        return 0
    if _SHA256.fullmatch(arguments.expected_plan_sha256 or "") is None:
        raise LegacyRailReconciliationError("legacy_rail_plan_identity_invalid")
    if arguments.operation == "preflight":
        _write_stdout(preflight(expected_plan_sha256=arguments.expected_plan_sha256))
        return 0
    if arguments.operation == "reconcile":
        if _SHA256.fullmatch(arguments.expected_preflight_sha256 or "") is None:
            raise LegacyRailReconciliationError(
                "legacy_rail_preflight_identity_invalid"
            )
        _write_stdout(
            reconcile(
                expected_plan_sha256=arguments.expected_plan_sha256,
                expected_preflight_sha256=(arguments.expected_preflight_sha256),
            )
        )
        return 0
    _write_stdout(verify(expected_plan_sha256=arguments.expected_plan_sha256))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LegacyRailReconciliationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
