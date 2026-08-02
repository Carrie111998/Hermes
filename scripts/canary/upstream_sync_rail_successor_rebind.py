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
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence

from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import upstream_sync_rail_cutover as activation
from scripts.canary.production_cutover_activation_lock import (
    authority_activation_lock,
)


AUTHORITY_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-authority.v1"
PREFLIGHT_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-preflight.v1"
STARTED_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-started.v1"
ARCHIVE_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-archive.v1"
TERMINAL_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-terminal.v1"
ROLLBACK_SCHEMA = "muncho-dual-upstream-sync-successor-rebind-rollback.v1"
ROLLBACK_INTENT_SCHEMA = (
    "muncho-dual-upstream-sync-successor-rebind-rollback-intent.v1"
)
OPERATION = "dual-upstream-sync-successor-rebind"

UNIT_NAMES = activation.UNIT_NAMES
TIMER_NAMES = activation.TIMER_NAMES
SERVICE_NAMES = (rail.SYNC_SERVICE_UNIT, rail.REPORT_SERVICE_UNIT)

SYSTEMD_ROOT = Path("/etc/systemd/system")
STAGED_ROOT = rail.PACKAGE_ROOT
AUTHORITY_PATH = STAGED_ROOT / "successor-rebind-authority.json"
PREFLIGHT_PATH = STAGED_ROOT / "successor-rebind-preflight.json"
EVIDENCE_ROOT = Path(
    "/var/lib/muncho-production-legacy-cutover/"
    "dual-upstream-sync-successor-rebind"
)
RUNTIME_RELATIVE = Path(
    "scripts/canary/upstream_sync_rail_successor_rebind.py"
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNIT_DIGEST_FIELDS = frozenset(UNIT_NAMES)
_ENABLED_STATES = frozenset(
    {
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
    }
)


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


def _receipt(unsigned: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(unsigned))
    return {
        **payload,
        "receipt_sha256": activation._sha256(  # noqa: SLF001
            activation._canonical(payload)  # noqa: SLF001
        ),
    }


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
    predecessor_sender_revision: str,
) -> tuple[str, ...]:
    if (
        _SHA40.fullmatch(predecessor_revision or "") is None
        or _SHA40.fullmatch(predecessor_sender_revision or "") is None
    ):
        _fail("upstream_sync_successor_revision_invalid")
    return tuple(
        sorted(
            {
                str(rail.release_root(predecessor_revision)),
                str(rail.release_root(predecessor_sender_revision)),
            }
        )
    )


