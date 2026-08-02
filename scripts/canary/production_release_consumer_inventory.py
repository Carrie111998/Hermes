#!/usr/bin/env python3
"""Pure, fail-closed inventory contract for pinned production releases.

The Stage-C transaction owns collection of systemd and ``/proc`` facts.  This
module deliberately performs no host I/O: callers inject complete observations
and receive a deterministic validation result.  The expected consumer catalog
is derived from the existing host, alias-projection, and trusted-cron package
contracts so the updater cannot silently maintain a second unit list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import PurePath, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from gateway import production_alias_projection_units as alias_units
from gateway.isolated_worker_units import (
    ISOLATED_WORKER_SERVICE_UNIT,
    ISOLATED_WORKER_SOCKET_UNIT,
)
from ops.muncho.runtime import trusted_cron_collector_rail as cron_rail
from ops.muncho.runtime import upstream_sync_job_rail as dual_sync_rail
from scripts.canary import package_production_cutover_artifacts as host_package


EXPECTED_UNIT_COUNT = 79
EXPECTED_EXECUTION_SERVICE_COUNT = 49
EXPECTED_TRIGGER_UNIT_COUNT = 30
EXPECTED_LONG_RUNNING_SERVICE_COUNT = 18
EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT = 1
EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT = 30
EXPECTED_ONESHOT_SERVICE_COUNT = (
    EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT + EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
)

ACTIVATION_CLASS_LONG_RUNNING = "long_running"
ACTIVATION_CLASS_STARTUP_ONESHOT = "startup_oneshot"
ACTIVATION_CLASS_TRIGGERED_ONESHOT = "triggered_oneshot"
_SERVICE_ACTIVATION_CLASSES = frozenset({
    ACTIVATION_CLASS_LONG_RUNNING,
    ACTIVATION_CLASS_STARTUP_ONESHOT,
    ACTIVATION_CLASS_TRIGGERED_ONESHOT,
})
_LONG_RUNNING_SYSTEMD_TYPES = frozenset({
    "dbus",
    "exec",
    "forking",
    "idle",
    "notify",
    "notify-reload",
    "simple",
})

SYSTEMD_ROOT = PurePosixPath(str(alias_units.SYSTEMD_ROOT))
RELEASES_ROOT = PurePosixPath(str(cron_rail.RELEASES_ROOT))
COMPATIBILITY_RELEASE_SYMLINK = PurePosixPath("/opt/adventico-ai-platform/hermes-agent")

SYSTEMD_RELEASE_REF_PROPERTIES = frozenset({
    "Names",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "MainPID",
    "ControlPID",
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStop",
    "ExecReload",
    "WorkingDirectory",
    "RootDirectory",
    "Environment",
    "EnvironmentFiles",
    "AssertPathExists",
    "ConditionPathExists",
    "ReadOnlyPaths",
    "BindReadOnlyPaths",
    "ReadWritePaths",
    "InaccessiblePaths",
    "Requires",
    "Wants",
    "BindsTo",
    "PartOf",
    "Before",
    "After",
    "TriggeredBy",
    "Triggers",
    "Unit",
    "Service",
    "Sockets",
})

PROCESS_RELEASE_REF_FIELDS = frozenset({"exe", "cwd", "root", "cmdline", "maps", "fds"})

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]+\.(?:service|socket|timer)$")
_RELEASE_REFERENCE = re.compile(
    re.escape(str(RELEASES_ROOT))
    + r"/hermes-agent-(?P<prefix>[0-9a-f]{12})"
    + r"(?=/|[^A-Za-z0-9_.-]|$)"
)
_COMPATIBILITY_REFERENCE = re.compile(
    re.escape(str(COMPATIBILITY_RELEASE_SYMLINK)) + r"(?=/|[^A-Za-z0-9_.-]|$)"
)
_RELATIONSHIP_PROPERTIES = frozenset({
    "Names",
    "Requires",
    "Wants",
    "BindsTo",
    "PartOf",
    "Before",
    "After",
    "TriggeredBy",
    "Triggers",
    "Unit",
    "Service",
    "Sockets",
})


class ProductionReleaseConsumerInventoryError(ValueError):
    """Stable, non-secret failure raised by the pure inventory boundary."""

    def __init__(self, code: str, subject: str | None = None) -> None:
        self.code = code
        self.subject = subject
        super().__init__(code if subject is None else f"{code}:{subject}")


class InventoryPhase(str, Enum):
    """The four loaded-reference states allowed by a Stage-C transaction."""

    PREDECESSOR_ACTIVE = "predecessor_active"
    PREDECESSOR_FENCED = "predecessor_fenced"
    TARGET_INSTALLED_STOPPED = "target_installed_stopped"
    TARGET_ACTIVE = "target_active"


@dataclass(frozen=True)
class ReleaseReference:
    """One content-addressed production release root found in an observation."""

    revision_prefix: str
    release_root: str


@dataclass(frozen=True)
class ConsumerSpec:
    """Exact installed identity and trigger contract for one systemd unit."""

    name: str
    source: str
    kind: str
    fragment_path: str
    drop_in_paths: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    triggered_by: tuple[str, ...] = ()
    executes_release: bool = False
    activation_class: str | None = None


@dataclass(frozen=True)
class UnitObservation:
    """Injected systemd properties plus stable-open unit file bytes."""

    name: str
    properties: Mapping[str, Any]
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class ProcessObservation:
    """Injected process links/content already attributed to a systemd cgroup."""

    pid: int
    unit: str | None
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class InventoryValidationResult:
    """Secret-free summary of one successful inventory validation."""

    phase: InventoryPhase
    expected_unit_count: int
    execution_service_count: int
    long_running_service_count: int
    startup_oneshot_service_count: int
    triggered_oneshot_service_count: int
    oneshot_service_count: int
    trigger_unit_count: int
    observed_expected_unit_count: int
    ignored_unrelated_unit_count: int
    observed_process_count: int
    unit_release_revision_prefixes: tuple[str, ...]
    process_release_revision_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class _ReleaseRefPolicy:
    unit_prefixes: frozenset[str]
    process_prefixes: frozenset[str]
    processes_must_be_stopped: bool


def _fail(code: str, subject: str | None = None) -> NoReturn:
    raise ProductionReleaseConsumerInventoryError(code, subject)


def _unit_kind(name: str) -> str:
    if not isinstance(name, str) or _UNIT_NAME.fullmatch(name) is None:
        _fail("production_release_consumer_unit_name_invalid")
    return name.rsplit(".", 1)[1]


def _host_consumer_specs() -> dict[str, ConsumerSpec]:
    dual_sync_services = {
        dual_sync_rail.SYNC_SERVICE_UNIT,
        dual_sync_rail.REPORT_SERVICE_UNIT,
    }
    unit_targets: dict[str, tuple[str, str]] = {}
    for artifact_name, (
        target,
        _binding_class,
    ) in host_package.HOST_ARTIFACT_TARGETS.items():
        path = PurePosixPath(target)
        if path.parent != SYSTEMD_ROOT or path.suffix not in {
            ".service",
            ".socket",
            ".timer",
        }:
            continue
        if path.name in unit_targets:
            _fail("production_release_consumer_catalog_invalid")
        activation_class = (
            ACTIVATION_CLASS_STARTUP_ONESHOT
            if artifact_name == "phase_b_unit"
            else ACTIVATION_CLASS_TRIGGERED_ONESHOT
            if path.name in dual_sync_services
            else ACTIVATION_CLASS_LONG_RUNNING
            if path.suffix == ".service"
            else ""
        )
        unit_targets[path.name] = (str(path), activation_class)

    try:
        gateway_name = PurePosixPath(
            host_package.HOST_ARTIFACT_TARGETS["gateway_unit"][0]
        ).name
        gateway_drop_in = host_package.HOST_ARTIFACT_TARGETS[
            "gateway_connector_drop_in"
        ][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProductionReleaseConsumerInventoryError(
            "production_release_consumer_catalog_invalid"
        ) from exc

    result: dict[str, ConsumerSpec] = {}
    for name, (fragment_path, activation_class) in unit_targets.items():
        kind = _unit_kind(name)
        triggers: tuple[str, ...] = ()
        triggered_by: tuple[str, ...] = ()
        if name == ISOLATED_WORKER_SOCKET_UNIT:
            triggers = (ISOLATED_WORKER_SERVICE_UNIT,)
        elif name == ISOLATED_WORKER_SERVICE_UNIT:
            triggered_by = (ISOLATED_WORKER_SOCKET_UNIT,)
        elif name == dual_sync_rail.SYNC_TIMER_UNIT:
            triggers = (dual_sync_rail.SYNC_SERVICE_UNIT,)
        elif name == dual_sync_rail.SYNC_SERVICE_UNIT:
            triggered_by = (dual_sync_rail.SYNC_TIMER_UNIT,)
        elif name == dual_sync_rail.REPORT_TIMER_UNIT:
            triggers = (dual_sync_rail.REPORT_SERVICE_UNIT,)
        elif name == dual_sync_rail.REPORT_SERVICE_UNIT:
            triggered_by = (dual_sync_rail.REPORT_TIMER_UNIT,)
        result[name] = ConsumerSpec(
            name=name,
            source="host",
            kind=kind,
            fragment_path=fragment_path,
            drop_in_paths=((gateway_drop_in,) if name == gateway_name else ()),
            triggers=triggers,
            triggered_by=triggered_by,
            executes_release=kind == "service",
            activation_class=activation_class or None,
        )
    return result


def _alias_consumer_specs() -> dict[str, ConsumerSpec]:
    names = (
        alias_units.EXPORTER_UNIT,
        alias_units.PROJECTOR_UNIT,
        alias_units.PROJECTOR_TIMER,
    )
    result: dict[str, ConsumerSpec] = {}
    for name in names:
        kind = _unit_kind(name)
        result[name] = ConsumerSpec(
            name=name,
            source="alias_projection",
            kind=kind,
            fragment_path=str(SYSTEMD_ROOT / name),
            triggers=(
                (alias_units.PROJECTOR_UNIT,)
                if name == alias_units.PROJECTOR_TIMER
                else ()
            ),
            triggered_by=(
                (alias_units.PROJECTOR_TIMER,)
                if name == alias_units.PROJECTOR_UNIT
                else ()
            ),
            executes_release=kind == "service",
            activation_class=(
                ACTIVATION_CLASS_TRIGGERED_ONESHOT if kind == "service" else None
            ),
        )
    return result


def _authoritative_cron_manifest() -> Mapping[str, Any]:
    dependency_paths = {str(cron_rail.SETPRIV)}
    dependency_paths.update(
        path for spec in cron_rail.COLLECTOR_SPECS for path in spec.dependency_paths
    )
    return cron_rail.build_package_manifest(
        revision="0" * 40,
        rail_sha256="1" * 64,
        dependency_facts={path: "2" * 64 for path in sorted(dependency_paths)},
    )


def _cron_consumer_specs() -> dict[str, ConsumerSpec]:
    manifest = _authoritative_cron_manifest()
    rows = manifest.get("units")
    if not isinstance(rows, Mapping):
        _fail("production_release_consumer_catalog_invalid")
    result: dict[str, ConsumerSpec] = {}
    for collector in cron_rail.COLLECTOR_SPECS:
        row = rows.get(collector.source_job_id)
        if not isinstance(row, Mapping):
            _fail("production_release_consumer_catalog_invalid")
        service = row.get("service")
        timer = row.get("timer")
        if not isinstance(service, str) or not isinstance(timer, str):
            _fail("production_release_consumer_catalog_invalid")
        if service in result or timer in result:
            _fail("production_release_consumer_catalog_invalid")
        result[service] = ConsumerSpec(
            name=service,
            source="cron",
            kind=_unit_kind(service),
            fragment_path=str(SYSTEMD_ROOT / service),
            triggered_by=(timer,),
            executes_release=True,
            activation_class=ACTIVATION_CLASS_TRIGGERED_ONESHOT,
        )
        result[timer] = ConsumerSpec(
            name=timer,
            source="cron",
            kind=_unit_kind(timer),
            fragment_path=str(SYSTEMD_ROOT / timer),
            triggers=(service,),
            executes_release=False,
        )
    return result


@lru_cache(maxsize=1)
def expected_consumer_catalog() -> Mapping[str, ConsumerSpec]:
    """Return the exact immutable production consumer catalog."""

    combined: dict[str, ConsumerSpec] = {}
    for partition in (
        _host_consumer_specs(),
        _alias_consumer_specs(),
        _cron_consumer_specs(),
    ):
        overlap = set(combined).intersection(partition)
        if overlap:
            _fail("production_release_consumer_catalog_invalid")
        combined.update(partition)

    execution_count = sum(spec.executes_release for spec in combined.values())
    activation_counts = {
        activation_class: sum(
            spec.activation_class == activation_class for spec in combined.values()
        )
        for activation_class in _SERVICE_ACTIVATION_CLASSES
    }
    trigger_count = sum(spec.kind in {"socket", "timer"} for spec in combined.values())
    if (
        len(combined) != EXPECTED_UNIT_COUNT
        or execution_count != EXPECTED_EXECUTION_SERVICE_COUNT
        or activation_counts[ACTIVATION_CLASS_LONG_RUNNING]
        != EXPECTED_LONG_RUNNING_SERVICE_COUNT
        or activation_counts[ACTIVATION_CLASS_STARTUP_ONESHOT]
        != EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        or activation_counts[ACTIVATION_CLASS_TRIGGERED_ONESHOT]
        != EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
        or trigger_count != EXPECTED_TRIGGER_UNIT_COUNT
        or any(
            spec.executes_release != (spec.kind == "service")
            for spec in combined.values()
        )
        or any(
            (spec.activation_class in _SERVICE_ACTIVATION_CLASSES)
            != spec.executes_release
            for spec in combined.values()
        )
    ):
        _fail("production_release_consumer_catalog_invalid")
    return MappingProxyType(dict(sorted(combined.items())))


def _text_fragments(value: Any) -> list[str]:
    if value is None or type(value) in {bool, int, float}:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        try:
            return [value.decode("utf-8", errors="strict")]
        except UnicodeError as exc:
            raise ProductionReleaseConsumerInventoryError(
                "production_release_consumer_observation_invalid"
            ) from exc
    if isinstance(value, PurePath):
        return [str(value)]
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            if not isinstance(key, (str, bytes, PurePath)):
                _fail("production_release_consumer_observation_invalid")
            result.extend(_text_fragments(key))
            result.extend(_text_fragments(item))
        return result
    if isinstance(value, Sequence) or isinstance(value, (set, frozenset)):
        result = []
        for item in value:
            result.extend(_text_fragments(item))
        return result
    _fail("production_release_consumer_observation_invalid")


def extract_release_references(value: Any) -> tuple[ReleaseReference, ...]:
    """Extract unique content-addressed release roots deterministically."""

    found: set[tuple[str, str]] = set()
    for text in _text_fragments(value):
        for match in _RELEASE_REFERENCE.finditer(text):
            prefix = match.group("prefix")
            root = str(RELEASES_ROOT / f"hermes-agent-{prefix}")
            found.add((prefix, root))
    return tuple(
        ReleaseReference(revision_prefix=prefix, release_root=root)
        for prefix, root in sorted(found)
    )


def contains_compatibility_release_reference(value: Any) -> bool:
    """Return whether an execution observation uses the mutable release link."""

    return any(
        _COMPATIBILITY_REFERENCE.search(text) is not None
        for text in _text_fragments(value)
    )


def _property_words(
    properties: Mapping[str, Any],
    name: str,
) -> tuple[str, ...]:
    value = properties.get(name)
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        words = tuple(value.split())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                _fail("production_release_consumer_observation_invalid")
            values.extend(item.split())
        words = tuple(values)
    else:
        _fail("production_release_consumer_observation_invalid")
    if any(not word for word in words) or len(set(words)) != len(words):
        _fail("production_release_consumer_observation_invalid")
    return words


def _validate_unit_structure(
    observation: UnitObservation,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if (
        not isinstance(observation.name, str)
        or _UNIT_NAME.fullmatch(observation.name) is None
        or not isinstance(observation.properties, Mapping)
        or not SYSTEMD_RELEASE_REF_PROPERTIES.issubset(observation.properties)
        or not isinstance(observation.files, Mapping)
    ):
        _fail("production_release_consumer_observation_invalid")

    names = _property_words(observation.properties, "Names")
    fragment_words = _property_words(observation.properties, "FragmentPath")
    drop_ins = _property_words(observation.properties, "DropInPaths")
    triggers = _property_words(observation.properties, "Triggers")
    triggered_by = _property_words(observation.properties, "TriggeredBy")
    need_daemon_reload = observation.properties.get("NeedDaemonReload")
    if (
        set(names) != {observation.name}
        or len(fragment_words) != 1
        or not (
            need_daemon_reload in {"no", "yes"}
            if isinstance(need_daemon_reload, str)
            else type(need_daemon_reload) is bool
        )
    ):
        _fail(
            "production_release_consumer_observation_invalid",
            observation.name,
        )
    fragment = fragment_words[0]
    expected_files = {fragment, *drop_ins}
    if (
        set(observation.files) != expected_files
        or len(observation.files) != len(expected_files)
        or any(
            not isinstance(path, str) or not isinstance(payload, bytes)
            for path, payload in observation.files.items()
        )
    ):
        _fail(
            "production_release_consumer_observation_invalid",
            observation.name,
        )
    # Decode every supplied unit byte now so malformed observations cannot be
    # silently treated as containing no release reference.
    _text_fragments(observation.files)
    return fragment, drop_ins, triggers, triggered_by


def _release_prefixes(value: Any) -> frozenset[str]:
    return frozenset(
        reference.revision_prefix for reference in extract_release_references(value)
    )


def _policy(
    phase: InventoryPhase | str,
    *,
    predecessor_revision: str,
    target_revision: str,
) -> tuple[InventoryPhase, _ReleaseRefPolicy]:
    if (
        not isinstance(predecessor_revision, str)
        or _REVISION.fullmatch(predecessor_revision) is None
        or not isinstance(target_revision, str)
        or _REVISION.fullmatch(target_revision) is None
        or predecessor_revision == target_revision
        or predecessor_revision[:12] == target_revision[:12]
    ):
        _fail("production_release_consumer_revision_invalid")
    try:
        selected = InventoryPhase(phase)
    except (TypeError, ValueError) as exc:
        raise ProductionReleaseConsumerInventoryError(
            "production_release_consumer_phase_invalid"
        ) from exc
    predecessor = predecessor_revision[:12]
    target = target_revision[:12]
    if selected is InventoryPhase.PREDECESSOR_ACTIVE:
        return selected, _ReleaseRefPolicy(
            unit_prefixes=frozenset({predecessor}),
            process_prefixes=frozenset({predecessor}),
            processes_must_be_stopped=False,
        )
    if selected is InventoryPhase.PREDECESSOR_FENCED:
        return selected, _ReleaseRefPolicy(
            unit_prefixes=frozenset({predecessor}),
            process_prefixes=frozenset(),
            processes_must_be_stopped=True,
        )
    if selected is InventoryPhase.TARGET_INSTALLED_STOPPED:
        return selected, _ReleaseRefPolicy(
            unit_prefixes=frozenset({target}),
            process_prefixes=frozenset(),
            processes_must_be_stopped=True,
        )
    return selected, _ReleaseRefPolicy(
        unit_prefixes=frozenset({target}),
        process_prefixes=frozenset({target}),
        processes_must_be_stopped=False,
    )


def _unknown_unit_touches_catalog(
    observation: UnitObservation,
    catalog: Mapping[str, ConsumerSpec],
    *,
    fragment: str,
    drop_ins: tuple[str, ...],
) -> bool:
    expected_paths = {spec.fragment_path for spec in catalog.values()} | {
        path for spec in catalog.values() for path in spec.drop_in_paths
    }
    if fragment in expected_paths or set(drop_ins).intersection(expected_paths):
        return True
    relationships: set[str] = set()
    for property_name in _RELATIONSHIP_PROPERTIES:
        relationships.update(_property_words(observation.properties, property_name))
    return bool(relationships.intersection(catalog))


def _validate_expected_unit(
    observation: UnitObservation,
    spec: ConsumerSpec,
    *,
    policy: _ReleaseRefPolicy,
    fragment: str,
    drop_ins: tuple[str, ...],
    triggers: tuple[str, ...],
    triggered_by: tuple[str, ...],
) -> frozenset[str]:
    if fragment != spec.fragment_path:
        _fail(
            "production_release_consumer_fragment_invalid",
            observation.name,
        )
    if tuple(sorted(drop_ins)) != tuple(sorted(spec.drop_in_paths)):
        _fail(
            "production_release_consumer_drop_ins_invalid",
            observation.name,
        )
    if tuple(sorted(triggers)) != tuple(sorted(spec.triggers)) or tuple(
        sorted(triggered_by)
    ) != tuple(sorted(spec.triggered_by)):
        _fail(
            "production_release_consumer_trigger_invalid",
            observation.name,
        )
    if spec.executes_release:
        _validate_service_activation_class(observation, spec)
    need_daemon_reload = observation.properties.get("NeedDaemonReload")
    if need_daemon_reload == "yes" or need_daemon_reload is True:
        _fail(
            "production_release_consumer_daemon_reload_required",
            observation.name,
        )
    combined = (observation.properties, observation.files)
    if contains_compatibility_release_reference(combined):
        _fail(
            "production_release_consumer_release_symlink_forbidden",
            observation.name,
        )
    prefixes = _release_prefixes(combined)
    if spec.executes_release:
        if prefixes != policy.unit_prefixes:
            _fail(
                "production_release_consumer_release_ref_invalid",
                observation.name,
            )
    elif prefixes:
        _fail(
            "production_release_consumer_trigger_release_ref_invalid",
            observation.name,
        )
    return prefixes


def _validate_service_activation_class(
    observation: UnitObservation,
    spec: ConsumerSpec,
) -> None:
    """Bind inventory classes to the stable-open rendered systemd bytes."""

    service_types: list[str] = []
    for path in (spec.fragment_path, *sorted(spec.drop_in_paths)):
        payload = observation.files.get(path)
        if not isinstance(payload, bytes):
            _fail(
                "production_release_consumer_service_type_invalid",
                observation.name,
            )
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ProductionReleaseConsumerInventoryError(
                "production_release_consumer_service_type_invalid",
                observation.name,
            ) from exc
        section = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != "Service" or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "Type":
                service_types.append(value.strip())
    if len(service_types) != 1:
        _fail(
            "production_release_consumer_service_type_invalid",
            observation.name,
        )
    service_type = service_types[0]
    observed_class = (
        "oneshot"
        if service_type == "oneshot"
        else "long_running"
        if service_type in _LONG_RUNNING_SYSTEMD_TYPES
        else None
    )
    expected_class = (
        "long_running"
        if spec.activation_class == ACTIVATION_CLASS_LONG_RUNNING
        else "oneshot"
        if spec.activation_class
        in {
            ACTIVATION_CLASS_STARTUP_ONESHOT,
            ACTIVATION_CLASS_TRIGGERED_ONESHOT,
        }
        else None
    )
    if observed_class is None or observed_class != expected_class:
        _fail(
            "production_release_consumer_service_type_invalid",
            observation.name,
        )


def validate_release_consumer_inventory(
    *,
    unit_observations: Iterable[UnitObservation],
    process_observations: Iterable[ProcessObservation],
    phase: InventoryPhase | str,
    predecessor_revision: str,
    target_revision: str,
    catalog: Mapping[str, ConsumerSpec] | None = None,
) -> InventoryValidationResult:
    """Validate one injected, complete Stage-C loaded-consumer observation."""

    selected_phase, ref_policy = _policy(
        phase,
        predecessor_revision=predecessor_revision,
        target_revision=target_revision,
    )
    expected = expected_consumer_catalog() if catalog is None else catalog
    if (
        not isinstance(expected, Mapping)
        or len(expected) != EXPECTED_UNIT_COUNT
        or any(
            not isinstance(name, str)
            or not isinstance(spec, ConsumerSpec)
            or name != spec.name
            for name, spec in expected.items()
        )
    ):
        _fail("production_release_consumer_catalog_invalid")
    if (
        sum(spec.executes_release for spec in expected.values())
        != EXPECTED_EXECUTION_SERVICE_COUNT
        or sum(
            spec.activation_class == ACTIVATION_CLASS_LONG_RUNNING
            for spec in expected.values()
        )
        != EXPECTED_LONG_RUNNING_SERVICE_COUNT
        or sum(
            spec.activation_class == ACTIVATION_CLASS_STARTUP_ONESHOT
            for spec in expected.values()
        )
        != EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        or sum(
            spec.activation_class == ACTIVATION_CLASS_TRIGGERED_ONESHOT
            for spec in expected.values()
        )
        != EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
        or sum(spec.kind in {"socket", "timer"} for spec in expected.values())
        != EXPECTED_TRIGGER_UNIT_COUNT
        or any(
            spec.executes_release != (spec.kind == "service")
            for spec in expected.values()
        )
        or any(
            (spec.activation_class in _SERVICE_ACTIVATION_CLASSES)
            != spec.executes_release
            for spec in expected.values()
        )
    ):
        _fail("production_release_consumer_catalog_invalid")

    observed: dict[str, UnitObservation] = {}
    for observation in unit_observations:
        if not isinstance(observation, UnitObservation):
            _fail("production_release_consumer_observation_invalid")
        if observation.name in observed:
            _fail(
                "production_release_consumer_duplicate",
                observation.name,
            )
        observed[observation.name] = observation

    missing = sorted(set(expected).difference(observed))
    if missing:
        _fail("production_release_consumer_missing", missing[0])

    ignored = 0
    unit_prefixes: set[str] = set()
    for name in sorted(observed):
        observation = observed[name]
        fragment, drop_ins, triggers, triggered_by = _validate_unit_structure(
            observation
        )
        spec = expected.get(name)
        if spec is None:
            combined = (observation.properties, observation.files)
            if contains_compatibility_release_reference(combined):
                _fail(
                    "production_release_consumer_release_symlink_forbidden",
                    name,
                )
            if _release_prefixes(combined) or _unknown_unit_touches_catalog(
                observation,
                expected,
                fragment=fragment,
                drop_ins=drop_ins,
            ):
                _fail("production_release_consumer_unknown", name)
            ignored += 1
            continue
        unit_prefixes.update(
            _validate_expected_unit(
                observation,
                spec,
                policy=ref_policy,
                fragment=fragment,
                drop_ins=drop_ins,
                triggers=triggers,
                triggered_by=triggered_by,
            )
        )

    processes: dict[int, ProcessObservation] = {}
    process_prefixes: set[str] = set()
    for observation in process_observations:
        if (
            not isinstance(observation, ProcessObservation)
            or type(observation.pid) is not int
            or observation.pid <= 0
            or observation.pid in processes
            or (observation.unit is not None and not isinstance(observation.unit, str))
            or not isinstance(observation.fields, Mapping)
            or not PROCESS_RELEASE_REF_FIELDS.issubset(observation.fields)
        ):
            _fail("production_release_consumer_process_observation_invalid")
        processes[observation.pid] = observation
        if contains_compatibility_release_reference(observation.fields):
            _fail(
                "production_release_consumer_release_symlink_forbidden",
                str(observation.pid),
            )
        prefixes = _release_prefixes(observation.fields)
        spec = expected.get(observation.unit) if observation.unit is not None else None
        if (
            ref_policy.processes_must_be_stopped
            and spec is not None
            and spec.executes_release
        ):
            _fail(
                "production_release_consumer_process_unexpected",
                str(observation.pid),
            )
        if prefixes and (spec is None or not spec.executes_release):
            _fail(
                "production_release_consumer_process_unknown",
                str(observation.pid),
            )
        if not prefixes.issubset(ref_policy.process_prefixes):
            _fail(
                "production_release_consumer_process_release_ref_invalid",
                str(observation.pid),
            )
        process_prefixes.update(prefixes)

    return InventoryValidationResult(
        phase=selected_phase,
        expected_unit_count=len(expected),
        execution_service_count=sum(
            spec.executes_release for spec in expected.values()
        ),
        long_running_service_count=sum(
            spec.activation_class == ACTIVATION_CLASS_LONG_RUNNING
            for spec in expected.values()
        ),
        startup_oneshot_service_count=sum(
            spec.activation_class == ACTIVATION_CLASS_STARTUP_ONESHOT
            for spec in expected.values()
        ),
        triggered_oneshot_service_count=sum(
            spec.activation_class == ACTIVATION_CLASS_TRIGGERED_ONESHOT
            for spec in expected.values()
        ),
        oneshot_service_count=sum(
            spec.activation_class
            in {
                ACTIVATION_CLASS_STARTUP_ONESHOT,
                ACTIVATION_CLASS_TRIGGERED_ONESHOT,
            }
            for spec in expected.values()
        ),
        trigger_unit_count=sum(
            spec.kind in {"socket", "timer"} for spec in expected.values()
        ),
        observed_expected_unit_count=len(expected),
        ignored_unrelated_unit_count=ignored,
        observed_process_count=len(processes),
        unit_release_revision_prefixes=tuple(sorted(unit_prefixes)),
        process_release_revision_prefixes=tuple(sorted(process_prefixes)),
    )


__all__ = [
    "ACTIVATION_CLASS_LONG_RUNNING",
    "ACTIVATION_CLASS_STARTUP_ONESHOT",
    "ACTIVATION_CLASS_TRIGGERED_ONESHOT",
    "COMPATIBILITY_RELEASE_SYMLINK",
    "ConsumerSpec",
    "EXPECTED_EXECUTION_SERVICE_COUNT",
    "EXPECTED_LONG_RUNNING_SERVICE_COUNT",
    "EXPECTED_ONESHOT_SERVICE_COUNT",
    "EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT",
    "EXPECTED_TRIGGER_UNIT_COUNT",
    "EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT",
    "EXPECTED_UNIT_COUNT",
    "InventoryPhase",
    "InventoryValidationResult",
    "PROCESS_RELEASE_REF_FIELDS",
    "ProcessObservation",
    "ProductionReleaseConsumerInventoryError",
    "ReleaseReference",
    "SYSTEMD_RELEASE_REF_PROPERTIES",
    "UnitObservation",
    "contains_compatibility_release_reference",
    "expected_consumer_catalog",
    "extract_release_references",
    "validate_release_consumer_inventory",
]