def build_authority(
    *,
    package: activation.PackageContext,
    predecessor_revision: str,
    predecessor_sender_revision: str,
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
        or _SHA40.fullmatch(str(package.manifest.get("release_revision", "")))
        is None
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
        or any(not isinstance(raw, bytes) or not raw for raw in predecessor_units.values())
    ):
        _fail("upstream_sync_successor_authority_invalid")
    missing_roots = _expected_missing_roots(
        predecessor_revision,
        predecessor_sender_revision,
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
        "predecessor_sender_revision": predecessor_sender_revision,
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
        "predecessor_sender_revision",
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
    predecessor_sender = str(value.get("predecessor_sender_revision", ""))
    target = str(value.get("target_revision", ""))
    digests = _unit_digest_map(
        value.get("predecessor_unit_digests"),
        code="upstream_sync_successor_authority_invalid",
    )
    target_digests = _unit_digest_map(
        value.get("target_unit_digests"),
        code="upstream_sync_successor_authority_invalid",
    )
    expected_missing = _expected_missing_roots(predecessor, predecessor_sender)
    if (
        set(value) != fields
        or value.get("schema") != AUTHORITY_SCHEMA
        or value.get("operation") != OPERATION
        or _SHA40.fullmatch(target) is None
        or target in {predecessor, predecessor_sender}
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
            raw, _metadata = activation._read_regular(  # noqa: SLF001
                Path(fragment),
                maximum=2 * 1024 * 1024,
            )
        except activation.UpstreamSyncRailCutoverError as exc:
            raise UpstreamSyncRailSuccessorRebindError(
                "upstream_sync_successor_systemd_fragment_invalid",
                unit,
            ) from exc
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


def _observe_all(host: RebindHost, *, systemd_root: Path) -> dict[str, UnitState]:
    return {
        name: host.observe(name, systemd_root=systemd_root)
        for name in UNIT_NAMES
    }


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
        if item.enabled_state not in {"enabled", "disabled"} or item.active_state not in {
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
    units = _unit_bytes(systemd_root=systemd_root, root_owned=root_owned)
    _validate_predecessor_units(units, authority)
    _prove_missing_predecessor_refs(authority)
    selected_host = host or _SystemdHost()
    observed = _observe_all(selected_host, systemd_root=systemd_root)
    _validate_loaded_digests(
        observed,
        expected_digests=authority["predecessor_unit_digests"],
        systemd_root=systemd_root,
    )
    if any(observed[name].active_state != "inactive" for name in SERVICE_NAMES):
        _fail("upstream_sync_successor_service_busy")
    timer_prestates = _timer_state_map(observed)
    return _receipt(
        {
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
    return _receipt(
        {
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
        }
    )


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
    return _receipt(
        {
            "schema": ROLLBACK_INTENT_SCHEMA,
            "authority_sha256": authority["authority_sha256"],
            "preflight_receipt_sha256": preflight_sha256,
            "cause": cause,
            "stop_scope": list(TIMER_NAMES),
            "rollback_must_resume_before_forward_recovery": True,
            "forward_mutation_may_have_occurred": True,
            "rollback_mutation_performed": False,
            "secret_material_recorded": False,
        }
    )


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
) -> dict[str, Any]:
    directory = _archive_root(
        authority["authority_sha256"],
        evidence_root=evidence_root,
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        activation._atomic_write(  # noqa: SLF001
            archive_path,
            units[name],
            mode=0o600,
            uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
            gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
        )
    receipt = _receipt(
        {
            "schema": ARCHIVE_SCHEMA,
            "authority_sha256": authority["authority_sha256"],
            "preflight_receipt_sha256": preflight_sha256,
            "archived_unit_digests": dict(authority["predecessor_unit_digests"]),
            "archived_unit_names": list(UNIT_NAMES),
            "archive_complete_before_replace": True,
            "secret_material_recorded": False,
        }
    )
    return _publish(
        receipt,
        path=_transaction_path(
            authority["authority_sha256"],
            "archive.json",
            evidence_root=evidence_root,
        ),
        root_owned=root_owned,
    )


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
    expected = _receipt(
        {
            "schema": ARCHIVE_SCHEMA,
            "authority_sha256": authority["authority_sha256"],
            "preflight_receipt_sha256": preflight_sha256,
            "archived_unit_digests": dict(authority["predecessor_unit_digests"]),
            "archived_unit_names": list(UNIT_NAMES),
            "archive_complete_before_replace": True,
            "secret_material_recorded": False,
        }
    )
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
) -> bool:
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
        activation._atomic_write(  # noqa: SLF001
            systemd_root / name,
            package.artifacts[name],
            mode=0o644,
            uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
            gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
        )
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
) -> dict[str, Any]:
    rollback_intent = _ensure_rollback_intent(
        authority=authority,
        preflight_sha256=preflight_sha256,
        evidence_root=evidence_root,
        root_owned=root_owned,
        cause=cause,
    )
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
            activation._atomic_write(  # noqa: SLF001
                systemd_root / name,
                archived[name],
                mode=0o644,
                uid=0 if root_owned else activation._effective_uid(),  # noqa: SLF001
                gid=0 if root_owned else activation._effective_gid(),  # noqa: SLF001
            )
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
    rollback = _receipt(
        {
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
        }
    )
    return _publish(
        rollback,
        path=_transaction_path(
            authority["authority_sha256"],
            "rollback.json",
            evidence_root=evidence_root,
        ),
        root_owned=root_owned,
    )


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
        if rollback_path.exists() or rollback_path.is_symlink():
            _fail("upstream_sync_successor_already_rolled_back")
        if terminal_path.exists() or terminal_path.is_symlink():
            if rollback_intent_path.exists() or rollback_intent_path.is_symlink():
                _fail("upstream_sync_successor_terminal_conflicts_with_rollback")
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
            _publish(
                _started_receipt(
                    authority=authority,
                    preflight_sha256=expected_preflight_sha256,
                ),
                path=started_path,
                root_owned=root_owned,
            )
        if rollback_intent_path.exists() or rollback_intent_path.is_symlink():
            intent = activation._read_canonical_json(  # noqa: SLF001
                rollback_intent_path,
                root_owned=root_owned,
                modes=frozenset({0o600}),
            )
            cause = intent.get("cause") if isinstance(intent, Mapping) else None
            if not isinstance(cause, str):
                _fail("upstream_sync_successor_rollback_intent_invalid")
            _ensure_rollback_intent(
                authority=authority,
                preflight_sha256=expected_preflight_sha256,
                evidence_root=evidence_root,
                root_owned=root_owned,
                cause=cause,
            )
            try:
                _restore_predecessor(
                    host=host,
                    authority=authority,
                    preflight=checked_preflight,
                    preflight_sha256=expected_preflight_sha256,
                    systemd_root=systemd_root,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                    progress_hook=progress_hook,
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
            host.mutate("stop", *TIMER_NAMES)
            stopped = _observe_all(host, systemd_root=systemd_root)
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
                )
                forward_recovery = False
            forward_recovery = (
                _install_target_units(
                    package=package,
                    authority=authority,
                    systemd_root=systemd_root,
                    root_owned=root_owned,
                    progress_hook=progress_hook,
                )
                or forward_recovery
            )
            host.mutate("daemon-reload")
            reloaded = _observe_all(host, systemd_root=systemd_root)
            _validate_loaded_digests(
                reloaded,
                expected_digests=authority["target_unit_digests"],
                systemd_root=systemd_root,
            )
            if any(reloaded[name].active_state != "inactive" for name in UNIT_NAMES):
                _fail("upstream_sync_successor_reload_quiescence_invalid")
            host.mutate("enable", *TIMER_NAMES)
            # Both oneshot services are run once before their timers are armed.
            # This is the exact catch-up proof for the new release bytes.
            host.mutate("start", *SERVICE_NAMES)
            host.mutate("start", *TIMER_NAMES)
            observed = _prove_target(
                host=host,
                authority=authority,
                systemd_root=systemd_root,
            )
            terminal = _receipt(
                {
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
                    "assert_result": {name: observed[name].assert_result for name in UNIT_NAMES},
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
                }
            )
            return _publish(terminal, path=terminal_path, root_owned=root_owned)
        except (
            UpstreamSyncRailSuccessorRebindError,
            activation.UpstreamSyncRailCutoverError,
            OSError,
        ) as exc:
            cause = (
                exc.code
                if isinstance(exc, UpstreamSyncRailSuccessorRebindError)
                else "upstream_sync_successor_runtime_mutation_failed"
            )
            try:
                _ensure_rollback_intent(
                    authority=authority,
                    preflight_sha256=expected_preflight_sha256,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                )
                _restore_predecessor(
                    host=host,
                    authority=authority,
                    preflight=checked_preflight,
                    preflight_sha256=expected_preflight_sha256,
                    systemd_root=systemd_root,
                    evidence_root=evidence_root,
                    root_owned=root_owned,
                    cause=cause,
                    progress_hook=progress_hook,
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
        host=_SystemdHost(),
        require_root=require_root,
        activation_lock_factory=activation_lock_factory,
        progress_hook=None,
    )


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
        host=_SystemdHost(),
    )


def _write_stdout(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(activation._canonical(value) + b"\n")  # noqa: SLF001
    sys.stdout.buffer.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "rebind", "verify"))
    parser.add_argument("--expected-authority-sha256", required=True)
    parser.add_argument("--expected-preflight-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
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
    "UNIT_NAMES",
    "TIMER_NAMES",
    "SERVICE_NAMES",
    "UnitState",
    "UpstreamSyncRailSuccessorRebindError",
    "build_authority",
    "main",
    "preflight",
    "rebind",
    "validate_authority",
    "verify",
]
